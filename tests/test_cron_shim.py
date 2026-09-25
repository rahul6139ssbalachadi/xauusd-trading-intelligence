"""Tests for the shared cron entry shim (~/.hermes/scripts/_trading_cron.py).

No MT5, no network: subprocess.run is stubbed. The invariant under test is
the one that makes a broken cron job visible — a no_agent job with empty
stdout is treated as SILENCE, so `run` must never return an empty string.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(r"D:\rahul_ai\hermes\scripts")
sys.path.insert(0, str(SCRIPTS))

_trading_cron = pytest.importorskip("_trading_cron")


class _P:
    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout, self.stderr, self.returncode = stdout, stderr, returncode


@pytest.fixture
def fake_proc(monkeypatch):
    seen = {}

    def _install(proc):
        seen["proc"] = proc
        seen["cmd"] = None

        def fake_run(cmd, **kw):
            seen["cmd"] = cmd
            seen["cwd"] = kw.get("cwd")
            return proc

        monkeypatch.setattr(subprocess, "run", fake_run)
        return seen

    return _install


class TestJobTable:
    def test_all_three_jobs_registered(self):
        assert set(_trading_cron.JOBS) == {"V11", "V12", "DAILY"}

    def test_version_jobs_pass_version_arg(self, fake_proc):
        seen = fake_proc(_P(stdout="ok"))
        _trading_cron.run("V11")
        assert seen["cmd"][-1] == "V11"
        assert seen["cmd"][-2].endswith("scheduled_live.py")

    def test_daily_job_passes_no_arg(self, fake_proc):
        seen = fake_proc(_P(stdout="ok"))
        _trading_cron.run("DAILY")
        assert seen["cmd"][-1].endswith("scheduled_daily.py")
        assert len(seen["cmd"]) == 2  # python + script only

    def test_runs_from_repo_root(self, fake_proc):
        seen = fake_proc(_P(stdout="ok"))
        _trading_cron.run("V12")
        assert seen["cwd"] == _trading_cron.REPO

    def test_unknown_job_raises(self):
        with pytest.raises(KeyError):
            _trading_cron.run("V99")


class TestNeverEmpty:
    """Empty stdout == silent cron job. Every branch must return text."""

    def test_stdout_returned_verbatim(self, fake_proc):
        fake_proc(_P(stdout="V11 LIVE CHECK\n\n"))
        assert _trading_cron.run("V11") == "V11 LIVE CHECK"

    def test_empty_stdout_synthesizes_failure_body(self, fake_proc):
        fake_proc(_P(stdout="   \n", stderr="Traceback: boom", returncode=1))
        out = _trading_cron.run("V11")
        assert out and "no output" in out and "exit 1" in out
        assert "Traceback: boom" in out

    def test_missing_stdout_attribute_handled(self, fake_proc):
        fake_proc(_P(stdout=None, stderr="", returncode=3))
        assert _trading_cron.run("DAILY")

    def test_stderr_tail_is_bounded(self, fake_proc):
        fake_proc(_P(stdout=None, stderr="E" * 5000, returncode=1))
        assert len(_trading_cron.run("V12")) < 2000
