"""
roster.py — the curated watchlist that gates every collector.

Single source of truth: roster.yaml. This module loads it, resolves any
fund names that are missing a CIK against SEC EDGAR (caching the result so
it's a once-ever lookup per fund), exposes match helpers the collectors use
to decide "do we care about this filing?", and appends to an append-only
history keyed per roster member.

Files it owns:
  roster.yaml            (you edit — the watchlist + group toggles)
  roster_resolved.json   (bot writes — name -> {cik, matched_name} cache)
  roster_history.json    (bot writes — durable, never-trimmed per-member log)

Nothing here hits the network at import time. Collectors call
`ensure_resolved()` once near startup; that's the only networked step, and it
no-ops when every fund already has a CIK (seeded or previously cached).

Design notes:
  * Funds match by CIK — exact, no name-fuzzing, so you never track the wrong
    "Capital Management LLC" by accident. That's the accuracy guarantee.
  * Activists / politicians match by name substring (their indexes don't give
    a clean filer CIK), case-insensitive.
  * Insiders match by ticker (you curate which companies' Form 4s survive).
  * Resolution is conservative: on 0 or ambiguous matches it SKIPS and warns
    rather than guessing, and it prints exactly what it matched so you can
    verify against the run log.
"""

import json
import os
import re
import time
from xml.etree import ElementTree as ET

try:
    import yaml
    HAVE_YAML = True
except ImportError:
    HAVE_YAML = False

try:
    import requests
    HAVE_REQUESTS = True
except ImportError:
    HAVE_REQUESTS = False

ROSTER_FILE = "roster.yaml"
RESOLVED_FILE = "roster_resolved.json"
HISTORY_FILE = "roster_history.json"

SEC_UA = os.environ.get("SEC_USER_AGENT", "stock-bot personal project contact@example.com")
_HEADERS = {"User-Agent": SEC_UA, "Accept-Encoding": "gzip, deflate"}

# browse-edgar company search, atom output — stable for 15+ years.
_SEARCH = ("https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany"
           "&company={q}&type=13F&dateb=&owner=include&count=10&output=atom")


# ── loading ────────────────────────────────────────────────────────────
def _norm_cik(cik):
    """Strip leading zeros / whitespace so '0001067983' == '1067983'."""
    s = str(cik or "").strip().lstrip("0")
    return s


def load_roster(path=ROSTER_FILE):
    """Parse roster.yaml into a normalized dict. Missing file -> permissive
    empty roster (every group on, no whitelist) so a fresh repo still runs."""
    default = {"groups": {"funds": True, "activists": True,
                          "politicians": True, "insiders": True},
               "funds": [], "activists": [], "politicians": [], "insiders_at": []}
    if not HAVE_YAML:
        print("roster: pyyaml missing; roster gating disabled (tracking all)")
        return default
    try:
        with open(path) as f:
            raw = yaml.safe_load(f) or {}
    except FileNotFoundError:
        print("roster: roster.yaml not found; tracking all (no curation)")
        return default
    except Exception as e:
        print(f"roster: roster.yaml unreadable ({e}); tracking all")
        return default

    groups = {**default["groups"], **(raw.get("groups") or {})}
    funds = []
    for f in (raw.get("funds") or []):
        if isinstance(f, str):
            f = {"name": f}
        funds.append({"name": (f.get("name") or "").strip(),
                      "cik": _norm_cik(f.get("cik")),
                      "tags": f.get("tags") or []})
    return {
        "groups": groups,
        "funds": funds,
        "activists": [str(a).strip() for a in (raw.get("activists") or []) if str(a).strip()],
        "politicians": [str(p).strip() for p in (raw.get("politicians") or []) if str(p).strip()],
        "insiders_at": [str(t).strip().upper() for t in (raw.get("insiders_at") or []) if str(t).strip()],
    }


def _load_json(path, fallback):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return fallback


# ── CIK resolution (the only networked part) ────────────────────────────
def _sec_get(url, retries=3):
    for attempt in range(retries):
        r = requests.get(url, headers=_HEADERS, timeout=25)
        if r.status_code == 200:
            return r
        if r.status_code in (403, 429):
            time.sleep(2 * (attempt + 1))
            continue
        r.raise_for_status()
    raise RuntimeError(f"SEC blocked/failed: {url}")


def _resolve_one(name, fetch=_sec_get):
    """Name -> (cik, matched_name) via EDGAR company search, or (None, reason).

    Conservative: returns the CIK only when the search yields a company block.
    On no match / error, returns (None, <reason>) and the caller skips it —
    we never guess a CIK, because a wrong CIK silently tracks the wrong fund.
    """
    q = re.sub(r"[^A-Za-z0-9 ]", " ", name).strip().replace(" ", "+")
    try:
        r = fetch(_SEARCH.format(q=q))
    except Exception as e:
        return None, f"lookup error ({e})"
    text = r.text if hasattr(r, "text") else r.content.decode("utf-8", "ignore")
    # atom company feed carries <cik> and <conformed-name> in company-info
    cik_m = re.search(r"<cik>(\d+)</cik>", text, re.I)
    name_m = re.search(r"<conformed-name>([^<]+)</conformed-name>", text, re.I)
    if cik_m:
        return _norm_cik(cik_m.group(1)), (name_m.group(1).strip() if name_m else name)
    # single-company results sometimes come back as an <company-info> id link
    id_m = re.search(r"CIK=(\d+)", text)
    if id_m:
        return _norm_cik(id_m.group(1)), name
    return None, "no company match"


def ensure_resolved(roster=None, fetch=_sec_get, save=True):
    """Fill in blank fund CIKs from cache or a live lookup. Idempotent.

    Mutates roster['funds'] in place so every resolvable fund has a cik.
    Returns the roster. Safe to call every run — it only hits the network for
    funds that have neither a seeded CIK nor a cached one.
    """
    roster = roster or load_roster()
    cache = _load_json(RESOLVED_FILE, {})
    changed = False

    for fund in roster["funds"]:
        if fund["cik"]:
            continue
        key = fund["name"].lower()
        if key in cache and cache[key].get("cik"):
            fund["cik"] = _norm_cik(cache[key]["cik"])
            continue
        if not HAVE_REQUESTS:
            print(f"roster: cannot resolve '{fund['name']}' (requests missing)")
            continue
        cik, info = _resolve_one(fund["name"], fetch=fetch)
        if cik:
            fund["cik"] = cik
            cache[key] = {"cik": cik, "matched_name": info}
            changed = True
            print(f"roster: resolved '{fund['name']}' -> CIK {cik} ({info})")
            time.sleep(0.2)          # be polite to SEC
        else:
            print(f"roster: could NOT resolve '{fund['name']}' — {info}; skipping")

    if save and changed:
        with open(RESOLVED_FILE, "w") as f:
            json.dump(cache, f, indent=2)
    return roster


# ── match helpers (what the collectors call) ────────────────────────────
def group_on(roster, group):
    return bool(roster.get("groups", {}).get(group, True))


def tracked_fund_ciks(roster):
    return {f["cik"] for f in roster["funds"] if f["cik"]}


def fund_name_for_cik(roster, cik):
    cik = _norm_cik(cik)
    for f in roster["funds"]:
        if f["cik"] == cik:
            return f["name"]
    return ""


def tracks_fund_cik(roster, cik):
    return _norm_cik(cik) in tracked_fund_ciks(roster)


def tracks_activist(roster, filer_name):
    fn = (filer_name or "").lower()
    return any(a.lower() in fn for a in roster["activists"])


def tracks_politician(roster, person):
    # empty list = no whitelist = keep everyone (group toggle still applies)
    if not roster["politicians"]:
        return True
    pn = (person or "").lower()
    return any(p.lower() in pn for p in roster["politicians"])


def tracks_insider_ticker(roster, ticker):
    return (ticker or "").strip().upper() in set(roster["insiders_at"])


# ── history (durable, append-only) ──────────────────────────────────────
def _hist_key(entry):
    """Stable de-dup key so re-runs don't double-log the same event."""
    return "|".join(str(entry.get(k, "")) for k in
                    ("action", "ticker", "issuer", "value", "period", "date"))


def append_history(group, member_id, member_name, event, path=HISTORY_FILE,
                   latest_top=None, latest_period=None):
    """Append one event to member_id's log under `group`. Never trims.

    group      : "funds" | "activists" | "politicians" | "insiders"
    member_id  : cik for funds/insiders-by-company, lowercased name otherwise
    event      : dict (action, issuer, ticker, value, date, period, ...)
    latest_top : optional list of current top holdings (funds) — overwrites
    """
    hist = _load_json(path, {})
    grp = hist.setdefault(group, {})
    slot = grp.setdefault(str(member_id), {"name": member_name, "events": []})
    slot["name"] = member_name or slot.get("name", "")
    keys = {_hist_key(e) for e in slot["events"]}
    if _hist_key(event) not in keys:
        slot["events"].append(event)
    if latest_top is not None:
        slot["latest_top"] = latest_top
    if latest_period is not None:
        slot["latest_period"] = latest_period
    with open(path, "w") as f:
        json.dump(hist, f, indent=1)
    return hist


def load_history(path=HISTORY_FILE):
    return _load_json(path, {})


# ── quick self-report: `python roster.py` prints the loaded watchlist ───
if __name__ == "__main__":
    r = load_roster()
    g = r["groups"]
    print("Groups:", ", ".join(f"{k}={'on' if v else 'off'}" for k, v in g.items()))
    print(f"Funds ({len(r['funds'])}):")
    for f in r["funds"]:
        print(f"  - {f['name']:<32} CIK {f['cik'] or '(resolve on run)'}")
    print(f"Activists: {', '.join(r['activists']) or '—'}")
    print(f"Politicians: {', '.join(r['politicians']) or '(all)'}")
    print(f"Insiders at: {', '.join(r['insiders_at']) or '—'}")
