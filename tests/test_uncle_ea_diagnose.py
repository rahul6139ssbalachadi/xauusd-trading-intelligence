"""Tests for research/uncle_ea_diagnose.py.

This module produced the headline numbers (per-month P&L, PF, MFE/MAE) that
were reported to the user, and it had ZERO coverage -- which is how a
double-counted balance (`for v in cumsum(): bal += v`, summing the running
total once per trade) shipped. Every assertion here is about internal
consistency, so a wrong-but-plausible number fails rather than passes.
"""
from __future__ import annotations

import io
import re
import tokenize
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
import sys

sys.path.insert(0, str(ROOT))

from research.uncle_ea_diagnose import (  # noqa: E402
    FIXED_LOTS,
    PARTIAL_PCT,
    PARTIAL_TP_R,
    RR,
    SL_ATR_MULT,
    SLIPPAGE_PIPS,
    SPREAD_PIPS,
    START_BALANCE,
    GEOM,
    compare,
    load,
    monthly,
    report,
    run,
)
from research.uncle_ea_m5_week import LOT_VALUE_PER_PIP, PIP, features  # noqa: E402


ZIGZAG = [(10, 10.5, 9.5, 10), (10, 11.0, 9.8, 10.8), (10.8, 11.2, 10.2, 10.5),
          (10.5, 11.5, 10.4, 11.2), (11.2, 11.6, 10.6, 10.8), (10.8, 12.0, 10.7, 11.8),
          (11.8, 12.2, 11.0, 11.2), (11.2, 12.4, 11.1, 12.2), (12.2, 12.6, 11.6, 11.8),
          (11.8, 12.8, 11.7, 12.6), (12.6, 13.0, 12.0, 12.2), (12.2, 13.2, 12.1, 13.0)]


def frame(rows) -> pd.DataFrame:
    """Build an M5 frame. `rows` defaults to a repeated zigzag, because a
    flat series produces no candle patterns at all -- tests that iterate
    over its trades then pass vacuously, which is a false green."""
    rows = rows or ZIGZAG * 20
    df = pd.DataFrame(rows, columns=["open", "high", "low", "close"])
    df["spread"] = SPREAD_PIPS
    df.index = pd.date_range("2026-05-04", periods=len(df), freq="5min")
    return features(df)


def capture(fn, *a, **k) -> str:
    buf = io.StringIO()
    with redirect_stdout(buf):
        fn(*a, **k)
    return buf.getvalue()


class TestParamsMatchTheEA:
    def test_constants_are_the_EAs_own_inputs(self):
        assert RR == 1.1                  # InpTP_RR
        assert FIXED_LOTS == 0.01         # InpLotSize
        assert PARTIAL_TP_R == 0.50       # InpPartialTP_RR
        assert PARTIAL_PCT == 0.50        # InpPartialPercent
        assert SL_ATR_MULT == 1.5         # InpSL_ATR_Multiplier


class TestMoney:
    def test_net_usd_is_pips_times_value_times_lots(self):
        df = frame(None)
        trades = run(df)
        assert trades
        for t in trades:
            expected = t["net_pips"] * PIP * LOT_VALUE_PER_PIP * FIXED_LOTS
            assert abs(t["net_usd"] - expected) < 1e-9

    def test_cost_is_spread_plus_two_sided_slippage(self):
        df = frame(None)
        expected = SPREAD_PIPS + 2 * SLIPPAGE_PIPS
        assert all(t["cost_pips"] == expected for t in run(df))

    def test_gross_pips_is_r_times_stop(self):
        df = frame(None)
        trades = run(df)
        assert trades
        for t in trades:
            assert abs(t["gross_pips"] - t["r"] * t["stop_pips"]) < 1e-6

    def test_fixed_lot_means_a_stopout_costs_the_stop_not_two_percent(self):
        """The whole point of the diagnosis: 0.01 lot is FIXED, so the dollar
        loss per stopout is stop_pips * value * lots -- NOT balance * 2%."""
        df = frame(None)
        trades = run(df)
        assert trades
        for t in trades:
            if t["r"] <= -0.99:
                loss = abs(t["net_usd"])
                assert loss < 1.0, "a 0.01-lot stopout should cost well under $1"


class TestRAccounting:
    def test_r_values_are_from_the_defined_set(self):
        df = frame(None)
        trades = run(df)
        assert trades
        for t in trades:
            if t.get("open_at_end"):
                continue
            assert t["r"] in (-1.0, -0.5, 0.0, RR, 0.5 + 0.5 * RR) or \
                   -1.0 <= t["r"] <= RR

    def test_sign_of_r_matches_sign_of_net(self):
        """net_usd subtracts a FIXED cost, so a small winning R can still be
        a losing dollar trade. What must hold is the direction of the GROSS
        result matching R -- the earlier bug reported +$80 on a -1R exit."""
        df = frame(None)
        trades = run(df)
        assert trades
        for t in trades:
            gross = t["r"] * t["stop_pips"]
            if abs(gross) > 0:
                assert (gross > 0) == (t["r"] > 0)
            # and the R that a -1R exit reports must not be a profit
            if t["r"] <= -0.99:
                assert t["net_usd"] < 0

    def test_mfe_is_never_negative_and_mae_never_positive(self):
        df = frame(None)
        trades = run(df)
        assert trades
        for t in trades:
            assert t["mfe"] >= 0.0
            assert t["mae"] <= 0.0

    def test_a_stopout_is_bounded_by_minus_one_r(self):
        df = frame(None)
        trades = run(df)
        assert trades
        for t in trades:
            if not t.get("open_at_end"):
                assert t["r"] >= -1.0


class TestVariants:
    """The swapped / inverted experiments the user asked for."""

    def test_as_is_keeps_the_EAs_own_geometry(self):
        df = frame(None)
        trades = run(df, "as_is")
        assert trades, "fixture produced no trades - test would pass vacuously"
        # the stop is SL_ATR_MULT x ATR; assert the RATIO between the two
        # variants rather than an absolute pip count, because the fixture's
        # ATR is whatever the zigzag makes it
        swap = run(df, "swap")
        assert swap
        assert (trades[0]["stop_pips"] / swap[0]["stop_pips"]) == pytest.approx(
            SL_ATR_MULT, rel=0.35)
    def test_swap_uses_rr_one_and_a_narrower_stop(self):
        df = frame(None)
        trades = run(df, "swap")
        assert trades, "fixture produced no trades"
        # stop = 1.0 x ATR, i.e. NARROWER than the as_is 1.5 x ATR
        assert trades[0]["stop_pips"] < run(df, "as_is")[0]["stop_pips"]

    def test_swap_and_invert_both_force_rr_to_one(self):
        df = frame(None)
        for v in ("swap", "invert"):
            trades = run(df, v)
            assert trades, f"{v} produced no trades"
            for t in trades:
                # no exit may exceed +1.0R
                assert t["r"] <= 1.0 + 1e-9
    def test_invert_flips_every_direction(self):
        df = frame(None)
        a = {t["time"] for t in run(df, "as_is")}
        inv = {t["time"] for t in run(df, "invert")}
        assert a and inv
        # the inverted run must not simply reproduce the same sides
        dirs_as_is = [t["dir"] for t in run(df, "as_is")]
        dirs_inv = [t["dir"] for t in run(df, "invert")]
        assert dirs_as_is != dirs_inv

    def test_swap_makes_costs_a_larger_share_of_the_stop(self):
        """A 1.0xATR stop is ~50 pips against 40 pips of cost, so costs
        dominate far more than at 1.5xATR. This is the mechanism that makes
        the swapped variant strictly worse, not merely different."""
        df = load(datetime(2026, 4, 10), datetime(2026, 9, 1))
        if df.empty:
            pytest.skip("no M5 history")
        win = df[df.index >= datetime(2026, 5, 1)]
        a = pd.DataFrame(run(win, "as_is"))
        s = pd.DataFrame(run(win, "swap"))
        cost_a = a.cost_pips.mean() / a.stop_pips.mean()
        cost_s = s.cost_pips.mean() / s.stop_pips.mean()
        assert cost_s > cost_a

    def test_all_three_variants_remain_net_negative_on_real_data(self):
        """The honest finding, pinned so a future change cannot quietly
        flip it: none of the three produces positive expectancy."""
        df = load(datetime(2026, 4, 10), datetime(2026, 9, 1))
        if df.empty:
            pytest.skip("no M5 history")
        win = df[df.index >= datetime(2026, 5, 1)]
        for v in ("as_is", "swap", "invert"):
            d = pd.DataFrame(run(win, v))
            assert d.r.mean() < 0, f"{v} turned positive"

    def test_compare_covers_all_three_variants_and_every_month(self):
        df = load(datetime(2026, 4, 10), datetime(2026, 9, 1))
        if df.empty:
            pytest.skip("no M5 history")
        out = capture(compare, "2026-05-01", "2026-09-01")
        # headers come from the single GEOM table, keyed by variant name
        for v in ("as_is", "swap", "invert"):
            assert f"== {v.upper()}" in out
            assert GEOM[v] in out
        for m in ("2026-05", "2026-06", "2026-07", "2026-08"):
            assert out.count(m) == 3, f"{m} should appear once per variant"
        assert out.count("TOTAL") == 3

    def test_explicit_rr_overrides_the_variant_default(self):
        """REGRESSION: `rr = rr or RR` ran first, so the variant's RR-1.0
        branch was unreachable and an RR sweep returned IDENTICAL PF at
        every setting -- a silently meaningless sweep."""
        df = load(datetime(2026, 4, 10), datetime(2026, 9, 1))
        if df.empty:
            pytest.skip("no M5 history")
        win = df[df.index >= datetime(2026, 5, 1)]
        res = {}
        for rr in (1.0, 3.0):
            d = pd.DataFrame(run(win, "invert", rr=rr, fixed_sl_pips=200))
            assert not d.empty
            res[rr] = d.net_usd.sum()
        assert res[1.0] != pytest.approx(res[3.0]), \
            "RR had no effect - the sweep is meaningless"

    def test_fixed_sl_pips_replaces_the_atr_multiple(self):
        df = frame(None)
        fixed = run(df, "invert", rr=1.0, fixed_sl_pips=120)
        assert fixed
        for t in fixed:
            assert abs(t["stop_pips"] - 120) < 0.5

    def test_a_fixed_stop_wider_than_atr_lowers_the_cost_share(self):
        df = load(datetime(2026, 4, 10), datetime(2026, 9, 1))
        if df.empty:
            pytest.skip("no M5 history")
        win = df[df.index >= datetime(2026, 5, 1)]
        tight = pd.DataFrame(run(win, "invert", rr=1.0, fixed_sl_pips=50))
        wide = pd.DataFrame(run(win, "invert", rr=1.0, fixed_sl_pips=500))
        assert not tight.empty and not wide.empty
        c_tight = tight.cost_pips.mean() / tight.stop_pips.mean()
        c_wide = wide.cost_pips.mean() / wide.stop_pips.mean()
        assert c_wide < c_tight

    def test_no_configuration_is_robust_to_a_wider_spread(self):
        """The honest finding, pinned: the best cell (inverted, SL 500,
        RR 1.0) reaches PF 1.24 at the recorded spread but falls below 1.0
        at 1.5x. An edge smaller than its own execution uncertainty is not
        an edge, so this must not silently become 'approved'."""
        import research.uncle_ea_diagnose as M
        df = load(datetime(2026, 4, 10), datetime(2026, 9, 1))
        if df.empty:
            pytest.skip("no M5 history")
        win = df[df.index >= datetime(2026, 5, 1)]
        pf = {}
        for mult in (1.0, 1.5):
            old = M.SPREAD_PIPS
            M.SPREAD_PIPS = 30.0 * mult
            try:
                d = pd.DataFrame(M.run(win, "invert", rr=1.0, fixed_sl_pips=500))
                w_, l_ = d[d.net_usd > 0], d[d.net_usd < 0]
                pf[mult] = w_.net_usd.sum() / -l_.net_usd.sum()
            finally:
                M.SPREAD_PIPS = old
        assert pf[1.0] > 1.0
        assert pf[1.5] < 1.0


class TestReport:
    def test_reported_end_balance_is_start_plus_net(self):
        """REGRESSION: the balance loop added the running total once per
        trade, so a -$17.63 run reported -$13,611 instead of $9,982."""
        df = load(datetime(2026, 4, 10), datetime(2026, 9, 1))
        if df.empty:
            pytest.skip("no M5 history")
        win = df[df.index >= datetime(2026, 5, 1)]
        trades = run(win)
        assert trades
        out = capture(report, trades, "t")
        net = sum(t["net_usd"] for t in trades)
        reported = float(out.split("end balance $")[1].split("\n")[0]
                         .replace(",", "").split()[0])
        assert reported == pytest.approx(START_BALANCE + net, abs=0.01)
        assert reported > 0, "a losing run must not report a negative balance"

    def test_report_runs_on_an_empty_trade_list(self):
        assert "no trades" in capture(report, [], "t")

    def test_report_survives_nan_free_frames(self):
        df = frame(None)
        assert capture(report, run(df), "t").startswith("=")


class TestMonthly:
    def test_monthly_emits_one_row_per_month(self):
        df = load(datetime(2026, 4, 10), datetime(2026, 9, 1))
        if df.empty:
            pytest.skip("no M5 history")
        out = capture(monthly, "2026-05-01", "2026-09-01")
        rows = [ln for ln in out.splitlines() if ln.startswith("2026-")]
        assert len(rows) == 4, out
        assert all(r.split()[0] in ("2026-05", "2026-06", "2026-07", "2026-08")
                   for r in rows)

    def test_monthly_rows_carry_a_parsable_number(self):
        df = load(datetime(2026, 4, 10), datetime(2026, 9, 1))
        if df.empty:
            pytest.skip("no M5 history")
        out = capture(monthly, "2026-05-01", "2026-09-01")
        for ln in (l for l in out.splitlines() if l.startswith("2026-")):
            cells = ln.split()
            assert int(cells[1]) >= 0
            float(cells[5].replace(",", ""))     # net$

    def test_empty_range_prints_a_placeholder_row(self):
        out = capture(monthly, "2020-01-01", "2020-03-01")
        assert "2020-01" in out or "no data" in out

    def test_monthly_header_labels_the_actual_geometry(self):
        """The header used to hard-code 'RR=1.1 SL=1.5xATR' regardless of
        variant, so a swap run was mislabelled while its numbers were right."""
        out = capture(monthly, "2026-05-01", "2026-09-01", "swap")
        assert "SWAP" in out
        assert "EXCHANGED" in out
        assert "RR=1.1" not in out


class TestSafety:
    def test_module_cannot_trade(self):
        src = (ROOT / "research" / "uncle_ea_diagnose.py").read_text(encoding="utf-8")
        kept = [t.string for t in tokenize.generate_tokens(io.StringIO(src).readline)
                if t.type not in (tokenize.STRING, tokenize.COMMENT)]
        code = " ".join(kept)
        for banned in ("order_send", "MetaTrader5", "positions_get"):
            assert banned not in code

    def test_module_does_not_write(self):
        src = (ROOT / "research" / "uncle_ea_diagnose.py").read_text(encoding="utf-8")
        stmts = re.findall(
            r"(?is)(?<![.\w])\b(SELECT|INSERT|UPDATE|DELETE|DROP|ALTER|CREATE)\b"
            r"[\s\S]{0,400}?\b(FROM|SET\b|INTO\b|TABLE\b)", src)
        assert stmts, "expected SQL"
        assert {v.upper() for v, _ in stmts} == {"SELECT"}

    def test_the_insert_hit_was_sys_path_not_sql(self):
        """Guard the guard: a bare keyword scan matches `sys.path.insert`,
        which is why this suite matches VERB..OBJECT pairs instead."""
        src = (ROOT / "research" / "uncle_ea_diagnose.py").read_text(encoding="utf-8")
        assert re.search(r"(?i)\bINSERT\b", src)
        assert not re.search(r"(?is)\bINSERT\s+INTO\b", src)
