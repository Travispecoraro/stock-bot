"""
notify.py — Discord embed builder + sender for stock-bot.

Pure presentation layer. Detection lives in monitor.py / insiders.py; those
modules hand structured dicts to the builders here, then call send().

Palette is lifted straight from the dashboard:
    emerald  #3FBE7C  buys / cluster buys / healthy
    coral    #E06A57  sells
    blue     #5AA9E6  heartbeat / info
    gold     #E0A23C  big single trade / warning
All builders tolerate missing keys so a schema drift degrades gracefully
instead of throwing inside a GitHub Actions run.
"""

import datetime as _dt
import requests

# ── palette (Discord wants a decimal int) ──────────────────────────────
EMERALD = 0x3FBE7C
CORAL   = 0xE06A57
BLUE    = 0x5AA9E6
GOLD    = 0xE0A23C

_FOOTER_ICON = None  # optional: URL to a 16px logo; leave None for clean text

# ── formatting helpers ─────────────────────────────────────────────────
def _now_iso():
    return _dt.datetime.now(_dt.timezone.utc).isoformat()

def _today():
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d")

def _money(v):
    """Accepts a number or a pre-formatted string; returns a tidy label."""
    if v is None:
        return "—"
    if isinstance(v, str):
        return v
    try:
        v = float(v)
    except (TypeError, ValueError):
        return str(v)
    if abs(v) >= 1_000_000:
        return f"${v/1_000_000:.1f}M"
    if abs(v) >= 1_000:
        return f"${v/1_000:.0f}K"
    return f"${v:,.0f}"

def _field(name, value, inline=True):
    return {"name": name, "value": value if value not in (None, "") else "—",
            "inline": inline}

def _footer(tag):
    f = {"text": f"stock-bot · {tag} · {_today()}"}
    if _FOOTER_ICON:
        f["icon_url"] = _FOOTER_ICON
    return f

def _side(direction):
    """Normalize a direction string to (label, is_buy)."""
    d = (direction or "").lower()
    is_buy = d.startswith("p") or "buy" in d or d.startswith("acq")
    return ("Purchase" if is_buy else "Sale"), is_buy

# ── embed builders ─────────────────────────────────────────────────────
def cluster_embed(cluster):
    """
    cluster: {
      "ticker": "NVDA", "company": "NVIDIA Corp",
      "group": "congress" | "insiders",
      "direction": "purchase" | "sale",
      "buyers": ["Pelosi", "Tuberville", "Khanna"],
      "window_days": 21, "window_max": 30,
      "combined_value": 1_200_000 | "$1.2M–$3.0M" | None,
    }
    """
    label, is_buy = _side(cluster.get("direction"))
    color = EMERALD if is_buy else CORAL
    arrow = "▲" if is_buy else "▼"
    group = (cluster.get("group") or "congress").upper()
    buyers = cluster.get("buyers") or []
    n = len(buyers)
    win = cluster.get("window_days")
    win_max = cluster.get("window_max", 30)

    return {
        "color": color,
        "author": {"name": f"CLUSTER · {group}"},
        "title": f"{arrow} ${cluster.get('ticker','?')} — {n} "
                 f"{'buyers' if is_buy else 'sellers'}"
                 + (f" in {win} days" if win else ""),
        "description": cluster.get("company") or "Same-direction cluster crossed threshold",
        "fields": [
            _field("Names", " · ".join(buyers) if buyers else "—"),
            _field("Window", f"{win} / {win_max} days" if win else f"≤{win_max} days"),
            _field("Combined", _money(cluster.get("combined_value"))),
            _field("Direction", label),
        ],
        "footer": _footer(f"{cluster.get('group','congress')} cluster"),
        "timestamp": _now_iso(),
    }


def trade_embed(trade):
    """
    A single notable trade (big-trade path). trade: {
      "ticker","company","group","name","direction","value","date","url"?
    }
    """
    label, is_buy = _side(trade.get("direction"))
    color = GOLD  # big single trades ride the gold accent to read distinct
    arrow = "▲" if is_buy else "▼"
    group = (trade.get("group") or "insiders").upper()
    e = {
        "color": color,
        "author": {"name": f"BIG TRADE · {group}"},
        "title": f"{arrow} ${trade.get('ticker','?')} — {label}",
        "description": trade.get("company") or "",
        "fields": [
            _field("Who", trade.get("name") or "—"),
            _field("Value", _money(trade.get("value"))),
            _field("Filed", trade.get("date") or "—"),
            _field("Direction", label),
        ],
        "footer": _footer(f"{trade.get('group','insiders')} trade"),
        "timestamp": _now_iso(),
    }
    if trade.get("url"):
        e["url"] = trade["url"]
    return e


def heartbeat_embed(snapshot):
    """
    Daily portfolio + cluster snapshot, matching heartbeat.build_snapshot():
      {
        "healthy": True, "last_run_utc": "08:58",
        "checks_today": 47, "pinged_today": 3, "errors_today": 0,
        "window_days": 14, "buyers_pooled": 9, "names_bought": 6,
        "trades_24h": 7,
        "active_clusters":  [{"ticker":"NVDA","count":3,"group":"congress"}],
        "top_accumulating": [{"ticker":"NVDA","buyers":3,"group":"mixed"}],
      }
    All keys optional; missing data renders as "—"/0 rather than throwing.
    """
    healthy = snapshot.get("healthy", True)
    dot = "🟢" if healthy else "🔴"
    checks = snapshot.get("checks_today", 0)
    last = snapshot.get("last_run_utc", "—")
    win = snapshot.get("window_days", 14)

    clusters = snapshot.get("active_clusters") or []
    cluster_str = "  ·  ".join(
        f"${c.get('ticker','?')} ×{c.get('count','?')} ({c.get('group','?')})"
        for c in clusters
    ) or "none active"

    top = snapshot.get("top_accumulating") or []
    top_str = "  ·  ".join(
        f"${t.get('ticker','?')} ×{t.get('buyers','?')}" for t in top[:8]
    ) or "—"

    fields = [
        _field(f"Active clusters ({len(clusters)})", cluster_str, inline=False),
        _field(f"Accumulating (last {win}d)", top_str, inline=False),
        _field("Pooled", f"{snapshot.get('buyers_pooled', 0)} buyers / "
                         f"{snapshot.get('names_bought', 0)} names"),
        _field("Last 24h", f"+{snapshot.get('trades_24h', 0)} trades"),
        _field("Alerts today", str(snapshot.get("pinged_today", 0))),
    ]
    errs = snapshot.get("errors_today", 0)
    if errs:
        fields.append(_field("Source errors", str(errs)))

    status = ("Healthy" if healthy else "Stale — check last run")
    return {
        "color": BLUE if healthy else CORAL,
        "author": {"name": "DAILY HEARTBEAT"},
        "title": "Portfolio & active clusters",
        "description": f"{dot}  {status} · {checks} checks today · last run {last} UTC",
        "fields": fields,
        "footer": _footer("heartbeat"),
        "timestamp": _now_iso(),
    }

# ── sender ─────────────────────────────────────────────────────────────
def send(webhook_url, embeds, content=None):
    """
    POST one or more embeds. Discord caps at 10 embeds per message, so we
    batch. `content` is optional plain text (use for <@user_id> pings).
    Returns True if every batch returned 2xx.
    """
    if isinstance(embeds, dict):
        embeds = [embeds]
    ok = True
    for i in range(0, len(embeds), 10):
        payload = {"embeds": embeds[i:i + 10]}
        if content and i == 0:
            payload["content"] = content
        try:
            r = requests.post(webhook_url, json=payload, timeout=15)
            if r.status_code == 429:  # rate limited — respect retry_after
                wait = r.json().get("retry_after", 1)
                import time; time.sleep(float(wait) + 0.25)
                r = requests.post(webhook_url, json=payload, timeout=15)
            ok = ok and r.ok
        except requests.RequestException:
            ok = False
    return ok


# ── quick self-test: `python notify.py <webhook_url>` sends live samples ─
if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("usage: python notify.py <discord_webhook_url>")
        raise SystemExit(1)
    url = sys.argv[1]
    send(url, [
        cluster_embed({
            "ticker": "NVDA", "company": "NVIDIA Corp", "group": "congress",
            "direction": "purchase",
            "buyers": ["Pelosi", "Tuberville", "Khanna"],
            "window_days": 21, "window_max": 30, "combined_value": 1_800_000,
        }),
        heartbeat_embed({
            "healthy": True, "checks_today": 47, "last_run_utc": "08:58",
            "pinged_today": 3, "errors_today": 0, "window_days": 14,
            "buyers_pooled": 9, "names_bought": 6, "trades_24h": 7,
            "active_clusters": [{"ticker": "NVDA", "count": 3, "group": "congress"},
                                {"ticker": "LMT", "count": 2, "group": "insiders"}],
            "top_accumulating": [{"ticker": "NVDA", "buyers": 3, "group": "mixed"},
                                 {"ticker": "MSFT", "buyers": 2, "group": "congress"}],
        }),
    ])
    print("sent.")
