"""Tests for research/send_deploy_report.py.

The one property worth testing is the delivery contract: exit 0 means
delivered, non-zero means NOT delivered. A report script that swallows a
failed send is worse than no script, because it reads as confirmation.

The network is always stubbed. Nothing here sends a real message.
"""
from __future__ import annotations

import io
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from research import send_deploy_report as S


class _Resp:
    def __init__(self, body: str):
        self._body = body

    def read(self) -> bytes:
        return self._body.encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def no_creds(monkeypatch):
    monkeypatch.setattr(S, "creds", lambda: ("TOKEN", "CHAT"))


class TestDeliveryContract:
    def test_success_exits_zero(self, no_creds, monkeypatch, capsys):
        monkeypatch.setattr(S.urllib.request, "urlopen",
                            lambda *a, **k: _Resp('{"ok":true}'))
        assert S.main() == 0
        assert "Telegram: sent" in capsys.readouterr().out

    def test_api_rejection_exits_nonzero(self, no_creds, monkeypatch, capsys):
        """Telegram answering 200 with ok:false is a FAILED delivery, and
        must not read as success."""
        monkeypatch.setattr(S.urllib.request, "urlopen",
                            lambda *a, **k: _Resp('{"ok":false,"description":"chat not found"}'))
        assert S.main() != 0
        assert "FAILED" in capsys.readouterr().out

    def test_network_error_exits_nonzero_and_never_raises(self, no_creds,
                                                          monkeypatch, capsys):
        def boom(*a, **k):
            raise OSError("network unreachable")
        monkeypatch.setattr(S.urllib.request, "urlopen", boom)
        assert S.main() == 1          # not an exception
        assert "TELEGRAM FAILED" in capsys.readouterr().out

    def test_spaced_ok_is_still_ok(self, no_creds, monkeypatch):
        """The API emits '{"ok": true}' with a space; the parser normalises."""
        monkeypatch.setattr(S.urllib.request, "urlopen",
                            lambda *a, **k: _Resp('{"ok": true, "result": {}}'))
        assert S.main() == 0

    def test_stdout_never_empty_on_failure(self, no_creds, monkeypatch, capsys):
        """An empty stdout reads as silence. Every path must speak."""
        for body in ('{"ok":false}', '{"ok":true}'):
            monkeypatch.setattr(S.urllib.request, "urlopen",
                                lambda *a, **k: _Resp(body))
            S.main()
            assert capsys.readouterr().out.strip()


class TestReport:
    def test_report_is_valid_html_for_telegram(self):
        """Unescaped '&' or bare '<' makes Telegram reject the whole
        message — the failure mode this test exists to catch."""
        import html
        from html.parser import HTMLParser

        class P(HTMLParser):
            def __init__(self):
                super().__init__()
                self.tags = []

            def handle_starttag(self, tag, attrs):
                self.tags.append(tag)

        p = P()
        p.feed(S.REPORT)
        assert p.tags, "report parsed to nothing"
        # every '&' must start a valid entity
        for m in re.finditer(r"&(?!amp;|lt;|gt;|quot;|#\d+;|apos;)\S{0,6}", S.REPORT):
            pytest.fail(f"unescaped ampersand: ...{m.group(0)[:20]}")

    def test_mentions_the_safety_split(self):
        """The report's whole point is the demo/live separation."""
        assert "demo_execution_enabled" in S.REPORT
        assert "live_trading_enabled" in S.REPORT

    def test_states_what_is_not_verified(self):
        """Honesty check: the report must not imply the live path was
        exercised."""
        assert "NOT verified" in S.REPORT


class TestNoSecrets:
    def test_no_token_committed(self):
        src = (ROOT / "research" / "send_deploy_report.py").read_text(encoding="utf-8")
        # a real bot token looks like 1234567890:AA... — reject any literal
        assert not re.search(r"\b\d{8,12}:[A-Za-z0-9_-]{30,}", src), \
            "a literal bot token appears to be committed"

    def test_credentials_read_at_call_time(self):
        """Read from the bridge file at call time, never baked in."""
        src = (ROOT / "research" / "send_deploy_report.py").read_text(encoding="utf-8")
        assert "hermes_telegram_bridge" in src
        assert "BOT_TOKEN" in src and "CHAT_ID" in src

    def test_prints_nothing_secret(self, no_creds, monkeypatch, capsys):
        captured = {}

        def spy(url, *a, **k):
            captured["url"] = url
            return _Resp('{"ok":true}')
        monkeypatch.setattr(S.urllib.request, "urlopen", spy)
        S.main()
        out = capsys.readouterr().out
        assert "TOKEN" not in out and "CHAT" not in out
