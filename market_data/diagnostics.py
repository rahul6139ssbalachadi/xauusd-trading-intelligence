from __future__ import annotations

import pandas as pd

from market_data.base import Timeframe
from market_data.validation import _TIMEFRAME_MINUTES


def gap_minutes_series(df: pd.DataFrame) -> pd.Series:
    """Every consecutive bar-to-bar gap in minutes, sorted by timestamp.
    Unlike find_gaps(), this includes normal spacing and small gaps too --
    it's raw evidence, not a flagged-issues list."""
    ts = df["timestamp"].sort_values().reset_index(drop=True)
    return (ts.diff().dt.total_seconds() / 60).dropna()


def print_gap_histogram(
    df: pd.DataFrame,
    timeframe: Timeframe,
    bin_width_minutes: float | None = None,
) -> None:
    """Print a text histogram of bar-to-bar gaps for `df`.

    Includes every consecutive gap, not just ones large enough for
    find_gaps() to flag -- the point is to see the *whole* distribution
    (normal bar spacing, recurring session breaks, weekend closures) once
    real GOLD.i# data is loaded, so session_break_windows thresholds for
    find_gaps() can be set from evidence instead of guessed.
    """
    gaps = gap_minutes_series(df)
    if gaps.empty:
        print("no bars to compare (need >= 2 rows)")
        return

    expected = _TIMEFRAME_MINUTES[timeframe]
    width = bin_width_minutes or expected
    bucket = (gaps // width * width)
    counts = bucket.value_counts().sort_index()
    total = len(gaps)

    print(
        f"gap histogram: {total} intervals, timeframe={timeframe.name} "
        f"(expected spacing = {expected} min), bin width = {width} min"
    )
    for bucket_start, count in counts.items():
        pct = count / total * 100
        bar = "#" * max(1, round(pct / 2))
        print(f"  {bucket_start:>8.0f}-{bucket_start + width:<8.0f} min | {count:>6} ({pct:5.1f}%) {bar}")
