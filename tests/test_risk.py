"""Tests for the Phase 10 risk engine.

Hand-computed sizing, hard caps, and a real-data integration that confirms
trades get lot sizes / USD risk attached (still read-only, no trading).
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from market_data import config as cfg
from strategy import Strategy
from backtest import run_backtest, Trade
from risk import (
    AccountState, RiskConfig, compute_lot_size, risk_per_trade_usd,
    can_open, apply_risk_to_backtest, CONTRACT_MULTIPLIER, PIP,
)

DEF = (
    Path(__file__).resolve().parents[1]
    / "strategy"
    / "defs"
    / "XAUUSD_STRUCTURE_BREAK_V1.json"
)


# ---- sizing math ----------------------------------------------------------

def test_lot_size_hand_computed():
    # equity 10k, risk 0.25% => $25 risk. stop distance $2 (200 pips).
    # risk_usd per lot = 2 * 100 = $200. lots = 25/200 = 0.125 -> 0.12
    lots = compute_lot_size(10_000, 0.0025, 2.0)
    assert lots == pytest.approx(0.12, abs=1e-6)


def test_lot_size_zero_on_bad_inputs():
    assert compute_lot_size(0, 0.0025, 2.0) == 0.0
    assert compute_lot_size(10_000, 0.0025, 0.0) == 0.0
    assert compute_lot_size(10_000, 0.0, 2.0) == 0.0


def test_lot_size_capped_by_max_risk():
    # strategy wants 5% but hard cap is 1% -> effective 1%, lots smaller
    cfg = RiskConfig(max_risk_per_trade_pct=0.01)
    lots = compute_lot_size(10_000, 0.05, 2.0, cfg=cfg)
    # risk $100 / $200 per lot = 0.5 lots (floored from 0.5)
    assert lots == pytest.approx(0.5, abs=1e-6)


def test_lot_size_floor_to_min():
    # tiny equity/risk -> below min_lots -> 0 (fail safe)
    cfg = RiskConfig(min_lots=0.01, lot_step=0.01)
    lots = compute_lot_size(10, 0.0025, 5.0, cfg=cfg)
    # risk $0.025 / (5*100=$500) = 0.00005 -> floored to 0.00 < min -> 0
    assert lots == 0.0


def test_risk_per_trade_usd():
    # 0.12 lots, stop $2 -> 0.12 * 2 * 100 = $24
    assert risk_per_trade_usd(0.12, 2.0) == pytest.approx(24.0)


def test_can_open_guard():
    assert can_open(0) is True
    assert can_open(1, RiskConfig(max_positions=1)) is False
    assert can_open(1, RiskConfig(max_positions=2)) is True


# ---- integration with backtest -------------------------------------------

def test_apply_risk_attaches_lots_and_usd():
    # build a minimal trade manually
    t = Trade(entry_bar=1, entry_price=100.0, side="BUY", stop=99.0,
              target=102.0, exit_bar=2, exit_price=102.0, exit_reason="target",
              points=2.0, cost_pips=0.3, net_pips=1.7, duration_bars=1)
    trades = [t]
    apply_risk_to_backtest(trades, equity_start=10_000)
    assert t.lots > 0
    assert t.risk_usd > 0
    # net_usd = net_pips * $0.10 * lots
    assert t.net_usd == pytest.approx(t.net_pips * PIP * t.lots)


def test_real_backtest_gets_sized():
    db = cfg.PROJECT_ROOT / cfg.load_settings()["paths"]["db"]
    con = sqlite3.connect(db)
    m5 = pd.read_sql_query(
        "SELECT ts_broker_epoch, open, high, low, close, spread FROM market_data "
        "WHERE symbol='XAUUSD' AND timeframe='M5' AND source='mt5' ORDER BY ts_broker_epoch "
        "LIMIT 8000", con)
    m15 = pd.read_sql_query(
        "SELECT ts_broker_epoch, open, high, low, close, spread FROM market_data "
        "WHERE symbol='XAUUSD' AND timeframe='M15' AND source='mt5' ORDER BY ts_broker_epoch "
        "LIMIT 2000", con)
    con.close()
    if len(m5) < 2000 or len(m15) < 2000:
        pytest.skip("not enough data")
    for d in (m5, m15):
        d["ts"] = pd.to_datetime(d["ts_broker_epoch"], unit="s", utc=True)
        d["spread_pips"] = d["spread"] * 0.01 / 0.10

    # use a permissive strategy with DOWN bias so it fires on this slice
    # (this early slice of the dataset is a downtrend; LONG-only V1 yields 0
    # signals here, which is why we flip bias for the sizing integration test)
    from strategy import Strategy as S
    perm = S(name="TEST", version="V0", market="XAUUSD",
             bias_timeframe="M15", trigger_timeframe="M5",
             entry_rules={"bias_trend": "down", "bias_min_adx": 0,
                          "trigger_structure": "BOS", "trigger_structure_dir": "down",
                          "require_structure": False,
                          "vol_regimes_allowed": ["normal", "high", "low"],
                          "rsi_max": 999.0, "rsi_min": -999.0, "max_spread_pips": 999.0},
             stop={"type": "atr", "atr_multiple": 1.5},
             target={"type": "risk_reward", "risk_reward": 2.0},
             risk_pct=0.0025, max_positions=1,
             sessions=["asia", "london", "newyork", "quiet"], notes="perm")
    res = run_backtest(perm, m15, m5, slippage_pips=0.5)
    assert res.metrics["total_trades"] >= 1
    sized = apply_risk_to_backtest(res.trades, equity_start=10_000)
    lots = [t.lots for t in sized]
    # lot sizes must be valid: >= 0, never negative, never above the ceiling
    assert all(l >= 0 for l in lots)
    # trades that ARE sized must respect the hard risk cap ($25 on 10k @0.25%)
    risks = [t.risk_usd for t in sized if t.lots > 0]
    assert all(r <= 25.0 + 1e-6 for r in risks)
    # the majority of trades should be sizeable on normal stop distances
    assert sum(1 for l in lots if l > 0) >= len(lots) * 0.5
    print("\n[Risk Phase10] trades=%d sized=%d lots[min/max]=%.3f/%.3f "
          "max_risk_usd=%.2f" % (
              len(sized), sum(1 for l in lots if l > 0),
              min(lots) if lots else 0, max(lots) if lots else 0,
              max(risks) if risks else 0.0))
