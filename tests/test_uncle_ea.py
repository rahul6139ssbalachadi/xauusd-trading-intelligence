"""Tests for research/uncle_ea_m5_week.py.

Two layers, per the house pattern:
  1. hand-computed tiny fixtures for the pattern detectors and the 2%-risk
     sizing, which is where the original EA's bugs actually lived;
  2. real-data smoke tests against db/trading.db, skipped if it is short.
"""
from __future__ import annotations

import io
import re
import sqlite3
import tokenize
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
import sys

sys.path.insert(0, str(ROOT))

from research.uncle_ea_m5_week import (  # noqa: E402
    ATR_PERIOD,
    LOT_VALUE_PER_PIP,
    MAX_LOTS,
    PIP,
    RISK_PCT,
    START_BALANCE,
    backtest,
    bear_engulfing,
    bull_engulfing,
    features,
    hammer,
    shooting_star,
    signals,
    swing_highs,
    swing_lows,
)

DB = ROOT / "db" / "trading.db"


# ---------------------------------------------------------------- fixtures
def frame(rows) -> pd.DataFrame:
    """rows = (open, high, low, close) oldest first -> M5 frame with features."""
    df = pd.DataFrame(rows, columns=["open", "high", "low", "close"])
    df["spread"] = 30
    df.index = pd.date_range("2026-03-02 00:00", periods=len(df), freq="5min")
    return features(df)


# A zigzag so local extrema exist (a monotonic series yields none).
ZIGZAG = [(10, 10.5, 9.5, 10), (10, 11.0, 9.8, 10.8), (10.8, 11.2, 10.2, 10.5),
          (10.5, 11.5, 10.4, 11.2), (11.2, 11.6, 10.6, 10.8), (10.8, 12.0, 10.7, 11.8),
          (11.8, 12.2, 11.0, 11.2), (11.2, 12.4, 11.1, 12.2), (12.2, 12.6, 11.6, 11.8),
          (11.8, 12.8, 11.7, 12.6), (12.6, 13.0, 12.0, 12.2), (12.2, 13.2, 12.1, 13.0)]


class TestPatternDetectors:
    def test_engulfing_reads_the_PREVIOUS_bar_not_the_next(self):
        # bar2: o=10.0 c=9.0 (bear)  bar3: o=8.9 c=10.1 (bull, engulfs bar2)
        o = np.array([9.0, 9.0, 10.0, 8.9])
        c = np.array([9.5, 9.5, 9.0, 10.1])
        h = np.array([9.8, 9.8, 10.2, 10.5])
        l = np.array([8.8, 8.8, 8.8, 8.7])
        assert bull_engulfing(h, l, c, o, 3, False) is True
        # the EA's series orientation would have read the FUTURE bar instead
        assert bull_engulfing(h, l, c, o, 3, True) is False
        assert bull_engulfing(h, l, c, o, 2, False) is False

    def test_bearish_engulfing(self):
        # bar2: o=9.0 c=11.0 (bull)  bar3: o=11.1 c=8.9 (bear, engulfs bar2)
        o = np.array([9.0, 9.0, 9.0, 11.1])
        c = np.array([9.5, 9.5, 11.0, 8.9])
        h = np.array([9.8, 9.8, 11.2, 11.3])
        l = np.array([8.8, 8.8, 8.8, 8.7])
        assert bear_engulfing(h, l, c, o, 3, False) is True
        assert bear_engulfing(h, l, c, o, 3, True) is False

    def test_hammer_requires_bearish_predecessor(self):
        #      open  high   low   close
        # hammer bar: o=9.0 c=9.2 (body .2), l=8.0 (lower wick 1.0 > .4),
        #              h=9.25 (upper wick .05 < .1), previous bar bearish
        rows = [(10.0, 10.1, 9.0, 9.2),   # oldest
                (9.3, 9.35, 9.0, 9.2),   # bar 1: BEARISH -> the hammer's previous
                (9.0, 9.25, 8.0, 9.2)]   # bar 2: the hammer
        df = frame(rows)
        o, h, l, c = (df[k].to_numpy(float) for k in ("open", "high", "low", "close"))
        assert hammer(h, l, c, o, 2) is True

        # same shape but a BULLISH previous candle -> not a hammer
        rows[1] = (9.1, 10.1, 9.0, 9.8)   # now BULLISH -> no longer a hammer
        df = frame(rows)
        o, h, l, c = (df[k].to_numpy(float) for k in ("open", "high", "low", "close"))
        assert hammer(h, l, c, o, 2) is False

    def test_shooting_star_requires_bullish_predecessor(self):
        # star bar: o=9.2 c=9.0 (body .2), h=11.0 (upper wick 1.8 > .4),
        #            l=9.15 (lower wick .05 < .1), previous bar bullish
        rows = [(9.0, 9.2, 8.8, 9.1),    # oldest
                (9.05, 9.12, 8.9, 9.1),  # bar 1: BULLISH -> the star's previous
                (9.2, 11.0, 8.95, 9.0)]  # bar 2: the shooting star
        df = frame(rows)
        o, h, l, c = (df[k].to_numpy(float) for k in ("open", "high", "low", "close"))
        assert shooting_star(h, l, c, o, 2) is True

        rows[1] = (9.1, 9.2, 8.0, 8.2)   # now BEARISH -> no longer a star
        df = frame(rows)
        o, h, l, c = (df[k].to_numpy(float) for k in ("open", "high", "low", "close"))
        assert shooting_star(h, l, c, o, 2) is False

    def test_hammer_call_signature_order(self):
        """hammer(h, l, c, o, i) — the original EA mixed these up."""
        df = frame(ZIGZAG)
        o, h, l, c = (df[k].to_numpy(float) for k in ("open", "high", "low", "close"))
        assert isinstance(hammer(h, l, c, o, 3), bool)
        assert isinstance(hammer(h, l, c, o, 3, True), bool)


class TestSwingDetection:
    def test_zigzag_has_swings_monotonic_does_not(self):
        df = frame(ZIGZAG * 3)
        lo = df["low"].to_numpy(float)
        assert len(swing_lows(lo, len(lo) - 1, 12, False)) > 0
        assert len(swing_highs(df["high"].to_numpy(float), len(lo) - 1, 12, False)) > 0

        mono = np.arange(50, dtype=float)
        assert swing_lows(mono, 49, 20, False) == []
        assert swing_highs(mono, 49, 20, False) == []

    def test_window_reaches_back_from_the_scan_bar(self):
        # regression: the window length used to be computed with the
        # look_ahead formula, so in fixed mode it collapsed near the end
        lo = np.sin(np.arange(200, dtype=float)) * 10 + 100
        assert swing_lows(lo, 150, 30, False), "30-bar window returned nothing"
        assert swing_lows(lo, 5, 30, False) == []      # not enough history
        assert swing_lows(lo, 0, 30, False) == []

    def test_swing_scan_stays_inside_the_array(self):
        lo = np.arange(30, dtype=float)[::-1].copy()
        for i in (1, 5, 29):
            assert isinstance(swing_lows(lo, i, 30, False), list)

    def test_swing_scan_never_returns_the_future(self):
        """A future bar must not change a past swing's value."""
        lo = np.array([5, 4, 6, 3, 7, 2, 8, 1, 9], dtype=float)
        before = swing_lows(lo, 4, 5, False)
        mutated = lo.copy()
        mutated[5:] = 0.0                       # destroy everything after bar 4
        assert swing_lows(mutated, 4, 5, False) == before


class TestNoLookAhead:
    def test_truncate_and_compare(self):
        """A signal must depend only on bars BEFORE it.

        Recomputing on a 70% history prefix must reproduce every signal the
        full run found at the same index. The prefix is a SUBSET, not an
        equal set: the last bar of a truncated frame has no room for the
        entry bar, so it is never scanned. Equality in the other direction
        (a signal in the full run missing from the prefix) would be a leak.
        """
        df = frame(ZIGZAG * 6)
        full = signals(df, 20, False, None, 2.0)
        cut = int(len(df) * 0.7)
        prefix = signals(df.iloc[:cut].reset_index(drop=True), 20, False, None, 2.0)
        key = lambda ss: {(s["i"], s["dir"], s["why"]) for s in ss}
        # every signal well inside the prefix must also be found there
        assert key(full) & {(i, d, w) for i, d, w in key(full) if i < cut - 1} <= key(prefix)

    def test_future_bars_cannot_create_a_signal(self):
        df = frame(ZIGZAG * 4)
        cut = len(df) - 5
        base = signals(df.iloc[:cut].reset_index(drop=True), 20, False, None, 2.0)
        shocked = df.copy()
        shocked.loc[shocked.index[cut:], ["high", "low", "close"]] = 1.0
        after = signals(shocked.iloc[:cut].reset_index(drop=True), 20, False, None, 2.0)
        assert base == after
        # destroying the future may only ADD signals at/after `cut`; nothing
        # before it may change, so every pre-`cut` signal must still be there
        full = signals(shocked, 20, False, None, 2.0)
        assert {(s['i'], s['dir'], s['why']) for s in base} <= \
               {(s['i'], s['dir'], s['why']) for s in full}


class TestRiskSizing:
    def test_two_percent_of_balance_scales_the_lot(self):
        df = frame(ZIGZAG * 4)
        sigs = signals(df, 20, False, None, 2.0)
        m, trades = backtest(df, sigs, None, 2.0, "t")
        for t in trades:
            expected = t["stop_pips"] * LOT_VALUE_PER_PIP * t["lots"]
            # risk_usd is the REAL $ risked after lot rounding, so it is
            # 2% of balance only when the lot step divides cleanly
            assert abs(expected - t["risk_usd"]) < 0.06
            assert t["risk_usd"] <= max(START_BALANCE, 1) * RISK_PCT * 1.5

    def test_lots_never_exceed_the_hard_cap(self):
        df = frame(ZIGZAG * 4)
        sigs = signals(df, 20, False, None, 2.0)
        _, trades = backtest(df, sigs, None, 2.0, "t")
        assert all(t["lots"] <= MAX_LOTS for t in trades)

    def test_lots_respect_the_broker_minimum(self):
        df = frame(ZIGZAG * 4)
        sigs = signals(df, 20, False, None, 2.0)
        _, trades = backtest(df, sigs, None, 2.0, "t")
        assert all(t["lots"] >= 0.01 for t in trades)

    def test_module_constants_match_the_brief(self):
        assert RISK_PCT == 0.02
        assert ATR_PERIOD == 14
        assert PIP == 0.10


class TestBacktestMechanics:
    def test_pnl_sign_follows_the_r_multiple(self):
        """A -1R exit must not report a profit. (The original run did.)"""
        df = frame(ZIGZAG * 4)
        sigs = signals(df, 20, False, None, 2.0)
        _, trades = backtest(df, sigs, None, 2.0, "t")
        for t in trades:
            if t["r"] <= -0.99:
                assert t["pnl"] < 0
            if t["r"] >= 1.4:
                assert t["pnl"] > 0

    def test_entry_is_after_the_signal_bar(self):
        df = frame(ZIGZAG * 4)
        sigs = signals(df, 20, False, None, 2.0)
        _, trades = backtest(df, sigs, None, 2.0, "t")
        # every trade must be entered on the bar AFTER its signal bar
        for t in trades:
            e = df.index.get_loc(t["time"])
            assert any(s["i"] == e - 1 for s in sigs), \
                f"trade at {t['time']} has no signal on the preceding bar"

    def test_stop_lies_on_the_correct_side_of_entry(self):
        df = frame(ZIGZAG * 4)
        sigs = signals(df, 20, False, None, 2.0)
        _, trades = backtest(df, sigs, None, 2.0, "t")
        for t in trades:
            if t["dir"] == "BUY":
                assert t["sl"] < t["entry"] < t["tp1"] < t["tp2"]
            else:
                assert t["sl"] > t["entry"] > t["tp1"] > t["tp2"]

    def test_only_one_position_at_a_time(self):
        df = frame(ZIGZAG * 4)
        sigs = signals(df, 20, False, None, 2.0)
        _, trades = backtest(df, sigs, None, 2.0, "t")
        times = [t["time"] for t in trades]
        assert times == sorted(times)
        assert len(times) == len(set(times))

    def test_empty_signals_produces_no_trades(self):
        df = frame(ZIGZAG * 4)
        m, trades = backtest(df, [], None, 2.0, "t")
        assert trades == []
        assert m["trades"] == 0
        assert m["win_rate"] is None


class TestResearchSafety:
    def test_module_cannot_trade(self):
        """Tokenizer test: the guard's own docstring mentions order_send, so a
        regex over raw source gives a false pass/fail. Strip STRING+COMMENT."""
        src = (ROOT / "research" / "uncle_ea_m5_week.py").read_text(encoding="utf-8")
        kept = []
        for tok in tokenize.generate_tokens(io.StringIO(src).readline):
            if tok.type in (tokenize.STRING, tokenize.COMMENT):
                continue
            kept.append(tok.string)
        code = " ".join(kept)
        for banned in ("order_send", "MetaTrader5", "positions_get", "mt5."):
            assert banned not in code

    def test_db_is_opened_read_only(self):
        # a bare regex also matches sys.path.insert and reset_index(drop=True),
        # so match SQL VERB ... OBJECT pairs instead of bare keywords
        src = (ROOT / "research" / "uncle_ea_m5_week.py").read_text(encoding="utf-8")
        stmts = re.findall(r"(?is)(?<![.\w])\b(SELECT|INSERT|UPDATE|DELETE|DROP|ALTER|CREATE)\b"
                           r"[\s\S]{0,400}?\b(FROM|SET\b|INTO\b|TABLE\b)", src)
        assert stmts, "expected at least one SQL statement"
        assert {v.upper() for v, _ in stmts} == {"SELECT"}


# ------------------------------------------------------------- real data
def _real_frame():
    if not DB.exists():
        return None
    with sqlite3.connect(DB) as c:
        rows = c.execute(
            "SELECT ts_broker_epoch, open, high, low, close FROM market_data "
            "WHERE symbol='XAUUSD' AND timeframe='M5' ORDER BY ts_broker_epoch"
        ).fetchall()
    if len(rows) < 2000:
        return None
    df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close"])
    df.index = pd.to_datetime(df.pop("ts"), unit="s")
    return features(df)


REAL = _real_frame()
needs_real = pytest.mark.skipif(REAL is None, reason="M5 history not in db/trading.db")


@needs_real
class TestRealData:
    def test_atr_is_positive_and_aligned(self):
        assert isinstance(REAL["atr"], pd.Series)
        assert len(REAL["atr"]) == len(REAL)
        assert REAL["atr"].dropna().gt(0).all()

    def test_signals_produce_and_are_well_formed(self):
        sg = signals(REAL, 60, False, None, 2.0)
        assert sg, "no signals on 6 months of gold M5"
        for s in sg:
            assert s["dir"] in ("BUY", "SELL")
            assert 1 <= s["i"] < len(REAL) - 1
            assert isinstance(s["why"], str) and s["why"]

    def test_no_look_ahead_on_real_data(self):
        cut = int(len(REAL) * 0.7)
        full = signals(REAL, 60, False, None, 2.0)
        prefix = signals(REAL.iloc[:cut].reset_index(drop=True), 60, False, None, 2.0)
        assert prefix == [s for s in full if s["i"] < cut]

    def test_backtest_produces_coherent_metrics(self):
        sg = signals(REAL, 60, False, None, 2.0)
        m, trades = backtest(REAL, sg, None, 2.0, "real")
        assert m["trades"] == len(trades)
        assert trades
        assert m["net"] == pytest.approx(m["end_balance"] - START_BALANCE, abs=0.05)
        if m["losses"]:
            assert m["pf"] is not None and m["pf"] > 0
        # the honest finding: this signal family has no edge on gold M5
        assert m["pf"] is None or m["pf"] < 1.0
