"""Market structure module (Phase 6).

Pure, vectorized functions that extract price-action structure from
OHLCV DataFrames: swing highs/lows, HH/HL/LH/LL classification,
Break-of-Structure (BOS) / Change-of-Character (CHoCH) events, trading
sessions, and a volatility-regime tag.

Consistent with the repo's data-layer style (see indicators/):
  - inputs are columns named open/high/low/close
  - no mutating the input frame
  - NaN warm-up rather than dropping rows
  - no trading logic, no state -- just structure math

These are FEATURES, not predictions. The CLAUDE.md spec is explicit:
structure concepts must be statistically tested before being trusted.
This module only computes them.

Reference definitions (standard SMC/lateral-structure framing):
  swing high  : bar whose high is the max over [i-left, i+right]
  swing low   : bar whose low  is the min over [i-left, i+right]
  HH / LH     : a swing high above / below the previous swing high
  HL / LL     : a swing low  above / below the previous swing low
  BOS up/down : swing high above / swing low below the prior same-type swing
  CHoCH down  : swing high below the prior swing high (trend-weakening)
  CHoCH up    : swing low  above the prior swing low  (trend-weakening)
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from indicators import atr

# Broker session windows, expressed in BROKER time (the stored epoch is
# broker-server time, not UTC -- see db/schema/market_data.sql and
# scripts/analyze_gold.py). broker_offset_h converts a UTC-derived hour
# back to broker hour via (utc_hour + offset) % 24.
_SESSION_BOUNDS = [
    ("asia", 0, 7),
    ("london", 7, 13),
    ("newyork", 13, 21),
    ("quiet", 21, 24),
]


# --------------------------------------------------------------------------
# Swing points
# --------------------------------------------------------------------------
def swing_highs(high: pd.Series, left: int = 5, right: int = 5) -> pd.Series:
    """Boolean mask: True where `high` is the local max over [i-left, i+right]."""
    left_max = high.rolling(left + 1, min_periods=left + 1).max()
    right_max = high.shift(-1).rolling(right, min_periods=right).max()
    window_max = pd.concat([left_max, right_max], axis=1).max(axis=1)
    has_left = left_max.notna()
    has_right = right_max.notna()
    return (high == window_max) & has_left & has_right & high.notna()


def swing_lows(low: pd.Series, left: int = 5, right: int = 5) -> pd.Series:
    """Boolean mask: True where `low` is the local min over [i-left, i+right]."""
    left_min = low.rolling(left + 1, min_periods=left + 1).min()
    right_min = low.shift(-1).rolling(right, min_periods=right).min()
    window_min = pd.concat([left_min, right_min], axis=1).min(axis=1)
    has_left = left_min.notna()
    has_right = right_min.notna()
    return (low == window_min) & has_left & has_right & low.notna()


def _swing_events(
    high: pd.Series, low: pd.Series, left: int, right: int
) -> list[dict]:
    """Ordered list of swing points as {idx, kind, price} (kind 'H'/'L')."""
    sh = swing_highs(high, left, right)
    sl = swing_lows(low, left, right)
    events: list[dict] = []
    for i in sh.index[sh]:
        events.append({"idx": i, "kind": "H", "price": float(high.loc[i])})
    for i in sl.index[sl]:
        events.append({"idx": i, "kind": "L", "price": float(low.loc[i])})
    events.sort(key=lambda e: e["idx"])
    return events


# --------------------------------------------------------------------------
# Swing classification (HH / HL / LH / LL)
# --------------------------------------------------------------------------
def classify_swings(
    high: pd.Series, low: pd.Series, left: int = 5, right: int = 5
) -> pd.Series:
    """Series of HH/HL/LH/LL labels at swing bars, NaN elsewhere.

    Equal-to-previous swings are left unlabeled (NaN) and do not update
    the running reference, so a flat run doesn't manufacture a false flip.
    """
    events = _swing_events(high, low, left, right)
    out = pd.Series(index=high.index, dtype="object")
    last_high = None
    last_low = None
    for e in events:
        if e["kind"] == "H":
            if last_high is None:
                last_high = e["price"]
                continue
            if e["price"] > last_high:
                out.loc[e["idx"]] = "HH"
            elif e["price"] < last_high:
                out.loc[e["idx"]] = "LH"
            last_high = e["price"]
        else:  # 'L'
            if last_low is None:
                last_low = e["price"]
                continue
            if e["price"] > last_low:
                out.loc[e["idx"]] = "HL"
            elif e["price"] < last_low:
                out.loc[e["idx"]] = "LL"
            last_low = e["price"]
    return out


# --------------------------------------------------------------------------
# BOS / CHoCH events
# --------------------------------------------------------------------------
def structure_events(
    high: pd.Series, low: pd.Series, left: int = 5, right: int = 5
) -> pd.DataFrame:
    """DataFrame of structure events.

    Columns: bar (int position), swing_type ('H'/'L'), kind
    ('BOS'/'CHoCH'), direction ('up'/'down'), price.

    BOS up   : swing high above the previous swing high
    BOS down : swing low  below the previous swing low
    CHoCH dn : swing high below the previous swing high (trend weakening)
    CHoCH up : swing low  above the previous swing low  (trend weakening)
    """
    events = _swing_events(high, low, left, right)
    rows: list[dict] = []
    last_high = None
    last_low = None
    for e in events:
        if e["kind"] == "H":
            if last_high is None:
                last_high = e["price"]
                continue
            if e["price"] > last_high:
                rows.append(
                    {"bar": e["idx"], "swing_type": "H", "kind": "BOS",
                     "direction": "up", "price": e["price"]}
                )
            elif e["price"] < last_high:
                rows.append(
                    {"bar": e["idx"], "swing_type": "H", "kind": "CHoCH",
                     "direction": "down", "price": e["price"]}
                )
            last_high = e["price"]
        else:  # 'L'
            if last_low is None:
                last_low = e["price"]
                continue
            if e["price"] < last_low:
                rows.append(
                    {"bar": e["idx"], "swing_type": "L", "kind": "BOS",
                     "direction": "down", "price": e["price"]}
                )
            elif e["price"] > last_low:
                rows.append(
                    {"bar": e["idx"], "swing_type": "L", "kind": "CHoCH",
                     "direction": "up", "price": e["price"]}
                )
            last_low = e["price"]
    if not rows:
        return pd.DataFrame(
            columns=["bar", "swing_type", "kind", "direction", "price"]
        )
    return pd.DataFrame(rows).reset_index(drop=True)


# --------------------------------------------------------------------------
# Trading sessions
# --------------------------------------------------------------------------
def session_of(ts: pd.Series, broker_offset_h: int = 3) -> pd.Series:
    """Categorical session label per timestamp, in BROKER time.

    `ts` is a tz-aware datetime Series (or DatetimeIndex). We recover
    broker hour via (utc_hour + offset) % 24, then map:
        asia    00:00-07:00
        london  07:00-13:00
        newyork 13:00-21:00
        quiet   21:00-24:00
    """
    utc_hour = ts.dt.hour if isinstance(ts, pd.Series) else ts.hour
    broker_hour = (utc_hour + broker_offset_h) % 24
    labels = []
    for lo, hi in [(b[1], b[2]) for b in _SESSION_BOUNDS]:
        labels.append(((broker_hour >= lo) & (broker_hour < hi)))
    cats = [b[0] for b in _SESSION_BOUNDS]
    if isinstance(ts, pd.Series):
        idx = ts.index
    else:
        idx = ts  # DatetimeIndex itself is the index
    return pd.Series(
        pd.Categorical(
            np.select(labels, cats, default="quiet"), categories=cats
        ),
        index=idx,
    )


# --------------------------------------------------------------------------
# Volatility regime
# --------------------------------------------------------------------------
def volatility_regime(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    atr_period: int = 14,
    lookback: int = 100,
    threshold: float = 1.5,
) -> pd.Series:
    """Tag each bar 'low' / 'normal' / 'high' by ATR vs its rolling median.

    ratio = ATR / ATR.rolling(lookback).median()
      ratio >  threshold            -> 'high'
      ratio <  1/threshold          -> 'low'
      otherwise                     -> 'normal'
    Warm-up (where ATR or its median is undefined) -> NaN.
    """
    a = atr(high, low, close, atr_period)
    median = a.rolling(lookback, min_periods=lookback).median()
    ratio = a / median.replace(0.0, np.nan)
    out = pd.Series(index=close.index, dtype="object")
    out[ratio > threshold] = "high"
    out[ratio < 1.0 / threshold] = "low"
    out[ratio.notna() & (ratio <= threshold) & (ratio >= 1.0 / threshold)] = "normal"
    return out
