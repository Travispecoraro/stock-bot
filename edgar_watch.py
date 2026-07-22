"""
edgar_watch.py — Activist (SC 13D/13G) + Fund (13F-HR) monitor, roster-gated.

Now driven by roster.yaml instead of scanning the whole market:

  FUNDS: for each fund in the roster, the bot fetches THAT fund's latest 13F
    directly by CIK (no more hoping it shows up in the global current-filings
    window), diffs its holdings against the prior filing, and records new
    stakes / exits plus a top-10 snapshot into roster_history.json. 13Fs lag
    up to 45 days after quarter end by law.

  ACTIVISTS: the SC 13D/13G current feed is still scanned (there's no per-CIK
    subscribe for "who just crossed 5% of someone"), but only filings whose
    FILER matches a roster `activists` name survive. Everything else is dropped.

State:
  state.json          -> "activists" / "funds" keys (dedup, recent tape)
  state_13f.json      -> per-fund holdings snapshot (diff baseline)
  roster_history.json -> durable per-member log (written via roster.py)

Run: python edgar_watch.py   (after monitor.py / insiders.py in the workflow)
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from xml.etree import ElementTree as ET

import requests

import roster

# ---------------------------------------------------------------- config ---

DEFAULTS = {
    "activists": {
        "include_13g": False,       # passive 5% stakes; noisier, off by default
        "alert_amendments": True,   # 13D/A stake changes
        "max_filings_per_run": 40,
    },
    "funds": {
        "min_position_musd": 50,    # track/alert positions >= this many $M
        "top_n": 10,                # holdings kept in the top-N snapshot
        "alert_new": True,
        "alert_exits": True,
        "max_alert_lines": 6,       # positions listed per fund embed
        "max_funds_per_run": 12,    # CIKs polled per run (rotates through roster)
    },
}

STATE_FILE = "state.json"
F13_FILE = "state_13f.json"
CONFIG_FILE = "config.yaml"

try:
    import yaml
    HAVE_YAML = True
except ImportError:
    HAVE_YAML = False

# The workflow exports DISCORD_WEBHOOK_URL (same secret monitor.py uses).
# WEBHOOK_URL is kept as a legacy fallback for anyone who set it manually.
WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL") or os.environ.get("WEBHOOK_URL", "")


def discord_user_id():
    """Mention target: DISCORD_USER_ID env if set, else config.yaml discord.user_id."""
    uid = os.environ.get("DISCORD_USER_ID", "").strip()
    if uid.isdigit():
        return uid
    if HAVE_YAML:
        try:
            with open(CONFIG_FILE) as f:
                raw = yaml.safe_load(f) or {}
            uid = str((raw.get("discord") or {}).get("user_id", "")).strip()
            if uid.isdigit():
                return uid
        except Exception:
            pass
    return ""


SEC_UA = os.environ.get("SEC_USER_AGENT", "stock-bot personal project contact@example.com")
HEADERS = {"User-Agent": SEC_UA, "Accept-Encoding": "gzip, deflate"}

FEED = ("https://www.sec.gov/cgi-bin/browse-edgar"
        "?action=getcurrent&type={t}&company=&dateb=&owner=include&count={n}&output=atom")
BY_CIK = ("https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany"
          "&CIK={cik}&type=13F-HR&dateb=&owner=include&count={n}&output=atom")
TICKER_MAP_URL = "https://www.sec.gov/files/company_tickers.json"

PURPLE, BLUE, RED = 0x9B59B6, 0x3498DB, 0xE74C3C


def _today():
    return datetime.now(timezone.utc).date().isoformat()


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
    if HAVE_YAML:
        try:
            with open(CONFIG_FILE) as f:
                user = yaml.safe_load(f) or {}
            for section in cfg:
                cfg = deep_merge(cfg, {section: user.get(section, {})})
        except FileNotFoundError:
            pass
        except Exception as e:
            print(f"edgar_watch: config.yaml unreadable ({e}); using defaults")
    return cfg


def load_json(path, fallback):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return fallback


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
    """[(accession, index_url, title)] newest first from the current feed."""
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


def latest_13f_by_cik(cik, n=3):
    """[(accession, index_url)] newest first for one fund's 13F-HR filings."""
    r = sec_get(BY_CIK.format(cik=cik, n=n))
    ns = {"a": "http://www.w3.org/2005/Atom"}
    out = []
    for entry in ET.fromstring(r.content).findall("a:entry", ns):
        link = entry.find("a:link", ns)
        href = link.get("href") if link is not None else ""
        m = re.search(r"(\d{10}-\d{2}-\d{6})", href)
        if m:
            out.append((m.group(1), href))
    return out


def local(tag):
    return tag.rsplit("}", 1)[-1]


def findtext_any(root, name):
    for el in root.iter():
        if local(el.tag) == name and el.text:
            return el.text.strip()
    return ""


def post_discord(embeds, content=""):
    if not WEBHOOK_URL:
        print("no WEBHOOK_URL; would post:", content, json.dumps(embeds)[:400])
        return
    for i in range(0, len(embeds), 10):
        requests.post(WEBHOOK_URL,
                      json={"content": content if i == 0 else "",
                            "embeds": embeds[i:i + 10]}, timeout=15)


def fmt_usd(v):
    v = float(v or 0)
    if v >= 1e9:
        return f"${v/1e9:,.1f}B"
    if v >= 1e6:
        return f"${v/1e6:,.0f}M"
    return f"${v:,.0f}"


# ------------------------------------------------------------- activists ---

def parse_index_parties(html):
    """From a filing index page, pull (subject_company, subject_cik, filed_by)."""
    subject, subj_cik, filer = "", "", ""
    blocks = re.split(r'class="companyName"', html)
    for blk in blocks[1:]:
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
    m = re.search(r"percent\s+of\s+class.{0,300}?(\d{1,3}(?:\.\d+)?)\s*%",
                  text, re.I | re.S)
    return m.group(1) if m else ""


def load_ticker_map():
    try:
        data = sec_get(TICKER_MAP_URL).json()
        return {str(v["cik_str"]): v["ticker"] for v in data.values()}
    except Exception as e:
        print(f"edgar_watch: ticker map unavailable ({e})")
        return {}


def run_activists(cfg, state, ticker_map, rost):
    ac = cfg["activists"]
    st = state.setdefault("activists", {})
    st.setdefault("seen", [])
    st.setdefault("recent_events", [])
    seen = set(st["seen"])
    forms = ["SC 13D"] + (["SC 13G"] if ac["include_13g"] else [])

    embeds, new_count, skipped = [], 0, 0
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
                # ── ROSTER GATE: only filers on your activists list survive ──
                if not roster.tracks_activist(rost, filer):
                    skipped += 1
                    continue
                doc_m = re.search(r'href="(/Archives/[^"]+\.(?:htm|txt))"', idx)
                pct = ""
                if doc_m:
                    pct = parse_stake_pct(sec_get("https://www.sec.gov" + doc_m.group(1)).text[:400000])
                time.sleep(0.15)
            except Exception as e:
                print(f"edgar_watch: activist skip {acc}: {e}")
                continue
            ticker = ticker_map.get(subj_cik.lstrip("0") if subj_cik else "", "")
            ftype = (form.replace("SC ", "") + ("/A" if is_amend else ""))
            event = {"filer": filer, "company": subject or "Unknown company",
                     "ticker": ticker, "pct": pct, "type": ftype,
                     "action": ftype, "issuer": subject or "",
                     "value": "", "date": _today(), "link": url}
            st["recent_events"].append(event)
            roster.append_history("activists", filer.lower(), filer, event)
            new_count += 1
            head = ticker or event["company"]
            embeds.append({
                "title": f"⚡ Activist Stake — {head}" if not is_amend
                         else f"⚡ Stake Update — {head}",
                "color": PURPLE,
                "description": (f"**{filer}** filed **{ftype}** on "
                                f"{event['company']}{' ('+ticker+')' if ticker else ''}\n"
                                + (f"Reported **{pct}%** of class\n" if pct else "")
                                + f"[Filing ↗]({url})"),
            })
    st["recent_events"] = st["recent_events"][-150:]
    st["seen"] = st["seen"][-2000:]
    print(f"edgar_watch: activists — {new_count} on-roster, {skipped} off-roster dropped")
    return embeds, new_count


# ----------------------------------------------------------------- funds ---

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
    """{cusip: {'issuer','value'}} for positions >= min_value (summed by cusip)."""
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return {}
    tmp = {}
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
        if cusip:
            slot = tmp.setdefault(cusip, {"issuer": issuer, "value": 0.0})
            slot["value"] += value
    return {c: {"issuer": v["issuer"], "value": round(v["value"], 2)}
            for c, v in tmp.items() if v["value"] >= min_value}


def run_funds(cfg, state, f13, rost):
    fd = cfg["funds"]
    st = state.setdefault("funds", {})
    st.setdefault("seen", [])
    st.setdefault("recent_moves", [])
    st.setdefault("cursor", 0)
    seen = set(st["seen"])
    min_pos = fd["min_position_musd"] * 1e6
    top_n = fd["top_n"]

    ciks = sorted(roster.tracked_fund_ciks(rost))
    if not ciks:
        print("edgar_watch: no fund CIKs on roster (nothing to poll)")
        return [], 0

    # rotate through the roster so a big roster doesn't blow the run budget.
    # take a UNIQUE slice starting at the cursor, wrapping once, never repeating.
    cursor = st["cursor"] % len(ciks)
    order = ciks[cursor:] + ciks[:cursor]
    batch = order[:min(fd["max_funds_per_run"], len(ciks))]
    st["cursor"] = (cursor + len(batch)) % len(ciks)

    embeds, new_count = [], 0
    for cik in batch:
        fname = roster.fund_name_for_cik(rost, cik) or f"CIK {cik}"
        try:
            filings = latest_13f_by_cik(cik, n=2)
        except Exception as e:
            print(f"edgar_watch: {fname} feed error: {e}")
            continue
        if not filings:
            continue
        acc, url = filings[0]
        if acc in seen:
            continue                       # already processed this fund's latest
        seen.add(acc)
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
            name = name or fname
            if info is None:
                info = next((h for h in xml_hrefs if h != primary), None)
            holdings = parse_13f_infotable(sec_get("https://www.sec.gov" + info).content, min_pos) if info else {}
            time.sleep(0.25)
        except Exception as e:
            print(f"edgar_watch: {fname} filing {acc} skip: {e}")
            continue

        prev = f13.get(cik, {}).get("h", {})
        cur_set, prev_set = set(holdings), set(prev)
        news = sorted((holdings[c] for c in cur_set - prev_set), key=lambda p: -p["value"])
        exits = sorted((prev[c] for c in prev_set - cur_set), key=lambda p: -p["value"])
        first_snapshot = not prev

        top = sorted(holdings.values(), key=lambda p: -p["value"])[:top_n]
        top_snapshot = [{"issuer": p["issuer"], "ticker": "", "value": p["value"]} for p in top]

        f13[cik] = {"name": name, "q": period, "h": holdings}

        roster.append_history("funds", cik, name,
                              {"action": "SNAPSHOT", "issuer": "", "ticker": "",
                               "value": total, "period": period, "date": _today()},
                              latest_top=top_snapshot, latest_period=period)
        if not first_snapshot:
            for p in news:
                mv = {"action": "NEW", "issuer": p["issuer"], "ticker": "",
                      "value": p["value"], "period": period, "date": _today()}
                roster.append_history("funds", cik, name, mv)
                st["recent_moves"].append({"fund": name, **mv})
            for p in exits:
                mv = {"action": "EXIT", "issuer": p["issuer"], "ticker": "",
                      "value": p["value"], "period": period, "date": _today()}
                roster.append_history("funds", cik, name, mv)
                st["recent_moves"].append({"fund": name, **mv})

        lines = []
        if not first_snapshot and fd["alert_new"]:
            lines += [f"🟦 NEW · {p['issuer']} · ~{fmt_usd(p['value'])}" for p in news[:fd["max_alert_lines"]]]
        if not first_snapshot and fd["alert_exits"]:
            lines += [f"⬛ EXIT · {p['issuer']} · was ~{fmt_usd(p['value'])}" for p in exits[:fd["max_alert_lines"]]]

        if first_snapshot:
            print(f"edgar_watch: {name} — seeded {len(holdings)} positions (silent)")
        elif lines:
            new_count += 1
            embeds.append({
                "title": f"🐋 Fund Moves — {name}",
                "color": BLUE,
                "description": (f"13F for quarter ending {period or '—'} · "
                                f"reported {fmt_usd(total)} · positions ≥ {fmt_usd(min_pos)}\n"
                                + "\n".join(lines) + f"\n[Filing ↗]({url})"),
            })
        else:
            print(f"edgar_watch: {name} — no threshold moves this filing")

    st["recent_moves"] = st["recent_moves"][-150:]
    st["seen"] = st["seen"][-2000:]
    return embeds, new_count


# ------------------------------------------------------------------ main ---

def run():
    cfg = load_config()
    rost = roster.load_roster()
    rost = roster.ensure_resolved(rost)        # fills blank fund CIKs (networked)

    state = load_json(STATE_FILE, {})
    f13 = load_json(F13_FILE, {})
    embeds = []

    if roster.group_on(rost, "activists"):
        ticker_map = load_ticker_map()
        e, n = run_activists(cfg, state, ticker_map, rost)
        embeds += e
    else:
        print("edgar_watch: activists group off")

    if roster.group_on(rost, "funds"):
        e, n = run_funds(cfg, state, f13, rost)
        embeds += e
        print(f"edgar_watch: funds — {n} funds with moves")
    else:
        print("edgar_watch: funds group off")

    uid = discord_user_id()
    mention = f"<@{uid}>" if uid and embeds else ""
    if embeds:
        post_discord(embeds, content=mention)

    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)
    with open(F13_FILE, "w") as f:
        json.dump(f13, f, separators=(",", ":"))
    print(f"edgar_watch: done — {len(embeds)} alerts")


if __name__ == "__main__":
    try:
        run()
    except Exception as e:
        post_discord([{"title": "⚠️ EDGAR watcher error", "color": RED,
                       "description": str(e)[:1000]}])
        sys.exit(1)
