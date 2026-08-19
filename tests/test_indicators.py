"""Tests for the indicator library (Phase 5).

Two kinds of checks:
  - hand-computed tiny series for exact-value regression on EMA/SMA/RSI/ATR/Bollinger
  - smoke test that the full set runs on the real ingested XAUUSD M15 data
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
from indicators import adx, atr, bollinger, ema, macd, rsi, sma


# ---- hand-computed fixtures -------------------------------------------------

@pytest.fixture
def close_series():
    # close prices; choose values so EMA/SMA are easy to check
    return pd.Series([10.0, 11.0, 12.0, 13.0, 14.0, 15.0, 16.0, 17.0])


@pytest.fixture
def ohlc():
    # tiny OHLC with known ATR/TR
    idx = pd.RangeIndex(5)
    high = pd.Series([10, 12, 11, 13, 14], index=idx)
    low = pd.Series([8, 9, 10, 11, 12], index=idx)
    close = pd.Series([9, 11, 10, 12, 13], index=idx)
    return high, low, close


# ---- exact-value tests ------------------------------------------------------

def test_sma_basic(close_series):
    out = sma(close_series, period=3)
    assert out.iloc[2] == pytest.approx(11.0)           # (10+11+12)/3
    assert out.iloc[5] == pytest.approx(14.0)           # (13+14+15)/3
    assert pd.isna(out.iloc[1])                          # warm-up


def test_ema_matches_formula(close_series):
    out = ema(close_series, period=3)
    assert pd.isna(out.iloc[0])            # warm-up (min_periods)
    assert pd.isna(out.iloc[1])
    # alpha = 2/(period+1) = 0.5; pandas ewm adjust=False seeds y0 = x0 = 10
    y0 = 10.0
    y1 = 0.5 * 11.0 + 0.5 * y0      # 10.5
    y2 = 0.5 * 12.0 + 0.5 * y1      # 11.25
    assert out.iloc[2] == pytest.approx(y2)


def test_rsi_monotonic_up_is_100():
    # strictly increasing closes -> no losses -> RSI 100
    s = pd.Series([1, 2, 3, 4, 5, 6, 7, 8, 9, 10])
    out = rsi(s, period=3)
    # last fully-warmed value
    assert out.dropna().iloc[-1] == pytest.approx(100.0)


def test_rsi_bounds(close_series):
    out = rsi(close_series, period=3)
    valid = out.dropna()
    assert (valid >= 0).all() and (valid <= 100).all()


def test_atr_manual(ohlc):
    high, low, close = ohlc
    out = atr(high, low, close, period=2)
    # TR[0] = high-low = 10-8 = 2 (prev_close is NaN -> skipped)
    # TR[1] = max(3, 3, 0) = 3 ; TR[2] = max(1, 0, 1) = 1
    # ewm(seed y0 = TR0 = 2): y1 = 0.5*3+0.5*2 = 2.5 ; y2 = 0.5*1+0.5*2.5 = 1.75
    assert pd.isna(out.iloc[0])            # warm-up (min_periods)
    assert out.iloc[1] == pytest.approx(2.5)
    assert out.iloc[2] == pytest.approx(1.75)


def test_bollinger_middle_is_sma(close_series):
    mid, up, low = bollinger(close_series, period=3, num_std=2.0)
    assert mid.iloc[2] == pytest.approx(sma(close_series, 3).iloc[2])
    assert up.iloc[2] > mid.iloc[2] > low.iloc[2]


def test_macd_zero_when_flat():
    s = pd.Series([100.0] * 40)
    line, sig, hist = macd(s)
    assert line.dropna().abs().max() < 1e-9


def test_adx_low_in_range():
    # flat-ish market -> ADX should stay low (< 25 typical non-trend)
    s = pd.Series([100 + np.sin(i / 3) for i in range(60)])
    a, p, m = adx(s + 5, s - 5, s, period=14)
    assert a.dropna().iloc[-1] < 30


# ---- live-data smoke test ---------------------------------------------------

def _load_m15():
    db = cfg.PROJECT_ROOT / cfg.load_settings()["paths"]["db"]
    con = sqlite3.connect(db)
    df = pd.read_sql_query(
        "SELECT open,high,low,close FROM market_data "
        "WHERE symbol='XAUUSD' AND timeframe='M15' AND source='mt5' ORDER BY ts_broker_epoch",
        con,
    )
    con.close()
    return df


def test_indicators_run_on_real_data():
    df = _load_m15()
    if len(df) < 50:
        pytest.skip("not enough ingested data")
    e = ema(df["close"], 14)
    r = rsi(df["close"], 14)
    a = atr(df["high"], df["low"], df["close"], 14)
    m_line, m_sig, m_hist = macd(df["close"])
    ad, pdi, mdi = adx(df["high"], df["low"], df["close"], 14)
    mid, up, low_b = bollinger(df["close"])
    for s in (e, r, a, m_line, m_sig, m_hist, ad, pdi, mdi, mid, up, low_b):
        assert isinstance(s, pd.Series)
        assert len(s) == len(df)
    # RSI within bounds on real data
    assert (r.dropna() >= 0).all() and (r.dropna() <= 100).all()
    # Bollinger ordering holds
    assert (up.dropna() >= mid.dropna()).all()
    assert (mid.dropna() >= low_b.dropna()).all()
    # ADX non-negative
    assert (ad.dropna() >= 0).all()
