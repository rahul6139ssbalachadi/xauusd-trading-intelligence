from __future__ import annotations

import pandas as pd
import pytest

from market_data.base import Timeframe

# Freq alias per Timeframe, using pandas >=3.0 lowercase codes.
_FREQ = {
    Timeframe.M15: "15min",
    Timeframe.H1: "h",
}


def make_clean_ohlcv(
    start: str,
    periods: int,
    timeframe: Timeframe,
    base_price: float = 2000.0,
) -> pd.DataFrame:
    """Weekday-only OHLCV series with no weekend rows, no gaps, no
    duplicate timestamps, and internally consistent OHLC relationships.
    `start` must fall on a weekday session hour for the series to stay
    weekend-clean when `periods` spans multiple days.
    """
    ts = pd.date_range(start, periods=periods, freq=_FREQ[timeframe], tz="UTC")
    ts = ts[ts.dayofweek < 5]  # drop any weekend timestamps outright
    n = len(ts)
    opens = base_price + pd.Series(range(n), dtype=float) * 0.1
    closes = opens + 0.05
    highs = pd.concat([opens, closes], axis=1).max(axis=1) + 0.2
    lows = pd.concat([opens, closes], axis=1).min(axis=1) - 0.2
    volume = pd.Series([100.0] * n)
    return pd.DataFrame(
        {
            "timestamp": ts,
            "open": opens.values,
            "high": highs.values,
            "low": lows.values,
            "close": closes.values,
            "volume": volume.values,
        }
    )


@pytest.fixture
def clean_xauusd_h1() -> pd.DataFrame:
    # 2024-01-01 is a Monday.
    return make_clean_ohlcv("2024-01-01 00:00", 40, Timeframe.H1, base_price=2050.0)


@pytest.fixture
def clean_eurusd_m15() -> pd.DataFrame:
    return make_clean_ohlcv("2024-01-01 00:00", 80, Timeframe.M15, base_price=1.0950)
