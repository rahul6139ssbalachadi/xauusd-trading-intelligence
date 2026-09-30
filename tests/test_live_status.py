"""Tests for api/live_status.py and the live dashboard endpoints.

These pin the two properties the dashboard depends on:
  - a dead/absent read service is reported as STALE, never as healthy
  - a malformed journal line cannot 500 the endpoint
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from api import live_status


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """Point every module path at a temp dir. Never touch the real
    journal or the real health file from a test."""
    monkeypatch.setattr(live_status, "ROOT", tmp_path)
    monkeypatch.setattr(live_status, "HEALTH", tmp_path / "run" / "h.json")
    monkeypatch.setattr(live_status, "JOURNAL", tmp_path / "journal" / "s.jsonl")
    monkeypatch.setattr(live_status, "APPROVED", tmp_path / "approved.json")


def _write_health(**kw):
    base = {"connected": True, "last_ok": "2026-09-30T09:00:00+00:00",
            "stale_seconds": 0.0, "stale": False, "positions": 0}
    base.update(kw)
    live_status.HEALTH.parent.mkdir(parents=True, exist_ok=True)
    live_status.HEALTH.write_text(json.dumps(base), encoding="utf-8")


# ----------------------------------------------------------------- health
def test_never_run_is_reported_as_not_running():
    h = live_status.read_health()
    assert h["running"] is False
    assert h["connected"] is False
    assert h["stale"] is True
    assert "never run" in h["detail"]


def test_corrupt_health_file_does_not_raise():
    live_status.HEALTH.parent.mkdir(parents=True, exist_ok=True)
    live_status.HEALTH.write_text("{not json", encoding="utf-8")
    h = live_status.read_health()
    assert h["running"] is False
    assert "unreadable" in h["detail"]


def test_fresh_health_is_not_stale():
    from datetime import datetime, timezone
    _write_health(last_ok=datetime.now(timezone.utc).isoformat())
    h = live_status.read_health()
    assert h["stale"] is False
    assert h["stale_seconds"] < 5


def test_old_health_is_stale():
    """The 33-day silence must never render as 'fresh'."""
    from datetime import datetime, timedelta, timezone
    _write_health(
        last_ok=(datetime.now(timezone.utc) - timedelta(days=33)).isoformat())
    h = live_status.read_health()
    assert h["stale"] is True
    assert h["stale_seconds"] > 2_000_000


def test_missing_last_ok_is_stale():
    _write_health(last_ok="")
    assert live_status.read_health()["stale"] is True


# ---------------------------------------------------------------- signals
def test_no_journal_returns_empty_list():
    assert live_status.read_latest_signals() == []


def test_torn_final_line_does_not_break_reading():
    live_status.JOURNAL.parent.mkdir(parents=True, exist_ok=True)
    live_status.JOURNAL.write_text(
        json.dumps({"strategy": "A", "strategy_number": "V1",
                    "timestamp": "2026-09-30T09:00:00+00:00"}) + "\n"
        + '{"strategy": "B", trunc',          # crashed mid-write
        encoding="utf-8")
    out = live_status.read_latest_signals()
    assert len(out) == 1
    assert out[0]["strategy"] == "A"


def test_latest_per_strategy_wins():
    live_status.JOURNAL.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        {"strategy": "A", "strategy_number": "V11", "direction": "WAIT",
         "timestamp": "2026-09-30T09:00:00+00:00"},
        {"strategy": "A", "strategy_number": "V11", "direction": "BUY",
         "timestamp": "2026-09-30T10:00:00+00:00"},
        {"strategy": "B", "strategy_number": "V12", "direction": "SELL",
         "timestamp": "2026-09-30T08:00:00+00:00"},
    ]
    live_status.JOURNAL.write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    out = live_status.read_latest_signals()
    assert len(out) == 2
    a = [s for s in out if s["strategy"] == "A"][0]
    assert a["direction"] == "BUY", "must show the newest, not the first"


def test_signals_sorted_newest_first():
    live_status.JOURNAL.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        {"strategy": "A", "strategy_number": "V1", "timestamp": "2026-09-01T00:00:00+00:00"},
        {"strategy": "B", "strategy_number": "V2", "timestamp": "2026-09-30T00:00:00+00:00"},
    ]
    live_status.JOURNAL.write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    out = live_status.read_latest_signals()
    assert out[0]["strategy"] == "B"


def test_blocked_signal_is_shown_with_its_reason():
    """A refusal must be visible, with the reason. Silence would be
    indistinguishable from a crashed runner."""
    live_status.JOURNAL.parent.mkdir(parents=True, exist_ok=True)
    live_status.JOURNAL.write_text(json.dumps({
        "strategy": "X", "strategy_number": "V11", "direction": "BLOCKED",
        "entry": None, "confidence": 0.0,
        "reason": "login 900909716957 is forbidden",
        "timestamp": "2026-09-30T09:00:00+00:00"}) + "\n", encoding="utf-8")
    s = live_status.read_latest_signals()[0]
    assert s["direction"] == "BLOCKED"
    assert "forbidden" in s["reason"]
    assert s["entry"] is None


# -------------------------------------------------------------- assembled
def test_status_shape_and_demo_banner():
    s = live_status.status()
    assert set(s) >= {"generated_at", "reader", "approved_strategies",
                      "signals", "positions", "execution"}
    assert s["execution"]["mode"] == "demo"
    assert s["execution"]["live_money"] is False


def test_approved_reads_the_json(tmp_path):
    live_status.APPROVED.write_text(json.dumps({"approved": [
        {"name": "X", "version": "V11", "market": "XAUUSD"}]}), encoding="utf-8")
    assert live_status.read_approved()[0]["version"] == "V11"


def test_corrupt_approved_returns_empty():
    live_status.APPROVED.write_text("{oops", encoding="utf-8")
    assert live_status.read_approved() == []


# ------------------------------------------------------------------- api
def test_live_endpoints_respond():
    """The routes must exist and return valid JSON."""
    from fastapi.testclient import TestClient
    from api.main import app

    c = TestClient(app)
    r = c.get("/api/live")
    assert r.status_code == 200
    d = r.json()
    assert "reader" in d and "signals" in d

    r2 = c.get("/api/live/health")
    assert r2.status_code == 200
    assert "connected" in r2.json()


def test_dashboard_page_is_served():
    from fastapi.testclient import TestClient
    from api.main import app

    c = TestClient(app)
    r = c.get("/live")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "Live Status" in r.text
