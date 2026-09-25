"""Tests for the self-improvement gate (CLAUDE.md §25).

No MT5, no network, no real strategies. The important assertions are the
NEGATIVE ones: that a weak or missing result is rejected, and that the
system cannot promote itself.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from self_improvement import (
    Gates,
    PromotionRefused,
    Proposal,
    Verdict,
    evaluate_gates,
    promote,
    propose,
)


def bt(trades=50, pf=1.5):
    return {"total_trades": trades, "profit_factor": pf, "net_pips": 500.0}


def wf(oos_trades=20, oos_net=200.0, deg=0.1):
    return {
        "n_windows": 4,
        "is_mean_net_pips": 400.0,
        "oos_mean_net_pips": oos_net,
        "oos_total_trades": oos_trades,
        "oos_degradation": deg,
    }


class FakeMC:
    def __init__(self, robust=True, p5=100.0, ruin=0.0):
        self.is_robust = robust
        self.net_p5 = p5
        self.ruin_prob = ruin
        self.n_iterations = 1000
        self.notes = ""


def names(verdict: Verdict) -> set[str]:
    return {r.name for r in verdict.results}


def failed(verdict: Verdict) -> set[str]:
    return {r.name for r in verdict.failures}


class TestGatesConfig:
    def test_rejects_nonsense_thresholds(self):
        with pytest.raises(ValueError):
            Gates(min_trades=0)
        with pytest.raises(ValueError):
            Gates(max_ruin_prob=1.5)
        with pytest.raises(ValueError):
            # Would make any out-of-sample GAIN a failure — nonsense.
            Gates(max_oos_degradation=2.0)


class TestHappyPath:
    def test_strong_candidate_is_accepted(self):
        v = evaluate_gates(bt(), wf(), FakeMC())
        assert v.accepted, v.reason()
        assert failed(v) == set()

    def test_mc_optional_when_disabled(self):
        v = evaluate_gates(bt(), wf(), None, Gates(require_mc_robust=False))
        assert v.accepted, v.reason()


class TestRejections:
    """Every one of these is a real historical failure mode in this repo."""

    def test_rejects_thin_sample(self):
        v = evaluate_gates(bt(trades=5), wf(), FakeMC())
        assert not v.accepted and "sample_size" in failed(v)

    def test_rejects_zero_profit_factor(self):
        # V1-V10 all scored PF 0.00-0.06.
        v = evaluate_gates(bt(pf=0.0), wf(), FakeMC())
        assert not v.accepted and "profit_factor" in failed(v)

    def test_rejects_missing_profit_factor(self):
        v = evaluate_gates({"total_trades": 50}, wf(), FakeMC())
        assert not v.accepted and "profit_factor" in failed(v)

    def test_rejects_zero_oos_trades(self):
        # The exact V1-V10 walk-forward failure: edge vanishes OOS.
        v = evaluate_gates(bt(), wf(oos_trades=0), FakeMC())
        assert not v.accepted and "oos_sample" in failed(v)

    def test_rejects_negative_oos_net(self):
        v = evaluate_gates(bt(), wf(oos_net=-120.0), FakeMC())
        assert not v.accepted and "oos_net" in failed(v)

    def test_rejects_oos_collapse(self):
        v = evaluate_gates(bt(), wf(deg=0.95), FakeMC())
        assert not v.accepted and "oos_degradation" in failed(v)

    def test_rejects_undefined_degradation_conservatively(self):
        # IS net was 0/NaN -> unknown, and unknown is NOT a pass.
        v = evaluate_gates(bt(), wf(deg=float("nan")), FakeMC())
        assert not v.accepted and "oos_degradation" in failed(v)

    def test_rejects_missing_monte_carlo(self):
        v = evaluate_gates(bt(), wf(), None)
        assert not v.accepted and "mc_robust" in failed(v)

    def test_rejects_non_robust_monte_carlo(self):
        v = evaluate_gates(bt(), wf(), FakeMC(robust=False, p5=-500, ruin=1.0))
        assert not v.accepted
        assert {"mc_robust", "ruin_prob"} <= failed(v)

    def test_rejects_high_ruin_even_when_flagged_robust(self):
        # Trust the explicit ruin budget, not just the boolean.
        v = evaluate_gates(bt(), wf(), FakeMC(robust=True, ruin=0.4))
        assert not v.accepted and "ruin_prob" in failed(v)

    def test_all_failure_reasons_are_reported(self):
        v = evaluate_gates(bt(trades=1, pf=0.0), wf(oos_trades=0, oos_net=-1.0),
                           None)
        assert len(v.failures) >= 5
        assert "sample_size" in v.reason()


class TestVerdictSerialisation:
    def test_to_dict_is_json_safe(self):
        v = evaluate_gates(bt(), wf(), FakeMC())
        json.dumps(v.to_dict())  # must not raise
        assert set(v.to_dict()) == {"accepted", "reason", "gates"}


class TestPropose:
    def test_writes_a_pending_proposal(self, tmp_path):
        p = propose("S", "V11", "V13", bt(), wf(), FakeMC(),
                    out_dir=tmp_path, notes="tighter stop")
        assert p.status == "PENDING" and p.approved_by is None
        files = list(tmp_path.glob("*.json"))
        assert len(files) == 1
        assert json.loads(files[0].read_text())["status"] == "PENDING"

    def test_captures_monte_carlo_evidence(self, tmp_path):
        p = propose("S", "V11", "V13", bt(), wf(), FakeMC(), out_dir=tmp_path)
        assert p.evidence["monte_carlo"]["is_robust"] is True

    def test_rejected_candidate_is_still_recorded(self, tmp_path):
        p = propose("S", "V11", "V13", bt(pf=0.0), wf(), FakeMC(),
                    out_dir=tmp_path)
        assert not p.verdict.accepted
        assert len(list(tmp_path.glob("*.json"))) == 1  # kept, not discarded

    def test_never_touches_approved_json(self, tmp_path):
        # propose() has no way to address approved.json at all — not a
        # parameter, not a default it falls back to. That absence is the
        # structural guarantee, so assert it directly.
        import inspect

        assert "approved_path" not in inspect.signature(propose).parameters
        assert "approved" not in inspect.signature(propose).parameters


class TestPromotion:
    def _prop(self, **kw):
        return Proposal(strategy="S", base_version="V11",
                        candidate_version=kw.pop("version", "V13"),
                        created_at="2026-09-25T00:00:00+00:00",
                        verdict=kw.pop("verdict",
                                       evaluate_gates(bt(), wf(), FakeMC())),
                        **kw)

    def test_refuses_without_approver_name(self, tmp_path):
        approved = tmp_path / "approved.json"
        approved.write_text(json.dumps({"approved": []}), encoding="utf-8")
        for bad in ("", "   ", None):
            with pytest.raises(PromotionRefused):
                promote(self._prop(), bad, approved_path=approved)

    def test_refuses_when_gates_failed(self, tmp_path):
        approved = tmp_path / "approved.json"
        approved.write_text(json.dumps({"approved": []}), encoding="utf-8")
        bad = self._prop(verdict=evaluate_gates(bt(pf=0.0), wf(), FakeMC()))
        with pytest.raises(PromotionRefused, match="failed its gates"):
            promote(bad, "rahul", approved_path=approved)

    def test_preserves_existing_approvals(self, tmp_path):
        approved = tmp_path / "approved.json"
        approved.write_text(json.dumps(
            {"approved": [{"name": "OLD", "version": "V1"}]}), encoding="utf-8")
        promote(self._prop(), "rahul", approved_path=approved)
        data = json.loads(approved.read_text())
        assert [a["version"] for a in data["approved"]] == ["V1", "V13"]

    def test_records_the_human_approver(self, tmp_path):
        approved = tmp_path / "approved.json"
        approved.write_text(json.dumps({"approved": []}), encoding="utf-8")
        promote(self._prop(), "  rahul  ", approved_path=approved)
        rec = json.loads(approved.read_text())["approved"][0]
        assert rec["approved_by"] == "rahul"        # trimmed
        assert rec["supersedes"] == "V11"

    def test_refuses_to_overwrite_an_existing_version(self, tmp_path):
        approved = tmp_path / "approved.json"
        approved.write_text(json.dumps(
            {"approved": [{"name": "S", "version": "V13"}]}), encoding="utf-8")
        with pytest.raises(PromotionRefused, match="already approved"):
            promote(self._prop(), "rahul", approved_path=approved)
        assert len(json.loads(approved.read_text())["approved"]) == 1

    def test_promotion_marks_the_proposal(self, tmp_path):
        approved = tmp_path / "approved.json"
        approved.write_text(json.dumps({"approved": []}), encoding="utf-8")
        p = self._prop()
        promote(p, "rahul", approved_path=approved)
        assert p.status == "APPROVED" and p.approved_by == "rahul"


class TestNoSelfPromotion:
    """The structural safety property: nothing in the propose path can
    reach approved.json without an explicit human name."""

    def test_propose_returns_a_pending_proposal(self, tmp_path):
        p = propose("S", "V11", "V13", bt(), wf(), FakeMC(), out_dir=tmp_path)
        assert p.status == "PENDING"
        assert p.approved_by is None

    def test_default_approved_path_untouched_by_propose(self, tmp_path):
        before = (ROOT / "execution" / "approved.json").read_text(encoding="utf-8")
        propose("S", "V11", "V13", bt(), wf(), FakeMC(), out_dir=tmp_path)
        after = (ROOT / "execution" / "approved.json").read_text(encoding="utf-8")
        assert before == after
