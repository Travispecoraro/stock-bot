#!/usr/bin/env python3
"""
Congress Trade Bot (v4)
-----------------------
Checks congressional stock-trade disclosures and posts to a Discord
webhook. Designed for a GitHub Actions cron schedule.

Sources:
  * PRIMARY: official Senate eFD site (efdsearch.senate.gov) — electronic
    Periodic Transaction Reports parsed directly. All senators.
  * DORMANT: CapitolTrades JSON API (broken with 503s as of Jul 2026;
    toggle capitoltrades_source back on if it recovers -> House coverage).
  * BACKUP: Finnhub per-ticker congressional endpoint (finnhub_enrichment,
    needs watch_tickers; covers both chambers for listed tickers).

Behavior:
  * First successful run seeds silently (no historical flood) and builds
    the holdings ledger from the full lookback window.
  * Alerts: one digest-style message per run featuring the newest trade,
    plus a count of other trades disclosed today.
  * Daily update: on quiet days only, at heartbeat_hour_utc.
  * Top-holdings section: shown once per day (quiet update or first alert).
  * Manual "Run workflow" trigger always produces a visible message.

Env vars required:
  DISCORD_WEBHOOK_URL   (GitHub secret)
  FINNHUB_API_KEY       (GitHub secret; only if finnhub_enrichment: true)
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
from bs4 import BeautifulSoup

CONFIG_PATH = "config.yaml"
STATE_PATH = "state.json"

EFD_BASE = "https://efdsearch.senate.gov"
EFD_HOME = EFD_BASE + "/search/home/"
EFD_SEARCH_REFERER = EFD_BASE + "/search/"
EFD_DATA = EFD_BASE + "/search/report/data/"

CAPITOLTRADES_URL = "https://bff.capitoltrades.com/trades"
FINNHUB_URL = "https://finnhub.io/api/v1/stock/congressional-trading"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Accept": "application/json",
}

# Muted, professional palette
GREEN = 0x1E8449    # purchases
RED = 0xB03A2E      # sales
NAVY = 0x2C3E50     # daily updates / info
AMBER = 0xB9770E    # warnings / errors

FOOTER = "Congress Trade Bot  •  Data: U.S. Senate eFD"


def is_manual_run() -> bool:
    """True when this run was triggered by the Actions 'Run workflow'
    button rather than the cron schedule."""
    return os.environ.get("GITHUB_EVENT_NAME", "") == "workflow_dispatch"


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
        "seen": [],
        "seen_filings": [],
        "daily": {"date": None, "count": 0, "section_sent": False},
        "positions": {},
        "heartbeat": {
            "last_sent_date": None,
            "checks": 0,
            "trades_found": 0,
            "errors": 0,
        },
    }


def save_state(state: dict) -> None:
    state["seen"] = state["seen"][-50000:]
    state["seen_filings"] = state.get("seen_filings", [])[-5000:]
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=1)


def roll_daily(state: dict) -> dict:
    """Return today's daily counters, resetting them on date change."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    daily = state.get("daily") or {}
    if daily.get("date") != today:
        daily = {"date": today, "count": 0, "section_sent": False}
    state["daily"] = daily
    return daily


# ----------------------------------------------------------------------
# Discord
# ----------------------------------------------------------------------

def discord_post(payload: dict) -> None:
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
    return f"<@{uid}>" if uid.isdigit() else ""


def base_embed(title: str, description: str, color: int) -> dict:
    footer = FOOTER + ("  •  Manual check" if is_manual_run() else "")
    return {
        "title": title,
        "description": description,
        "color": color,
        "fields": [],
        "footer": {"text": footer},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def send_embed(cfg: dict, title: str, description: str, color: int,
               fields: list | None = None, url: str = "",
               mention_user: bool = False) -> None:
    embed = base_embed(title, description, color)
    if fields:
        embed["fields"] = fields
    if url:
        embed["url"] = url
    content = mention(cfg) if mention_user else ""
    discord_post({"content": content, "embeds": [embed]})


def send_trade_alert(cfg: dict, t: dict, others_today: int,
                     holdings_field: dict | None = None) -> None:
    """Congressional Trading Alert — featured trade + detail columns."""
    is_buy = t["side"] == "buy"
    action = "purchase" if is_buy else "sale"
    company = t["asset"] or t["ticker"] or "an unlisted asset"
    ticker_part = f" (`{t['ticker']}`)" if t["ticker"] else ""
    headline = (
        f"**{t['person']}** disclosed a **{action}** of "
        f"**{company}**{ticker_part}."
    )
    fields = [
        {"name": "Amount", "value": t["amount"] or "—", "inline": True},
        {"name": "Trade Date", "value": t["transaction_date"] or "—", "inline": True},
        {"name": "Disclosed", "value": t["disclosure_date"] or "—", "inline": True},
    ]
    if others_today > 0:
        plural = "trade was" if others_today == 1 else "trades were"
        fields.append({
            "name": "Today's Activity",
            "value": f"**{others_today}** other notable {plural} disclosed today.",
            "inline": False,
        })
    if holdings_field:
        fields.append(holdings_field)
    send_embed(
        cfg, "Congressional Trading Alert", headline,
        GREEN if is_buy else RED,
        fields=fields, url=t.get("link", ""),
        mention_user=cfg["discord"].get("mention_on_trade", True),
    )
    time.sleep(1.2)


def send_daily_update(cfg: dict, state: dict, body: str) -> None:
    """Congressional Trading Update — quiet-day / manual status message.
    Carries the holdings section if it hasn't been shown today."""
    daily = roll_daily(state)
    fields = []
    if not daily.get("section_sent"):
        hf = top_holdings_field(state)
        if hf:
            fields.append(hf)
            daily["section_sent"] = True
    send_embed(
        cfg, "Congressional Trading Update", body, NAVY,
        fields=fields,
        mention_user=cfg["discord"].get("mention_on_heartbeat", False),
    )


# ----------------------------------------------------------------------
# Parsing helpers
# ----------------------------------------------------------------------

def parse_amount_low(amount: str) -> int:
    """'$1,001 - $15,000' or '~$15,000' -> first number. Unknown -> 0."""
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
    t = (raw_type or "").lower()
    if "purchase" in t or t == "buy":
        return "buy"
    if "sale" in t or "sell" in t or "sold" in t:
        return "sell"
    return None  # Exchange / receive / unknown


def parse_date(s: str) -> datetime | None:
    if not s:
        return None
    s = s.strip()[:10]
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def trade_hash(t: dict) -> str:
    if t.get("uid"):
        return f"uid:{t['uid']}"
    key = "|".join(str(t.get(k, "")) for k in (
        "chamber", "person", "ticker", "asset",
        "transaction_date", "side", "amount",
    ))
    return hashlib.sha256(key.encode()).hexdigest()[:24]


# ----------------------------------------------------------------------
# Data source: Senate eFD (primary — official government site)
# ----------------------------------------------------------------------

def efd_session() -> requests.Session:
    """Open a session and accept the eFD usage agreement (CSRF dance)."""
    s = requests.Session()
    s.headers.update(HEADERS)
    r = s.get(EFD_HOME, timeout=30)
    r.raise_for_status()
    m = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', r.text)
    token = m.group(1) if m else s.cookies.get("csrftoken", "")
    r = s.post(
        EFD_HOME,
        data={"prohibition_agreement": "1", "csrfmiddlewaretoken": token},
        headers={"Referer": EFD_HOME},
        timeout=30,
    )
    r.raise_for_status()
    return s


def efd_search_ptrs(s: requests.Session, days: int) -> list[dict]:
    """List electronic PTR filings from the last `days` days, newest first.
    Paper (scanned) filings are skipped — images, not tables."""
    start = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%m/%d/%Y")
    payload = {
        "draw": "1", "start": "0", "length": "100",
        "report_types": "[11]",          # 11 = Periodic Transaction Report
        "filer_types": "[]",
        "submitted_start_date": f"{start} 00:00:00",
        "submitted_end_date": "",
        "candidate_state": "", "senator_state": "", "office_id": "",
        "first_name": "", "last_name": "",
        "order[0][column]": "4", "order[0][dir]": "desc",
    }
    r = s.post(
        EFD_DATA, data=payload,
        headers={
            "Referer": EFD_SEARCH_REFERER,
            "X-CSRFToken": s.cookies.get("csrftoken", ""),
        },
        timeout=30,
    )
    r.raise_for_status()
    out = []
    for row in r.json().get("data", []):
        joined = " ".join(str(c) for c in row)
        m = re.search(r'href="(/search/view/[^"]+)"', joined)
        if not m:
            continue
        href = m.group(1)
        if "/paper/" in href:
            continue
        cells = [re.sub(r"<[^>]+>", " ", str(c)).strip() for c in row]
        name = " ".join(x for x in cells[:2] if x).strip() or "Unknown senator"
        date = cells[-1].strip() if cells else ""
        out.append({"url": EFD_BASE + href, "name": name, "date": date})
    return out


def efd_parse_ptr(s: requests.Session, filing: dict) -> list[dict]:
    """Fetch one PTR page and parse its transactions table into trades."""
    r = s.get(filing["url"], headers={"Referer": EFD_SEARCH_REFERER}, timeout=30)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    table = soup.find("table")
    if not table:
        return []
    uuid_m = re.search(r"/view/\w+/([\w-]+)/?", filing["url"])
    filing_uid = uuid_m.group(1) if uuid_m else filing["url"]
    trades = []
    for tr in table.find_all("tr"):
        tds = [td.get_text(" ", strip=True) for td in tr.find_all("td")]
        if len(tds) < 8:
            continue
        # Columns: # | Transaction Date | Owner | Ticker | Asset Name
        #          | Asset Type | Type | Amount | Comment
        side = classify_side(tds[6])
        if not side:
            continue
        trades.append({
            "uid": f"efd-{filing_uid}-{tds[0]}",
            "raw_type": tds[6],
            "chamber": "Senate",
            "person": filing["name"],
            "ticker": tds[3].replace("--", "").strip(),
            "asset": tds[4].strip(),
            "side": side,
            "amount": tds[7].strip(),
            "transaction_date": tds[1].strip(),
            "disclosure_date": filing["date"],
            "link": filing["url"],
        })
    return trades


def fetch_senate_efd(state: dict, days: int, max_filings: int,
                     seed: bool = False) -> list[dict]:
    """Pull and parse new PTR filings; each filing is parsed once (cached
    in state['seen_filings']). When seed=True, EVERY filing in the window
    is marked seen — including unparsed ones — so historical backlog can
    never trigger alerts later."""
    s = efd_session()
    filings = efd_search_ptrs(s, days)
    seen = set(state.get("seen_filings", []))
    trades = []
    parsed = 0
    for f in filings:
        if f["url"] in seen or parsed >= max_filings:
            continue
        try:
            trades += efd_parse_ptr(s, f)
            state.setdefault("seen_filings", []).append(f["url"])
            seen.add(f["url"])
            parsed += 1
            time.sleep(1.0)  # be polite to the government
        except Exception as e:
            print(f"WARN: failed to parse {f['url']}: {e}", file=sys.stderr)
    if seed:
        for f in filings:
            if f["url"] not in seen:
                state.setdefault("seen_filings", []).append(f["url"])
                seen.add(f["url"])
    return trades


# ----------------------------------------------------------------------
# Data source: CapitolTrades (dormant — flip on if their API recovers)
# ----------------------------------------------------------------------

def fetch_capitoltrades(pages: int) -> list:
    rows = []
    for page in range(1, max(1, pages) + 1):
        r = requests.get(
            CAPITOLTRADES_URL,
            params={"page": page, "pageSize": 96},
            headers=HEADERS, timeout=30,
        )
        r.raise_for_status()
        batch = r.json().get("data") or []
        if not batch:
            break
        rows.extend(batch)
        time.sleep(0.7)
    return rows


def normalize_capitoltrades(rows: list) -> list[dict]:
    out = []
    for r in rows:
        try:
            side = classify_side(r.get("txType"))
            if not side:
                continue
            pol = r.get("politician") or {}
            person = " ".join(
                x for x in [pol.get("firstName"), pol.get("lastName")] if x
            ).strip() or str(r.get("_politicianId") or "Unknown member")
            asset = r.get("asset") or {}
            issuer = r.get("issuer") or {}
            raw_ticker = asset.get("assetTicker") or issuer.get("issuerTicker") or ""
            low, high = r.get("sizeRangeLow"), r.get("sizeRangeHigh")
            value = r.get("value")
            if low and high:
                amount = f"${low:,.0f} - ${high:,.0f}"
            elif value:
                amount = f"~${value:,.0f}"
            else:
                amount = ""
            tx_id = r.get("_txId")
            link = (r.get("filingURL") or "").strip()
            if not link and tx_id:
                link = f"https://www.capitoltrades.com/trades/{tx_id}"
            out.append({
                "uid": f"ct-{tx_id}" if tx_id else "",
                "raw_type": r.get("txType") or "",
                "chamber": (pol.get("chamber") or "").title() or "Congress",
                "person": person,
                "ticker": raw_ticker.split(":")[0].strip(),
                "asset": (issuer.get("issuerName") or "").strip(),
                "side": side,
                "amount": amount,
                "transaction_date": (r.get("txDate") or "")[:10],
                "disclosure_date": (r.get("pubDate") or r.get("filingDate") or "")[:10],
                "link": link,
            })
        except Exception as e:
            print(f"WARN: skipped malformed CapitolTrades row: {e}", file=sys.stderr)
    return out


# ----------------------------------------------------------------------
# Data source: Finnhub (backup — per-ticker, needs watch_tickers)
# ----------------------------------------------------------------------

def fetch_finnhub(tickers: list[str]) -> list[dict]:
    key = os.environ.get("FINNHUB_API_KEY", "")
    if not key:
        return []
    out = []
    for sym in tickers[:25]:
        try:
            r = requests.get(
                FINNHUB_URL, params={"symbol": sym, "token": key}, timeout=30,
            )
            r.raise_for_status()
            for row in r.json().get("data", []):
                side = classify_side(row.get("transactionType"))
                if not side:
                    continue
                amt_from, amt_to = row.get("amountFrom"), row.get("amountTo")
                amount = ""
                if amt_from is not None and amt_to is not None:
                    amount = f"${amt_from:,.0f} - ${amt_to:,.0f}"
                out.append({
                    "uid": "",
                    "raw_type": row.get("transactionType") or "",
                    "chamber": row.get("position") or "Congress",
                    "person": (row.get("name") or "").strip(),
                    "ticker": (row.get("symbol") or sym).strip(),
                    "asset": (row.get("assetName") or "").strip(),
                    "side": side,
                    "amount": amount,
                    "transaction_date": (row.get("transactionDate") or "")[:10],
                    "disclosure_date": (row.get("filingDate") or "")[:10],
                    "link": "",
                })
            time.sleep(1.1)
        except Exception as e:
            print(f"WARN: Finnhub failed for {sym}: {e}", file=sys.stderr)
    return out


# ----------------------------------------------------------------------
# Position ledger — reconstructed from disclosed trades
# ----------------------------------------------------------------------

def update_positions(state: dict, trades: list[dict]) -> None:
    """Approximate who-holds-what ledger. Purchase opens a position;
    a full sale closes it; a partial sale keeps it open. Only trades
    the bot has observed count — approximate by design."""
    pos = state.setdefault("positions", {})
    for t in trades:
        ticker = (t.get("ticker") or "").strip()
        person = (t.get("person") or "").strip()
        if not ticker or not person:
            continue
        rec = pos.setdefault(ticker, {"holders": {}, "name": ""})
        if t.get("asset"):
            rec["name"] = t["asset"]
        if t["side"] == "buy":
            rec["holders"][person] = 1
        elif "partial" not in (t.get("raw_type") or "").lower():
            rec["holders"].pop(person, None)


def top_holdings_field(state: dict, n: int = 3) -> dict | None:
    """Top-N most-held stocks among senators as an embed field, or None."""
    rows = []
    for ticker, rec in state.get("positions", {}).items():
        count = len(rec.get("holders", {}))
        if count > 0:
            rows.append((count, ticker, rec.get("name", "")))
    if not rows:
        return None
    rows.sort(key=lambda r: (-r[0], r[1]))
    lines = []
    for i, (count, ticker, name) in enumerate(rows[:n], 1):
        label = f"{name} (`{ticker}`)" if name else f"`{ticker}`"
        word = "senator" if count == 1 else "senators"
        lines.append(f"**{i}.**  {label}  —  **{count}** {word}  ·  {count}%")
    lines.append("-# Reconstructed from disclosed trades")
    return {
        "name": "📊  Top Stocks Held by Senators",
        "value": "\n".join(lines),
        "inline": False,
    }


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
# Daily update scheduling
# ----------------------------------------------------------------------

def reset_heartbeat(state: dict, today: str) -> None:
    hb = state["heartbeat"]
    hb["last_sent_date"] = today
    hb["checks"] = 0
    hb["trades_found"] = 0
    hb["errors"] = 0


def maybe_heartbeat(cfg: dict, state: dict) -> None:
    """On quiet days, send the daily update at heartbeat_hour_utc.
    On active days the alerts were the update — send nothing extra."""
    if not cfg["features"].get("heartbeat", True):
        return
    hb = state["heartbeat"]
    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")
    if hb["last_sent_date"] == today:
        return
    if now.hour < int(cfg.get("heartbeat_hour_utc", 14)):
        return
    if hb["trades_found"] == 0:
        send_daily_update(
            cfg, state,
            "No notable congressional stock trades were disclosed today.",
        )
    reset_heartbeat(state, today)


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

    # Self-heal: an "initialized" state with nothing indexed doesn't count.
    effectively_initialized = state["initialized"] and len(state["seen"]) > 0

    all_trades: list[dict] = []
    errors: list[str] = []

    if feats.get("senate_efd_source", True):
        try:
            all_trades += fetch_senate_efd(
                state,
                days=int(cfg["filters"].get("lookback_days", 45)),
                max_filings=int(cfg.get(
                    "seed_max_filings" if not effectively_initialized
                    else "max_filings_per_run",
                    100 if not effectively_initialized else 25,
                )),
                seed=not effectively_initialized,
            )
        except Exception as e:
            errors.append(f"Senate eFD source failed: {e}")

    if feats.get("capitoltrades_source", False):
        try:
            pages = int(cfg.get(
                "seed_pages" if not effectively_initialized else "pages_per_run",
                10 if not effectively_initialized else 3,
            ))
            all_trades += normalize_capitoltrades(fetch_capitoltrades(pages))
        except Exception as e:
            errors.append(f"CapitolTrades source failed: {e}")

    if feats.get("finnhub_enrichment", False):
        watch = cfg["filters"].get("watch_tickers") or []
        if watch:
            try:
                all_trades += fetch_finnhub(watch)
            except Exception as e:
                errors.append(f"Finnhub source failed: {e}")

    for msg in errors:
        print(f"ERROR: {msg}", file=sys.stderr)
        state["heartbeat"]["errors"] += 1
        if feats.get("notify_on_error", True):
            send_embed(cfg, "⚠️ Source Error", msg, AMBER)

    seen = set(state["seen"])
    new_trades = []
    for t in all_trades:
        h = trade_hash(t)
        if h in seen:
            continue
        seen.add(h)
        state["seen"].append(h)
        new_trades.append(t)

    update_positions(state, new_trades)

    # First successful run: seed silently — history never floods the channel.
    if not effectively_initialized:
        if all_trades:
            state["initialized"] = True
            save_state(state)
            send_embed(
                cfg, "🏛️ Congress Trade Bot Initialized",
                f"Connected to the U.S. Senate eFD data source and indexed "
                f"**{len(state['seen'])}** recent trades as already-seen. "
                f"You'll be alerted to anything new from now on.",
                NAVY,
                fields=[f for f in [top_holdings_field(state)] if f],
                mention_user=True,
            )
            print(f"Initialized with {len(state['seen'])} trades.")
        else:
            save_state(state)
            print("No data fetched; initialization deferred to next run.")
        return 0

    alertable = [t for t in new_trades if passes_filters(cfg, t)]
    alertable.sort(
        key=lambda t: parse_date(t["disclosure_date"])
        or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )

    daily = roll_daily(state)
    if alertable:
        featured = alertable[0]
        others_today = (len(alertable) - 1) + daily["count"]
        hf = None if daily.get("section_sent") else top_holdings_field(state)
        send_trade_alert(cfg, featured, others_today, holdings_field=hf)
        if hf:
            daily["section_sent"] = True
        daily["count"] += len(alertable)

    # Manual "Run workflow" trigger: always produce a visible message.
    # If no alert just went out, send the daily update immediately —
    # regardless of the scheduled hour — and mark today's update as
    # sent so the schedule doesn't duplicate it later.
    if is_manual_run() and not alertable:
        n = daily["count"]
        if n > 0:
            plural = "trade has" if n == 1 else "trades have"
            body = f"**{n}** notable {plural} been disclosed today."
        else:
            body = "No notable congressional stock trades were disclosed today."
        send_daily_update(cfg, state, body)
        reset_heartbeat(state, daily["date"])

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
