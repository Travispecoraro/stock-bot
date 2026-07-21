"""
congress_sources.py — official government sources for congressional trades.

Drop-in replacement for the community S3 mirrors (which now 403). Each fetch_*
returns rows in the exact shape monitor.py already consumes:

  {chamber, person, ticker, asset, side, amount, owner,
   transaction_date, disclosure_date, link}

`side` is pre-classified to "buy"/"sell"; rows that are neither (exchanges,
etc.) are dropped here so monitor.py sees only actionable trades.

SENATE  efdsearch.senate.gov — accept the agreement (CSRF), page through the
        report search API for Periodic Transaction Reports (type 11), then
        parse each electronic report's HTML transaction table. Paper (scanned)
        filings have no structured data and are skipped.

HOUSE   disclosures-clerk.house.gov — download the yearly bulk {year}FD.zip,
        read its XML index for PTR filings (FilingType "P"), then pull each
        filing's PDF and extract transactions with pdfplumber. Scanned PDFs
        that yield no text are skipped (logged), so House coverage is
        digital-filings-only and best-effort.

Everything is defensive: one bad filing logs and continues; a dead source
raises so monitor.py's per-source try/except turns it into a styled notice.
"""

import io
import re
import time
import zipfile
from datetime import datetime, timedelta, timezone
from xml.etree import ElementTree as ET

import requests

try:
    from bs4 import BeautifulSoup
    HAVE_BS4 = True
except ImportError:
    HAVE_BS4 = False

try:
    import pdfplumber
    HAVE_PDF = True
except ImportError:
    HAVE_PDF = False

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36")


# ── shared helpers ─────────────────────────────────────────────────────
def _classify(raw):
    t = (raw or "").lower()
    if "purchase" in t or t.strip() in ("p", "buy"):
        return "buy"
    if "sale" in t or "sold" in t or t.strip() in ("s", "sell"):
        return "sell"
    return None            # exchange / receipt / unknown → dropped


def _strip(html):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", html or "")).strip()


def _ticker_from_asset(asset):
    """'Apple Inc (AAPL) [ST]' -> ('AAPL', 'Apple Inc')."""
    m = re.search(r"\(([A-Z][A-Z0-9.\-]{0,6})\)", asset or "")
    ticker = m.group(1) if m else ""
    name = re.sub(r"\s*\([^)]*\)\s*", " ", asset or "")
    name = re.sub(r"\s*\[[^\]]*\]\s*", " ", name).strip(" .")
    return ticker, name


# ── Senate: efdsearch.senate.gov ───────────────────────────────────────
SEN_ROOT = "https://efdsearch.senate.gov"
SEN_LANDING = SEN_ROOT + "/search/"
SEN_HOME = SEN_ROOT + "/search/home/"
SEN_DATA = SEN_ROOT + "/search/report/data/"


def _senate_session():
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})
    s.get(SEN_LANDING, timeout=30)
    token = s.cookies.get("csrftoken", "")
    s.post(SEN_HOME,
           data={"prohibition_agreement": "1", "csrfmiddlewaretoken": token},
           headers={"Referer": SEN_LANDING}, timeout=30)
    # token may rotate after accepting the agreement
    return s, s.cookies.get("csrftoken", token)


def _parse_senate_report(session, url, person, filed_date):
    """Parse one electronic PTR's transaction table into normalized rows."""
    rows = []
    r = session.get(url, timeout=30)
    if r.status_code != 200:
        return rows
    if not HAVE_BS4:
        return rows
    soup = BeautifulSoup(r.text, "html.parser")
    table = soup.find("table")
    if not table:
        return rows
    # map columns by header text so we survive column-order changes
    headers = [_strip(th.get_text()).lower() for th in table.select("thead th")]

    def col(includes, excludes=()):
        for i, h in enumerate(headers):        # exact match wins
            if h in includes:
                return i
        for i, h in enumerate(headers):        # then substring, honouring excludes
            if any(n in h for n in includes) and not any(x in h for x in excludes):
                return i
        return None

    ci = {
        "date": col(["transaction date", "date"]),
        "owner": col(["owner"]),
        "ticker": col(["ticker"]),
        "asset": col(["asset name", "asset"], excludes=["type"]),
        "type": col(["transaction type", "type"], excludes=["asset"]),
        "amount": col(["amount"]),
    }

    for tr in table.select("tbody tr"):
        cells = [_strip(td.get_text()) for td in tr.find_all("td")]
        if not cells:
            continue

        def get(key):
            i = ci.get(key)
            return cells[i] if i is not None and i < len(cells) else ""

        side = _classify(get("type"))
        if not side:
            continue
        ticker = get("ticker")
        ticker = "" if ticker in ("--", "") else ticker.upper()
        asset = get("asset")
        if not ticker:
            ticker, asset = _ticker_from_asset(asset) or (ticker, asset)
        rows.append({
            "chamber": "Senate", "person": person,
            "ticker": ticker, "asset": asset, "side": side,
            "amount": get("amount"), "owner": get("owner"),
            "transaction_date": get("date"), "disclosure_date": filed_date,
            "link": url,
        })
    return rows


def fetch_senate(lookback_days=45, skip_reports=None, max_reports=400):
    """Electronic PTRs filed within lookback_days -> (rows, processed_urls).

    skip_reports: set of report URLs already handled on a previous run; those
    are not re-fetched. processed_urls is what to add to that set afterwards,
    so steady-state runs only fetch genuinely new reports."""
    if not HAVE_BS4:
        raise RuntimeError("beautifulsoup4 not installed (add it to requirements.txt)")
    skip = set(skip_reports or ())
    session, token = _senate_session()
    start = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).strftime("%m/%d/%Y")

    out, processed, offset, page, seen_reports = [], [], 0, 100, 0
    while seen_reports < max_reports:
        payload = {
            "start": str(offset), "length": str(page),
            "report_types": "[11]",          # 11 = Periodic Transaction Report
            "filer_types": "[]",
            "submitted_start_date": f"{start} 00:00:00",
            "submitted_end_date": "",
            "candidate_state": "", "senator_state": "", "office_id": "",
            "first_name": "", "last_name": "",
            "csrfmiddlewaretoken": token,
        }
        r = session.post(SEN_DATA, data=payload,
                         headers={"Referer": SEN_LANDING, "X-CSRFToken": token},
                         timeout=30)
        r.raise_for_status()
        j = r.json()
        data = j.get("data", [])
        if not data:
            break
        for row in data:
            seen_reports += 1
            first, last = _strip(row[0]), _strip(row[1])
            link_html, filed = row[3], _strip(row[4])
            href_m = re.search(r'href="([^"]+)"', link_html)
            if not href_m:
                continue
            href = href_m.group(1)
            if "/ptr/" not in href:          # /paper/ = scanned, no table
                continue
            url = SEN_ROOT + href if href.startswith("/") else href
            if url in skip:
                continue
            try:
                out += _parse_senate_report(session, url, f"{first} {last}".strip(), filed)
                processed.append(url)
                time.sleep(0.2)
            except Exception as e:
                print(f"congress_sources: senate report skip ({e})")
        total = j.get("recordsTotal", 0)
        offset += page
        if offset >= total:
            break
    print(f"congress_sources: senate — {len(out)} transactions from "
          f"{len(processed)} new reports ({seen_reports} scanned)")
    return out, processed


# ── House: disclosures-clerk.house.gov ─────────────────────────────────
HOUSE_ZIP = "https://disclosures-clerk.house.gov/public_disc/financial-pdfs/{year}FD.zip"
HOUSE_PDF = "https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/{year}/{doc}.pdf"


def _house_index(year):
    """Return PTR filings [(doc_id, name, filed_date)] from the yearly ZIP."""
    r = requests.get(HOUSE_ZIP.format(year=year),
                     headers={"User-Agent": UA}, timeout=60)
    r.raise_for_status()
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    xml_name = next((n for n in zf.namelist() if n.lower().endswith(".xml")), None)
    if not xml_name:
        return []
    root = ET.fromstring(zf.read(xml_name))
    out = []
    for m in root.findall(".//Member"):
        if (m.findtext("FilingType") or "").strip().upper() != "P":   # P = PTR
            continue
        first = (m.findtext("First") or "").strip()
        last = (m.findtext("Last") or "").strip()
        doc = (m.findtext("DocID") or "").strip()
        filed = (m.findtext("FilingDate") or "").strip()
        if doc:
            out.append((doc, f"{first} {last}".strip(), filed))
    return out


_HOUSE_ROW = re.compile(
    r"(?P<asset>.+?\([A-Z][A-Z0-9.\-]{0,6}\).*?)\s+"
    r"(?P<type>P|S|S \(partial\)|E)\s+"
    r"(?P<date>\d{2}/\d{2}/\d{4})\s+"
    r"(?P<notif>\d{2}/\d{2}/\d{4})\s+"
    r"(?P<amount>\$[\d,]+\s*-\s*\$[\d,]+)", re.I)


def _parse_house_pdf(pdf_bytes, person, filed):
    """Extract transactions from one digital PTR PDF. Empty for scanned PDFs."""
    rows = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        text = "\n".join((pg.extract_text() or "") for pg in pdf.pages)
    if not text.strip():
        return rows                          # scanned/handwritten → nothing to parse
    for m in _HOUSE_ROW.finditer(text):
        side = _classify(m.group("type"))
        if not side:
            continue
        ticker, asset = _ticker_from_asset(m.group("asset"))
        rows.append({
            "chamber": "House", "person": person,
            "ticker": ticker, "asset": asset, "side": side,
            "amount": re.sub(r"\s+", " ", m.group("amount")), "owner": "",
            "transaction_date": m.group("date"), "disclosure_date": filed,
            "link": "",
        })
    return rows


def fetch_house(lookback_days=45, skip_docs=None, max_filings=200):
    """Recent House PTRs -> (rows, processed_doc_ids). Digital PDFs only.

    skip_docs: set of DocIDs already handled; their PDFs are not re-downloaded.
    processed_doc_ids is what to add to that set afterwards."""
    if not HAVE_PDF:
        raise RuntimeError("pdfplumber not installed (add it to requirements.txt)")
    skip = set(skip_docs or ())
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=lookback_days)
    years = {now.year}
    if cutoff.year != now.year:
        years.add(cutoff.year)

    filings = []
    for y in sorted(years, reverse=True):
        try:
            filings += [(y,) + f for f in _house_index(y)]
        except Exception as e:
            print(f"congress_sources: house index {y} failed ({e})")

    def in_window(filed):
        for fmt in ("%m/%d/%Y", "%Y-%m-%d"):
            try:
                return datetime.strptime(filed, fmt).replace(tzinfo=timezone.utc) >= cutoff
            except ValueError:
                continue
        return True                          # unparseable date → keep, let dedup handle it

    filings = [f for f in filings if in_window(f[3]) and f[1] not in skip][:max_filings]
    out, processed, empty = [], [], 0
    for year, doc, person, filed in filings:
        try:
            pr = requests.get(HOUSE_PDF.format(year=year, doc=doc),
                              headers={"User-Agent": UA}, timeout=30)
            if pr.status_code != 200:
                continue
            rows = _parse_house_pdf(pr.content, person, filed)
            out += rows
            processed.append(doc)
            if not rows:
                empty += 1
            time.sleep(0.15)
        except Exception as e:
            print(f"congress_sources: house pdf {doc} skip ({e})")
    print(f"congress_sources: house — {len(out)} transactions from {len(processed)} "
          f"new PDFs ({empty} scanned / no text)")
    return out, processed


if __name__ == "__main__":
    # Manual smoke test (needs network + deps). Run: python congress_sources.py
    print("deps:", "bs4" if HAVE_BS4 else "NO bs4", "|", "pdfplumber" if HAVE_PDF else "NO pdfplumber")
    try:
        s, _ = fetch_senate(lookback_days=10, max_reports=40)
        print("senate sample:", s[:2])
    except Exception as e:
        print("senate failed:", e)
    try:
        h, _ = fetch_house(lookback_days=10, max_filings=10)
        print("house sample:", h[:2])
    except Exception as e:
        print("house failed:", e)
