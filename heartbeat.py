#!/usr/bin/env python3
"""
heartbeat.py — the daily portfolio snapshot for stock-bot.

Runs LAST in the workflow, after monitor / insiders / edgar_watch have each
appended to their rolling ledgers in state.json. Pools recent *buys* across
Congress and insiders into the dashboard's "model portfolio" view — every
disclosed purchase pooled into one book, ranked by how many distinct people
are buying each name — and posts a single heartbeat embed.

Gating: fires once per UTC day, on the first run at/after heartbeat_hour_utc.
Set the env var HEARTBEAT_FORCE=1 (the workflow does this on manual dispatch)
to send immediately regardless of the clock.

Reads:  state.json  (congress.recent_trades, insiders.recent_trades, heartbeat)
Writes: state.json  (heartbeat.last_sent_date + resets the daily counters)
Env:    DISCORD_WEBHOOK_URL, optional HEARTBEAT_FORCE, optional heartbeat mention
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone

import notify

STATE_PATH = "state.json"
CONFIG_PATH = "config.yaml"
WINDOW_DAYS = 14           # accumulation window (matches the cluster window)
CLUSTER_MIN = 2            # distinct buyers to count as an "active cluster"
TOP_N = 8

try:
    import yaml
except ImportError:
    yaml = None


def load(path, fallback):
    try:
        with open(path) as f:
            return json.load(f) if path.endswith(".json") else yaml.safe_load(f)
    except (FileNotFoundError, json.JSONDecodeError, AttributeError):
        return fallback


def _iter_buys(state):
    """Yield (ticker, buyer_id, group, logged_date) for every buy in-window."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=WINDOW_DAYS)).date().isoformat()

    for t in state.get("congress", {}).get("recent_trades", []):
        if (t.get("side") or "").lower().startswith("b") and t.get("ticker"):
            when = t.get("logged") or t.get("date") or ""
            if when >= cutoff:
                yield t["ticker"].upper(), (t.get("person") or "?"), "congress", when

    for t in state.get("insiders", {}).get("recent_trades", []):
        if (t.get("side") or "").upper() == "BUY" and t.get("ticker"):
            when = t.get("logged") or t.get("date") or ""
            if when >= cutoff:
                yield t["ticker"].upper(), (t.get("owner") or "?"), "insiders", when


def build_snapshot(state, config):
    pool = {}                       # ticker -> {"buyers": set, "groups": set}
    all_buyers = set()
    day_ago = (datetime.now(timezone.utc) - timedelta(days=1)).date().isoformat()
    trades_24h = 0

    for ticker, buyer, group, when in _iter_buys(state):
        slot = pool.setdefault(ticker, {"buyers": set(), "groups": set()})
        slot["buyers"].add(f"{group}:{buyer}")
        slot["groups"].add(group)
        all_buyers.add(f"{group}:{buyer}")
        if when >= day_ago:
            trades_24h += 1

    def group_tag(groups):
        return "mixed" if len(groups) > 1 else next(iter(groups), "—")

    ranked = sorted(pool.items(), key=lambda kv: (-len(kv[1]["buyers"]), kv[0]))
    top = [{"ticker": tk, "buyers": len(v["buyers"]), "group": group_tag(v["groups"])}
           for tk, v in ranked[:TOP_N]]
    clusters = [{"ticker": tk, "count": len(v["buyers"]), "group": group_tag(v["groups"])}
                for tk, v in ranked if len(v["buyers"]) >= CLUSTER_MIN]

    hb = state.get("heartbeat", {})
    return {
        "healthy": True,
        "last_run_utc": datetime.now(timezone.utc).strftime("%H:%M"),
        # monitor.py increments these between heartbeats; we read + reset them.
        "checks_today": hb.get("checks", 0),
        "pinged_today": hb.get("trades_found", 0),
        "errors_today": hb.get("errors", 0),
        "window_days": WINDOW_DAYS,
        "buyers_pooled": len(all_buyers),
        "names_bought": len(pool),
        "trades_24h": trades_24h,
        "active_clusters": clusters,
        "top_accumulating": top,
    }


def main():
    webhook = os.environ.get("DISCORD_WEBHOOK_URL", "")
    force = os.environ.get("HEARTBEAT_FORCE", "").strip() in ("1", "true", "yes")

    config = load(CONFIG_PATH, {}) or {}
    if not config.get("features", {}).get("heartbeat", True):
        print("heartbeat: disabled in config")
        return 0

    state = load(STATE_PATH, {}) or {}
    hb = state.setdefault("heartbeat", {})
    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")

    if not force:
        if hb.get("last_sent_date") == today:
            print("heartbeat: already sent today")
            return 0
        if now.hour < int(config.get("heartbeat_hour_utc", 14)):
            print("heartbeat: before heartbeat_hour_utc; skipping")
            return 0

    snapshot = build_snapshot(state, config)
    mention = ""
    if config.get("discord", {}).get("mention_on_heartbeat"):
        uid = str(config["discord"].get("user_id", "")).strip()
        if uid.isdigit():
            mention = f"<@{uid}>"

    notify.send(webhook, notify.heartbeat_embed(snapshot), content=mention)

    hb["last_sent_date"] = today
    hb["checks"] = 0
    hb["trades_found"] = 0
    hb["errors"] = 0
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, indent=1)

    print(f"heartbeat: sent — {snapshot['names_bought']} names, "
          f"{len(snapshot['active_clusters'])} clusters, "
          f"{snapshot['buyers_pooled']} buyers pooled")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        notify.send(os.environ.get("DISCORD_WEBHOOK_URL", ""),
                    notify.notice_embed("Heartbeat error", str(e)[:1000], "error"))
        sys.exit(1)
