"""Tests for the market_structure module (Phase 6).

Two kinds of checks:
  - hand-built OHLC fixtures where swing/structure labels are manually
    verifiable (we know exactly which bars should be HH/HL/BOS/CHoCH)
  - smoke test that the full set runs on real XAUUSD M15 data and
    obeys basic invariants (valid category values, no exceptions)
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
from market_structure import (
    classify_swings,
    session_of,
    structure_events,
    swing_highs,
    swing_lows,
    volatility_regime,
)


# ---- hand-built fixtures --------------------------------------------------

def _df(high, low, close=None):
    idx = pd.RangeIndex(len(high))
    high = pd.Series(high, dtype=float, index=idx)
    low = pd.Series(low, dtype=float, index=idx)
    close = pd.Series(close if close is not None else high, dtype=float, index=idx)
    return high, low, close, idx


@pytest.fixture
def up_then_down():
    # left=right=1 swing detection. Peak at idx2 (high 15) is the local
    # max of [1,2,3]; trough at idx4 (low 8) is the local min of [3,4,5].
    high = [10, 12, 15, 13, 11, 10]
    low = [9, 11, 13, 9, 8, 9]
    close = [10, 12, 14, 11, 9, 10]
    return _df(high, low, close)


@pytest.fixture
def trending_up():
    # Alternating peaks/troughs so each interior bar is a clean swing with
    # left=right=1. Strengthening highs and lows = uptrend.
    # peaks (H): idx1=12, idx3=14, idx5=16, idx7=18 -> HH,HH,HH
    # troughs(L): idx2=8, idx4=10, idx6=12 -> HL,HL
    high = [10, 12, 11, 14, 13, 16, 15, 18, 17]
    low = [8, 9, 8, 11, 10, 13, 12, 15, 14]
    close = [9, 12, 10, 14, 11, 16, 13, 18, 16]
    return _df(high, low, close)


# ---- swing detection ------------------------------------------------------

def test_swing_high_mid_peak(up_then_down):
    high, low, close, idx = up_then_down
    # peak at idx2 (15) is max of window [1,2,3] with left=1,right=1
    sh = swing_highs(high, left=1, right=1)
    assert sh.iloc[2]
    assert not sh.iloc[0]  # not enough left context
    assert not sh.iloc[1]
    assert not sh.iloc[3]


def test_swing_low_trough(up_then_down):
    high, low, close, idx = up_then_down
    # trough at idx4 (low 8) is min of window [3,4,5] = [9,8,9]
    sl = swing_lows(low, left=1, right=1)
    assert sl.iloc[4]
    assert not sl.iloc[3]


# ---- swing classification -------------------------------------------------

def test_classify_higher_highs(trending_up):
    high, low, close, idx = trending_up
    labels = classify_swings(high, low, left=1, right=1)
    # swing highs at idx1(12),idx3(14),idx5(16),idx7(18):
    #   idx1 has no prior -> unlabeled; idx3,5,7 are HH
    high_bars = labels[labels == "HH"]
    assert len(high_bars) == 3  # idx3,5,7
    assert pd.isna(labels.loc[1])
    # swing lows at idx2(8),idx4(10),idx6(12): idx2 is first -> unlabeled;
    # idx4,idx6 are HL
    low_labels = labels[labels == "HL"]
    assert len(low_labels) == 2


def test_classify_equal_swing_unlabeled():
    # Flat consolidation should not manufacture a flip.
    high = [10, 10, 10, 10]
    low = [8, 8, 8, 8]
    close = [9, 9, 9, 9]
    high, low, close, idx = _df(high, low, close)
    labels = classify_swings(high, low, left=1, right=1)
    # no swing at idx0 (no left), idx3 (no right); idx1,2 are equal -> NaN
    assert labels.isna().all()


# ---- structure events (BOS / CHoCH) ---------------------------------------

def test_structure_bos_and_choch():
    # Construct: uptrend then a clear lower high (CHoCH down) and lower low.
    # peaks (H): idx1=12, idx3=14 -> BOS up ; idx5=13 -> CHoCH down (below 14)
    # troughs(L): idx2=9 -> HL ; idx4=11 -> HL ; idx6=8 -> BOS down (below 11)
    high = [10, 12, 11, 14, 13, 13, 11]
    low = [8, 10, 9, 12, 11, 10, 8]
    close = [9, 12, 10, 14, 12, 11, 9]
    high, low, close, idx = _df(high, low, close)
    ev = structure_events(high, low, left=1, right=1)
    kinds = set(ev["kind"])
    assert "BOS" in kinds
    assert "CHoCH" in kinds
    # CHoCH down must have direction 'down'
    ch = ev[ev["kind"] == "CHoCH"]
    assert (ch["direction"] == "down").any()


def test_structure_empty_when_too_short():
    high, low, close, idx = _df([10, 11, 12], [9, 10, 11], [10, 11, 12])
    ev = structure_events(high, low, left=2, right=2)
    assert ev.empty  # not enough context for any swing


# ---- sessions -------------------------------------------------------------

def test_session_of_categories():
    # Build tz-aware UTC timestamps across the broker windows.
    base = pd.Timestamp("2026-01-05", tz="UTC")  # Monday
    # broker offset = 3. broker hour = (utc_hour + 3) % 24.
    # utc 21:00 -> broker 00:00 (asia); utc 04:00 -> broker 07:00 (london);
    # utc 10:00 -> broker 13:00 (newyork); utc 18:00 -> broker 21:00 (quiet)
    times = pd.to_datetime(
        ["2026-01-05 21:00", "2026-01-06 04:00", "2026-01-06 10:00", "2026-01-06 18:00"],
        utc=True,
    )
    s = session_of(times, broker_offset_h=3)
    assert s.iloc[0] == "asia"
    assert s.iloc[1] == "london"
    assert s.iloc[2] == "newyork"
    assert s.iloc[3] == "quiet"


def test_session_of_default_offset():
    # default offset 3; a single UTC midnight bar -> broker 03:00 -> asia
    t = pd.to_datetime(["2026-01-05 00:00"], utc=True)
    s = session_of(t)
    assert s.iloc[0] == "asia"


# ---- volatility regime -----------------------------------------------------

def test_volatility_regime_labels():
    # Build a calm series then a volatility spike.
    rng = np.random.default_rng(0)
    calm = 100 + np.cumsum(rng.normal(0, 0.1, 200))
    spike = calm.copy().astype(float)
    spike[-30:] += np.linspace(0, 5, 30) * (np.arange(30) % 2 * 2 - 1)
    high = pd.Series(calm + 0.2)
    low = pd.Series(calm - 0.2)
    close = pd.Series(calm)
    high_s = pd.Series(spike + 0.2)
    low_s = pd.Series(spike - 0.2)
    close_s = pd.Series(spike)
    # run on the spiked series
    reg = volatility_regime(high_s, low_s, close_s, atr_period=14, lookback=100)
    valid = {"low", "normal", "high"}
    assert set(reg.dropna().unique()).issubset(valid)
    # early bars are warm-up -> NaN
    assert pd.isna(reg.iloc[50])


def test_vol_regime_invariants_on_real_data():
    df = _load_m15()
    if len(df) < 250:
        pytest.skip("not enough ingested data")
    reg = volatility_regime(df["high"], df["low"], df["close"])
    valid = {"low", "normal", "high"}
    assert set(reg.dropna().unique()).issubset(valid)
    # there should be some 'high' and some 'low' over 2 years of gold
    assert (reg == "high").any()
    assert (reg == "low").any()


# ---- real-data smoke test --------------------------------------------------

def _load_m15():
    db = cfg.PROJECT_ROOT / cfg.load_settings()["paths"]["db"]
    con = sqlite3.connect(db)
    df = pd.read_sql_query(
        "SELECT ts_broker_epoch, open, high, low, close FROM market_data "
        "WHERE symbol='XAUUSD' AND timeframe='M15' AND source='mt5' ORDER BY ts_broker_epoch",
        con,
    )
    con.close()
    df["ts"] = pd.to_datetime(df["ts_broker_epoch"], unit="s", utc=True)
    return df


def test_market_structure_runs_on_real_data():
    df = _load_m15()
    if len(df) < 50:
        pytest.skip("not enough ingested data")
    sh = swing_highs(df["high"], 5, 5)
    sl = swing_lows(df["low"], 5, 5)
    labels = classify_swings(df["high"], df["low"], 5, 5)
    ev = structure_events(df["high"], df["low"], 5, 5)
    sess = session_of(df["ts"])
    reg = volatility_regime(df["high"], df["low"], df["close"])

    # invariants
    assert isinstance(sh, pd.Series) and len(sh) == len(df)
    assert isinstance(sl, pd.Series) and len(sl) == len(df)
    assert isinstance(labels, pd.Series) and len(labels) == len(df)
    assert set(ev.columns) == {"bar", "swing_type", "kind", "direction", "price"}
    assert set(sess.unique()).issubset({"asia", "london", "newyork", "quiet"})
    assert set(reg.dropna().unique()).issubset({"low", "normal", "high"})
    # at least some structure detected over 2 years of gold
    assert sh.sum() > 0
    assert sl.sum() > 0
    assert len(ev) > 0
