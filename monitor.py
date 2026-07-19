#!/usr/bin/env python3
"""
Congress Trade Bot
------------------
Checks congressional stock-trade disclosures and pings a Discord webhook
when new trades appear. Designed to run on a GitHub Actions cron schedule.

State (already-seen trades, heartbeat counters) lives in state.json, which
the workflow commits back to the repo after each run.

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

CONFIG_PATH = "config.yaml"
STATE_PATH = "state.json"

SENATE_URL = (
    "https://senate-stock-watcher-data.s3-us-west-2.amazonaws.com"
    "/aggregate/all_transactions.json"
)
HOUSE_URL = (
    "https://house-stock-watcher-data.s3-us-west-2.amazonaws.com"
    "/data/all_transactions.json"
)
FINNHUB_URL = "https://finnhub.io/api/v1/stock/congressional-trading"

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
        "heartbeat": {
            "last_sent_date": None,   # "YYYY-MM-DD"
            "checks": 0,
            "trades_found": 0,
            "errors": 0,
        },
    }


def save_state(state: dict) -> None:
    # Keep the seen-set bounded so state.json doesn't grow forever.
    state["seen"] = state["seen"][-50000:]
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

def fetch_json(url: str) -> list:
    r = requests.get(url, timeout=60)
    r.raise_for_status()
    return r.json()


def normalize_senate(rows: list) -> list[dict]:
    out = []
    for r in rows:
        side = classify_side(r.get("type"))
        if not side:
            continue
        out.append({
            "chamber": "Senate",
            "person": (r.get("senator") or "").strip(),
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


def normalize_house(rows: list) -> list[dict]:
    out = []
    for r in rows:
        side = classify_side(r.get("type"))
        if not side:
            continue
        out.append({
            "chamber": "House",
            "person": (r.get("representative") or "").strip(),
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
# Heartbeat
# ----------------------------------------------------------------------

def maybe_heartbeat(cfg: dict, state: dict) -> None:
    if not cfg["features"].get("heartbeat", True):
        return
    hb = state["heartbeat"]
    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")
    if hb["last_sent_date"] == today:
        return
    if now.hour < int(cfg.get("heartbeat_hour_utc", 14)):
        return
    desc = (
        f"✅ Bot is alive.\n"
        f"Since last heartbeat: **{hb['checks']}** checks, "
        f"**{hb['trades_found']}** new trades pinged, "
        f"**{hb['errors']}** source errors."
    )
    if hb["trades_found"] == 0:
        desc += "\nNothing new found — Congress is behaving (or just quiet)."
    send_simple(
        cfg, "Daily heartbeat", desc, BLUE,
        mention_user=cfg["discord"].get("mention_on_heartbeat", False),
    )
    hb["last_sent_date"] = today
    hb["checks"] = 0
    hb["trades_found"] = 0
    hb["errors"] = 0


# ----------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------

def main() -> int:
    if "DISCORD_WEBHOOK_URL" not in os.environ:
        print("ERROR: DISCORD_WEBHOOK_URL env var not set", file=sys.stderr)
        return 1

    cfg = load_config()
    state = load_state()
    feats = cfg["features"]
    state["heartbeat"]["checks"] += 1

    all_trades: list[dict] = []
    errors: list[str] = []

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

    # First run: seed state silently so you don't get years of backfill.
    if not state["initialized"]:
        state["initialized"] = True
        save_state(state)
        send_simple(
            cfg, "🏛️ Congress Trade Bot initialized",
            f"Indexed **{len(state['seen'])}** historical trades as already-seen. "
            f"You'll be pinged for anything new from now on.",
            BLUE, mention_user=True,
        )
        print(f"Initialized with {len(state['seen'])} historical trades.")
        return 0

    alertable = [t for t in new_trades if passes_filters(cfg, t)]
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
    maybe_heartbeat(cfg, state)
    save_state(state)

    print(
        f"Run complete: {len(all_trades)} rows fetched, "
        f"{len(new_trades)} new, {len(alertable)} alerted, "
        f"{len(errors)} errors."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
