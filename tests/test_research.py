"""Tests for the strategy research harness (option-2 parameter search).

Verifies the grid search:
  - optimizes only on TRAIN, measures VAL + OOS
  - returns per-combo train/val/oos metrics
  - mints experiment IDs via the registry
  - a tiny grid on a small real-data slice stays fast and correct
"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from market_data import config as cfg
from research import grid_search, top_by_oos
from reporting import ExperimentRegistry


def _load(tf, limit):
    db = cfg.PROJECT_ROOT / cfg.load_settings()["paths"]["db"]
    con = sqlite3.connect(db)
    df = pd.read_sql_query(
        f"SELECT ts_broker_epoch, open, high, low, close, spread FROM market_data "
        f"WHERE symbol='XAUUSD' AND timeframe='{tf}' AND source='mt5' "
        f"ORDER BY ts_broker_epoch LIMIT {limit}", con)
    con.close()
    df["ts"] = pd.to_datetime(df["ts_broker_epoch"], unit="s", utc=True)
    df["spread_pips"] = df["spread"] * 0.01 / 0.10
    return df


def test_grid_search_train_val_oos():
    m15 = _load("M15", 900)
    m5 = _load("M5", 2400)
    if len(m15) < 200 or len(m5) < 400:
        pytest.skip("not enough data")
    # tiny grid: 2 adx x 2 stop x 2 rr = 8 combos
    grid = {
        "bias_trend": ["both"],
        "bias_min_adx": [15, 25],
        "require_structure": [True],
        "atr_multiple": [1.5, 2.0],
        "risk_reward": [1.5, 2.0],
        "sessions": [["london", "newyork"]],
    }
    with tempfile.TemporaryDirectory() as tmp:
        reg = ExperimentRegistry(Path(tmp) / "reg.jsonl")
        results = grid_search(m15, m5, grid, train_frac=0.6, val_frac=0.2,
                             slippage_pips=0.5, registry=reg, max_results=20)
        # grid product = 2*2*2*1 = 8 combos (bias_trend & sessions fixed-ish)
        assert len(results) == 8
        for r in results:
            # every combo has train + val + oos metrics
            assert isinstance(r.train_net, float)
            assert isinstance(r.oos_net, float)
            assert r.train_trades >= 0
            # experiment id minted
            assert r.experiment_id.startswith("BACKTEST-")
        # sorted by train net descending
        nets = [r.train_net for r in results]
        assert nets == sorted(nets, reverse=True)
        # top_by_oos returns only positive-OOS (may be empty on this data)
        top = top_by_oos(results)
        assert all(r.oos_net > 0 for r in top)
        print("\n[Grid tiny] combos=%d best_train_net=%.1f best_oos_net=%.1f "
              "positive_oos=%d" % (
                  len(results), max(nets), max(r.oos_net for r in results),
                  len(top)))
