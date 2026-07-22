"""
test_phase2.py — wiring fixes: env vars, single heartbeat, embed schema.
Offline (Discord + network mocked). Run: pytest -q test_phase2.py
"""

import json
import os
import shutil
import sys

import pytest

SRC = os.path.dirname(os.path.abspath(__file__))
FILES = ("monitor.py", "congress_sources.py", "roster.py", "heartbeat.py",
         "notify.py", "insiders.py", "edgar_watch.py", "prices.py",
         "config.yaml", "roster.yaml")


@pytest.fixture
def repo(tmp_path, monkeypatch):
    for f in FILES:
        shutil.copy(os.path.join(SRC, f), tmp_path / f)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://example.invalid/hook")
    monkeypatch.delenv("WEBHOOK_URL", raising=False)
    monkeypatch.delenv("DISCORD_USER_ID", raising=False)
    monkeypatch.syspath_prepend(str(tmp_path))
    for m in ("monitor", "congress_sources", "roster", "heartbeat",
              "notify", "insiders", "edgar_watch", "prices"):
        sys.modules.pop(m, None)
    return tmp_path


# ── env-var wiring ─────────────────────────────────────────────────────

def test_insiders_and_edgar_pick_up_discord_webhook(repo):
    import insiders, edgar_watch
    assert insiders.WEBHOOK_URL == "https://example.invalid/hook"
    assert edgar_watch.WEBHOOK_URL == "https://example.invalid/hook"


def test_user_id_falls_back_to_config_yaml(repo):
    import insiders, edgar_watch
    # config.yaml in this repo carries discord.user_id
    assert insiders.discord_user_id().isdigit()
    assert edgar_watch.discord_user_id().isdigit()


def test_user_id_env_overrides_config(repo, monkeypatch):
    monkeypatch.setenv("DISCORD_USER_ID", "12345")
    import insiders
    assert insiders.discord_user_id() == "12345"


# ── single heartbeat ───────────────────────────────────────────────────

def test_monitor_no_longer_sends_heartbeat(repo):
    import monitor
    assert not hasattr(monitor, "maybe_heartbeat")


def test_heartbeat_end_to_end(repo, monkeypatch):
    """monitor increments counters -> heartbeat reads them, sends once,
    resets them, and skips on the second call the same day."""
    import monitor, heartbeat, notify

    sen = {"chamber": "Senate", "person": "Nancy Pelosi", "ticker": "NVDA",
           "asset": "NVIDIA Corporation", "side": "buy",
           "amount": "$1,000,001 - $5,000,000", "owner": "Spouse",
           "transaction_date": "2026-07-20", "disclosure_date": "2026-07-21",
           "link": "https://efdsearch.senate.gov/search/view/ptr/abc/"}
    monkeypatch.setattr(monitor.congress_sources, "fetch_senate",
                        lambda **kw: ([dict(sen)], [sen["link"]]))
    monkeypatch.setattr(monitor.congress_sources, "fetch_house",
                        lambda **kw: ([], []))
    monkeypatch.setattr(monitor, "discord_post", lambda p: None)
    monkeypatch.setattr(monitor.time, "sleep", lambda *_: None)
    monitor.main()                                   # seed run writes ledger

    sent = []
    monkeypatch.setattr(notify, "send",
                        lambda url, embeds, content=None: sent.append(embeds))
    monkeypatch.setenv("HEARTBEAT_FORCE", "1")
    assert heartbeat.main() == 0
    assert len(sent) == 1
    embed = sent[0] if isinstance(sent[0], dict) else sent[0][0]

    # Snapshot data actually lands in the embed (schema reconciled).
    text = json.dumps(embed)
    assert "checks today" in text                    # description renders counters
    assert "NVDA" in text                            # accumulation shows the buy
    assert "Accumulating" in text and "Pooled" in text

    # Counters reset + date stamped in the SHARED heartbeat key.
    with open("state.json") as f:
        hb = json.load(f)["heartbeat"]
    assert hb["checks"] == 0 and hb["trades_found"] == 0 and hb["errors"] == 0
    assert hb["last_sent_date"]

    # Same day, no force -> skip (monitor didn't pre-stamp it; heartbeat did).
    monkeypatch.delenv("HEARTBEAT_FORCE")
    heartbeat.main()
    assert len(sent) == 1


def test_monitor_counters_feed_snapshot(repo):
    import heartbeat
    state = {"heartbeat": {"checks": 7, "trades_found": 3, "errors": 1},
             "congress": {"recent_trades": []}, "insiders": {"recent_trades": []}}
    snap = heartbeat.build_snapshot(state, {})
    assert snap["checks_today"] == 7
    assert snap["pinged_today"] == 3
    assert snap["errors_today"] == 1


# ── embed builder tolerance ────────────────────────────────────────────

def test_heartbeat_embed_tolerates_empty_snapshot(repo):
    import notify
    e = notify.heartbeat_embed({})
    assert e["title"] and e["fields"]                # degrades, doesn't throw


def test_import_notify_lowercase(repo):
    import notify                                    # fails if file is Notify.py
    assert callable(notify.heartbeat_embed)
