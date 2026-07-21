#!/usr/bin/env python3
"""
Congress Trade Bot — monitor.py
-------------------------------
Checks congressional stock-trade disclosures, pings Discord for new trades,
and (new) detects CLUSTERS: 2+ distinct members buying/selling the same stock
in the same direction inside the cluster window. Every message is rendered by
notify.py so the whole bot matches the dashboard style.

State (state.json), committed back to the repo after each run:
  seen                    list of trade fingerprints (dedup)
  congress.recent_trades  rolling ledger for cluster + portfolio views
  congress.alerted_clusters  cluster fingerprints already pinged
  heartbeat               {last_sent_date, pinged_today, errors_today}

The daily heartbeat itself lives in heartbeat.py (runs last in the workflow).

Env: DISCORD_WEBHOOK_URL, FINNHUB_API_KEY (only if finnhub_enrichment: true)
"""

import hashlib
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone

import requests
import yaml

import notify

CONFIG_PATH = "config.yaml"
STATE_PATH = "state.json"

SENATE_URL = ("https://senate-stock-watcher-data.s3-us-west-2.amazonaws.com"
              "/aggregate/all_transactions.json")
HOUSE_URL = ("https://house-stock-watcher-data.s3-us-west-2.amazonaws.com"
             "/data/all_transactions.json")
FINNHUB_URL = "https://finnhub.io/api/v1/stock/congressional-trading"

LEDGER_RETAIN_DAYS = 45         # keep enough history for the 14d window + slack
CLUSTER_WINDOW_DAYS = 14        # 2+ members within this window = a cluster
CLUSTER_MIN = 2


# ── config & state ─────────────────────────────────────────────────────
def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_state():
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            state = json.load(f)
    else:
        state = {}
    state.setdefault("initialized", False)
    state.setdefault("seen", [])
    cong = state.setdefault("congress", {})
    cong.setdefault("recent_trades", [])
    cong.setdefault("alerted_clusters", [])
    state.setdefault("heartbeat", {"last_sent_date": None,
                                   "pinged_today": 0, "errors_today": 0})
    return state


def save_state(state):
    state["seen"] = state["seen"][-50000:]
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=1)


def bump_pinged(state, n=1):
    state["heartbeat"]["pinged_today"] = state["heartbeat"].get("pinged_today", 0) + n


# ── parsing helpers ────────────────────────────────────────────────────
def parse_amount_low(amount):
    if not amount:
        return 0
    nums = re.findall(r"[\d,]+", amount)
    if not nums:
        return 0
    try:
        return int(nums[0].replace(",", ""))
    except ValueError:
        return 0


def classify_side(raw_type):
    t = (raw_type or "").lower()
    if "purchase" in t or t == "buy":
        return "buy"
    if "sale" in t or "sell" in t or "sold" in t:
        return "sell"
    return None


def parse_date(s):
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(s.strip(), fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def trade_hash(t):
    key = "|".join(str(t.get(k, "")) for k in (
        "chamber", "person", "ticker", "asset",
        "transaction_date", "side", "amount", "owner"))
    return hashlib.sha256(key.encode()).hexdigest()[:24]


# ── data sources ───────────────────────────────────────────────────────
def fetch_json(url):
    r = requests.get(url, timeout=60)
    r.raise_for_status()
    return r.json()


def _normalize(rows, chamber, name_key):
    out = []
    for r in rows:
        side = classify_side(r.get("type"))
        if not side:
            continue
        out.append({
            "chamber": chamber,
            "person": (r.get(name_key) or "").strip(),
            "ticker": (r.get("ticker") or "").replace("--", "").strip(),
            "asset": (r.get("asset_description") or "").strip(),
            "side": side,
            "amount": (r.get("amount") or "").strip(),
            "owner": (r.get("owner") or "").strip(),
            "transaction_date": (r.get("transaction_date") or "").strip(),
            "disclosure_date": (r.get("disclosure_date") or "").strip(),
            "link": (r.get("ptr_link") or "").strip(),
        })
    return out


def normalize_senate(rows):
    return _normalize(rows, "Senate", "senator")


def normalize_house(rows):
    return _normalize(rows, "House", "representative")


def fetch_finnhub(tickers):
    key = os.environ.get("FINNHUB_API_KEY", "")
    if not key:
        return []
    import time
    out = []
    for sym in tickers[:25]:
        try:
            r = requests.get(FINNHUB_URL, params={"symbol": sym, "token": key}, timeout=30)
            r.raise_for_status()
            for row in r.json().get("data", []):
                side = classify_side(row.get("transactionType"))
                if not side:
                    continue
                a_from, a_to = row.get("amountFrom"), row.get("amountTo")
                amount = f"${a_from:,.0f} - ${a_to:,.0f}" if a_from is not None and a_to is not None else ""
                out.append({
                    "chamber": row.get("position") or "Congress",
                    "person": (row.get("name") or "").strip(),
                    "ticker": (row.get("symbol") or sym).strip(),
                    "asset": (row.get("assetName") or "").strip(),
                    "side": side, "amount": amount,
                    "owner": (row.get("ownerType") or "").strip(),
                    "transaction_date": (row.get("transactionDate") or "").strip(),
                    "disclosure_date": (row.get("filingDate") or "").strip(),
                    "link": "",
                })
            time.sleep(1.1)
        except Exception as e:
            print(f"WARN: Finnhub failed for {sym}: {e}", file=sys.stderr)
    return out


# ── filtering ──────────────────────────────────────────────────────────
def passes_filters(cfg, t):
    f, feats = cfg["filters"], cfg["features"]
    if t["side"] == "buy" and not feats.get("ping_buys", True):
        return False
    if t["side"] == "sell" and not feats.get("ping_sells", True):
        return False
    min_val = f.get("min_trade_value", 0)
    if min_val and t["amount"] and parse_amount_low(t["amount"]) < min_val:
        return False
    tickers = [s.upper() for s in (f.get("watch_tickers") or [])]
    if tickers and t["ticker"].upper() not in tickers:
        return False
    pols = [p.lower() for p in (f.get("watch_politicians") or [])]
    if pols and not any(p in t["person"].lower() for p in pols):
        return False
    lookback = f.get("lookback_days", 45)
    d = parse_date(t["disclosure_date"]) or parse_date(t["transaction_date"])
    if d and d < datetime.now(timezone.utc) - timedelta(days=lookback):
        return False
    return True


# ── congress ledger + cluster detection ────────────────────────────────
def append_ledger(state, trades):
    """Record new trades for the rolling portfolio/cluster views, then prune."""
    today = datetime.now(timezone.utc).date().isoformat()
    led = state["congress"]["recent_trades"]
    for t in trades:
        led.append({
            "person": t["person"], "ticker": t["ticker"].upper(),
            "side": t["side"], "value": parse_amount_low(t["amount"]),
            "chamber": t["chamber"],
            "date": t["transaction_date"] or today,
            "logged": today,
        })
    cutoff = (datetime.now(timezone.utc) - timedelta(days=LEDGER_RETAIN_DAYS)).date().isoformat()
    state["congress"]["recent_trades"] = [e for e in led
                                          if (e.get("logged") or e.get("date") or "") >= cutoff]


def find_clusters(state):
    """(ticker, side) -> {members} where >= CLUSTER_MIN distinct members
    traded that name/direction inside the window."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=CLUSTER_WINDOW_DAYS)).date().isoformat()
    groups = {}
    for e in state["congress"]["recent_trades"]:
        when = e.get("date") or e.get("logged") or ""
        if when >= cutoff and e.get("ticker"):
            groups.setdefault((e["ticker"], e["side"]), {})[e["person"]] = \
                groups.get((e["ticker"], e["side"]), {}).get(e["person"], 0) + (e.get("value") or 0)
    return {k: v for k, v in groups.items() if len(v) >= CLUSTER_MIN}


def cluster_alerts(cfg, state):
    """Build cluster embeds for newly-formed congress clusters."""
    feats = cfg["features"]
    alerted = state["congress"]["alerted_clusters"]
    embeds = []
    for (ticker, side), members in find_clusters(state).items():
        if side == "buy" and not feats.get("ping_buys", True):
            continue
        if side == "sell" and not feats.get("ping_sells", True):
            continue
        names = sorted(members)
        fp = f"{ticker}:{side}:{'|'.join(names)}"
        if fp in alerted:
            continue
        alerted.append(fp)
        embeds.append(notify.cluster_embed({
            "ticker": ticker, "group": "congress", "direction": side,
            "buyers": names, "window_days": CLUSTER_WINDOW_DAYS,
            "combined_value": sum(members.values()) or None,
        }))
    state["congress"]["alerted_clusters"] = alerted[-500:]
    return embeds


# ── discord routing (all via notify) ───────────────────────────────────
def mention(cfg):
    uid = str(cfg["discord"].get("user_id", "")).strip()
    return f"<@{uid}>" if uid.isdigit() else ""


def trade_to_embed(t):
    return notify.trade_embed({
        "group": "congress", "ticker": t["ticker"], "company": t["asset"],
        "name": t["person"], "subtitle": t["chamber"], "direction": t["side"],
        "value": t["amount"], "date": t["transaction_date"] or t["disclosure_date"],
        "url": t.get("link"),
    })


# ── main ───────────────────────────────────────────────────────────────
def main():
    webhook = os.environ.get("DISCORD_WEBHOOK_URL")
    if not webhook:
        print("ERROR: DISCORD_WEBHOOK_URL not set", file=sys.stderr)
        return 1

    cfg = load_config()
    state = load_state()
    feats = cfg["features"]

    all_trades, errors = [], []
    if feats.get("senate_source", True):
        try:
            all_trades += normalize_senate(fetch_json(SENATE_URL))
        except Exception as e:
            errors.append(f"Senate source failed: {e}")
    if feats.get("house_source", True):
        try:
            all_trades += normalize_house(fetch_json(HOUSE_URL))
        except Exception as e:
            errors.append(f"House source failed: {e}")
    if feats.get("finnhub_enrichment", False):
        watch = cfg["filters"].get("watch_tickers") or []
        if watch:
            all_trades += fetch_finnhub(watch)

    for msg in errors:
        print(f"ERROR: {msg}", file=sys.stderr)
        state["heartbeat"]["errors_today"] = state["heartbeat"].get("errors_today", 0) + 1
        if feats.get("notify_on_error", True):
            notify.send(webhook, notify.notice_embed("Source error", msg, "error"))

    seen = set(state["seen"])
    new_trades = []
    for t in all_trades:
        h = trade_hash(t)
        if h in seen:
            continue
        seen.add(h)
        state["seen"].append(h)
        new_trades.append(t)

    # First run: seed silently so you don't get years of backfill.
    if not state["initialized"]:
        state["initialized"] = True
        save_state(state)
        notify.send(webhook, notify.notice_embed(
            "Congress Trade Bot initialized",
            f"Indexed **{len(state['seen'])}** historical trades as already-seen. "
            f"You'll be pinged for anything new from now on.", "info"),
            content=mention(cfg))
        print(f"Initialized with {len(state['seen'])} historical trades.")
        return 0

    alertable = [t for t in new_trades if passes_filters(cfg, t)]
    alertable.sort(key=lambda t: parse_date(t["disclosure_date"])
                   or datetime.min.replace(tzinfo=timezone.utc), reverse=True)

    # Record to the ledger BEFORE clustering so today's trades count.
    append_ledger(state, alertable)

    embeds = [trade_to_embed(t) for t in alertable]
    cap = int(cfg.get("max_pings_per_run", 20))
    overflow = max(0, len(embeds) - cap)
    embeds = embeds[:cap]

    # Cluster embeds ride along after the individual trades.
    embeds += cluster_alerts(cfg, state)

    if embeds:
        notify.send(webhook, embeds, content=mention(cfg) if feats.get("ping_buys") else None)
        bump_pinged(state, len(embeds))
    if overflow:
        notify.send(webhook, notify.notice_embed(
            "…and more", f"{overflow} additional new trades this run "
            f"(capped by max_pings_per_run).", "info"))

    save_state(state)
    print(f"Run complete: {len(all_trades)} fetched, {len(new_trades)} new, "
          f"{len(alertable)} alertable, {len(embeds)} embeds, {len(errors)} errors.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        notify.send(os.environ.get("DISCORD_WEBHOOK_URL", ""),
                    notify.notice_embed("Monitor error", str(e)[:1000], "error"))
        sys.exit(1)
