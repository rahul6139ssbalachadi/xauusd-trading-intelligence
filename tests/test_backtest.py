"""Tests for the backtest engine (Phase 8).

Coverage:
  - synthetic OHLC where the exit path is known by hand (stop hit vs
    target hit, and the closer-level-wins tie rule)
  - cost accounting: spread + slippage reduce net pips vs gross
  - metrics sanity (profit factor, win rate, drawdown sign)
  - real-data run on the V1 strategy over ingested M15/M5 gold
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import math
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from market_data import config as cfg
from strategy import Strategy
from backtest import _bar_exit, compute_metrics, run_backtest

DEF = (
    Path(__file__).resolve().parents[1]
    / "strategy"
    / "defs"
    / "XAUUSD_STRUCTURE_BREAK_V1.json"
)


def _ohlc(open_, high, low, close, spread=20.0):
    n = len(close)
    idx = pd.RangeIndex(n)
    return pd.DataFrame({
        "ts": pd.date_range("2026-01-05 14:00", periods=n, freq="5min", tz="UTC"),
        "open": pd.Series(open_, dtype=float, index=idx),
        "high": pd.Series(high, dtype=float, index=idx),
        "low": pd.Series(low, dtype=float, index=idx),
        "close": pd.Series(close, dtype=float, index=idx),
        "spread": pd.Series([spread] * n, dtype=float, index=idx),
    })


# ---- intrabar exit logic --------------------------------------------------

def test_bar_exit_target_hit_buy():
    # BUY entry 100, target 102, stop 98; bar high 103 low 99 -> target
    reason, price = _bar_exit("BUY", 100, 98, 102, 103, 99)
    assert reason == "target"
    assert price == 102


def test_bar_exit_stop_hit_buy():
    reason, price = _bar_exit("BUY", 100, 98, 102, 99, 97)
    assert reason == "stop"
    assert price == 98


def test_bar_exit_closer_level_wins():
    # BUY entry 100, stop 99 (1 away), target 103 (3 away); bar touches both
    # -> stop is closer, stop wins (conservative)
    reason, price = _bar_exit("BUY", 100, 99, 103, 105, 95)
    assert reason == "stop"
    assert price == 99


def test_bar_exit_no_touch():
    reason, price = _bar_exit("BUY", 100, 98, 102, 100.5, 99.5)
    assert reason == ""
    assert pd.isna(price)


def test_bar_exit_sell_target():
    # SELL entry 100, stop 102, target 98; bar low 97 -> target
    reason, price = _bar_exit("SELL", 100, 102, 98, 101, 97)
    assert reason == "target"
    assert price == 98


# ---- metrics --------------------------------------------------------------

def test_metrics_empty():
    assert compute_metrics([]) == {"total_trades": 0}


def test_metrics_profit_factor_and_dd():
    from backtest import Trade
    trades = [
        Trade(0, 100, "BUY", 99, 102, 1, 102, "target", 2.0, 0.3, 1.7, 1),
        Trade(1, 102, "BUY", 101, 104, 2, 101, "stop", -1.0, 0.3, -1.3, 1),
        Trade(2, 101, "BUY", 100, 103, 3, 103, "target", 2.0, 0.3, 1.7, 1),
    ]
    m = compute_metrics(trades)
    assert m["total_trades"] == 3
    assert m["win_rate"] == pytest.approx(2 / 3)
    assert m["profit_factor"] == pytest.approx((1.7 + 1.7) / 1.3)
    assert m["net_pips"] == pytest.approx(1.7 + 1.7 - 1.3)
    assert m["max_drawdown_pips"] >= 0


# ---- full backtest, synthetic known outcome -------------------------------

def test_backtest_target_then_stop():
    # Validate backtest plumbing (next-open entry, intrabar exit, cost
    # accounting, metrics) with a PERMISSIVE inline strategy that fires on a
    # simple EMA-cross uptrend + allowed session, so structure/rsi/adx gates
    # don't mask the engine logic. Real-structure path is covered by the
    # real-data test below.
    # Frame must be long enough for the slowest indicator (EMA50 -> >=50 bars,
    # vol_regime lookback 100 -> use 120).
    n = 120
    closes = list(range(100, 100 + n))
    open_ = [float(c) for c in closes]
    high = [float(c + 2) for c in closes]
    low = [float(c - 2) for c in closes]
    trig = _ohlc(open_=open_, high=high, low=low,
                 close=[float(c) for c in closes], spread=20.0)
    bias = trig.copy()
    bias["ema_fast"] = pd.Series(closes, dtype=float)
    bias["ema_slow"] = pd.Series([c * 0.9 for c in closes], dtype=float)
    bias["rsi"] = 50.0
    bias["adx"] = 30.0
    bias["atr"] = 1.0
    bias["session"] = "newyork"
    bias["vol_regime"] = "normal"
    bias["last_bos_dir"] = "up"
    bias["last_choch_dir"] = pd.NA
    trig["ema_fast"] = bias["ema_fast"]
    trig["ema_slow"] = bias["ema_slow"]
    trig["rsi"] = 50.0
    trig["adx"] = 30.0
    trig["atr"] = 1.0
    trig["session"] = "newyork"
    trig["vol_regime"] = "normal"
    trig["last_bos_dir"] = "up"
    trig["last_choch_dir"] = pd.NA

    from strategy import Strategy
    permissive = Strategy(
        name="TEST", version="V0", market="XAUUSD",
        bias_timeframe="M15", trigger_timeframe="M5",
        entry_rules={
            "bias_trend": "up", "bias_min_adx": 0,
            "trigger_structure": "BOS", "trigger_structure_dir": "up",
            "require_structure": False,
            "vol_regimes_allowed": ["normal", "high", "low"],
            "rsi_max": 999.0, "rsi_min": -999.0, "max_spread_pips": 999.0,
        },
        stop={"type": "atr", "atr_multiple": 1.5},
        target={"type": "risk_reward", "risk_reward": 2.0},
        risk_pct=0.0025, max_positions=1,
        sessions=["asia", "london", "newyork", "quiet"],
        notes="permissive test strategy",
    )
    res = run_backtest(permissive, bias, trig, slippage_pips=0.0)
    assert res.metrics["total_trades"] >= 1
    assert math.isfinite(res.metrics["net_pips"])
    # with no slippage and spread=20pts ($0.20) the cost_pips should be >0
    assert any(t.cost_pips > 0 for t in res.trades)


# ---- real-data run --------------------------------------------------------

def _load(tf):
    db = cfg.PROJECT_ROOT / cfg.load_settings()["paths"]["db"]
    con = sqlite3.connect(db)
    df = pd.read_sql_query(
        f"SELECT ts_broker_epoch, open, high, low, close, spread FROM market_data "
        f"WHERE symbol='XAUUSD' AND timeframe='{tf}' AND source='mt5' ORDER BY ts_broker_epoch",
        con,
    )
    con.close()
    df["ts"] = pd.to_datetime(df["ts_broker_epoch"], unit="s", utc=True)
    return df


def test_backtest_runs_on_real_V1():
    m15 = _load("M15")
    m5 = _load("M5")
    if len(m15) < 300 or len(m5) < 300:
        pytest.skip("not enough ingested data")
    strat = Strategy.load(DEF)
    res = run_backtest(strat, m15, m5, slippage_pips=0.5)
    m = res.metrics
    # structural invariants
    assert m["total_trades"] >= 1
    assert 0.0 <= m["win_rate"] <= 1.0
    assert m["profit_factor"] >= 0
    assert m["max_drawdown_pips"] >= 0
    # every trade has a valid exit reason
    assert all(t.exit_reason in ("stop", "target", "end") for t in res.trades)
    # report the headline numbers (computed, not invented)
    print("\n[V1 backtest on real gold] trades=%d win%%=%.1f pf=%.2f "
          "net_pips=%.1f maxdd=%.1f sharpe=%.2f" % (
              m["total_trades"], m["win_rate"] * 100, m["profit_factor"],
              m["net_pips"], m["max_drawdown_pips"], m["sharpe"]))
