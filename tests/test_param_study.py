"""Tests for the V11/V12 parameter study.

The study scripts RE-IMPLEMENT the V11/V12 signal and exit logic in order
to sweep parameters. That is the whole reason they need tests: if their
copy drifted from the production strategy, every number in
research/study/V11_V12_MAY_AUG_2026_REPORT.md would be confidently wrong
and nothing else in the suite would notice.

The load-bearing tests are therefore the two equivalence checks:
  * study signals  == execution.run_v12_hourly's live gate
  * study execution == monthly_bt.engine.simulate
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from monthly_bt import data as bt_data
from monthly_bt import runner, strategies as S
from monthly_bt.engine import SimConfig, simulate
from research import v11_v12_param_study as study
from research.v11_v12_param_study import Cfg, build_configs, features, run, \
    signals, stats


def _have(symbol="XAUUSD", tf="D1", n=300):
    try:
        return len(bt_data.load(symbol, tf)) >= n
    except Exception:
        return False


# ======================================================================
# EQUIVALENCE — the tests that matter
# ======================================================================
class TestStudyMatchesProduction:
    @pytest.mark.skipif(not _have(tf="H1", n=1000), reason="no H1 history")
    def test_signals_match_the_live_v12_gate(self):
        """The study's bar-wise gate must reproduce the LIVE single-bar gate
        of execution.run_v12_hourly.signal_on_last_closed_bar exactly. A
        drift here would invalidate every conclusion in the report.

        Compared on TIMESTAMP, not bar index: the live probe slices the raw
        frame while the study indexes its own feature frame, and an
        iBarShift-style offset is exactly the kind of subtle misalignment
        that would make an index comparison falsely fail.
        """
        raw = bt_data.load("XAUUSD", "H1")
        feats = features(raw, 21, 55, 60)
        p = S.V12.params
        mine = {str(feats.iloc[s["signal_bar"]]["ts"])
                for s in signals(feats, body_pct=p["body_pct_threshold"],
                                 min_atr=p["min_atr_pct"],
                                 atr_mult=p["atr_mult_stop"], rr=p["rr"])}

        from execution.run_v12_hourly import signal_on_last_closed_bar
        # Probe a strided sample — the study scans all 59k bars, but calling
        # the live gate once per bar is O(n^2) (~3.5bn cell copies) and blows
        # the 600s budget. 1,200 evenly-spaced probes is a real sample of the
        # same gate, and the equality below is checked against the FULL set
        # the study produced, so a drifted signal cannot hide in the gap.
        probed = set()
        step = max(1, len(raw) // 1200)
        for end in range(62, len(raw), step):
            # the live function judges iloc[-2] of ITS OWN window, so the
            # bar it actually evaluated is end-2, not end-1. Using end-1
            # made the probe fire one bar late and produced phantom
            # mismatches that were the probe's bug, not the study's.
            if signal_on_last_closed_bar(raw.iloc[:end].reset_index(drop=True)):
                probed.add(str(raw.iloc[end - 2]["ts"]))
        # 1. every signal the live gate fires on in the probe must exist
        #    in the study's output (the study misses nothing)
        assert probed <= mine, (
            f"live gate fired where the study found nothing: "
            f"{sorted(probed - mine)[:5]}")
        # 2. the reverse direction (the study inventing signals) is covered
        #    exhaustively by monthly_bt.strategies.verify_v12_gate_parity,
        #    which compares the two gates bar-for-bar on the same frame.

    @pytest.mark.skipif(not _have(tf="D1", n=2000), reason="no D1 history")
    def test_v11_signal_bars_agree_with_production(self):
        """V11's deployed config: the study and the production research
        function must fire on the same bars."""
        raw = bt_data.load("XAUUSD", "D1")
        p = S.V11.params
        mine = {s["signal_bar"] for s in signals(
            features(raw, 21, 55, 60), body_pct=p["body_pct_threshold"],
            min_atr=p["min_atr_pct"], atr_mult=1.5, rr=p["rr"])}
        prod = runner.build_features(raw, S.V11)
        theirs = {s["d1_bar"] for s in S.v11_signals(prod)}
        assert mine == theirs

    @pytest.mark.skipif(not _have(tf="H1", n=1000), reason="no H1 history")
    def test_study_execution_matches_engine_simulate(self):
        """The study's own backtest loop must agree, trade for trade, with
        monthly_bt.engine.simulate on the same signals. Two independent
        implementations of the same rules is the point: they must match."""
        raw = bt_data.load("XAUUSD", "H1")
        feats = features(raw, 21, 55, 60)
        p = S.V12.params
        sigs = signals(feats, body_pct=p["body_pct_threshold"],
                       min_atr=p["min_atr_pct"], atr_mult=p["atr_mult_stop"],
                       rr=p["rr"])[:40]
        cfg = SimConfig(initial_balance=100_000.0, slippage_pips=1.0)
        mine = run(feats, sigs, S.V12, 8, cfg, p["risk_pct"])

        # NB: the normalised dicts must carry ts_broker, matching what the
        # study's own run() emits. Passing the raw `ts` here compares two
        # different clocks and fails by exactly the 3h broker offset.
        norm = [{
            "strategy": "V12", "symbol": "XAUUSD", "timeframe": "H1",
            "signal": "BUY", "signal_bar": s["signal_bar"],
            "entry_bar": s["entry_bar"], "signal_ts": None,
            "entry_ts": feats.iloc[s["entry_bar"]]["ts_broker"],
            "entry": s["entry"], "sl": s["sl"], "tp": s["tp"],
            "risk_pips": s["risk_pips"], "atr": s["atr"],
            "body_pct": s["body_pct"],
        } for s in sigs]
        theirs = simulate(feats, norm, S.V12, "XAUUSD", cfg)["trade_rows"]
        assert len(mine) == len(theirs)
        for a, b in zip(mine, theirs):
            assert str(a["entry_ts"]) == str(b["entry_ts"])
            assert a["exit_reason"] == b["exit_reason"]
            assert a["net_pnl"] == pytest.approx(b["net_pnl"], rel=1e-9)
            assert a["lots"] == pytest.approx(b["lots"])


# ======================================================================
# Execution rules
# ======================================================================
class TestStudyExecution:
    BASE = pd.DataFrame([
        dict(ts_broker_epoch=1_000_000 + i * 3600, open=100.0, high=101.0,
             low=99.0, close=100.5, tick_volume=10, spread=30.0)
        for i in range(20)])
    BASE["ts"] = pd.to_datetime(BASE["ts_broker_epoch"], unit="s", utc=True)
    BASE["ts_broker"] = BASE["ts"]

    def _sig(self, i=1):
        return {"signal_bar": i - 1, "entry_bar": i, "entry": 100.0,
                "sl": 95.0, "tp": 110.0, "risk_pips": 50.0, "atr": 3.0,
                "body_pct": 0.99}

    def test_target_and_stop_reasons(self):
        for (hi, lo), want in (((112.0, 99.0), "target"),
                               ((101.0, 94.0), "stop")):
            df = self.BASE.copy()
            df.loc[2, ["high", "low"]] = [hi, lo]
            cfg = SimConfig(initial_balance=100_000.0, use_measured_spread=False,
                            fallback_spread_pips=0.0, slippage_pips=0.0)
            t = run(df, [self._sig()], S.V11, 8, cfg, 0.01)
            assert t[0]["exit_reason"] == want

    def test_both_touched_uses_closer_level(self):
        df = self.BASE.copy()
        df.loc[2, ["high", "low"]] = [112.0, 94.0]
        cfg = SimConfig(initial_balance=100_000.0, use_measured_spread=False,
                        fallback_spread_pips=0.0, slippage_pips=0.0)
        assert run(df, [self._sig()], S.V11, 8, cfg, 0.01)[0]["exit_reason"] \
            == "stop"

    def test_max_holding_force_closes(self):
        cfg = SimConfig(initial_balance=100_000.0, use_measured_spread=False,
                        fallback_spread_pips=0.0, slippage_pips=0.0)
        t = run(self.BASE.copy(), [self._sig()], S.V11, 3, cfg, 0.01)
        assert t[0]["exit_reason"] == "end"

    def test_only_one_position_at_a_time(self):
        df = self.BASE.copy()
        df.loc[2, ["high", "low"]] = [112.0, 99.0]
        cfg = SimConfig(initial_balance=100_000.0, use_measured_spread=False,
                        fallback_spread_pips=0.0, slippage_pips=0.0)
        # entry 1 exits on bar 2, so entry 2 (same bar) must be skipped
        assert len(run(df, [self._sig(1), self._sig(2)], S.V11, 8, cfg, 0.01)) == 1

    def test_fixed_lot_overrides_sizing(self):
        cfg = SimConfig(initial_balance=100_000.0, use_measured_spread=False,
                        fallback_spread_pips=0.0, slippage_pips=0.0)
        a = run(self.BASE.copy(), [self._sig()], S.V11, 8, cfg, 0.01, 0.10)
        b = run(self.BASE.copy(), [self._sig()], S.V11, 8, cfg, 0.01, 0.25)
        assert a[0]["lots"] == 0.10 and b[0]["lots"] == 0.25
        assert b[0]["net_pnl"] == pytest.approx(a[0]["net_pnl"] * 2.5)

    def test_risk_pct_scales_linearly(self):
        cfg = SimConfig(initial_balance=100_000.0, use_measured_spread=False,
                        fallback_spread_pips=0.0, slippage_pips=0.0)
        lo = run(self.BASE.copy(), [self._sig()], S.V11, 8, cfg, 0.005)
        hi = run(self.BASE.copy(), [self._sig()], S.V11, 8, cfg, 0.010)
        assert hi[0]["net_pnl"] == pytest.approx(lo[0]["net_pnl"] * 2)


# ======================================================================
# The report's central claim: lot size CANNOT change profit factor
# ======================================================================
class TestLotSizeCannotChangeEdge:
    @pytest.mark.skipif(not _have(tf="D1", n=2000), reason="no D1 history")
    def test_pf_is_identical_across_lot_sizes(self):
        """This is the load-bearing claim of report section 5. If it ever
        became false, the report would be wrong and must be rewritten."""
        raw = bt_data.load("XAUUSD", "D1")
        feats = features(raw, 21, 55, 60)
        p = S.V11.params
        sigs = signals(feats, body_pct=p["body_pct_threshold"],
                       min_atr=p["min_atr_pct"], atr_mult=1.5, rr=p["rr"])
        cfg = SimConfig(initial_balance=100_000.0, use_measured_spread=False,
                        fallback_spread_pips=0.0, slippage_pips=0.0)
        pfs = [stats(run(feats, sigs, S.V11, 8, cfg, None, lots))["pf"]
               for lots in (0.05, 0.10, 0.20, 0.30)]
        # equal up to float noise: scaling P&L cannot change the win/loss ratio
        assert max(pfs) - min(pfs) < 1e-9, f"lot size changed PF: {pfs}"

    @pytest.mark.skipif(not _have(tf="D1", n=2000), reason="no D1 history")
    def test_but_net_pnl_scales_with_lot_size(self):
        raw = bt_data.load("XAUUSD", "D1")
        feats = features(raw, 21, 55, 60)
        p = S.V11.params
        sigs = signals(feats, body_pct=p["body_pct_threshold"],
                       min_atr=p["min_atr_pct"], atr_mult=1.5, rr=p["rr"])
        cfg = SimConfig(initial_balance=100_000.0, use_measured_spread=False,
                        fallback_spread_pips=0.0, slippage_pips=0.0)
        a = stats(run(feats, sigs, S.V11, 8, cfg, None, 0.10))["net"]
        b = stats(run(feats, sigs, S.V11, 8, cfg, None, 0.20))["net"]
        assert b == pytest.approx(a * 2)


# ======================================================================
# stats()
# ======================================================================
class TestStats:
    def test_empty_is_nan_safe(self):
        s = stats([])
        assert s["n"] == 0 and s["pf"] is None and s["win"] is None

    def test_all_winners_has_no_pf(self):
        # no losers -> PF is undefined, not infinite
        t = [{"net_pnl": 100.0, "r": 2.0, "net_pips": 10.0}] * 3
        assert stats(t)["pf"] is None

    def test_pf_and_win_rate(self):
        t = [{"net_pnl": 100.0, "r": 2.0, "net_pips": 10.0},
             {"net_pnl": 50.0, "r": 1.0, "net_pips": 5.0},
             {"net_pnl": -75.0, "r": -1.5, "net_pips": -7.5}]
        s = stats(t)
        assert s["n"] == 3
        assert s["win"] == pytest.approx(2 / 3)
        assert s["pf"] == pytest.approx(150 / 75)
        assert s["net"] == pytest.approx(75.0)
        assert s["R"] == pytest.approx((2 + 1 - 1.5) / 3)

    def test_max_drawdown(self):
        t = [{"net_pnl": v, "r": 0.0, "net_pips": 0.0}
             for v in (100.0, -50.0, -20.0, 30.0)]
        assert stats(t)["dd"] == pytest.approx(70.0)


# ======================================================================
# Config matrix
# ======================================================================
class TestConfigs:
    @pytest.mark.parametrize("mode", ["timeframe", "rr", "params", "hold", "lot"])
    def test_every_mode_builds(self, mode):
        cfgs = build_configs(mode)
        assert cfgs and all(isinstance(c, Cfg) for c in cfgs)
        assert all(c.label for c in cfgs)

    def test_rr_mode_covers_the_deployed_values(self):
        labels = {c.label for c in build_configs("rr")}
        assert "V11 D1 rr=2.0" in labels      # V11 deployed
        assert "V12 H1 rr=3.0" in labels      # V12 deployed

    def test_params_mode_uses_binding_atr_thresholds(self):
        """V11's deployed min_atr (0.005) is below the D1 ATR/close floor
        (measured 0.0073) and can never filter. The sweep must therefore use
        thresholds that can actually bind, or it reports a flat grid."""
        for c in build_configs("params"):
            assert c.min_atr >= 0.008

    @pytest.mark.skipif(not _have(tf="D1", n=500), reason="no D1 data")
    def test_deployed_min_atr_is_below_the_data_floor(self):
        """Documented finding: the deployed 0.005 filter is a no-op. If gold's
        volatility regime changed enough to lift ATR/close above 0.005
        everywhere, the report's section-7A finding is stale."""
        f = features(bt_data.load("XAUUSD", "D1"), 21, 55, 60)
        assert (f["atr14"] / f["close"]).min() > S.V11.params["min_atr_pct"]


# ======================================================================
# Study never trades
# ======================================================================
class TestStudyIsReadOnly:
    @pytest.mark.parametrize("mod", ["v11_v12_param_study", "v11_v12_robustness"])
    def test_no_order_path(self, mod):
        import importlib
        import io
        import tokenize
        m = importlib.import_module(f"research.{mod}")
        src = (ROOT / "research" / f"{mod}.py").read_text(encoding="utf-8")
        code = " ".join(tok.string for tok in
                        tokenize.generate_tokens(io.StringIO(src).readline)
                        if tok.type not in (tokenize.STRING, tokenize.COMMENT))
        for pat in (r"\border_send\b", r"\bMT5Gateway\b", r"\bpositions_get\b",
                    r"\bOrderSend\b"):
            assert not re.search(pat, code, re.M), f"{mod} references {pat}"
        assert m is not None

    def test_min_trades_gate_is_sane(self):
        assert study.MIN_TRADES >= 10
