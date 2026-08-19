"""Tests for the strategy engine (Phase 7).

Two kinds of checks:
  - unit tests on the evaluator with hand-built feature frames where the
    WAIT/BUY decision and reasons are known in advance
  - smoke test: load the V1 JSON, build features on real M15/M5 gold data,
    run evaluate() end-to-end, and assert structural invariants
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from market_data import config as cfg
from strategy import (
    Strategy,
    build_features,
    evaluate,
)
from backtest import run_backtest

DEF = (
    Path(__file__).resolve().parents[1]
    / "strategy"
    / "defs"
    / "XAUUSD_STRUCTURE_BREAK_V1.json"
)


@pytest.fixture
def strat():
    return Strategy.load(DEF)


def _frame(close, high=None, low=None, session="newyork", vol="normal",
           rsi=50.0, adx=30.0, ema_fast=None, ema_slow=None, atr=1.0,
           bos_dir="up", ch_dir=pd.NA):
    n = len(close)
    idx = pd.RangeIndex(n)
    close = pd.Series(close, dtype=float, index=idx)
    high = pd.Series(high if high is not None else close, dtype=float, index=idx)
    low = pd.Series(low if low is not None else close, dtype=float, index=idx)
    ema_fast = pd.Series(ema_fast if ema_fast is not None else close, dtype=float, index=idx)
    ema_slow = pd.Series(ema_slow if ema_slow is not None else (close * 0.9), dtype=float, index=idx)
    out = pd.DataFrame({
        "ts": pd.date_range("2026-01-05 14:00", periods=n, freq="5min", tz="UTC"),
        "open": close, "high": high, "low": low, "close": close,
        "ema_fast": ema_fast, "ema_slow": ema_slow,
        "rsi": pd.Series([rsi] * n, dtype=float, index=idx),
        "adx": pd.Series([adx] * n, dtype=float, index=idx),
        "atr": pd.Series([atr] * n, dtype=float, index=idx),
        "session": pd.Series([session] * n, dtype="object", index=idx),
        "vol_regime": pd.Series([vol] * n, dtype="object", index=idx),
        "spread_pips": pd.Series([1.0] * n, dtype=float, index=idx),
        "last_bos_dir": pd.Series([bos_dir] * n, dtype="object", index=idx),
        "last_choch_dir": pd.Series([ch_dir] * n, dtype="object", index=idx),
    })
    return out


# ---- evaluator unit tests -------------------------------------------------

def test_wait_when_session_excluded(strat):
    df = _frame([10, 10, 10], session="asia")
    res = evaluate(strat, df, df)
    assert (res["signal"] == "WAIT").all()
    assert any("session" in r[0].lower() for r in res["reasons"])


def test_wait_when_bias_down(strat):
    # ema_fast below ema_slow -> bias down, but strategy requires up
    df = _frame([10, 9, 8], ema_fast=[9, 8, 7], ema_slow=[10, 10, 10])
    res = evaluate(strat, df, df)
    assert (res["signal"] == "WAIT").all()
    assert any("bias" in r[0].lower() for r in res["reasons"])


def test_wait_when_adx_too_low(strat):
    df = _frame([10, 11, 12], adx=10.0)
    res = evaluate(strat, df, df)
    assert (res["signal"] == "WAIT").all()
    assert any("adx" in r[0].lower() for r in res["reasons"])


def test_wait_when_rsi_overbought(strat):
    df = _frame([10, 11, 12], rsi=82.0)
    res = evaluate(strat, df, df)
    assert (res["signal"] == "WAIT").all()


def test_wait_when_no_structure_trigger(strat):
    df = _frame([10, 11, 12], bos_dir=pd.NA, ch_dir=pd.NA)
    res = evaluate(strat, df, df)
    assert (res["signal"] == "WAIT").all()
    assert any("structure" in r[0].lower() for r in res["reasons"])


def test_buy_when_all_conditions_met(strat):
    df = _frame([10, 11, 12], adx=30.0, rsi=55.0, bos_dir="up")
    res = evaluate(strat, df, df)
    assert (res["signal"] == "BUY").all()
    # ATR-based stop below price, target above (R:R 2, mult 1.5)
    stop = res["stop"].iloc[-1]
    target = res["target"].iloc[-1]
    assert stop < res["close"].iloc[-1] < target


def test_stops_targets_direction():
    # BUY: stop below, target above. Check via a SELL-required strategy by
    # flipping bias_trend isn't supported; instead assert BUY geometry only.
    s = Strategy.load(DEF)
    df = _frame([10, 11, 12], adx=30.0, rsi=55.0, bos_dir="up")
    res = evaluate(s, df, df)
    assert res["stop"].iloc[-1] < res["close"].iloc[-1] < res["target"].iloc[-1]


# ---- serialization --------------------------------------------------------

def test_strategy_roundtrip(strat):
    s2 = Strategy.load(DEF)
    assert s2.name == strat.name
    assert s2.version == strat.version
    assert s2.risk_pct == strat.risk_pct
    # to_json is valid json and round-trips
    import json
    json.loads(strat.to_json())


def test_strategy_save_creates_file(tmp_path, strat):
    p = strat.save(tmp_path)
    assert p.exists()
    assert p.name == "XAUUSD_STRUCTURE_BREAK_V1.json"
    reloaded = Strategy.load(p)
    assert reloaded.name == strat.name


# ---- real-data smoke test --------------------------------------------------

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
    # spread stored in POINTS; convert to pips (gold: $ = pts*0.01, pip = $/0.10)
    df["spread_pips"] = df["spread"] * 0.01 / 0.10
    return df


def test_evaluate_runs_on_real_data(strat):
    m15 = _load("M15")
    m5 = _load("M5")
    if len(m15) < 200 or len(m5) < 200:
        pytest.skip("not enough ingested data")
    feats_m15 = build_features(m15, swing_left=5, swing_right=5)
    feats_m5 = build_features(m5, swing_left=5, swing_right=5)
    # align m5 to m15 not required for evaluate; evaluate uses bias (m15) + trigger (m5)
    res = evaluate(strat, feats_m15, feats_m5)
    assert len(res) == len(feats_m5)
    assert set(res["signal"].unique()).issubset({"BUY", "SELL", "WAIT"})
    # every non-WAIT row must have a stop and target
    acts = res[res["signal"] != "WAIT"]
    assert (acts["stop"].notna() & acts["target"].notna()).all()
    # there should be at least some WAIT rows (filters reject most bars)
    assert (res["signal"] == "WAIT").any()


V2 = (
    Path(__file__).resolve().parents[1]
    / "strategy"
    / "defs"
    / "XAUUSD_STRUCTURE_BREAK_V2.json"
)


def test_v2_two_sided_emits_both_directions():
    m15 = _load("M15")
    m5 = _load("M5")
    if len(m15) < 200 or len(m5) < 200:
        pytest.skip("not enough ingested data")
    feats_m15 = build_features(m15, swing_left=5, swing_right=5)
    feats_m5 = build_features(m5, swing_left=5, swing_right=5)
    v2 = Strategy.load(V2)
    res = evaluate(v2, feats_m15, feats_m5)
    acts = res[res["signal"] != "WAIT"]
    # V2 is two-sided, so it must be able to emit SELL as well as BUY
    assert "SELL" in acts["signal"].values
    assert "BUY" in acts["signal"].values


def test_v1_vs_v2_backtest_on_real_data():
    """Honest comparison: V1 (long-only) vs V2 (two-sided) on the same data.

    Both run read-only against the stored DB. We only COMPARE; neither is
    tuned here. Reports the computed headline metrics for each.
    """
    m15 = _load("M15")
    m5 = _load("M5")
    if len(m15) < 300 or len(m5) < 300:
        pytest.skip("not enough ingested data")
    v1 = Strategy.load(DEF)
    v2 = Strategy.load(V2)
    r1 = run_backtest(v1, m15, m5, slippage_pips=0.5)
    r2 = run_backtest(v2, m15, m5, slippage_pips=0.5)
    m1, m2 = r1.metrics, r2.metrics
    # both must produce a valid metrics dict
    assert m1["total_trades"] >= 0 and m2["total_trades"] >= 0
    # two-sided V2 should trade at least as often as long-only V1
    assert m2["total_trades"] >= m1["total_trades"]
    print("\n[V1 vs V2 real gold] V1 trades=%d net=%.1f pf=%.2f | "
          "V2 trades=%d net=%.1f pf=%.2f win%%=%.1f" % (
              m1["total_trades"], m1["net_pips"], m1["profit_factor"],
              m2["total_trades"], m2["net_pips"], m2["profit_factor"],
              m2["win_rate"] * 100))
