"""
prices.py — portfolio performance index builder for stock-bot.

Runs after the collectors. Reads the trade tape in state.json, works out which
tickers the tracked people are STILL net-long (same netting the dashboard's
Portfolio uses: a person's sells cancel their buys in a name, dollar-wise;
a ticker is "held" while at least one person is net-long), fetches daily
closing prices for those names, and builds ONE equal-weight index line:

  * each ticker contributes its daily return only while it is held
    (index-style membership: it enters at its first buy date and stops
    contributing the day everyone is net-out — history stays, tracking stops)
  * the index is the equal-weighted average of active members' daily returns,
    compounded from 100

Output: perf.json  (committed by the workflow, read by the dashboard)
  {
    "updated": "YYYY-MM-DD",
    "start": "YYYY-MM-DD",
    "index": [["YYYY-MM-DD", 101.23], ...],
    "members": [{"ticker": "NVDA", "from": "...", "to": null|"...",
                 "ret_pct": 12.3}, ...],   # per-name return over its window
    "active": 7, "exited": 2
  }

Price source: Stooq (stooq.com) — free daily CSVs, no API key, no auth.
One request per ticker per run; polite 0.3s spacing. If a ticker's prices
can't be fetched it is skipped (logged) rather than failing the run.

Run: python prices.py    (after monitor.py / insiders.py in the workflow)
"""

import json
import io
import os
import re
import sys
import time
import csv
from datetime import datetime, timedelta, timezone

import requests

STATE_FILE = "state.json"
OUT_FILE = "perf.json"
LOOKBACK_DAYS = 220           # price history window (~10 months of sessions)
MAX_TICKERS = 60              # safety cap per run

STOOQ = "https://stooq.com/q/d/l/?s={sym}&d1={d1}&d2={d2}&i=d"
UA = {"User-Agent": "stock-bot personal project"}


# ── helpers ────────────────────────────────────────────────────────────
def amt_mid(s):
    """'$1,001 - $15,000' / '$1K–15K' / number -> midpoint dollars."""
    if isinstance(s, (int, float)):
        return float(s) if s == s else 0.0          # NaN guard
    if not s:
        return 0.0
    nums = []
    for tok in re.findall(r"[\d,.]+\s*[KMBkmb]?", str(s)):
        m = re.match(r"([\d,.]+)\s*([KMBkmb]?)", tok)
        if not m:
            continue
        try:
            n = float(m.group(1).replace(",", ""))
        except ValueError:
            continue
        n *= {"K": 1e3, "M": 1e6, "B": 1e9}.get(m.group(2).upper(), 1)
        nums.append(n)
    if not nums:
        return 0.0
    return nums[0] if len(nums) == 1 else (min(nums) + max(nums)) / 2


def sane(v):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return 0.0
    return v if (v == v and 0 <= v < 1e12 and v != 2147483647) else 0.0


def pdate(s):
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(str(s).strip(), fmt).date()
        except (ValueError, TypeError):
            continue
    return None


# ── membership: who still holds what, and since/until when ────────────
def gather_trades(state):
    """Normalize congress + insider trades -> [(date, person, ticker, +/-usd)]."""
    out = []
    for t in (state.get("congress", {}) or {}).get("recent_trades", []) \
             or state.get("recent_trades", []) or []:
        d = pdate(t.get("transaction_date") or t.get("date"))
        tk = (t.get("ticker") or "").strip().upper()
        side = (t.get("side") or "").lower()
        if not d or not tk or side not in ("buy", "sell"):
            continue
        amt = sane(amt_mid(t.get("amount")))
        out.append((d, "c|" + (t.get("person") or "?"), tk,
                    amt if side == "buy" else -amt))
    for t in (state.get("insiders", {}) or {}).get("recent_trades", []):
        d = pdate(t.get("date"))
        tk = (t.get("ticker") or "").strip().upper()
        side = (t.get("side") or "").upper()
        if not d or not tk or side not in ("BUY", "SELL"):
            continue
        amt = sane(t.get("value"))
        out.append((d, "i|" + (t.get("owner") or "?"), tk,
                    amt if side == "BUY" else -amt))
    return out


def membership_windows(trades):
    """Per ticker -> list of (from_date, to_date_or_None) held windows.

    A person is net-long while their cumulative signed dollars > 0. A ticker
    is held while >=1 person is net-long. Windows open at the first date that
    makes someone net-long and close on the date the last holder nets out.
    """
    by_tk = {}
    for d, person, tk, amt in sorted(trades):
        by_tk.setdefault(tk, []).append((d, person, amt))

    windows = {}
    for tk, evs in by_tk.items():
        pos = {}                       # person -> cumulative dollars
        holders = set()
        wins, open_from = [], None
        for d, person, amt in evs:
            pos[person] = pos.get(person, 0.0) + amt
            was = person in holders
            now = pos[person] > 0
            if now and not was:
                holders.add(person)
                if open_from is None:
                    open_from = d
            elif was and not now:
                holders.discard(person)
                if not holders and open_from is not None:
                    wins.append((open_from, d))
                    open_from = None
        if open_from is not None:
            wins.append((open_from, None))          # still held
        if wins:
            windows[tk] = wins
    return windows


# ── prices ─────────────────────────────────────────────────────────────
def fetch_prices(ticker, d1, d2, fetch=None):
    """Daily closes from Stooq -> {date: close}. Empty dict on any failure."""
    sym = ticker.lower() + ".us"
    url = STOOQ.format(sym=sym, d1=d1.strftime("%Y%m%d"), d2=d2.strftime("%Y%m%d"))
    try:
        if fetch:
            text = fetch(url)
        else:
            r = requests.get(url, headers=UA, timeout=20)
            r.raise_for_status()
            text = r.text
        out = {}
        for row in csv.DictReader(io.StringIO(text)):
            d = pdate(row.get("Date"))
            try:
                c = float(row.get("Close") or 0)
            except ValueError:
                continue
            if d and c > 0:
                out[d] = c
        return out
    except Exception as e:
        print(f"prices: {ticker} fetch failed ({e})")
        return {}


# ── the index ──────────────────────────────────────────────────────────
def build_index(windows, prices):
    """Equal-weight daily-rebalanced index of active members. Base 100.

    windows: {ticker: [(from, to|None)]}   prices: {ticker: {date: close}}
    Returns (series [(date, value)], per_ticker_return {tk: pct}).
    """
    def active(tk, d):
        return any(f <= d and (t is None or d <= t) for f, t in windows.get(tk, []))

    all_days = sorted({d for p in prices.values() for d in p})
    if not all_days:
        return [], {}

    series, idx = [], 100.0
    prev = {}
    for d in all_days:
        rets = []
        for tk, p in prices.items():
            if d not in p:
                continue
            if tk in prev and active(tk, d):
                pr = prev[tk]
                if pr > 0:
                    rets.append(p[d] / pr - 1.0)
            prev[tk] = p[d]
        if rets:
            idx *= 1.0 + sum(rets) / len(rets)
        series.append((d.isoformat(), round(idx, 3)))

    # per-ticker return over its held window (first->last priced day inside it)
    per = {}
    for tk, wins in windows.items():
        p = prices.get(tk) or {}
        if not p:
            continue
        days = sorted(p)
        f0, t0 = wins[0][0], wins[-1][1]
        span = [d for d in days if d >= f0 and (t0 is None or d <= t0)]
        if len(span) >= 2 and p[span[0]] > 0:
            per[tk] = round((p[span[-1]] / p[span[0]] - 1.0) * 100, 2)
    return series, per


# ── main ───────────────────────────────────────────────────────────────
def run(fetch=None, state=None, out_file=OUT_FILE):
    if state is None:
        try:
            with open(STATE_FILE) as f:
                state = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            print("prices: no state.json yet; nothing to do")
            return 0

    trades = gather_trades(state)
    windows = membership_windows(trades)
    if not windows:
        print("prices: no held positions found in the tape")
        with open(out_file, "w") as f:
            json.dump({"updated": datetime.now(timezone.utc).date().isoformat(),
                       "index": [], "members": [], "active": 0, "exited": 0}, f)
        return 0

    today = datetime.now(timezone.utc).date()
    d1 = today - timedelta(days=LOOKBACK_DAYS)
    tickers = sorted(windows)[:MAX_TICKERS]

    prices = {}
    for tk in tickers:
        p = fetch_prices(tk, d1, today, fetch=fetch)
        if p:
            prices[tk] = p
        time.sleep(0 if fetch else 0.3)
    print(f"prices: {len(prices)}/{len(tickers)} tickers priced")

    series, per = build_index({k: windows[k] for k in prices}, prices)

    members = []
    for tk in tickers:
        wins = windows[tk]
        members.append({"ticker": tk,
                        "from": wins[0][0].isoformat(),
                        "to": wins[-1][1].isoformat() if wins[-1][1] else None,
                        "ret_pct": per.get(tk)})
    active = sum(1 for m in members if m["to"] is None)

    with open(out_file, "w") as f:
        json.dump({
            "updated": today.isoformat(),
            "start": series[0][0] if series else None,
            "index": series,
            "members": members,
            "active": active,
            "exited": len(members) - active,
        }, f, separators=(",", ":"))
    print(f"prices: perf.json written — {len(series)} points, "
          f"{active} active / {len(members)-active} exited members")
    return 0


if __name__ == "__main__":
    sys.exit(run())
