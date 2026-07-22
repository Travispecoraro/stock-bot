"""
test_phase1.py — offline verification of the congress_sources -> monitor wiring.

Live endpoints (efdsearch.senate.gov / disclosures-clerk.house.gov) are NOT hit;
everything runs on fixtures so it works in CI and sandboxes. Run: pytest -q
"""

import io
import json
import os
import sys
import types

import pytest

import congress_sources as cs


# ── 1. Senate report-page parsing ──────────────────────────────────────

SENATE_REPORT_HTML = """
<html><body><table>
<thead><tr>
  <th>#</th><th>Transaction Date</th><th>Owner</th><th>Ticker</th>
  <th>Asset Name</th><th>Asset Type</th><th>Transaction Type</th>
  <th>Amount</th><th>Comment</th>
</tr></thead>
<tbody>
<tr><td>1</td><td>06/12/2026</td><td>Spouse</td><td>NVDA</td>
    <td>NVIDIA Corporation</td><td>Stock</td><td>Purchase</td>
    <td>$50,001 - $100,000</td><td>--</td></tr>
<tr><td>2</td><td>06/15/2026</td><td>Self</td><td>--</td>
    <td>Apple Inc (AAPL) [ST]</td><td>Stock</td><td>Sale (Full)</td>
    <td>$15,001 - $50,000</td><td>--</td></tr>
<tr><td>3</td><td>06/16/2026</td><td>Self</td><td>MSFT</td>
    <td>Microsoft Corp</td><td>Stock</td><td>Exchange</td>
    <td>$1,001 - $15,000</td><td>--</td></tr>
</tbody></table></body></html>
"""


class FakeResp:
    def __init__(self, text, status=200):
        self.text, self.status_code = text, status


class FakeSession:
    def __init__(self, text):
        self._text = text
    def get(self, url, timeout=None):
        return FakeResp(self._text)


def test_senate_report_parse():
    rows = cs._parse_senate_report(
        FakeSession(SENATE_REPORT_HTML),
        "https://efdsearch.senate.gov/search/view/ptr/xyz/",
        "Jane Senator", "06/20/2026")
    # Exchange row dropped; buy + sell kept.
    assert len(rows) == 2
    buy, sell = rows
    assert buy["side"] == "buy" and buy["ticker"] == "NVDA"
    assert buy["amount"] == "$50,001 - $100,000"
    assert buy["chamber"] == "Senate" and buy["person"] == "Jane Senator"
    # Ticker recovered from the asset string when the ticker column is "--".
    assert sell["side"] == "sell" and sell["ticker"] == "AAPL"
    assert "Apple" in sell["asset"]


def test_senate_header_remap_survives_reorder():
    reordered = SENATE_REPORT_HTML.replace(
        "<th>#</th><th>Transaction Date</th><th>Owner</th><th>Ticker</th>",
        "<th>#</th><th>Owner</th><th>Transaction Date</th><th>Ticker</th>"
    ).replace(
        "<td>1</td><td>06/12/2026</td><td>Spouse</td><td>NVDA</td>",
        "<td>1</td><td>Spouse</td><td>06/12/2026</td><td>NVDA</td>"
    ).replace(
        "<td>2</td><td>06/15/2026</td><td>Self</td><td>--</td>",
        "<td>2</td><td>Self</td><td>06/15/2026</td><td>--</td>"
    ).replace(
        "<td>3</td><td>06/16/2026</td><td>Self</td><td>MSFT</td>",
        "<td>3</td><td>Self</td><td>06/16/2026</td><td>MSFT</td>"
    )
    rows = cs._parse_senate_report(FakeSession(reordered), "u", "P", "06/20/2026")
    assert rows[0]["transaction_date"] == "06/12/2026"
    assert rows[0]["owner"] == "Spouse"


# ── 2. House PDF parsing ───────────────────────────────────────────────

def _make_house_pdf():
    from reportlab.pdfgen import canvas
    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    c.drawString(40, 800, "SP Apple Inc (AAPL) [ST] P 06/10/2026 06/11/2026 $1,001 - $15,000")
    c.drawString(40, 780, "SP NVIDIA Corporation (NVDA) [ST] S 06/12/2026 06/13/2026 $15,001 - $50,000")
    c.drawString(40, 760, "SP Some Fund (XYZ) [MF] E 06/14/2026 06/15/2026 $1,001 - $15,000")
    c.save()
    return buf.getvalue()


def test_house_pdf_parse():
    pytest.importorskip("reportlab")
    rows = cs._parse_house_pdf(_make_house_pdf(), "Rep Example", "06/16/2026")
    assert len(rows) == 2                       # E (exchange) dropped
    assert rows[0]["side"] == "buy" and rows[0]["ticker"] == "AAPL"
    assert rows[1]["side"] == "sell" and rows[1]["ticker"] == "NVDA"
    assert rows[0]["transaction_date"] == "06/10/2026"
    assert rows[0]["amount"] == "$1,001 - $15,000"
    assert rows[0]["chamber"] == "House" and rows[0]["person"] == "Rep Example"


def test_house_scanned_pdf_yields_nothing():
    from reportlab.pdfgen import canvas
    buf = io.BytesIO()
    canvas.Canvas(buf).save()                   # empty page = no text
    assert cs._parse_house_pdf(buf.getvalue(), "X", "06/16/2026") == []


# ── 3. monitor.py integration (fetchers + Discord mocked) ─────────────

SEN_ROW = {
    "chamber": "Senate", "person": "Nancy Pelosi", "ticker": "NVDA",
    "asset": "NVIDIA Corporation", "side": "buy",
    "amount": "$1,000,001 - $5,000,000", "owner": "Spouse",
    "transaction_date": "2026-07-10", "disclosure_date": "2026-07-18",
    "link": "https://efdsearch.senate.gov/search/view/ptr/abc/",
}
HOUSE_ROW = {
    "chamber": "House", "person": "Random Member", "ticker": "KO",
    "asset": "Coca-Cola Co", "side": "sell",
    "amount": "$1,001 - $15,000", "owner": "",
    "transaction_date": "07/09/2026", "disclosure_date": "07/17/2026",
    "link": "",
}


@pytest.fixture
def bot(tmp_path, monkeypatch):
    """Import monitor inside a temp repo with fetchers + webhook mocked."""
    import shutil
    for f in ("monitor.py", "congress_sources.py", "roster.py",
              "config.yaml", "roster.yaml"):
        shutil.copy(os.path.join(os.path.dirname(__file__), f), tmp_path / f)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://example.invalid/hook")
    monkeypatch.syspath_prepend(str(tmp_path))
    for m in ("monitor", "congress_sources", "roster"):
        sys.modules.pop(m, None)
    import monitor

    calls = {"senate": 0, "house": 0, "posts": []}

    def fake_senate(lookback_days=45, skip_reports=None, **kw):
        calls["senate"] += 1
        skip = set(skip_reports or ())
        if SEN_ROW["link"] in skip:
            return [], []
        return [dict(SEN_ROW)], [SEN_ROW["link"]]

    def fake_house(lookback_days=45, skip_docs=None, **kw):
        calls["house"] += 1
        skip = set(skip_docs or ())
        if "DOC001" in skip:
            return [], []
        return [dict(HOUSE_ROW)], ["DOC001"]

    monkeypatch.setattr(monitor.congress_sources, "fetch_senate", fake_senate)
    monkeypatch.setattr(monitor.congress_sources, "fetch_house", fake_house)
    monkeypatch.setattr(monitor, "discord_post", lambda p: calls["posts"].append(p))
    monkeypatch.setattr(monitor.time, "sleep", lambda *_: None)
    return monitor, calls, tmp_path


def _state(tmp_path):
    with open(tmp_path / "state.json") as f:
        return json.load(f)


def test_run1_seeds_ledger_no_trade_alerts(bot):
    monitor, calls, tmp = bot
    assert monitor.main() == 0
    st = _state(tmp)
    # Both rows on the ledger with a logged stamp; skip lists recorded.
    tape = st["congress"]["recent_trades"]
    assert {t["ticker"] for t in tape} == {"NVDA", "KO"}
    assert all(t["logged"] for t in tape)
    assert st["congress"]["processed_senate"] == [SEN_ROW["link"]]
    assert st["congress"]["processed_house"] == ["DOC001"]
    # Only the init notice posted — no per-trade alerts on the seed run.
    assert len(calls["posts"]) == 1
    assert "initialized" in calls["posts"][0]["embeds"][0]["title"].lower()


def test_run2_incremental_no_refetch_no_dupes(bot):
    monitor, calls, tmp = bot
    monitor.main()
    posts_after_1 = len(calls["posts"])
    monitor.main()                               # steady-state run
    st = _state(tmp)
    # Skip lists made the fetchers return nothing new -> no dupes, no alerts.
    assert len(st["congress"]["recent_trades"]) == 2
    assert st["congress"]["processed_senate"] == [SEN_ROW["link"]]
    assert len(calls["posts"]) == posts_after_1
    assert calls["senate"] == 2 and calls["house"] == 2


def test_roster_gates_alerts_not_ledger(bot, monkeypatch):
    monitor, calls, tmp = bot
    monitor.main()                               # seed
    # New trade from an off-roster member next run:
    new_row = dict(HOUSE_ROW, person="Off Roster",
                   transaction_date="07/12/2026", disclosure_date="07/19/2026")
    pelosi = dict(SEN_ROW, transaction_date="2026-07-12",
                  disclosure_date="2026-07-19",
                  link="https://efdsearch.senate.gov/search/view/ptr/def/")
    monkeypatch.setattr(monitor.congress_sources, "fetch_senate",
                        lambda **kw: ([pelosi], [pelosi["link"]]))
    monkeypatch.setattr(monitor.congress_sources, "fetch_house",
                        lambda **kw: ([new_row], ["DOC002"]))
    calls["posts"].clear()
    monitor.main()
    st = _state(tmp)
    # Both hit the ledger (ledger_scope: all)…
    people = [t["person"] for t in st["congress"]["recent_trades"]]
    assert "Off Roster" in people and people.count("Nancy Pelosi") == 2
    # …but only the roster member (Pelosi) triggers a trade alert.
    alert_titles = [e["title"] for p in calls["posts"] for e in p.get("embeds", [])]
    assert any("Pelosi" in t for t in alert_titles)
    assert not any("Off Roster" in t for t in alert_titles)


def test_ledger_scope_roster(bot, monkeypatch):
    monitor, calls, tmp = bot
    import yaml
    cfgp = tmp / "config.yaml"
    cfg = yaml.safe_load(cfgp.read_text())
    cfg["filters"]["ledger_scope"] = "roster"
    cfgp.write_text(yaml.safe_dump(cfg))
    monitor.main()
    st = _state(tmp)
    people = {t["person"] for t in st["congress"]["recent_trades"]}
    assert people == {"Nancy Pelosi"}            # off-roster row excluded


def test_prune_ledger_ages_out_and_caps(bot):
    monitor, _, tmp = bot
    st = monitor.load_state()
    c = monitor._congress_state(st)
    c["recent_trades"] = [
        {"transaction_date": "2020-01-01", "logged": "2020-01-01"},   # ancient
        {"transaction_date": "2026-07-01", "logged": "2026-07-01"},   # fresh
        {"transaction_date": "", "logged": "2026-07-01"},             # fresh by log
        {"transaction_date": "", "logged": "2020-01-01"},             # ancient by log
    ]
    monitor.prune_ledger(st)
    assert len(c["recent_trades"]) == 2


def test_downstream_contracts_read_the_ledger(bot):
    """heartbeat.build_snapshot and prices.gather_trades must see the rows."""
    monitor, _, tmp = bot
    monitor.main()
    st = _state(tmp)

    import importlib, shutil
    for f in ("heartbeat.py", "prices.py", "Notify.py"):
        src = os.path.join(os.path.dirname(__file__), f)
        shutil.copy(src, tmp / f)
    shutil.copy(os.path.join(os.path.dirname(__file__), "Notify.py"),
                tmp / "notify.py")               # case-sensitive import fix
    sys.modules.pop("heartbeat", None); sys.modules.pop("prices", None)
    import heartbeat, prices

    snap = heartbeat.build_snapshot(st, {})
    assert snap["names_bought"] >= 1             # NVDA buy is in the window

    trades = prices.gather_trades(st)
    tickers = {t[2] for t in trades}
    assert "NVDA" in tickers and "KO" in tickers
