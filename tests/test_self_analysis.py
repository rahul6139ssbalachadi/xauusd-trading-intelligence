"""Tests for research/self_analysis.py.

The script's job is to feed candidates through self_improvement's §25
gate. The risk it carries is not a crash — it is a WRONG VERDICT, which
would either talk you out of a working strategy or wave a broken one
through. So the tests pin the wiring, not the formatting.
"""
from __future__ import annotations

import io
import re
import sys
import tokenize
from pathlib import Path

import pytest

from backtest import Trade

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from research import self_analysis as SA


# ======================================================================
# The gate wiring
# ======================================================================
class TestGateWiring:
    def test_gate_inputs_use_the_gate_real_key_names(self):
        """evaluate_gates reads total_trades / profit_factor from the
        backtest dict and oos_total_trades / oos_mean_net_pips /
        oos_degradation from the walk-forward dict. A typo here would make
        every gate read None and reject on 'unknown' rather than on merit.
        """
        r = {"is_trades": 40, "is_pf": 2.0, "oos_trades": 12,
             "oos_net_pips": 500.0, "oos_degradation": 0.2, "_mc": None}
        bm, wf, mc = SA.gate_inputs(r)
        assert bm["total_trades"] == 40
        assert bm["profit_factor"] == 2.0
        assert wf["oos_total_trades"] == 12
        assert wf["oos_mean_net_pips"] == 500.0
        assert wf["oos_degradation"] == 0.2
        assert mc is None

    def test_a_strong_candidate_passes_the_real_gate(self):
        """Sanity-check the whole chain: if this fails, the script would
        reject everything and nobody would notice."""
        from self_improvement import Gates, evaluate_gates

        class MC:
            net_p5 = 1000.0
            ruin_prob = 0.0
            is_robust = True

        bm = {"total_trades": 47, "profit_factor": 2.35}
        wf = {"oos_total_trades": 15, "oos_mean_net_pips": 4565.0,
              "oos_degradation": 0.41}
        v = evaluate_gates(bm, wf, MC(), Gates())
        assert v.accepted, v.reason

    def test_a_flat_in_sample_candidate_is_rejected(self):
        """The V12 case: OOS is fine but in-sample PF is 0.98, which must
        fail rather than pass on the OOS number alone."""
        from self_improvement import Gates, evaluate_gates

        class MC:
            net_p5 = 1000.0
            ruin_prob = 0.0
            is_robust = True

        bm = {"total_trades": 50, "profit_factor": 0.98}
        wf = {"oos_total_trades": 61, "oos_mean_net_pips": 3060.0,
              "oos_degradation": 69.67}
        v = evaluate_gates(bm, wf, MC(), Gates())
        assert not v.accepted
        # .failures holds GateResult objects with .name/.passed/.detail
        assert any("profit_factor" in g.name for g in v.failures)


# ======================================================================
# Metrics helper
# ======================================================================
class TestPipsPf:
    def test_pf_over_net_pips(self):
        t = [{"net_pips": 100.0}, {"net_pips": 50.0}, {"net_pips": -75.0}]
        assert SA.pips_pf(t) == pytest.approx(150 / 75)

    def test_empty_is_none_not_infinite(self):
        assert SA.pips_pf([]) is None

    def test_all_winners_has_no_pf(self):
        """No losers means PF is undefined. Returning inf would sail the
        gate for the wrong reason."""
        assert SA.pips_pf([{"net_pips": 1.0}] * 5) is None

    def test_matches_backtest_compute_metrics(self):
        """Cross-check against the repo's own metric layer on a real
        backtest result, so the two cannot drift apart."""
        from backtest import compute_metrics
        trades = [Trade(entry_bar=0, entry_price=100.0, side="BUY", stop=95.0,
                        target=110.0, exit_bar=1, exit_price=110.0,
                        exit_reason="target", points=100.0, cost_pips=0.0,
                        net_pips=100.0, duration_bars=1),
                  Trade(entry_bar=2, entry_price=100.0, side="BUY", stop=95.0,
                        target=110.0, exit_bar=3, exit_price=95.0,
                        exit_reason="stop", points=-50.0, cost_pips=0.0,
                        net_pips=-50.0, duration_bars=1)]
        rows = [{"net_pips": t.net_pips} for t in trades]
        assert SA.pips_pf(rows) == pytest.approx(
            compute_metrics(trades)["profit_factor"])


# ======================================================================
# Candidates
# ======================================================================
class TestCandidates:
    def test_baselines_are_present(self):
        """The two live strategies must be in the list, or the analysis
        silently omits what is actually running."""
        labels = " ".join(c[0] for c in SA.CANDIDATES)
        assert "V11 D1 deployed" in labels
        assert "V12 H1 deployed" in labels

    def test_every_candidate_has_the_full_parameter_tuple(self):
        for c in SA.CANDIDATES:
            assert len(c) == 9, f"{c[0]} has {len(c)} fields"
            label, tf, strat, bp, ma, am, rr, hold, risk = c
            assert label and tf in ("D1", "H1")
            assert strat in ("V11", "V12")
            assert 0 < bp <= 1 and 0 < ma < 0.1
            assert 0 < am < 10 and rr > 0 and hold > 0
            assert 0 < risk <= 0.05

    def test_hold_candidates_actually_vary_the_hold(self):
        holds = {c[7] for c in SA.CANDIDATES if "V11" in c[2]}
        assert len(holds) > 1, "no holding-period variation to evaluate"


# ======================================================================
# Read-only — the property that matters most
# ======================================================================
class TestReadOnly:
    def test_no_order_path(self):
        src = (ROOT / "research" / "self_analysis.py").read_text(encoding="utf-8")
        code = " ".join(tok.string for tok in
                        tokenize.generate_tokens(io.StringIO(src).readline)
                        if tok.type not in (tokenize.STRING, tokenize.COMMENT))
        for pat in (r"\border_send\b", r"\bMT5Gateway\b", r"\bpositions_get\b"):
            assert not re.search(pat, code, re.M), f"references {pat}"

    def test_never_imports_the_promoter(self):
        """promote() is the only writer of approved.json. This script may
        evaluate, never promote. A static import would be the danger."""
        src = (ROOT / "research" / "self_analysis.py").read_text(encoding="utf-8")
        code = "\n".join(ln for ln in src.splitlines()
                         if not ln.strip().startswith("#"))
        assert "def promote" not in code
        # the only self_improvement names used are the read-only ones
        for m in re.finditer(r"from self_improvement import ([^\n]+)", src):
            names = m.group(1)
            assert "promote" not in names, \
                "self_analysis must not import the promoter"

    def test_does_not_write_to_approved_json(self):
        """The file may MENTION approved.json (the docstring says it does
        not touch it) but must never open it for writing. Strip strings and
        comments, then look for the approved path in the actual code."""
        src = (ROOT / "research" / "self_analysis.py").read_text(encoding="utf-8")
        code = " ".join(tok.string for tok in
                        tokenize.generate_tokens(io.StringIO(src).readline)
                        if tok.type not in (tokenize.STRING, tokenize.COMMENT))
        assert "approved.json" not in code
        assert "APPROVED" not in code       # the execution.approved constant

    def test_writes_only_under_research_study(self):
        assert SA.OUT.name == "study"
        assert "research" in SA.OUT.parts
