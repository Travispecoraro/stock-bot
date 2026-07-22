"""
test_phase4.py — SPY benchmark in perf.json (prices.py). Offline; the Stooq
fetch is stubbed with canned CSVs. Run: pytest -q test_phase4.py
"""

import json
import os

import prices


CSV = {
    "nvda.us": "Date,Open,High,Low,Close,Volume\n"
               "2026-07-01,100,101,99,100,1\n"
               "2026-07-02,100,103,100,102,1\n"
               "2026-07-03,102,105,102,104,1\n",
    "spy.us":  "Date,Open,High,Low,Close,Volume\n"
               "2026-07-01,500,501,499,500,1\n"
               "2026-07-02,500,503,500,505,1\n"
               "2026-07-03,505,506,504,510,1\n",
}


def fake_fetch(url):
    for sym, csv in CSV.items():
        if f"s={sym}" in url:
            return csv
    raise RuntimeError("no fixture for " + url)


STATE = {"congress": {"recent_trades": [
    {"person": "Nancy Pelosi", "ticker": "NVDA", "side": "buy",
     "amount": "$1,000,001 - $5,000,000", "transaction_date": "2026-07-01"},
]}}


def test_benchmark_written_and_rebased(tmp_path):
    out = tmp_path / "perf.json"
    assert prices.run(fetch=fake_fetch, state=STATE, out_file=str(out)) == 0
    perf = json.loads(out.read_text())
    b = perf["benchmark"]
    assert b["symbol"] == "SPY"
    series = dict((d, v) for d, v in b["series"])
    # Rebased to 100 at the index's first date; 500 -> 505 -> 510
    assert series["2026-07-01"] == 100.0
    assert abs(series["2026-07-02"] - 101.0) < 1e-6
    assert abs(series["2026-07-03"] - 102.0) < 1e-6
    # Index still present and independent of the benchmark
    assert perf["index"][0][1] == 100.0


def test_benchmark_failure_is_labeled_absence(tmp_path):
    def no_spy(url):
        if "s=spy.us" in url:
            raise RuntimeError("stooq down")
        return fake_fetch(url)
    out = tmp_path / "perf.json"
    assert prices.run(fetch=no_spy, state=STATE, out_file=str(out)) == 0
    perf = json.loads(out.read_text())
    assert perf["benchmark"] is None          # omitted, not faked
    assert perf["index"]                      # index unaffected


def test_build_benchmark_no_overlap():
    assert prices.build_benchmark({}, [["2026-07-01", 100.0]]) == []
