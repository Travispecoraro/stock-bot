"""
insiders.py — Corporate insider (SEC Form 4) monitor for stock-bot.

Watches SEC EDGAR Form 4s, parses open-market buys (P) and sells (S), and
pings Discord — via notify.py — when either:
  - CLUSTER: 2+ distinct insiders trade the same direction/stock in-window, or
  - BIG TRADE: a single trade crosses the buy/sell dollar threshold.

Shares state.json with the other monitors (its own "insiders" key). The
rolling ledger (insiders.recent_trades) also feeds the daily heartbeat.

Run: python insiders.py   (after monitor.py in the workflow)
Env: DISCORD_WEBHOOK_URL, SEC_USER_AGENT
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from xml.etree import ElementTree as ET

import requests

import notify

DEFAULTS = {
    "enabled": True,
    "alert_buys": True,
    "alert_sells": True,
    "big_buy_usd": 100_000,
    "big_sell_usd": 1_000_000,
    "cluster_count": 2,
    "cluster_window_days": 14,
    "max_filings_per_run": 80,
    "silent_seed": True,
}

STATE_FILE = "state.json"
CONFIG_YAML = "config.yaml"

try:
    import yaml as _yaml
except ImportError:
    _yaml = None

WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")   # fixed: was WEBHOOK_URL
SEC_UA = os.environ.get("SEC_USER_AGENT", "stock-bot personal project contact@example.com")
HEADERS = {"User-Agent": SEC_UA, "Accept-Encoding": "gzip, deflate"}

FEED_URL = ("https://www.sec.gov/cgi-bin/browse-edgar"
            "?action=getcurrent&type=4&company=&dateb=&owner=include&count={n}&output=atom")


def load_config():
    """Returns (insider_cfg, discord_cfg). user_id now comes from config.yaml,
    not an env var — matches monitor.py."""
    cfg = dict(DEFAULTS)
    discord = {}
    if _yaml is not None:
        try:
            with open(CONFIG_YAML) as f:
                user = _yaml.safe_load(f) or {}
            ins = user.get("insiders", {}) or {}
            cfg.update({k: v for k, v in ins.items() if k in cfg})
            groups = user.get("groups", {}) or {}
            if "insiders" in groups:
                cfg["enabled"] = bool(groups["insiders"])
            discord = user.get("discord", {}) or {}
        except FileNotFoundError:
            pass
        except Exception as e:
            print(f"insiders: config.yaml unreadable ({e})")
    return cfg, discord


def mention(discord):
    uid = str(discord.get("user_id", "")).strip()
    return f"<@{uid}>" if uid.isdigit() and discord.get("mention_on_trade", True) else ""


def load_state():
    try:
        with open(STATE_FILE) as f:
            state = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        state = {}
    ins = state.setdefault("insiders", {})
    ins.setdefault("seen_accessions", [])
    ins.setdefault("recent_trades", [])
    ins.setdefault("alerted_clusters", [])
    ins.setdefault("seeded", False)
    state.setdefault("heartbeat", {"last_sent_date": None,
                                   "pinged_today": 0, "errors_today": 0})
    return state


def save_state(state):
    ins = state["insiders"]
    ins["seen_accessions"] = ins["seen_accessions"][-3000:]
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=1)


def bump_pinged(state, n):
    state["heartbeat"]["pinged_today"] = state["heartbeat"].get("pinged_today", 0) + n


# ── fetch ──────────────────────────────────────────────────────────────
def sec_get(url, retries=3):
    for attempt in range(retries):
        r = requests.get(url, headers=HEADERS, timeout=20)
        if r.status_code == 200:
            return r
        if r.status_code in (403, 429):
            time.sleep(2 * (attempt + 1))
            continue
        r.raise_for_status()
    raise RuntimeError(f"SEC blocked or failed after {retries} tries: {url}")


def latest_form4_accessions(limit):
    r = sec_get(FEED_URL.format(n=limit))
    ns = {"a": "http://www.w3.org/2005/Atom"}
    out = []
    for entry in ET.fromstring(r.content).findall("a:entry", ns):
        link = entry.find("a:link", ns)
        href = link.get("href") if link is not None else ""
        m = re.search(r"(\d{10}-\d{2}-\d{6})", href)
        if m:
            out.append((m.group(1), href))
    return out


def fetch_form4_xml(index_url):
    r = sec_get(index_url)
    candidates = re.findall(r'href="(/Archives/[^"]+\.xml)"', r.text)
    doc = next((c for c in candidates if "primary_doc" in c.lower()),
               candidates[0] if candidates else None)
    return sec_get("https://www.sec.gov" + doc).content if doc else None


def parse_form4(xml_bytes):
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
    today = datetime.now(timezone.utc).date().isoformat()
    for txn in root.findall(".//nonDerivativeTransaction"):
        code = text(txn, ".//transactionCode")
        if code not in ("P", "S"):
            continue
        try:
            shares = float(text(txn, ".//transactionShares/value") or 0)
            price = float(text(txn, ".//transactionPricePerShare/value") or 0)
        except ValueError:
            continue
        trades.append({
            "ticker": ticker, "issuer": issuer, "owner": owner, "role": role,
            "side": "BUY" if code == "P" else "SELL",
            "shares": shares, "price": price, "value": round(shares * price, 2),
            "date": text(txn, ".//transactionDate/value") or today,
            "logged": today,
        })
    return trades


# ── signals ────────────────────────────────────────────────────────────
def find_clusters(ledger, cfg):
    cutoff = (datetime.now(timezone.utc) - timedelta(days=cfg["cluster_window_days"])).date().isoformat()
    groups = {}
    for t in ledger:
        when = t.get("date") or t.get("logged") or ""
        if when >= cutoff and t.get("ticker"):
            groups.setdefault((t["ticker"], t["side"]), set()).add(t["owner"])
    return {k: v for k, v in groups.items() if len(v) >= cfg["cluster_count"]}


def big_trade_embed(t):
    return notify.trade_embed({
        "group": "insiders", "ticker": t["ticker"], "company": t["issuer"],
        "name": t["owner"], "subtitle": t["role"], "direction": t["side"],
        "value": t["value"], "date": t["date"], "big": True,
    })


def cluster_to_embed(ticker, side, owners, ledger, cfg):
    trades = [t for t in ledger if t["ticker"] == ticker and t["side"] == side]
    total = sum(t["value"] for t in trades)
    return notify.cluster_embed({
        "ticker": ticker, "group": "insiders", "direction": side,
        "buyers": sorted(owners), "window_days": cfg["cluster_window_days"],
        "combined_value": total or None,
    })


# ── main ───────────────────────────────────────────────────────────────
def run():
    cfg, discord = load_config()
    if not cfg["enabled"]:
        print("insiders: disabled in config")
        return
    state = load_state()
    ins = state["insiders"]
    seen = set(ins["seen_accessions"])

    filings = latest_form4_accessions(cfg["max_filings_per_run"])
    new = [(acc, url) for acc, url in filings if acc not in seen]

    if cfg["silent_seed"] and not ins["seeded"]:
        ins["seen_accessions"].extend(acc for acc, _ in new)
        ins["seeded"] = True
        save_state(state)
        print(f"insiders: seeded {len(new)} filings silently")
        return

    new_trades, embeds = [], []
    for acc, url in reversed(new):
        ins["seen_accessions"].append(acc)
        try:
            xml = fetch_form4_xml(url)
            trades = parse_form4(xml) if xml else []
        except Exception as e:
            print(f"insiders: skip {acc}: {e}")
            continue
        time.sleep(0.15)
        for t in trades:
            if t["side"] == "BUY" and not cfg["alert_buys"]:
                continue
            if t["side"] == "SELL" and not cfg["alert_sells"]:
                continue
            new_trades.append(t)
            threshold = cfg["big_buy_usd"] if t["side"] == "BUY" else cfg["big_sell_usd"]
            if t["value"] >= threshold:
                embeds.append(big_trade_embed(t))

    cutoff = (datetime.now(timezone.utc) - timedelta(days=cfg["cluster_window_days"] + 1)).date().isoformat()
    ins["recent_trades"] = [t for t in ins["recent_trades"]
                            if (t.get("date") or t.get("logged") or "") >= cutoff] + new_trades

    for (ticker, side), owners in find_clusters(ins["recent_trades"], cfg).items():
        key = f"{ticker}:{side}:{sorted(owners)}"
        if key not in ins["alerted_clusters"]:
            ins["alerted_clusters"].append(key)
            embeds.append(cluster_to_embed(ticker, side, owners, ins["recent_trades"], cfg))
    ins["alerted_clusters"] = ins["alerted_clusters"][-500:]

    if embeds:
        notify.send(WEBHOOK_URL, embeds, content=mention(discord))
        bump_pinged(state, len(embeds))

    save_state(state)
    print(f"insiders: {len(new)} new filings, {len(new_trades)} P/S trades, {len(embeds)} alerts")


if __name__ == "__main__":
    try:
        run()
    except Exception as e:
        notify.send(WEBHOOK_URL, notify.notice_embed("Insider monitor error", str(e)[:1000], "error"))
        sys.exit(1)
