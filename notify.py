"""
notify.py — the single Discord presentation layer for stock-bot.

Every module (monitor / insiders / edgar_watch / heartbeat) builds plain dicts
and hands them to the builders here, then calls send(). Nothing else in the
codebase should construct an embed or POST to Discord — style lives in one
place so the whole bot stays consistent with the dashboard.

Palette lifted straight from the dashboard:
    emerald  #3FBE7C  buys · cluster buys · healthy
    coral    #E06A57  sells · errors
    blue     #5AA9E6  heartbeat · funds · notices
    gold     #E0A23C  big single trades · warnings
    violet   #8F88F0  activist stakes
Builders tolerate missing keys so a schema drift degrades gracefully instead
of throwing inside a GitHub Actions run.
"""

import datetime as _dt
import time as _time
import requests

# ── palette (Discord wants a decimal int) ──────────────────────────────
EMERALD = 0x3FBE7C
CORAL   = 0xE06A57
BLUE    = 0x5AA9E6
GOLD    = 0xE0A23C
VIOLET  = 0x8F88F0

_FOOTER_ICON = None  # optional URL to a 16px logo; None keeps the footer clean

# ── formatting helpers ─────────────────────────────────────────────────
def _now_iso():
    return _dt.datetime.now(_dt.timezone.utc).isoformat()

def _today():
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d")

def _money(v):
    """A number or a pre-formatted string -> a tidy label."""
    if v is None or v == "":
        return "—"
    if isinstance(v, str):
        return v
    try:
        v = float(v)
    except (TypeError, ValueError):
        return str(v)
    if abs(v) >= 1_000_000_000:
        return f"${v/1_000_000_000:.1f}B"
    if abs(v) >= 1_000_000:
        return f"${v/1_000_000:.1f}M"
    if abs(v) >= 1_000:
        return f"${v/1_000:.0f}K"
    return f"${v:,.0f}"

def _field(name, value, inline=True):
    return {"name": name, "value": (value if value not in (None, "") else "—"),
            "inline": inline}

def _footer(tag):
    f = {"text": f"stock-bot · {tag} · {_today()}"}
    if _FOOTER_ICON:
        f["icon_url"] = _FOOTER_ICON
    return f

def _is_buy(direction):
    d = (direction or "").lower()
    return d.startswith("p") or d.startswith("b") or d.startswith("acq")

def _side_label(direction):
    return "Purchase" if _is_buy(direction) else "Sale"

# ── embed builders ─────────────────────────────────────────────────────
def trade_embed(t):
    """
    A single disclosed trade (congress or insider). t: {
      "group": "congress" | "insiders",
      "ticker","company","name",
      "subtitle": chamber ("Senate"/"House") or role ("CFO", "Director"),
      "direction": "buy"|"purchase"|"sell"|"sale",
      "value": number|str|None, "date": "YYYY-MM-DD", "url"?, "big"?: bool
    }
    Buys ride emerald, sells coral. A big insider trade rides gold + a
    BIG TRADE label so it reads apart from routine pings.
    """
    buy = _is_buy(t.get("direction"))
    big = bool(t.get("big"))
    color = GOLD if big else (EMERALD if buy else CORAL)
    arrow = "▲" if buy else "▼"
    group = (t.get("group") or "congress").upper()
    tag = "BIG TRADE" if big else "TRADE"
    e = {
        "color": color,
        "author": {"name": f"{tag} · {group}"},
        "title": f"{arrow} ${t.get('ticker') or '—'} · {_side_label(t.get('direction'))}",
        "description": t.get("company") or "",
        "fields": [
            _field("Who", t.get("name") or "—"),
            _field("Role" if group == "INSIDERS" else "Chamber", t.get("subtitle") or "—"),
            _field("Value", _money(t.get("value"))),
            _field("Date", t.get("date") or "—"),
        ],
        "footer": _footer(f"{(t.get('group') or 'congress')} trade"),
        "timestamp": _now_iso(),
    }
    if t.get("url"):
        e["url"] = t["url"]
    return e


def cluster_embed(c):
    """
    Multiple people, same stock, same direction. c: {
      "ticker","company","group": "congress"|"insiders",
      "direction": "buy"|"sell",
      "buyers": ["Pelosi","Tuberville","Khanna"],
      "window_days": 14, "combined_value": number|str|None
    }
    """
    buy = _is_buy(c.get("direction"))
    color = EMERALD if buy else CORAL
    arrow = "▲" if buy else "▼"
    group = (c.get("group") or "congress").upper()
    people = c.get("buyers") or []
    n = len(people)
    win = c.get("window_days", 14)
    noun = "buyers" if buy else "sellers"
    return {
        "color": color,
        "author": {"name": f"CLUSTER · {group}"},
        "title": f"{arrow} ${c.get('ticker') or '—'} — {n} {noun} in {win} days",
        "description": c.get("company") or "Same-direction cluster crossed threshold",
        "fields": [
            _field("Names", " · ".join(people) if people else "—", inline=False),
            _field("Window", f"≤ {win} days"),
            _field("Combined", _money(c.get("combined_value"))),
            _field("Direction", _side_label(c.get("direction"))),
        ],
        "footer": _footer(f"{(c.get('group') or 'congress')} cluster"),
        "timestamp": _now_iso(),
    }


def activist_embed(ev):
    """
    5%+ stake filing (SC 13D/G). ev: {
      "filer","company","ticker","pct","type": "13D"|"13D/A"|..., "url"?
    }
    """
    amend = "/A" in (ev.get("type") or "")
    return {
        "color": VIOLET,
        "author": {"name": "ACTIVIST · STAKE" + (" UPDATE" if amend else "")},
        "title": f"◆ ${ev.get('ticker') or ''} {ev.get('company') or 'Undisclosed company'}".strip(),
        "description": f"**{ev.get('filer') or 'Unknown filer'}** filed {ev.get('type') or '13D'}",
        "fields": [
            _field("Stake", f"{ev['pct']}% of class" if ev.get("pct") else "—"),
            _field("Form", ev.get("type") or "13D"),
        ] + ([_field("Filing", f"[open ↗]({ev['url']})")] if ev.get("url") else []),
        "footer": _footer("activist filing"),
        "timestamp": _now_iso(),
    }


def fund_embed(f):
    """
    13F quarterly moves for one manager. f: {
      "fund","period","aum": number, "lines": ["NEW · Acme · $80M", ...], "url"?
    }
    """
    return {
        "color": BLUE,
        "author": {"name": "FUND · 13F"},
        "title": f"◇ {f.get('fund') or 'Fund'}",
        "description": (f"Quarter ending {f.get('period') or '—'} · "
                        f"{_money(f.get('aum'))} AUM"),
        "fields": [
            _field("Moves", "\n".join(f.get("lines") or []) or "—", inline=False),
        ] + ([_field("Filing", f"[open ↗]({f['url']})")] if f.get("url") else []),
        "footer": _footer("fund 13F"),
        "timestamp": _now_iso(),
    }


def heartbeat_embed(s):
    """
    Daily portfolio snapshot — a Discord render of the dashboard's model
    portfolio (recent buys pooled, ranked by how many distinct people are
    buying each name). s: {
      "healthy": bool, "last_run_utc": "08:58",
      "pinged_today": int, "errors_today": int, "window_days": 14,
      "buyers_pooled": int, "names_bought": int, "trades_24h": int,
      "active_clusters": [{"ticker","count","group"}],
      "top_accumulating": [{"ticker","buyers","group"}],
    }
    """
    healthy = s.get("healthy", True) and not s.get("errors_today")
    dot = "🟢" if healthy else "🔴"
    status = "Healthy" if healthy else "Attention — errors today"

    clusters = s.get("active_clusters") or []
    cl_str = "   ".join(f"`${c.get('ticker','?')} ×{c.get('count','?')}`"
                        for c in clusters) or "_none active_"

    top = s.get("top_accumulating") or []
    top_lines = "\n".join(
        f"`${r.get('ticker',''):<6}` ×{r.get('buyers','?')}  · {r.get('group','')}"
        for r in top[:8]) or "_no recent buys_"

    win = s.get("window_days", 14)
    desc = (f"{dot} {status} · last run {s.get('last_run_utc','—')} UTC · "
            f"{s.get('pinged_today', 0)} alerts today")
    return {
        "color": BLUE if healthy else CORAL,
        "author": {"name": "DAILY HEARTBEAT"},
        "title": "Portfolio & active clusters",
        "description": desc,
        "fields": [
            _field(f"Active clusters ({len(clusters)})", cl_str, inline=False),
            _field(f"Top accumulating · last {win}d", top_lines, inline=False),
            _field("Pooled", f"{s.get('buyers_pooled', 0)} buyers · "
                             f"{s.get('names_bought', 0)} names"),
            _field("Last 24h", f"+{s.get('trades_24h', 0)} buys"),
        ],
        "footer": _footer("heartbeat"),
        "timestamp": _now_iso(),
    }


def notice_embed(title, body, kind="info"):
    """Small utility embed for init / overflow / errors. kind in
    {'info','warn','error'}."""
    color = {"info": BLUE, "warn": GOLD, "error": CORAL}.get(kind, BLUE)
    mark = {"info": "•", "warn": "▲", "error": "■"}.get(kind, "•")
    return {
        "color": color,
        "author": {"name": "STATUS"},
        "title": f"{mark} {title}",
        "description": body or "",
        "footer": _footer(kind),
        "timestamp": _now_iso(),
    }

# ── sender ─────────────────────────────────────────────────────────────
def send(webhook_url, embeds, content=None):
    """
    POST one or more embeds. Discord caps 10 embeds per message, so batch.
    `content` is optional plain text — use it for the <@user_id> ping, only
    on the first batch. Returns True if every batch returned 2xx. If
    webhook_url is falsy, prints what it would have sent (safe for dry runs).
    """
    if isinstance(embeds, dict):
        embeds = [embeds]
    if not embeds:
        return True
    if not webhook_url:
        print("notify: no webhook set; would send", len(embeds), "embed(s)")
        return True
    ok = True
    for i in range(0, len(embeds), 10):
        payload = {"embeds": embeds[i:i + 10]}
        if content and i == 0:
            payload["content"] = content
        try:
            r = requests.post(webhook_url, json=payload, timeout=15)
            if r.status_code == 429:
                wait = r.json().get("retry_after", 1)
                _time.sleep(float(wait) + 0.25)
                r = requests.post(webhook_url, json=payload, timeout=15)
            ok = ok and r.ok
        except requests.RequestException as e:
            print(f"notify: send failed ({e})")
            ok = False
        _time.sleep(0.3)  # stay friendly with webhook rate limits
    return ok


# ── self-test: `python notify.py <webhook_url>` fires one of each ───────
if __name__ == "__main__":
    import sys
    url = sys.argv[1] if len(sys.argv) > 1 else ""
    send(url, [
        cluster_embed({"ticker": "NVDA", "company": "NVIDIA Corp",
                       "group": "congress", "direction": "buy",
                       "buyers": ["Pelosi", "Tuberville", "Khanna"],
                       "window_days": 14, "combined_value": 1_800_000}),
        trade_embed({"group": "insiders", "ticker": "ELV",
                     "company": "Elevance Health", "name": "Boudreaux Gail K",
                     "subtitle": "CEO", "direction": "buy",
                     "value": 420_000, "date": "2026-07-19", "big": True}),
        heartbeat_embed({"healthy": True, "last_run_utc": "08:58",
                         "pinged_today": 3, "errors_today": 0, "window_days": 14,
                         "buyers_pooled": 84, "names_bought": 90, "trades_24h": 7,
                         "active_clusters": [{"ticker": "NVDA", "count": 3, "group": "mixed"},
                                             {"ticker": "LMT", "count": 2, "group": "congress"}],
                         "top_accumulating": [{"ticker": "EWSB", "buyers": 4, "group": "insiders"},
                                              {"ticker": "GLOO", "buyers": 4, "group": "insiders"},
                                              {"ticker": "NVDA", "buyers": 3, "group": "mixed"}]}),
    ])
    print("sent." if url else "dry run (pass a webhook URL to actually send).")
