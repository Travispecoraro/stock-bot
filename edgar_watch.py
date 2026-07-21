"""
edgar_watch.py — Activist (SC 13D/13G) + Fund (13F-HR) monitor for stock-bot.

  ACTIVISTS: any investor crossing 5% files SC 13D (activist) or SC 13G
    (passive). Alerts on new filings + amendments.
  FUNDS: managers over the AUM floor file quarterly 13F holdings; the bot
    diffs large positions against the prior filing and alerts NEW stakes /
    EXITs above the position floor.

All Discord output goes through notify.py so activist/fund alerts match the
rest of the bot. Runs after monitor.py / insiders.py.

State: state.json ("activists","funds","heartbeat")  +  state_13f.json (diff base)
Env:   DISCORD_WEBHOOK_URL, SEC_USER_AGENT
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from xml.etree import ElementTree as ET

import requests

import notify

try:
    import yaml
    HAVE_YAML = True
except ImportError:
    HAVE_YAML = False

DEFAULTS = {
    "groups": {"activists": True, "funds": True},
    "activists": {"include_13g": False, "alert_amendments": True, "max_filings_per_run": 40},
    "funds": {"min_aum_busd": 1, "min_position_musd": 50, "alert_new": True,
              "alert_exits": True, "max_filings_per_run": 30, "max_alert_lines": 6},
}

STATE_FILE = "state.json"
F13_FILE = "state_13f.json"
CONFIG_FILE = "config.yaml"

WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")   # fixed: was WEBHOOK_URL
SEC_UA = os.environ.get("SEC_USER_AGENT", "stock-bot personal project contact@example.com")
HEADERS = {"User-Agent": SEC_UA, "Accept-Encoding": "gzip, deflate"}

FEED = ("https://www.sec.gov/cgi-bin/browse-edgar"
        "?action=getcurrent&type={t}&company=&dateb=&owner=include&count={n}&output=atom")
TICKER_MAP_URL = "https://www.sec.gov/files/company_tickers.json"


def deep_merge(base, over):
    out = dict(base)
    for k, v in (over or {}).items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = deep_merge(out[k], v)
        elif k in out:
            out[k] = v
    return out


def load_config():
    cfg = {k: dict(v) for k, v in DEFAULTS.items()}
    discord = {}
    if HAVE_YAML:
        try:
            with open(CONFIG_FILE) as f:
                user = yaml.safe_load(f) or {}
            for section in cfg:
                cfg = deep_merge(cfg, {section: user.get(section, {})})
            discord = user.get("discord", {}) or {}
        except FileNotFoundError:
            pass
        except Exception as e:
            print(f"edgar_watch: config.yaml unreadable ({e}); using defaults")
    return cfg, discord


def load_json(path, fallback):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return fallback


def bump_pinged(state, n):
    hb = state.setdefault("heartbeat", {})
    hb["pinged_today"] = hb.get("pinged_today", 0) + n


def sec_get(url, retries=3):
    for attempt in range(retries):
        r = requests.get(url, headers=HEADERS, timeout=25)
        if r.status_code == 200:
            return r
        if r.status_code in (403, 429):
            time.sleep(2 * (attempt + 1))
            continue
        r.raise_for_status()
    raise RuntimeError(f"SEC blocked/failed: {url}")


def feed_entries(form_type, limit):
    r = sec_get(FEED.format(t=form_type.replace(" ", "+"), n=limit))
    ns = {"a": "http://www.w3.org/2005/Atom"}
    out, seen = [], set()
    for entry in ET.fromstring(r.content).findall("a:entry", ns):
        link = entry.find("a:link", ns)
        title = (entry.findtext("a:title", "", ns) or "")
        href = link.get("href") if link is not None else ""
        m = re.search(r"(\d{10}-\d{2}-\d{6})", href)
        if m and m.group(1) not in seen:
            seen.add(m.group(1))
            out.append((m.group(1), href, title))
    return out


def local(tag):
    return tag.rsplit("}", 1)[-1]


def findtext_any(root, name):
    for el in root.iter():
        if local(el.tag) == name and el.text:
            return el.text.strip()
    return ""


def fmt_usd(v):
    v = float(v or 0)
    if v >= 1e9:
        return f"${v/1e9:,.1f}B"
    if v >= 1e6:
        return f"${v/1e6:,.0f}M"
    return f"${v:,.0f}"


# ── activists ──────────────────────────────────────────────────────────
def parse_index_parties(html):
    subject, subj_cik, filer = "", "", ""
    for blk in re.split(r'class="companyName"', html)[1:]:
        cik_m = re.search(r"CIK=(\d{4,10})", blk)
        clean = re.sub(r"<[^>]+>", " ", blk[:500])
        name_m = re.search(r"^\W*([^()]{2,120}?)\s*\(", clean)
        name = name_m.group(1).strip() if name_m else ""
        role = clean.lower()
        if "subject" in role and not subject:
            subject, subj_cik = name, (cik_m.group(1) if cik_m else "")
        elif ("filed by" in role or "filer" in role) and not filer:
            filer = name
    return subject, subj_cik, filer


def parse_stake_pct(text):
    m = re.search(r"percent\s+of\s+class.{0,300}?(\d{1,3}(?:\.\d+)?)\s*%", text, re.I | re.S)
    return m.group(1) if m else ""


def load_ticker_map():
    try:
        data = sec_get(TICKER_MAP_URL).json()
        return {str(v["cik_str"]): v["ticker"] for v in data.values()}
    except Exception as e:
        print(f"edgar_watch: ticker map unavailable ({e})")
        return {}


def run_activists(cfg, state, ticker_map):
    ac = cfg["activists"]
    st = state.setdefault("activists", {})
    st.setdefault("seen", [])
    st.setdefault("recent_events", [])
    seen = set(st["seen"])
    forms = ["SC 13D"] + (["SC 13G"] if ac["include_13g"] else [])

    embeds, new_count = [], 0
    for form in forms:
        for acc, url, title in reversed(feed_entries(form, ac["max_filings_per_run"])):
            if acc in seen:
                continue
            st["seen"].append(acc)
            is_amend = "/A" in title or "%2FA" in url or "13D/A" in title or "13G/A" in title
            if is_amend and not ac["alert_amendments"]:
                continue
            try:
                idx = sec_get(url).text
                subject, subj_cik, filer = parse_index_parties(idx)
                doc_m = re.search(r'href="(/Archives/[^"]+\.(?:htm|txt))"', idx)
                pct = ""
                if doc_m:
                    pct = parse_stake_pct(sec_get("https://www.sec.gov" + doc_m.group(1)).text[:400000])
                time.sleep(0.15)
            except Exception as e:
                print(f"edgar_watch: activist skip {acc}: {e}")
                continue
            ticker = ticker_map.get(subj_cik.lstrip("0") if subj_cik else "", "")
            ftype = form.replace("SC ", "") + ("/A" if is_amend else "")
            event = {"filer": filer or "Unknown filer", "company": subject or "Unknown company",
                     "ticker": ticker, "pct": pct, "type": ftype,
                     "date": datetime.now(timezone.utc).date().isoformat(), "link": url}
            st["recent_events"].append(event)
            new_count += 1
            embeds.append(notify.activist_embed({
                "filer": event["filer"], "company": event["company"],
                "ticker": ticker, "pct": pct, "type": ftype, "url": url,
            }))
    st["recent_events"] = st["recent_events"][-150:]
    st["seen"] = st["seen"][-2000:]
    return embeds, new_count


# ── funds ──────────────────────────────────────────────────────────────
def parse_13f_primary(xml_bytes):
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return "", "", 0.0
    name = findtext_any(root, "name") or findtext_any(root, "filingManager")
    period = findtext_any(root, "periodOfReport")
    try:
        total = float(findtext_any(root, "tableValueTotal") or 0)
    except ValueError:
        total = 0.0
    return name, period, total


def parse_13f_infotable(xml_bytes, min_value):
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return {}
    out = {}
    for tbl in root.iter():
        if local(tbl.tag) != "infoTable":
            continue
        issuer = cusip = ""
        value = 0.0
        for el in tbl.iter():
            t = local(el.tag)
            if t == "nameOfIssuer" and el.text:
                issuer = el.text.strip()
            elif t == "cusip" and el.text:
                cusip = el.text.strip()
            elif t == "value" and el.text:
                try:
                    value = float(el.text.strip())
                except ValueError:
                    pass
        if cusip and value >= min_value:
            prev = out.get(cusip, {}).get("value", 0)
            out[cusip] = {"issuer": issuer, "value": round(value + prev, 2)}
    return out


def run_funds(cfg, state, f13):
    fd = cfg["funds"]
    st = state.setdefault("funds", {})
    st.setdefault("seen", [])
    st.setdefault("recent_moves", [])
    seen = set(st["seen"])
    min_aum = fd["min_aum_busd"] * 1e9
    min_pos = fd["min_position_musd"] * 1e6

    embeds, new_count = [], 0
    for acc, url, title in reversed(feed_entries("13F-HR", fd["max_filings_per_run"])):
        if acc in seen:
            continue
        st["seen"].append(acc)
        try:
            idx = sec_get(url).text
            xml_hrefs = re.findall(r'href="(/Archives/[^"]+\.xml)"', idx)
            primary = next((h for h in xml_hrefs if "primary" in h.lower()), None)
            info = next((h for h in xml_hrefs if "info" in h.lower() or "table" in h.lower()), None)
            if primary is None and xml_hrefs:
                primary = xml_hrefs[0]
            if primary is None:
                continue
            name, period, total = parse_13f_primary(sec_get("https://www.sec.gov" + primary).content)
            if total < min_aum:
                continue
            if info is None:
                info = next((h for h in xml_hrefs if h != primary), None)
            holdings = parse_13f_infotable(sec_get("https://www.sec.gov" + info).content, min_pos) if info else {}
            time.sleep(0.2)
        except Exception as e:
            print(f"edgar_watch: fund skip {acc}: {e}")
            continue

        cik_m = re.search(r"/data/(\d+)/", url)
        cik = cik_m.group(1) if cik_m else acc
        prev = f13.get(cik, {}).get("h", {})
        cur_set, prev_set = set(holdings), set(prev)
        news = sorted((holdings[c] for c in cur_set - prev_set), key=lambda p: -p["value"])
        exits = sorted((prev[c] for c in prev_set - cur_set), key=lambda p: -p["value"])
        f13[cik] = {"name": name, "q": period, "h": dict(holdings)}

        first_snapshot = not prev
        lines = []
        if fd["alert_new"] and not first_snapshot:
            for p in news[:fd["max_alert_lines"]]:
                lines.append(f"NEW · {p['issuer']} · ~{fmt_usd(p['value'])}")
        if fd["alert_exits"] and not first_snapshot:
            for p in exits[:fd["max_alert_lines"]]:
                lines.append(f"EXIT · {p['issuer']} · was ~{fmt_usd(p['value'])}")
        for p in (news if not first_snapshot else [])[:3]:
            st["recent_moves"].append({"fund": name, "action": "NEW", "issuer": p["issuer"],
                                       "ticker": "", "value": p["value"], "period": period,
                                       "date": datetime.now(timezone.utc).date().isoformat()})
        for p in (exits if not first_snapshot else [])[:3]:
            st["recent_moves"].append({"fund": name, "action": "EXIT", "issuer": p["issuer"],
                                       "ticker": "", "value": p["value"], "period": period,
                                       "date": datetime.now(timezone.utc).date().isoformat()})
        if lines:
            new_count += 1
            embeds.append(notify.fund_embed({
                "fund": name, "period": period, "aum": total, "lines": lines, "url": url,
            }))
    st["recent_moves"] = st["recent_moves"][-150:]
    st["seen"] = st["seen"][-2000:]
    return embeds, new_count


# ── main ───────────────────────────────────────────────────────────────
def run():
    cfg, discord = load_config()
    state = load_json(STATE_FILE, {})
    f13 = load_json(F13_FILE, {})
    embeds = []

    ticker_map = load_ticker_map() if cfg["groups"]["activists"] else {}
    if cfg["groups"]["activists"]:
        e, n = run_activists(cfg, state, ticker_map)
        embeds += e
        print(f"edgar_watch: activists — {n} new events")
    else:
        print("edgar_watch: activists disabled")

    if cfg["groups"]["funds"]:
        e, n = run_funds(cfg, state, f13)
        embeds += e
        print(f"edgar_watch: funds — {n} funds with moves")
    else:
        print("edgar_watch: funds disabled")

    uid = str(discord.get("user_id", "")).strip()
    mention = f"<@{uid}>" if uid.isdigit() and embeds else ""
    if embeds:
        notify.send(WEBHOOK_URL, embeds, content=mention)
        bump_pinged(state, len(embeds))

    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=1)
    with open(F13_FILE, "w") as f:
        json.dump(f13, f, separators=(",", ":"))
    print(f"edgar_watch: done — {len(embeds)} alerts")


if __name__ == "__main__":
    try:
        run()
    except Exception as e:
        notify.send(WEBHOOK_URL, notify.notice_embed("EDGAR watcher error", str(e)[:1000], "error"))
        sys.exit(1)
