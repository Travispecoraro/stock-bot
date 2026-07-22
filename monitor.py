#!/usr/bin/env python3
"""
Congress Trade Bot
------------------
Checks congressional stock-trade disclosures and pings a Discord webhook
when new trades appear. Designed to run on a GitHub Actions cron schedule.

Data sources (official, via congress_sources.py):
  Senate  efdsearch.senate.gov            electronic PTRs only
  House   disclosures-clerk.house.gov     digital-PDF PTRs only
The old community S3 mirrors (senate/house-stock-watcher) are gone (403)
and are no longer used.

state.json contract written by this module:
  seen                       trade-hash dedup set (bounded)
  congress.recent_trades     rolling trade tape — read by heartbeat.py,
                             prices.py, and the dashboard. Each row is the
                             congress_sources row shape plus "logged"
                             (ISO date the bot first saw it).
  congress.processed_senate  Senate report URLs already parsed (skip list)
  congress.processed_house   House DocIDs already parsed (skip list)
  heartbeat                  counters for the daily summary

Env vars required:
  DISCORD_WEBHOOK_URL   (GitHub secret)
  FINNHUB_API_KEY       (GitHub secret; only used if finnhub_enrichment: true)
"""

import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone

import requests
import yaml

import congress_sources
import roster

CONFIG_PATH = "config.yaml"
STATE_PATH = "state.json"

FINNHUB_URL = "https://finnhub.io/api/v1/stock/congressional-trading"

# Ledger retention. Longer than alert lookback on purpose: prices.py needs
# months of tape to compute membership windows, and the dashboard shows
# history. Rows age out by trade/disclosure date, with a hard row cap so
# state.json stays committable.
KEEP_LEDGER_DAYS = 400
MAX_LEDGER_ROWS = 20000
MAX_PROCESSED_KEYS = 5000

GREEN = 0x2ECC71   # buys
RED = 0xE74C3C     # sells
BLUE = 0x3498DB    # heartbeat / info
ORANGE = 0xE67E22  # warnings / errors


# ----------------------------------------------------------------------
# Config & state
# ----------------------------------------------------------------------

def load_config() -> dict:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_state() -> dict:
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {
        "initialized": False,
        "seen": [],  # list of trade hashes
        "congress": {
            "recent_trades": [],      # rolling tape (dashboard / prices / heartbeat)
            "processed_senate": [],   # report URLs already parsed
            "processed_house": [],    # DocIDs already parsed
        },
        "heartbeat": {
            "last_sent_date": None,   # "YYYY-MM-DD"
            "checks": 0,
            "trades_found": 0,
            "errors": 0,
        },
    }


def _congress_state(state: dict) -> dict:
    """Return state['congress'], creating/normalizing it for pre-existing
    state.json files that were written before this key existed."""
    c = state.setdefault("congress", {})
    c.setdefault("recent_trades", [])
    c.setdefault("processed_senate", [])
    c.setdefault("processed_house", [])
    return c


def prune_ledger(state: dict) -> None:
    """Age out old tape rows and bound the skip lists."""
    c = _congress_state(state)
    cutoff = (datetime.now(timezone.utc)
              - timedelta(days=KEEP_LEDGER_DAYS)).date().isoformat()

    def keep(t: dict) -> bool:
        d = parse_date(t.get("transaction_date")) or parse_date(t.get("disclosure_date"))
        if d is not None:
            return d.date().isoformat() >= cutoff
        # Unparseable dates: fall back to when the bot logged the row.
        return (t.get("logged") or "9999") >= cutoff

    c["recent_trades"] = [t for t in c["recent_trades"] if keep(t)][-MAX_LEDGER_ROWS:]
    c["processed_senate"] = c["processed_senate"][-MAX_PROCESSED_KEYS:]
    c["processed_house"] = c["processed_house"][-MAX_PROCESSED_KEYS:]


def save_state(state: dict) -> None:
    # Keep the seen-set bounded so state.json doesn't grow forever.
    state["seen"] = state["seen"][-50000:]
    prune_ledger(state)
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=1)


# ----------------------------------------------------------------------
# Discord
# ----------------------------------------------------------------------

def discord_post(payload: dict) -> None:
    """Post to the webhook with basic 429 retry."""
    url = os.environ["DISCORD_WEBHOOK_URL"]
    for _ in range(4):
        r = requests.post(url, json=payload, timeout=15)
        if r.status_code == 429:
            retry = r.json().get("retry_after", 2)
            time.sleep(float(retry) + 0.5)
            continue
        r.raise_for_status()
        return
    print("WARN: Discord post gave up after repeated 429s", file=sys.stderr)


def mention(cfg: dict) -> str:
    uid = str(cfg["discord"].get("user_id", "")).strip()
    if uid and uid.isdigit():
        return f"<@{uid}>"
    return ""


def send_trade_alert(cfg: dict, t: dict) -> None:
    is_buy = t["side"] == "buy"
    verb = "bought" if is_buy else "sold"
    emoji = "🟢" if is_buy else "🔴"
    content = mention(cfg) if cfg["discord"].get("mention_on_trade") else ""
    embed = {
        "title": f"{emoji} {t['person']} {verb} {t['ticker'] or t['asset']}",
        "color": GREEN if is_buy else RED,
        "fields": [
            {"name": "Chamber", "value": t["chamber"], "inline": True},
            {"name": "Amount", "value": t["amount"] or "n/a", "inline": True},
            {"name": "Owner", "value": t.get("owner") or "n/a", "inline": True},
            {"name": "Trade date", "value": t["transaction_date"] or "n/a", "inline": True},
            {"name": "Disclosed", "value": t["disclosure_date"] or "n/a", "inline": True},
        ],
        "footer": {"text": "Congress Trade Bot"},
    }
    if t.get("asset") and t.get("ticker"):
        embed["description"] = t["asset"]
    if t.get("link"):
        embed["url"] = t["link"]
    discord_post({"content": content, "embeds": [embed]})
    time.sleep(1.2)  # stay friendly with webhook rate limits


def send_simple(cfg: dict, title: str, description: str, color: int,
                mention_user: bool = False) -> None:
    content = mention(cfg) if mention_user else ""
    discord_post({
        "content": content,
        "embeds": [{
            "title": title,
            "description": description,
            "color": color,
            "footer": {"text": "Congress Trade Bot"},
        }],
    })


# ----------------------------------------------------------------------
# Parsing helpers
# ----------------------------------------------------------------------

def parse_amount_low(amount: str) -> int:
    """'$1,001 - $15,000' -> 1001. Unknown formats -> 0."""
    if not amount:
        return 0
    nums = re.findall(r"[\d,]+", amount)
    if not nums:
        return 0
    try:
        return int(nums[0].replace(",", ""))
    except ValueError:
        return 0


def classify_side(raw_type: str) -> str | None:
    """Normalize transaction type -> 'buy' / 'sell' / None (exchange etc.)."""
    t = (raw_type or "").lower()
    if "purchase" in t or t == "buy":
        return "buy"
    if "sale" in t or "sell" in t or "sold" in t:
        return "sell"
    return None


def parse_date(s: str) -> datetime | None:
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(s.strip(), fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def trade_hash(t: dict) -> str:
    key = "|".join(str(t.get(k, "")) for k in (
        "chamber", "person", "ticker", "asset",
        "transaction_date", "side", "amount", "owner",
    ))
    return hashlib.sha256(key.encode()).hexdigest()[:24]


# ----------------------------------------------------------------------
# Data sources
# ----------------------------------------------------------------------

def fetch_finnhub(tickers: list[str]) -> list[dict]:
    """Optional per-ticker enrichment. Finnhub's congressional endpoint is
    symbol-based, so this only runs against watch_tickers."""
    key = os.environ.get("FINNHUB_API_KEY", "")
    if not key:
        return []
    out = []
    for sym in tickers[:25]:  # respect free-tier rate limits
        try:
            r = requests.get(
                FINNHUB_URL,
                params={"symbol": sym, "token": key},
                timeout=30,
            )
            r.raise_for_status()
            for row in r.json().get("data", []):
                side = classify_side(row.get("transactionType"))
                if not side:
                    continue
                amt_from = row.get("amountFrom")
                amt_to = row.get("amountTo")
                amount = ""
                if amt_from is not None and amt_to is not None:
                    amount = f"${amt_from:,.0f} - ${amt_to:,.0f}"
                out.append({
                    "chamber": row.get("position") or "Congress",
                    "person": (row.get("name") or "").strip(),
                    "ticker": (row.get("symbol") or sym).strip(),
                    "asset": (row.get("assetName") or "").strip(),
                    "side": side,
                    "amount": amount,
                    "owner": (row.get("ownerType") or "").strip(),
                    "transaction_date": (row.get("transactionDate") or "").strip(),
                    "disclosure_date": (row.get("filingDate") or "").strip(),
                    "link": "",
                })
            time.sleep(1.1)  # ~60 req/min free tier
        except Exception as e:
            print(f"WARN: Finnhub failed for {sym}: {e}", file=sys.stderr)
    return out


# ----------------------------------------------------------------------
# Filtering
# ----------------------------------------------------------------------

def passes_filters(cfg: dict, t: dict) -> bool:
    f = cfg["filters"]
    feats = cfg["features"]

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


# ----------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------
# NOTE: the daily heartbeat now lives ONLY in heartbeat.py (it runs last in
# the workflow and has the portfolio snapshot). monitor.py just increments
# the counters below; heartbeat.py reads and resets them.

def main() -> int:
    if "DISCORD_WEBHOOK_URL" not in os.environ:
        print("ERROR: DISCORD_WEBHOOK_URL env var not set", file=sys.stderr)
        return 1

    cfg = load_config()
    rost = roster.load_roster()
    if not roster.group_on(rost, "politicians"):
        print("monitor: politicians group off in roster; skipping congress.")
        return 0
    state = load_state()
    feats = cfg["features"]
    state["heartbeat"]["checks"] += 1

    cstate = _congress_state(state)
    lookback = int(cfg["filters"].get("lookback_days", 45))

    all_trades: list[dict] = []
    errors: list[str] = []

    if feats.get("senate_source", True):
        try:
            rows, processed = congress_sources.fetch_senate(
                lookback_days=lookback,
                skip_reports=cstate["processed_senate"],
            )
            all_trades += rows
            cstate["processed_senate"].extend(processed)
        except Exception as e:
            errors.append(f"Senate source failed: {e}")

    if feats.get("house_source", True):
        try:
            rows, processed = congress_sources.fetch_house(
                lookback_days=lookback,
                skip_docs=cstate["processed_house"],
            )
            all_trades += rows
            cstate["processed_house"].extend(processed)
        except Exception as e:
            errors.append(f"House source failed: {e}")

    if feats.get("finnhub_enrichment", False):
        watch = cfg["filters"].get("watch_tickers") or []
        if watch:
            all_trades += fetch_finnhub(watch)

    for msg in errors:
        print(f"ERROR: {msg}", file=sys.stderr)
        state["heartbeat"]["errors"] += 1
        if feats.get("notify_on_error", True):
            send_simple(cfg, "⚠️ Source error", msg, ORANGE)

    seen = set(state["seen"])
    new_trades = []
    for t in all_trades:
        h = trade_hash(t)
        if h in seen:
            continue
        seen.add(h)
        state["seen"].append(h)
        new_trades.append(t)

    # ── LEDGER: every new buy/sell goes on the tape (dashboard / prices /
    # heartbeat read this), regardless of alert filters. ledger_scope:
    #   "all"    (default) full tape of everything the sources disclosed
    #   "roster" only politicians on your roster.yaml whitelist
    today_iso = datetime.now(timezone.utc).date().isoformat()
    ledger_scope = str(cfg["filters"].get("ledger_scope", "all")).lower()
    ledger_added = 0
    for t in new_trades:
        if t.get("side") not in ("buy", "sell"):
            continue
        if ledger_scope == "roster" and not roster.tracks_politician(rost, t["person"]):
            continue
        rec = dict(t)
        rec["logged"] = today_iso
        cstate["recent_trades"].append(rec)
        ledger_added += 1

    # First run: seed alert state silently so you don't get pinged for the
    # whole lookback window's backfill. The ledger above IS populated, so the
    # dashboard has real history from day one.
    if not state["initialized"]:
        state["initialized"] = True
        save_state(state)
        send_simple(
            cfg, "🏛️ Congress Trade Bot initialized",
            f"Indexed **{len(state['seen'])}** historical trades as already-seen "
            f"and put **{ledger_added}** on the ledger. "
            f"You'll be pinged for anything new from now on.",
            BLUE, mention_user=True,
        )
        print(f"Initialized: {len(state['seen'])} seen, {ledger_added} on ledger.")
        return 0

    # ── ROSTER GATE: only the politicians you curated survive ──
    alertable = [t for t in new_trades
                 if passes_filters(cfg, t) and roster.tracks_politician(rost, t["person"])]
    # durable, never-trimmed history per politician
    for t in alertable:
        roster.append_history(
            "politicians", t["person"].lower(), t["person"],
            {"action": t["side"].upper(), "issuer": t.get("asset") or "",
             "ticker": t.get("ticker") or "", "value": t.get("amount") or "",
             "period": "", "date": t.get("disclosure_date") or ""})
    # Newest disclosures first
    alertable.sort(
        key=lambda t: parse_date(t["disclosure_date"]) or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )

    cap = int(cfg.get("max_pings_per_run", 20))
    for t in alertable[:cap]:
        send_trade_alert(cfg, t)
    if len(alertable) > cap:
        send_simple(
            cfg, "…and more",
            f"{len(alertable) - cap} additional new trades this run "
            f"(capped by max_pings_per_run).",
            BLUE,
        )

    state["heartbeat"]["trades_found"] += len(alertable)
    save_state(state)

    print(
        f"Run complete: {len(all_trades)} rows fetched, "
        f"{len(new_trades)} new, {ledger_added} to ledger, "
        f"{len(alertable)} alerted, {len(errors)} errors."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
