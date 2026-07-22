"""
insiders.py — Corporate insider (SEC Form 4) monitor for stock-bot.

Watches the SEC EDGAR "current filings" feed for Form 4s, parses open-market
buys (code P) and sells (code S), and pings Discord when either:
  - CLUSTER: 2+ distinct insiders trade the same direction in the same stock
    within CLUSTER_WINDOW_DAYS, or
  - BIG TRADE: a single trade's dollar value crosses the buy/sell threshold.

Shares state.json with the congressional monitor (separate "insiders" key),
so the existing GitHub Actions commit step persists everything.

Run: python insiders.py          (called after monitor.py in the workflow)
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from xml.etree import ElementTree as ET

import requests

import roster

# ---------------------------------------------------------------- config ---

DEFAULTS = {
    "enabled": True,
    "alert_buys": True,
    "alert_sells": True,          # you chose both; flip to False to quiet sells
    "big_buy_usd": 100_000,       # single-buy ping threshold
    "big_sell_usd": 1_000_000,    # single-sell ping threshold (sells are noisy;
                                  # set to 100000 to match buys if you want)
    "cluster_count": 2,           # insiders needed for a cluster ping
    "cluster_window_days": 14,
    "max_filings_per_run": 80,    # newest Form 4s examined each run
    "silent_seed": True,          # first ever run: mark current feed as seen,
                                  # don't flood the channel with backlog
}

STATE_FILE = "state.json"
CONFIG_FILE = "config.json"          # legacy fallback
CONFIG_YAML = "config.yaml"          # primary — managed by the control panel

try:
    import yaml as _yaml
except ImportError:
    _yaml = None

WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "")
USER_ID = os.environ.get("DISCORD_USER_ID", "")

# SEC requires a descriptive User-Agent with contact info, or it blocks you.
# CHANGE THIS to your real name/email before running.
SEC_UA = os.environ.get("SEC_USER_AGENT", "stock-bot personal project contact@example.com")
HEADERS = {"User-Agent": SEC_UA, "Accept-Encoding": "gzip, deflate"}

FEED_URL = (
    "https://www.sec.gov/cgi-bin/browse-edgar"
    "?action=getcurrent&type=4&company=&dateb=&owner=include&count={n}&output=atom"
)

GREEN, RED, TEAL = 0x2ECC71, 0xE74C3C, 0x1ABC9C


def load_config():
    cfg = dict(DEFAULTS)
    loaded = False
    if _yaml is not None:
        try:
            with open(CONFIG_YAML) as f:
                user = _yaml.safe_load(f) or {}
            ins = user.get("insiders", {}) or {}
            cfg.update({k: v for k, v in ins.items() if k in cfg})
            groups = user.get("groups", {}) or {}
            if "insiders" in groups:
                cfg["enabled"] = bool(groups["insiders"])
            loaded = True
        except FileNotFoundError:
            pass
        except Exception as e:
            print(f"insiders: config.yaml unreadable ({e})")
    if not loaded:
        try:
            with open(CONFIG_FILE) as f:
                user_cfg = json.load(f).get("insiders", {})
            cfg.update({k: v for k, v in user_cfg.items() if k in cfg})
        except (FileNotFoundError, json.JSONDecodeError):
            pass
    return cfg


def load_state():
    try:
        with open(STATE_FILE) as f:
            state = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        state = {}
    ins = state.setdefault("insiders", {})
    ins.setdefault("seen_accessions", [])
    ins.setdefault("recent_trades", [])   # rolling ledger for cluster detection
    ins.setdefault("tape", [])            # durable position record (see below)
    ins.setdefault("alerted_clusters", [])
    ins.setdefault("seeded", False)
    return state


TAPE_RETENTION_DAYS = 400   # matches monitor.py; prices.py needs full history
TAPE_MAX = 8000


def append_tape(ins, new_trades):
    """Durable insider position record.

    Separate from recent_trades on purpose. recent_trades is pruned to the
    cluster window (14 days) because that is all cluster detection needs —
    but prices.py nets a position from its ENTIRE buy/sell history, so a
    14-day tape means every insider name silently falls out of the
    performance index a fortnight after it was bought. This keeps the long
    record; recent_trades stays short and does its own job.
    """
    tape = ins.setdefault("tape", [])

    def key(t):
        return (f"{t.get('owner','')}|{t.get('ticker','')}|"
                f"{t.get('side','')}|{t.get('date','')}|{t.get('shares','')}")

    have = {key(t) for t in tape}
    added = 0
    for t in new_trades:
        if not t.get("ticker") or not t.get("date"):
            continue
        if key(t) in have:
            continue
        have.add(key(t))
        tape.append({k: t.get(k) for k in
                     ("ticker", "issuer", "owner", "role", "side",
                      "shares", "price", "value", "date")})
        added += 1

    cutoff = (datetime.now(timezone.utc)
              - timedelta(days=TAPE_RETENTION_DAYS)).date().isoformat()
    tape = [t for t in tape if (t.get("date") or "") >= cutoff]
    tape.sort(key=lambda t: t.get("date") or "")
    ins["tape"] = tape[-TAPE_MAX:]
    return added


def save_state(state):
    # trim: keep last 3000 accession numbers, prune ledger past cluster window
    ins = state["insiders"]
    ins["seen_accessions"] = ins["seen_accessions"][-3000:]
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


# ----------------------------------------------------------------- fetch ---

def sec_get(url, retries=3):
    for attempt in range(retries):
        r = requests.get(url, headers=HEADERS, timeout=20)
        if r.status_code == 200:
            return r
        if r.status_code in (403, 429):
            time.sleep(2 * (attempt + 1))   # be polite; SEC rate-limits hard
            continue
        r.raise_for_status()
    raise RuntimeError(f"SEC blocked or failed after {retries} tries: {url}")


def latest_form4_accessions(limit):
    """Return [(accession_no, filing_index_url), ...] newest first."""
    r = sec_get(FEED_URL.format(n=limit))
    ns = {"a": "http://www.w3.org/2005/Atom"}
    root = ET.fromstring(r.content)
    out = []
    for entry in root.findall("a:entry", ns):
        link = entry.find("a:link", ns)
        href = link.get("href") if link is not None else ""
        m = re.search(r"(\d{10}-\d{2}-\d{6})", href)
        if m:
            out.append((m.group(1), href))
    return out


def fetch_form4_xml(index_url):
    """From a filing index page, find and fetch the ownership XML document."""
    r = sec_get(index_url)
    # the primary doc is an .xml file linked on the index page
    candidates = re.findall(r'href="(/Archives/[^"]+\.xml)"', r.text)
    doc = next((c for c in candidates if "primary_doc" in c.lower()),
               candidates[0] if candidates else None)
    if not doc:
        return None
    return sec_get("https://www.sec.gov" + doc).content


def parse_form4(xml_bytes):
    """Extract open-market transactions (codes P and S) from a Form 4 XML."""
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return []

    def text(node, path):
        el = node.find(path)
        return el.text.strip() if el is not None and el.text else ""

    ticker = text(root, ".//issuerTradingSymbol")
    issuer = text(root, ".//issuerName")
    owner = text(root, ".//rptOwnerName")
    is_officer = text(root, ".//isOfficer") in ("1", "true")
    is_director = text(root, ".//isDirector") in ("1", "true")
    title = text(root, ".//officerTitle")
    role = title or ("Director" if is_director else "Officer" if is_officer else "10% owner")

    trades = []
    for txn in root.findall(".//nonDerivativeTransaction"):
        code = text(txn, ".//transactionCode")
        if code not in ("P", "S"):     # only open-market buys/sells; skips
            continue                   # grants (A), exercises (M), tax (F), gifts (G)
        try:
            shares = float(text(txn, ".//transactionShares/value") or 0)
            price = float(text(txn, ".//transactionPricePerShare/value") or 0)
        except ValueError:
            continue
        trades.append({
            "ticker": ticker, "issuer": issuer, "owner": owner, "role": role,
            "side": "BUY" if code == "P" else "SELL",
            "shares": shares, "price": price, "value": round(shares * price, 2),
            "date": text(txn, ".//transactionDate/value") or datetime.now(timezone.utc).date().isoformat(),
        })
    return trades


# --------------------------------------------------------------- signals ---

def find_clusters(ledger, cfg):
    """Group ledger by (ticker, side); return keys meeting the cluster bar."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=cfg["cluster_window_days"])).date().isoformat()
    groups = {}
    for t in ledger:
        if t["date"] >= cutoff and t["ticker"]:
            groups.setdefault((t["ticker"], t["side"]), set()).add(t["owner"])
    return {k: v for k, v in groups.items() if len(v) >= cfg["cluster_count"]}


def fmt_usd(v):
    return f"${v:,.0f}"


def post_discord(embeds, content=""):
    if not WEBHOOK_URL:
        print("No WEBHOOK_URL set; would have posted:", content, embeds)
        return
    requests.post(WEBHOOK_URL, json={"content": content, "embeds": embeds}, timeout=15)


def big_trade_embed(t):
    buy = t["side"] == "BUY"
    return {
        "title": f"{'🟢' if buy else '🔴'} Insider {'Buy' if buy else 'Sell'} — {t['ticker']}",
        "color": GREEN if buy else RED,
        "description": (
            f"**{t['owner']}** ({t['role']}) at {t['issuer']}\n"
            f"{t['shares']:,.0f} shares @ ${t['price']:,.2f} = **{fmt_usd(t['value'])}**\n"
            f"Trade date: {t['date']} · via SEC Form 4"
        ),
    }


def cluster_embed(ticker, side, owners, ledger, cfg):
    trades = [t for t in ledger if t["ticker"] == ticker and t["side"] == side]
    total = sum(t["value"] for t in trades)
    names = "\n".join(f"• {t['owner']} ({t['role']}) — {fmt_usd(t['value'])} on {t['date']}"
                      for t in trades[-6:])
    buy = side == "BUY"
    return {
        "title": f"{'🧲' } Insider Cluster {'Buy' if buy else 'Sell'} — {ticker}",
        "color": TEAL,
        "description": (
            f"**{len(owners)} insiders** {'bought' if buy else 'sold'} within "
            f"{cfg['cluster_window_days']} days · combined **{fmt_usd(total)}**\n{names}"
        ),
    }


# ------------------------------------------------------------------ main ---

def run():
    cfg = load_config()
    if not cfg["enabled"]:
        print("insiders: disabled in config")
        return
    rost = roster.load_roster()
    if not roster.group_on(rost, "insiders"):
        print("insiders: insiders group off in roster; skipping.")
        return
    if not rost["insiders_at"]:
        print("insiders: no tickers on roster (insiders_at empty); skipping.")
        return
    state = load_state()
    ins = state["insiders"]
    seen = set(ins["seen_accessions"])

    filings = latest_form4_accessions(cfg["max_filings_per_run"])
    new = [(acc, url) for acc, url in filings if acc not in seen]

    # first run: seed silently so you don't get 80 backlog pings
    if cfg["silent_seed"] and not ins["seeded"]:
        ins["seen_accessions"].extend(acc for acc, _ in new)
        ins["seeded"] = True
        save_state(state)
        print(f"insiders: seeded {len(new)} filings silently")
        return

    new_trades, embeds = [], []
    for acc, url in reversed(new):        # oldest first
        ins["seen_accessions"].append(acc)
        try:
            xml = fetch_form4_xml(url)
            trades = parse_form4(xml) if xml else []
        except Exception as e:
            print(f"insiders: skip {acc}: {e}")
            continue
        time.sleep(0.15)                  # stay under SEC's 10 req/sec limit
        for t in trades:
            # ── ROSTER GATE: only Form 4s at your curated tickers survive ──
            if not roster.tracks_insider_ticker(rost, t["ticker"]):
                continue
            if t["side"] == "BUY" and not cfg["alert_buys"]:
                continue
            if t["side"] == "SELL" and not cfg["alert_sells"]:
                continue
            new_trades.append(t)
            roster.append_history(
                "insiders", t["ticker"].upper(), t["ticker"].upper(),
                {"action": t["side"], "issuer": t.get("issuer") or "",
                 "ticker": t["ticker"].upper(), "value": t.get("value") or 0,
                 "period": "", "date": t.get("date") or "",
                 "owner": t.get("owner") or ""})
            threshold = cfg["big_buy_usd"] if t["side"] == "BUY" else cfg["big_sell_usd"]
            if t["value"] >= threshold:
                embeds.append(big_trade_embed(t))

    # cluster detection over rolling ledger
    taped = append_tape(ins, new_trades)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=cfg["cluster_window_days"] + 1)).date().isoformat()
    ins["recent_trades"] = [t for t in ins["recent_trades"] if t["date"] >= cutoff] + new_trades
    for (ticker, side), owners in find_clusters(ins["recent_trades"], cfg).items():
        key = f"{ticker}:{side}:{sorted(owners)}"
        if key not in ins["alerted_clusters"]:
            ins["alerted_clusters"].append(key)
            embeds.append(cluster_embed(ticker, side, owners, ins["recent_trades"], cfg))
    ins["alerted_clusters"] = ins["alerted_clusters"][-500:]

    for i in range(0, len(embeds), 10):   # Discord caps 10 embeds/message
        mention = f"<@{USER_ID}>" if USER_ID and i == 0 else ""
        post_discord(embeds[i:i + 10], content=mention)

    save_state(state)
    print(f"insiders: {len(new)} new filings, {len(new_trades)} P/S trades, "
          f"{taped} added to tape ({len(ins['tape'])} total), {len(embeds)} alerts")


if __name__ == "__main__":
    try:
        run()
    except Exception as e:
        post_discord([{"title": "⚠️ Insider monitor error", "color": RED,
                       "description": str(e)[:1000]}])
        sys.exit(1)
