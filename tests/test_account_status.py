"""Tests for the live status/Telegram reporting layer.

No MT5 terminal, no network: `send_telegram` is stubbed and the gateway is
faked. These lock in the two things that must not silently break — the
credential reader and the strategy-state renderer — because a failure in
either means trading alerts stop arriving with no visible error.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from execution import account_status as st
from execution import scheduled_daily as sd


BRIDGE_SRC = '''\
BOT_TOKEN = "123456:AAFakeTokenForTests"
CHAT_ID = "6436300996"
'''


@pytest.fixture
def bridge(tmp_path, monkeypatch):
    p = tmp_path / "bridge.py"
    p.write_text(BRIDGE_SRC, encoding="utf-8")
    monkeypatch.setattr(st, "BRIDGE", p)
    return p


class TestCredentials:
    def test_reads_token_and_chat_id(self, bridge):
        token, chat_id = st._load_creds()
        assert token == "123456:AAFakeTokenForTests"
        assert chat_id == "6436300996"

    def test_missing_file_is_a_hard_error(self, tmp_path, monkeypatch):
        monkeypatch.setattr(st, "BRIDGE", tmp_path / "nope.py")
        with pytest.raises(OSError):
            st._load_creds()

    def test_malformed_bridge_fails_closed(self, tmp_path, monkeypatch):
        # Never fall back to a default token/chat — that would post to the
        # wrong chat, or silently swallow the failure.
        p = tmp_path / "bridge.py"
        p.write_text("# no credentials here\n", encoding="utf-8")
        monkeypatch.setattr(st, "BRIDGE", p)
        with pytest.raises(SystemExit):
            st._load_creds()


class TestSendTelegram:
    def test_posts_to_the_bot_api(self, bridge, monkeypatch):
        seen = {}

        class _Resp:
            def read(self):
                return b'{"ok": true, "description": "Message sent"}'

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake_urlopen(url, data=None, timeout=None):
            seen["url"] = url
            seen["body"] = data.decode()
            return _Resp()

        monkeypatch.setattr(st.urllib.request, "urlopen", fake_urlopen)
        ok, desc = st.send_telegram("hello")
        assert ok and desc == "Message sent"
        assert seen["url"] == (
            "https://api.telegram.org/bot123456:AAFakeTokenForTests/sendMessage")
        assert "chat_id=6436300996" in seen["body"]

    def test_network_error_reported_not_raised(self, bridge, monkeypatch):
        def boom(*a, **k):
            raise OSError("connection reset")

        monkeypatch.setattr(st.urllib.request, "urlopen", boom)
        ok, desc = st.send_telegram("hello")
        assert ok is False and "connection reset" in desc


class TestStrategyState:
    """The renderer must degrade to an error line, never raise, and must
    distinguish a genuine WAIT from a load failure."""

    def test_no_signal_renders_wait(self):
        assert st._strategy_state("V11 D1", lambda: None, lambda df: None) == (
            "V11 D1 : WAIT (no signal on last closed bar)")

    def test_signal_renders_buy_with_metrics(self):
        out = st._strategy_state(
            "V12 H1", lambda: None,
            lambda df: {"body_pct": 0.97, "atr": 12.5},
        )
        assert "BUY SIGNAL" in out and "0.97" in out and "12.5" in out

    def test_load_failure_degrades_to_error_line(self):
        def explode():
            raise ValueError("no rates")

        out = st._strategy_state("V11 D1", explode, lambda df: None)
        assert out.startswith("V11 D1 : ERROR")
        assert "ValueError" in out


class TestScheduledLiveExitCode:
    """A silently undelivered trade update is worse than a noisy failure, so
    `scheduled_live.main` must exit non-zero when Telegram send fails."""

    @staticmethod
    def _run(monkeypatch, send, version="V12"):
        """Run scheduled_live.main with the terminal + network stubbed out.
        `send` stubs send_telegram: return (ok, desc) or raise to simulate
        a transport failure."""
        import execution.scheduled_live as sl

        monkeypatch.setattr(sys, "argv", ["scheduled_live.py", version])
        monkeypatch.setattr(sl, "_run", lambda script: "decision : WAIT")
        monkeypatch.setattr(st, "collect", lambda: ("STATUS", {}), raising=False)
        monkeypatch.setattr(st, "send_telegram", send, raising=False)
        monkeypatch.setitem(sys.modules, "execution.account_status", st)
        return sl.main()

    def test_zero_on_success(self, monkeypatch):
        assert self._run(monkeypatch, lambda text: (True, "Message sent")) == 0

    def test_nonzero_when_send_reports_failure(self, monkeypatch):
        assert self._run(monkeypatch, lambda text: (False, "OSError: timeout")) != 0

    def test_nonzero_when_send_raises(self, monkeypatch):
        def boom(text):
            raise RuntimeError("no network stack")

        assert self._run(monkeypatch, boom) != 0

    @pytest.mark.parametrize("version", ["V11", "V12"])
    def test_known_versions_accepted(self, monkeypatch, version):
        assert self._run(monkeypatch, lambda text: (True, "ok"), version) == 0

    def test_unknown_version_is_rejected(self, monkeypatch):
        assert self._run(monkeypatch, lambda text: (True, "ok"), "V99") == 2


class TestDeliver:
    """`deliver` is the single exit-code contract shared by every scheduled
    entrypoint. It must never raise and must never report success falsely."""

    @staticmethod
    def _call(monkeypatch, send):
        monkeypatch.setattr(st, "send_telegram", send, raising=False)
        return st.deliver("MSG", "V12")

    def test_zero_and_sent(self, monkeypatch, capsys):
        assert self._call(monkeypatch, lambda t: (True, "Message sent")) == 0
        assert "V12 Telegram: sent" in capsys.readouterr().out

    def test_nonzero_on_reported_failure(self, monkeypatch, capsys):
        code = self._call(monkeypatch, lambda t: (False, "OSError: timeout"))
        assert code != 0
        assert "FAILED" in capsys.readouterr().out

    def test_raise_is_contained_and_nonzero(self, monkeypatch, capsys):
        def boom(t):
            raise RuntimeError("no network stack")

        assert self._call(monkeypatch, boom) != 0
        out = capsys.readouterr().out
        assert "RuntimeError" in out and "no network stack" in out


class TestDailyJournal:
    """recent_activity must never let a malformed journal kill the update —
    a corrupt line degrades to a marker, it does not raise."""

    @staticmethod
    def _journal(monkeypatch, tmp_path, text):
        p = tmp_path / "journal.jsonl"
        p.write_text(text, encoding="utf-8")
        monkeypatch.setattr(sd, "JOURNAL", p)
        return p

    def test_missing_journal(self, monkeypatch, tmp_path):
        monkeypatch.setattr(sd, "JOURNAL", tmp_path / "nope.jsonl")
        assert sd.recent_activity() == ["journal: not created yet"]

    def test_empty_journal(self, monkeypatch, tmp_path):
        self._journal(monkeypatch, tmp_path, "")
        assert sd.recent_activity() == ["  (journal empty)"]

    def test_tails_most_recent(self, monkeypatch, tmp_path):
        lines = "".join(
            json.dumps({"ts": f"2026-09-2{i}T00:00:00",
                        "action": f"A{i}", "reason": "r"}) + "\n"
            for i in range(1, 6)
        )
        self._journal(monkeypatch, tmp_path, lines)
        out = sd.recent_activity(limit=2)
        assert len(out) == 2 and "A4" in out[0] and "A5" in out[1]

    def test_corrupt_line_degrades_not_raises(self, monkeypatch, tmp_path):
        self._journal(monkeypatch, tmp_path, "{not json}\n")
        assert "unparseable" in sd.recent_activity()[0]

    def test_blank_lines_skipped(self, monkeypatch, tmp_path):
        good = json.dumps({"ts": "2026-09-25T00:00:00",
                           "action": "WAIT", "reason": "no signal"})
        self._journal(monkeypatch, tmp_path, f"\n\n{good}\n\n")
        out = sd.recent_activity()
        assert len(out) == 1 and "WAIT" in out[0] and "no signal" in out[0]
