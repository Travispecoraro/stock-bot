#!/usr/bin/env python3
"""
Congress Trade Bot (v3 — official Senate eFD primary source)
-------------------------------------------------------------
Checks congressional stock-trade disclosures and pings a Discord webhook
when new trades appear. Designed to run on a GitHub Actions cron schedule.

v3 changes:
  * Primary source is now the official Senate eFD site
    (efdsearch.senate.gov) — electronic Periodic Transaction Reports
    parsed directly from the government source. Covers all senators.
  * CapitolTrades kept as an OFF-by-default toggle (their API broke
    with CloudFront/Lambda 503s in Jul 2026; flip capitoltrades_source
    back on if it recovers to regain House coverage).
  * Finnhub remains an optional per-ticker backup (finnhub_enrichment) —
    it covers BOTH chambers for tickers you watchlist, so it is the
    interim way to see House trades on stocks you care about.
  * Parsed filings are cached in state so PTR pages are fetched once.

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
        "seen": [],
        "seen_filings": [],
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
            {"name": "Chamber", "value": t["chamber"] or "n/a", "inline": True},
            {"name": "Party", "value": t.get("party") or "n/a", "inline": True},
            {"name": "Amount", "value": t["amount"] or "n/a", "inline": True},
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
    return None  # exchange / receive / unknown


def parse_date(s: str) -> datetime | None:
    if not s:
        return None
    s = s.strip()[:10]  # tolerate ISO timestamps like 2026-07-18T12:00:00Z
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
    """List electronic PTR filings submitted in the last `days` days.
    Returns [{url, name, date}] newest first. Paper (scanned) filings
    are skipped — they're images, not parseable tables."""
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
    rows = r.json().get("data", [])
    out = []
    for row in rows:
        joined = " ".join(str(c) for c in row)
        m = re.search(r'href="(/search/view/[^"]+)"', joined)
        if not m:
            continue
        href = m.group(1)
        if "/paper/" in href:
            continue  # scanned paper filing — no table to parse
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
            continue  # header or malformed row
        # Columns: # | Transaction Date | Owner | Ticker | Asset Name
        #          | Asset Type | Type | Amount | Comment
        side = classify_side(tds[6])
        if not side:
            continue  # Exchange / other
        ticker = tds[3].replace("--", "").strip()
        trades.append({
            "uid": f"efd-{filing_uid}-{tds[0]}",
            "chamber": "Senate",
            "party": "",
            "person": filing["name"],
            "ticker": ticker,
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
    """Pull new PTR filings and parse them. Already-parsed filings are
    skipped via state['seen_filings'] so each PTR page is fetched once.

    When seed=True (the bot's first successful run), EVERY filing in the
    window is marked as seen — including ones beyond the per-run parse
    cap — so historical backlog never gets alerted later."""
    s = efd_session()
    filings = efd_search_ptrs(s, days)
    seen_filings = set(state.get("seen_filings", []))
    trades = []
    parsed = 0
    for f in filings:
        if f["url"] in seen_filings:
            continue
        if parsed >= max_filings:
            break
        try:
            trades += efd_parse_ptr(s, f)
            state.setdefault("seen_filings", []).append(f["url"])
            seen_filings.add(f["url"])
            parsed += 1
            time.sleep(1.0)  # be polite to the government
        except Exception as e:
            print(f"WARN: failed to parse {f['url']}: {e}", file=sys.stderr)
    if seed:
        for f in filings:
            if f["url"] not in seen_filings:
                state.setdefault("seen_filings", []).append(f["url"])
                seen_filings.add(f["url"])
    return trades

# ----------------------------------------------------------------------
# Data source: CapitolTrades (dormant toggle — flip on if their API recovers)
# ----------------------------------------------------------------------

def fetch_capitoltrades(pages: int) -> list:
    """Fetch recent trades from CapitolTrades' public JSON endpoint.
    Newest trades come first; each page is up to ~96 rows."""
    rows = []
    for page in range(1, max(1, pages) + 1):
        r = requests.get(
            CAPITOLTRADES_URL,
            params={"page": page, "pageSize": 96},
            headers=HEADERS,
            timeout=30,
        )
        r.raise_for_status()
        payload = r.json()
        batch = payload.get("data") or []
        if not batch:
            break
        rows.extend(batch)
        time.sleep(0.7)  # be polite
    return rows


def _fmt_party(p: str) -> str:
    return {"democrat": "Democrat", "republican": "Republican"}.get(
        (p or "").lower(), (p or "").title()
    )


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
            chamber = (pol.get("chamber") or "").title() or "Congress"

            asset = r.get("asset") or {}
            issuer = r.get("issuer") or {}
            raw_ticker = asset.get("assetTicker") or issuer.get("issuerTicker") or ""
            ticker = raw_ticker.split(":")[0].strip()  # "AAPL:US" -> "AAPL"
            asset_name = (issuer.get("issuerName") or "").strip()

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
                "chamber": chamber,
                "party": _fmt_party(pol.get("party")),
                "person": person,
                "ticker": ticker,
                "asset": asset_name,
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
                FINNHUB_URL,
                params={"symbol": sym, "token": key},
                timeout=30,
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
                    "chamber": row.get("position") or "Congress",
                    "party": "",
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

    # Self-heal: a previous "initialized" run that indexed nothing doesn't count.
    effectively_initialized = state["initialized"] and len(state["seen"]) > 0

    all_trades: list[dict] = []
    errors: list[str] = []

    if feats.get("senate_efd_source", True):
        try:
            all_trades += fetch_senate_efd(
                state,
                days=int(cfg["filters"].get("lookback_days", 45)),
                max_filings=int(cfg.get("max_filings_per_run", 25)),
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

    # First *successful* run: seed silently so you don't get flooded
    # with historical backfill. Only counts if we actually got data.
    if not effectively_initialized:
        if all_trades:
            state["initialized"] = True
            save_state(state)
            send_simple(
                cfg, "🏛️ Congress Trade Bot initialized",
                f"Connected to data source and indexed "
                f"**{len(state['seen'])}** recent trades as already-seen. "
                f"You'll be pinged for anything new from now on.",
                BLUE, mention_user=True,
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
