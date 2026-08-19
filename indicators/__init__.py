"""Indicator library (Phase 5).

Small, pure, vectorized functions over OHLCV DataFrames. Each returns a
pandas Series (or tuple of Series) aligned to the input index, with NaN
for warm-up periods. No trading logic, no state -- just math.

Design rules (consistent with the rest of the repo's data layer):
  - inputs are columns named open/high/low/close/volume
  - no mutating the input frame
  - NaN warm-up rather than dropping rows (callers decide alignment)

Reference formulas are standard; tests check known values on a tiny
hand-computed series so a regression is caught immediately.
"""
from __future__ import annotations

import pandas as pd


def ema(close: pd.Series, period: int = 14) -> pd.Series:
    """Exponential moving average of close."""
    return close.ewm(span=period, adjust=False, min_periods=period).mean()


def sma(close: pd.Series, period: int = 14) -> pd.Series:
    """Simple moving average of close."""
    return close.rolling(period, min_periods=period).mean()


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """Wilder's RSI in [0, 100]."""
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0.0, pd.NA)
    out = 100 - 100 / (1 + rs)
    # When avg_loss == 0 (all gains) RSI is 100; when both 0, NaN.
    out = out.where(avg_loss != 0, 100.0)
    return out


def atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    """Average True Range (Wilder's smoothing)."""
    prev_close = close.shift(1)
    tr = pd.concat(
        [
            (high - low),
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()


def macd(
    close: pd.Series,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Return (macd_line, signal_line, histogram)."""
    ema_fast = close.ewm(span=fast, adjust=False, min_periods=fast).mean()
    ema_slow = close.ewm(span=slow, adjust=False, min_periods=slow).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False, min_periods=signal).mean()
    hist = macd_line - signal_line
    return macd_line, signal_line, hist


def adx(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    period: int = 14,
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Return (adx, plus_di, minus_di)."""
    prev_high = high.shift(1)
    prev_low = low.shift(1)
    prev_close = close.shift(1)

    up_move = high - prev_high
    down_move = prev_low - low

    plus_dm = ((up_move > down_move) & (up_move > 0)).astype(float) * up_move
    minus_dm = ((down_move > up_move) & (down_move > 0)).astype(float) * down_move

    tr = pd.concat(
        [(high - low), (high - prev_close).abs(), (low - prev_close).abs()],
        axis=1,
    ).max(axis=1)

    atr_ = tr.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    plus_dm_sm = plus_dm.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    minus_dm_sm = minus_dm.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()

    plus_di = 100 * plus_dm_sm / atr_.replace(0.0, pd.NA)
    minus_di = 100 * minus_dm_sm / atr_.replace(0.0, pd.NA)
    dx = (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0.0, pd.NA) * 100
    adx_ = dx.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    return adx_, plus_di, minus_di


def bollinger(
    close: pd.Series,
    period: int = 20,
    num_std: float = 2.0,
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Return (middle, upper, lower)."""
    middle = close.rolling(period, min_periods=period).mean()
    std = close.rolling(period, min_periods=period).std(ddof=0)
    upper = middle + num_std * std
    lower = middle - num_std * std
    return middle, upper, lower
