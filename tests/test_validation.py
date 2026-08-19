"""Tests for the Phase 9 validation harness.

Covers:
  - train_val_test_split fractions / no-overlap / contiguous
  - walk_forward_windows ranges correct and rolling
  - run_walk_forward runs without leakage and returns results
  - summarize_walk_forward degradation math
  - real-data walk-forward on the V1 gold strategy (structural invariants)
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
from validation import (
    train_val_test_split,
    walk_forward_windows,
    run_walk_forward,
    summarize_walk_forward,
)

DEF = (
    Path(__file__).resolve().parents[1]
    / "strategy"
    / "defs"
    / "XAUUSD_STRUCTURE_BREAK_V1.json"
)


def _syn(n):
    return pd.DataFrame({"x": range(n)}, index=pd.RangeIndex(n))


# ---- split ----------------------------------------------------------------

def test_split_fractions():
    tr, va, te = train_val_test_split(_syn(100), 0.6, 0.2, 0.2)
    assert len(tr) == 60 and len(va) == 20 and len(te) == 20


def test_split_no_overlap_contiguous():
    tr, va, te = train_val_test_split(_syn(100), 0.6, 0.2, 0.2)
    # contiguous: train ends where val begins, val ends where test begins
    assert tr.index[-1] + 1 == va.index[0]
    assert va.index[-1] + 1 == te.index[0]
    # no shared rows
    assert set(tr.index) & set(va.index) == set()
    assert set(tr.index) & set(te.index) == set()


def test_split_bad_fractions():
    with pytest.raises(ValueError):
        train_val_test_split(_syn(100), 0.5, 0.2, 0.2)


# ---- walk-forward windows -------------------------------------------------

def test_wf_windows_ranges():
    wins = list(walk_forward_windows(100, 0.5, 0.25))
    # first window: train [0,50), test [50,75)
    assert wins[0] == ((0, 50), (50, 75))
    # step defaults to test size (25) -> next train [25,75), test [75,100)
    assert wins[1] == ((25, 75), (75, 100))
    assert len(wins) == 2


def test_wf_windows_rolls_until_room():
    wins = list(walk_forward_windows(120, 0.5, 0.25))
    # train 60, test 30; step 30 -> windows start at 0,30 (start 60 invalid:
    # 60+60+30=150 > 120)
    assert wins[0][0] == (0, 60)
    assert wins[1][0] == (30, 90)
    assert len(wins) == 2


# ---- walk-forward run + summary ------------------------------------------

def test_summarize_degradation():
    from validation import WindowResult
    # IS net +100, OOS net +50 -> degradation 0.5 (half the edge survives)
    r = [WindowResult((0, 5), (5, 8),
                      train_metrics={"net_pips": 100.0, "profit_factor": 2.0, "win_rate": 0.6, "total_trades": 10},
                      test_metrics={"net_pips": 50.0, "profit_factor": 1.5, "win_rate": 0.5, "total_trades": 8})]
    s = summarize_walk_forward(r)
    assert s["n_windows"] == 1
    assert s["is_mean_net_pips"] == 100.0
    assert s["oos_mean_net_pips"] == 50.0
    assert s["oos_degradation"] == pytest.approx(0.5)


def test_wf_runs_no_leakage_real():
    db = cfg.PROJECT_ROOT / cfg.load_settings()["paths"]["db"]
    con = sqlite3.connect(db)
    m5 = pd.read_sql_query(
        "SELECT ts_broker_epoch, open, high, low, close, spread FROM market_data "
        "WHERE symbol='XAUUSD' AND timeframe='M5' AND source='mt5' ORDER BY ts_broker_epoch", con)
    m15 = pd.read_sql_query(
        "SELECT ts_broker_epoch, open, high, low, close, spread FROM market_data "
        "WHERE symbol='XAUUSD' AND timeframe='M15' AND source='mt5' ORDER BY ts_broker_epoch", con)
    con.close()
    if len(m5) < 2000 or len(m15) < 2000:
        pytest.skip("not enough ingested data")
    for d in (m5, m15):
        d["ts"] = pd.to_datetime(d["ts_broker_epoch"], unit="s", utc=True)
        d["spread_pips"] = d["spread"] * 0.01 / 0.10

    strat = Strategy.load(DEF)
    results = run_walk_forward(
        strat, m15, m5,
        train_frac=0.5, test_frac=0.25,
        bias_warmup_bars=200, slippage_pips=0.5,
    )
    assert len(results) >= 1
    s = summarize_walk_forward(results)
    # invariants
    assert s["n_windows"] == len(results)
    assert s["oos_total_trades"] >= 0
    assert s["is_total_trades"] >= 0
    # each window's test range must sit strictly AFTER its train range
    for r in results:
        assert r.test_range[0] == r.train_range[1]
    # report the real numbers (computed, not invented)
    print("\n[Walk-forward V1 gold] windows=%d IS_net=%.1f OOS_net=%.1f "
          "IS_PF=%.2f OOS_PF=%.2f OOS_deg=%.2f IS_trades=%d OOS_trades=%d" % (
              s["n_windows"], s["is_mean_net_pips"], s["oos_mean_net_pips"],
              s["is_mean_profit_factor"], s["oos_mean_profit_factor"],
              s["oos_degradation"], s["is_total_trades"], s["oos_total_trades"]))
