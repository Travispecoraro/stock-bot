#!/usr/bin/env python3
"""
Congress Trade Bot
------------------
Two jobs, not one:

  1. ALERT  — ping Discord when a tracked politician files a new trade.
  2. TAPE   — maintain the durable trade record everything else reads.

Job 2 is the one that was missing. The old version only stored SHA hashes in
state["seen"], which is enough to avoid duplicate pings and useless for
anything else. prices.py found nothing to price, so perf.json came back empty
and the dashboard's Performance tab stayed blank.

The tape lives at the TOP LEVEL of state.json as `recent_trades`, because
that is the key dashboard_14.html already reads (see merged() / unified()).
prices.py falls back to the same key. Nothing downstream needs changing.

Data sources, in order:
  1. congress_sources.py -> efdsearch.senate.gov + disclosures-clerk.house.gov
     (the official government sites; the only ones still serving data)
  2. the community S3 mirrors, kept only as a fallback — they have returned
     403 since the projects shut down, so expect them to fail.

state.json layout this writes:
  recent_trades              durable congress tape (what the dashboard reads)
  seen                       trade hashes, alert dedup only
  congress.seen_reports      Senate PTR URLs already parsed
  congress.seen_docs         House DocIDs already parsed
  heartbeat                  daily counters

Env:
  DISCORD_WEBHOOK_URL   (required)
  SEC_USER_AGENT        (used by congress_sources / roster)
  FINNHUB_API_KEY       (optional, only if finnhub_enrichment: true)
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

import roster

try:
    import congress_sources
    HAVE_OFFICIAL = True
    _OFFICIAL_ERR = ""
except Exception as e:                      # missing deps, syntax error, etc.
    congress_sources = None
    HAVE_OFFICIAL = False
    _OFFICIAL_ERR = str(e)

CONFIG_PATH = "config.yaml"
STATE_PATH = "state.json"

# Legacy community mirrors. Dead since 2026 (403) — fallback only.
SENATE_URL = (
    "https://senate-stock-watcher-data.s3-us-west-2.amazonaws.com"
    "/aggregate/all_transactions.json"
)
HOUSE_URL = (
    "https://house-stock-watcher-data.s3-us-west-2.amazonaws.com"
    "/data/all_transactions.json"
)
FINNHUB_URL = "https://finnhub.io/api/v1/stock/congressional-trading"

# Tape retention. prices.py needs the FULL buy/sell history of a position to
# net it correctly, so this is deliberately long — far longer than the alert
# lookback. Stooq only gives ~220 days of prices, so 400 is ample headroom.
# TAPE_MAX is sized for an open roster (all of Congress ~ 20-30k rows/400d).
# Watch state.json's size: it is committed every 30 minutes, so a very large
# tape inflates the repo and slows the dashboard's GitHub fetch.
TAPE_RETENTION_DAYS = 400
TAPE_MAX = 30000
SEEN_FILINGS_MAX = 4000

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
    state = {}
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            try:
                state = json.load(f)
            except json.JSONDecodeError:
                print("WARN: state.json unreadable; starting fresh", file=sys.stderr)
                state = {}

    # Defensive setdefaults so an existing state.json upgrades in place
    # without losing the insiders / funds / activists keys other scripts own.
    state.setdefault("initialized", False)
    state.setdefault("seen", [])
    state.setdefault("recent_trades", [])          # <- the tape
    cg = state.setdefault("congress", {})
    cg.setdefault("seen_reports", [])
    cg.setdefault("seen_docs", [])
    state.setdefault("heartbeat", {
        "last_sent_date": None, "checks": 0, "trades_found": 0, "errors": 0,
    })
    return state


def save_state(state: dict) -> None:
    state["seen"] = state["seen"][-50000:]
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=1)


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


def classify_side(raw_type: str):
    """Normalize transaction type -> 'buy' / 'sell' / None (exchange etc.)."""
    t = (raw_type or "").lower()
    if "purchase" in t or t == "buy":
        return "buy"
    if "sale" in t or "sell" in t or "sold" in t:
        return "sell"
    return None


def parse_date(s: str):
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(str(s).strip(), fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def iso(s: str) -> str:
    """Normalize any accepted date format to YYYY-MM-DD, or '' if unparseable.

    The tape stores ISO only. This matters: the dashboard dedups live rows
    against SAMPLE_HISTORY on `person|ticker|side|transaction_date`, and
    SAMPLE_HISTORY uses ISO. House PDFs emit MM/DD/YYYY, so without this a
    House row would never dedup against its baked-in twin.
    """
    d = parse_date(s)
    return d.date().isoformat() if d else ""


def trade_hash(t: dict) -> str:
    key = "|".join(str(t.get(k, "")) for k in (
        "chamber", "person", "ticker", "asset",
        "transaction_date", "side", "amount", "owner",
    ))
    return hashlib.sha256(key.encode()).hexdigest()[:24]


# ----------------------------------------------------------------------
# Discord
# ----------------------------------------------------------------------

def discord_post(payload: dict) -> None:
    """Post to the webhook with basic 429 retry."""
    url = os.environ.get("DISCORD_WEBHOOK_URL", "")
    if not url:
        print("WARN: DISCORD_WEBHOOK_URL unset; skipping post", file=sys.stderr)
        return
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
    uid = str(cfg.get("discord", {}).get("user_id", "")).strip()
    return f"<@{uid}>" if uid.isdigit() else ""


def send_trade_alert(cfg: dict, t: dict) -> None:
    is_buy = t["side"] == "buy"
    content = mention(cfg) if cfg.get("discord", {}).get("mention_on_trade") else ""
    embed = {
        "title": f"{'🟢' if is_buy else '🔴'} {t['person']} "
                 f"{'bought' if is_buy else 'sold'} {t['ticker'] or t['asset']}",
        "color": GREEN if is_buy else RED,
        "fields": [
            {"name": "Chamber", "value": t["chamber"] or "n/a", "inline": True},
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
    time.sleep(1.2)


def send_simple(cfg: dict, title: str, description: str, color: int,
                mention_user: bool = False) -> None:
    discord_post({
        "content": mention(cfg) if mention_user else "",
        "embeds": [{"title": title, "description": description, "color": color,
                    "footer": {"text": "Congress Trade Bot"}}],
    })


# ----------------------------------------------------------------------
# Data sources
# ----------------------------------------------------------------------

def fetch_json(url: str) -> list:
    r = requests.get(url, timeout=60)
    r.raise_for_status()
    return r.json()


def normalize_mirror(rows: list, chamber: str, person_key: str) -> list:
    out = []
    for r in rows:
        side = classify_side(r.get("type"))
        if not side:
            continue
        out.append({
            "chamber": chamber,
            "person": (r.get(person_key) or "").strip(),
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


def fetch_official(cfg: dict, state: dict):
    """Official .gov sources via congress_sources. -> (rows, errors)."""
    rows, errors = [], []
    cg = state["congress"]
    lookback = int(cfg.get("filters", {}).get("lookback_days", 45))
    feats = cfg.get("features", {})

    if feats.get("senate_source", True):
        try:
            r, processed = congress_sources.fetch_senate(
                lookback_days=lookback, skip_reports=cg["seen_reports"])
            rows += r
            cg["seen_reports"] = (cg["seen_reports"] + processed)[-SEEN_FILINGS_MAX:]
        except Exception as e:
            errors.append(f"Senate (efdsearch.senate.gov) failed: {e}")

    if feats.get("house_source", True):
        try:
            r, processed = congress_sources.fetch_house(
                lookback_days=lookback, skip_docs=cg["seen_docs"])
            rows += r
            cg["seen_docs"] = (cg["seen_docs"] + processed)[-SEEN_FILINGS_MAX:]
        except Exception as e:
            errors.append(f"House (disclosures-clerk.house.gov) failed: {e}")

    return rows, errors


def fetch_mirrors(cfg: dict):
    """Legacy S3 mirrors. Dead since 2026 — fallback only. -> (rows, errors)."""
    rows, errors = [], []
    feats = cfg.get("features", {})
    if feats.get("senate_source", True):
        try:
            rows += normalize_mirror(fetch_json(SENATE_URL), "Senate", "senator")
        except Exception as e:
            errors.append(f"Senate mirror failed: {e}")
    if feats.get("house_source", True):
        try:
            rows += normalize_mirror(fetch_json(HOUSE_URL), "House", "representative")
        except Exception as e:
            errors.append(f"House mirror failed: {e}")
    return rows, errors


def fetch_finnhub(tickers: list) -> list:
    """Optional per-ticker enrichment; Finnhub's endpoint is symbol-based."""
    key = os.environ.get("FINNHUB_API_KEY", "")
    if not key:
        return []
    out = []
    for sym in tickers[:25]:
        try:
            r = requests.get(FINNHUB_URL, params={"symbol": sym, "token": key},
                             timeout=30)
            r.raise_for_status()
            for row in r.json().get("data", []):
                side = classify_side(row.get("transactionType"))
                if not side:
                    continue
                a_from, a_to = row.get("amountFrom"), row.get("amountTo")
                amount = (f"${a_from:,.0f} - ${a_to:,.0f}"
                          if a_from is not None and a_to is not None else "")
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


# ----------------------------------------------------------------------
# The tape
# ----------------------------------------------------------------------

def tape_key(t: dict) -> str:
    """Matches the dashboard's dedup key: person|ticker|side|transaction_date."""
    return (f"{t.get('person','')}|{t.get('ticker','')}|"
            f"{t.get('side','')}|{t.get('transaction_date','')}")


def append_tape(state: dict, trades: list) -> int:
    """Append roster-tracked trades to the durable tape. Returns count added.

    Deliberately independent of the ping_buys / ping_sells config flags. Those
    are notification preferences; the tape is a position record. Dropping sells
    here would mean positions could never net out, and every name would sit in
    the performance index forever.
    """
    tape = state["recent_trades"]
    have = {tape_key(t) for t in tape}
    today = datetime.now(timezone.utc).date().isoformat()
    added = 0

    for t in trades:
        ticker = (t.get("ticker") or "").strip().upper()
        side = t.get("side")
        txn = iso(t.get("transaction_date"))
        if not ticker or side not in ("buy", "sell") or not txn:
            continue
        row = {
            "person": (t.get("person") or "").strip(),
            "ticker": ticker,
            "side": side,
            "asset": (t.get("asset") or "").strip(),
            "amount": t.get("amount") or "",
            "owner": t.get("owner") or "",
            "chamber": t.get("chamber") or "",
            "transaction_date": txn,
            "disclosure_date": iso(t.get("disclosure_date")),
            "link": t.get("link") or "",
            "logged": today,
        }
        k = tape_key(row)
        if k in have:
            continue
        have.add(k)
        tape.append(row)
        added += 1

    cutoff = (datetime.now(timezone.utc)
              - timedelta(days=TAPE_RETENTION_DAYS)).date().isoformat()
    tape = [t for t in tape if t.get("transaction_date", "") >= cutoff]
    tape.sort(key=lambda t: t.get("transaction_date", ""))
    state["recent_trades"] = tape[-TAPE_MAX:]
    return added


# ----------------------------------------------------------------------
# Filtering (alerts only — the tape is not filtered by these)
# ----------------------------------------------------------------------

def passes_filters(cfg: dict, t: dict) -> bool:
    f = cfg.get("filters", {})
    feats = cfg.get("features", {})

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
    if not cfg.get("features", {}).get("heartbeat", True):
        return
    hb = state["heartbeat"]
    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")
    if hb.get("last_sent_date") == today:
        return
    if now.hour < int(cfg.get("heartbeat_hour_utc", 14)):
        return
    desc = (f"✅ Bot is alive.\n"
            f"Since last heartbeat: **{hb['checks']}** checks, "
            f"**{hb['trades_found']}** new trades pinged, "
            f"**{hb['errors']}** source errors.\n"
            f"Tape: **{len(state['recent_trades'])}** congress trades on file.")
    if hb["trades_found"] == 0:
        desc += "\nNothing new found — Congress is behaving (or just quiet)."
    send_simple(cfg, "Daily heartbeat", desc, BLUE,
                mention_user=cfg.get("discord", {}).get("mention_on_heartbeat", False))
    hb.update({"last_sent_date": today, "checks": 0, "trades_found": 0, "errors": 0})


# ----------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------

def main() -> int:
    if not os.environ.get("DISCORD_WEBHOOK_URL"):
        print("ERROR: DISCORD_WEBHOOK_URL env var not set", file=sys.stderr)
        return 1

    cfg = load_config()
    rost = roster.load_roster()
    if not roster.group_on(rost, "politicians"):
        print("monitor: politicians group off in roster; skipping congress.")
        return 0

    state = load_state()
    feats = cfg.get("features", {})
    state["heartbeat"]["checks"] += 1

    # ── fetch: official first, mirrors only if official gave us nothing ──
    all_trades, errors = [], []
    if HAVE_OFFICIAL:
        all_trades, errors = fetch_official(cfg, state)
        print(f"monitor: official sources -> {len(all_trades)} transactions")
    else:
        errors.append(f"congress_sources unavailable ({_OFFICIAL_ERR})")

    if not all_trades:
        m_rows, m_errs = fetch_mirrors(cfg)
        if m_rows:
            print(f"monitor: fell back to S3 mirrors -> {len(m_rows)} rows")
        all_trades += m_rows
        errors += m_errs

    if feats.get("finnhub_enrichment", False):
        watch = cfg.get("filters", {}).get("watch_tickers") or []
        if watch:
            all_trades += fetch_finnhub(watch)

    for msg in errors:
        print(f"ERROR: {msg}", file=sys.stderr)
        state["heartbeat"]["errors"] += 1
        if feats.get("notify_on_error", True):
            send_simple(cfg, "⚠️ Source error", msg, ORANGE)

    # ── dedup against the alert-seen set ──
    seen = set(state["seen"])
    new_trades = []
    for t in all_trades:
        h = trade_hash(t)
        if h in seen:
            continue
        seen.add(h)
        state["seen"].append(h)
        new_trades.append(t)

    # ── ROSTER GATE ──
    tracked = [t for t in new_trades if roster.tracks_politician(rost, t["person"])]

    # ── TAPE: always, regardless of ping prefs or the first-run gate ──
    added = append_tape(state, tracked)

    for t in tracked:
        roster.append_history(
            "politicians", t["person"].lower(), t["person"],
            {"action": t["side"].upper(), "issuer": t.get("asset") or "",
             "ticker": t.get("ticker") or "", "value": t.get("amount") or "",
             "period": "", "date": iso(t.get("disclosure_date"))})

    # First ever run: record everything, ping nothing.
    if not state["initialized"]:
        state["initialized"] = True
        save_state(state)
        send_simple(cfg, "🏛️ Congress Trade Bot initialized",
                    f"Indexed **{len(state['seen'])}** historical trades as seen "
                    f"and seeded the tape with **{added}**. "
                    f"You'll be pinged for anything new from now on.",
                    BLUE, mention_user=True)
        print(f"Initialized: {len(state['seen'])} seen, {added} on tape.")
        return 0

    # ── ALERT ──
    alertable = [t for t in tracked if passes_filters(cfg, t)]
    alertable.sort(key=lambda t: parse_date(t["disclosure_date"])
                   or datetime.min.replace(tzinfo=timezone.utc), reverse=True)

    cap = int(cfg.get("max_pings_per_run", 20))
    for t in alertable[:cap]:
        send_trade_alert(cfg, t)
    if len(alertable) > cap:
        send_simple(cfg, "…and more",
                    f"{len(alertable) - cap} additional new trades this run "
                    f"(capped by max_pings_per_run).", BLUE)

    state["heartbeat"]["trades_found"] += len(alertable)
    maybe_heartbeat(cfg, state)
    save_state(state)

    print(f"Run complete: {len(all_trades)} fetched, {len(new_trades)} new, "
          f"{len(tracked)} on roster, {added} added to tape "
          f"({len(state['recent_trades'])} total), {len(alertable)} alerted, "
          f"{len(errors)} errors.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
