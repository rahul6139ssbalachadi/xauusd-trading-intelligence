"""Tests for the read-only `proposals` CLI command (CLAUDE.md §25).

No MT5, no network. Writes only into tmp_path and monkeypatches the module's
PROPOSALS_DIR, so the real execution/proposals/ queue is never touched.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from runner import cli


@pytest.fixture
def queue(tmp_path, monkeypatch):
    from self_improvement import PROPOSALS_DIR

    monkeypatch.setattr("self_improvement.PROPOSALS_DIR", tmp_path)
    monkeypatch.setattr(cli, "json", json)
    return tmp_path


def _write(d: Path, **fields):
    base = {
        "strategy": "XAUUSD_D1_MOMENTUM_BREAKOUT",
        "base_version": "V11",
        "candidate_version": "V13",
        "created_at": "2026-09-25T16:39:44+00:00",
        "status": "PENDING",
        "approved_by": None,
        "notes": "",
        "evidence": {},
        "verdict": {"accepted": True, "reason": "all gates passed", "gates": []},
    }
    base.update(fields)
    d.write_text(json.dumps(base), encoding="utf-8")


class TestProposalsCommand:
    def test_empty_queue_explains_what_to_do(self, queue, capsys):
        cli.cmd_proposals()
        out = capsys.readouterr().out
        assert "No proposals yet" in out and "gate_check_v11" in out

    def test_lists_accepted_candidate(self, queue, capsys):
        _write(queue / "a.json")
        cli.cmd_proposals()
        out = capsys.readouterr().out
        assert "V13" in out and "supersedes V11" in out
        assert "status=PENDING" in out and "gate=ACCEPTED" in out

    def test_lists_rejected_candidate_with_reason(self, queue, capsys):
        _write(queue / "b.json", verdict={
            "accepted": False, "reason": "profit_factor: PF 0.0", "gates": []})
        cli.cmd_proposals()
        out = capsys.readouterr().out
        assert "gate=REJECTED" in out and "PF 0.0" in out

    def test_shows_the_human_approver_when_promoted(self, queue, capsys):
        _write(queue / "c.json", status="APPROVED", approved_by="rahul")
        cli.cmd_proposals()
        assert "approved_by=rahul" in capsys.readouterr().out

    def test_sorts_by_filename(self, queue, capsys):
        for name in ("c.json", "a.json", "b.json"):
            _write(queue / name)
        cli.cmd_proposals()
        names = [ln for ln in capsys.readouterr().out.splitlines() if ".json" in ln]
        assert [n.strip() for n in names] == ["a.json", "b.json", "c.json"]


class TestNoPromotionViaCli:
    """Promotion must stay a deliberate API call, never a CLI subcommand."""

    @staticmethod
    def _help(monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", ["cli", "--help"])
        with pytest.raises(SystemExit):
            cli.main()
        return capsys.readouterr().out

    def test_proposals_is_listed_in_help(self, monkeypatch, capsys):
        assert "proposals" in self._help(monkeypatch, capsys)

    def test_cli_exposes_no_promote_subcommand(self, monkeypatch, capsys):
        # 'promotion' appears in the help prose; the bare verb must not.
        help_text = self._help(monkeypatch, capsys).replace("promotion", "")
        assert "promote" not in help_text
