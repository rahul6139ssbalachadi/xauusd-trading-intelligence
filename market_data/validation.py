from __future__ import annotations

from dataclasses import dataclass, field
from datetime import time

import pandas as pd

from market_data.base import OHLCV_COLUMNS, Timeframe

_TIMEFRAME_MINUTES = {
    Timeframe.M1: 1,
    Timeframe.M5: 5,
    Timeframe.M15: 15,
    Timeframe.M30: 30,
    Timeframe.H1: 60,
    Timeframe.H4: 240,
    Timeframe.D1: 1440,
}

# XM's daily rollover break for GOLD.i#, confirmed via real MT5 fetch
# 2026-08-16 (XAUUSD M5, 2026-07-17..2026-08-14): every non-Friday day's
# last bar is 23:55 UTC and the next day's first bar is 01:00 UTC.
XM_DAILY_ROLLOVER_WINDOW: list[tuple[time, time]] = [(time(23, 55), time(1, 0))]


def validate_schema(df: pd.DataFrame) -> list[str]:
    """Structural checks only -- columns present, correct dtypes."""
    issues: list[str] = []
    missing = [c for c in OHLCV_COLUMNS if c not in df.columns]
    if missing:
        issues.append(f"missing columns: {missing}")
        return issues  # remaining checks need the columns to exist

    if not pd.api.types.is_datetime64_any_dtype(df["timestamp"]):
        issues.append("timestamp column is not datetime dtype")
    elif df["timestamp"].dt.tz is None:
        issues.append("timestamp column is timezone-naive (expected UTC-aware)")
    elif str(df["timestamp"].dt.tz) != "UTC":
        issues.append(f"timestamp column is tz-aware but not UTC (found {df['timestamp'].dt.tz})")
    for c in ("open", "high", "low", "close", "volume"):
        if not pd.api.types.is_numeric_dtype(df[c]):
            issues.append(f"{c} column is not numeric dtype")
    return issues


def find_duplicate_timestamps(df: pd.DataFrame) -> pd.DataFrame:
    return df[df.duplicated(subset="timestamp", keep=False)]


def find_ohlc_violations(df: pd.DataFrame) -> pd.DataFrame:
    """Rows that cannot represent a real candle: high below open/close/low,
    low above open/close/high, non-positive or missing prices."""
    row_max = df[["open", "close"]].max(axis=1)
    row_min = df[["open", "close"]].min(axis=1)
    prices = df[["open", "high", "low", "close"]]
    bad = (
        (df["high"] < row_max)
        | (df["high"] < df["low"])
        | (df["low"] > row_min)
        | (prices <= 0).any(axis=1)
        | prices.isna().any(axis=1)
    )
    return df[bad]


def _time_in_window(t: time, start: time, end: time) -> bool:
    """True if t falls in [start, end). Handles windows that wrap past
    midnight (e.g. start=23:00, end=00:15)."""
    if start <= end:
        return start <= t < end
    return t >= start or t < end


def find_gaps(
    df: pd.DataFrame,
    timeframe: Timeframe,
    weekend_tolerance_hours: float = 72,
    session_break_windows: list[tuple[time, time]] | None = None,
) -> pd.DataFrame:
    """Bar-to-bar gaps larger than 1.5x the expected interval, excluding
    gaps that bridge a normal FX/CFD weekend closure (Friday close ->
    Sunday/Monday open). This is a heuristic, not a session calendar --
    it will under-flag on symbols with unusual trading hours and should
    be revisited once real data is in hand.

    `session_break_windows`, if given, is a list of (start, end) UTC
    times-of-day for recurring daily broker breaks (e.g. XM's GOLD.i
    rollover). A gap starting inside any window is exempted the same way
    a weekend bridge is. Left as None/unpopulated for now -- we don't
    know XM's actual break schedule yet. Use diagnostics.print_gap_histogram
    on real data first to find the real window(s) before setting this.
    """
    expected_minutes = _TIMEFRAME_MINUTES[timeframe]
    ts = df["timestamp"].sort_values().reset_index(drop=True)
    gap_minutes = ts.diff().dt.total_seconds() / 60
    prev_dow = ts.dt.dayofweek.shift(1)  # Mon=0 ... Sun=6
    is_weekend_bridge = (gap_minutes <= weekend_tolerance_hours * 60) & prev_dow.isin([4, 5])

    is_session_break = pd.Series(False, index=ts.index)
    if session_break_windows:
        prev_time = ts.shift(1).dt.time
        for start, end in session_break_windows:
            is_session_break |= prev_time.apply(
                lambda t: pd.notna(t) and _time_in_window(t, start, end)
            )

    flagged = (
        (gap_minutes > expected_minutes * 1.5)
        & ~is_weekend_bridge
        & ~is_session_break
        & gap_minutes.notna()
    )

    return pd.DataFrame(
        {
            "gap_start": ts.shift(1)[flagged],
            "gap_end": ts[flagged],
            "gap_minutes": gap_minutes[flagged],
        }
    ).reset_index(drop=True)


def find_weekend_rows(
    df: pd.DataFrame,
    friday_close_hour_utc: int = 24,
    sunday_open_hour_utc: int = 21,
) -> pd.DataFrame:
    """Rows timestamped while FX/CFD markets are conventionally closed:
    all of Saturday, Friday after close, Sunday before reopen.

    friday_close_hour_utc default of 24 (never matches an hour 0-23)
    reflects GOLD.i#'s actual observed close: confirmed via real MT5
    fetch 2026-08-16 that it trades right up to the same 23:55 UTC
    daily-rollover cutoff on Fridays as every other day -- the earlier
    21:00 UTC guess was flagging legitimate trading rows. sunday_open_hour_utc
    remains an unverified heuristic -- same caveat as find_gaps() -- but is
    currently harmless for GOLD.i#, which produced zero Sunday rows in
    that fetch (first bar after the weekend was Monday 01:00 UTC).
    24/7 symbols (e.g. BTCUSD) should pass check_weekend=False to
    validate() rather than rely on this producing an empty result.
    """
    ts = df["timestamp"]
    dow = ts.dt.dayofweek  # Mon=0 ... Sun=6
    hour = ts.dt.hour
    is_saturday = dow == 5
    is_friday_after_close = (dow == 4) & (hour >= friday_close_hour_utc)
    is_sunday_before_open = (dow == 6) & (hour < sunday_open_hour_utc)
    flagged = is_saturday | is_friday_after_close | is_sunday_before_open
    return df[flagged]


def compare_sources(
    df_a: pd.DataFrame,
    df_b: pd.DataFrame,
    name_a: str = "source_a",
    name_b: str = "source_b",
    tolerance_pct: float = 0.05,
) -> pd.DataFrame:
    """Section 31: compare two OHLCV frames for the same symbol/timeframe
    on overlapping timestamps. Returns rows where the close price differs
    by more than `tolerance_pct` percent. A non-empty result is a
    DATA_CONFLICT -- callers must not silently pick one source and trade
    on it; the conflict has to be surfaced and resolved.
    """
    cols = ["timestamp", "open", "high", "low", "close"]
    merged = df_a[cols].merge(df_b[cols], on="timestamp", suffixes=(f"_{name_a}", f"_{name_b}"))
    if merged.empty:
        return merged

    close_a = merged[f"close_{name_a}"]
    close_b = merged[f"close_{name_b}"]
    merged["close_pct_diff"] = (close_a - close_b).abs() / close_a.abs() * 100
    conflicts = merged[merged["close_pct_diff"] > tolerance_pct]
    return conflicts.reset_index(drop=True)


@dataclass
class ValidationReport:
    schema_issues: list[str]
    duplicates: pd.DataFrame
    ohlc_violations: pd.DataFrame
    gaps: pd.DataFrame
    weekend_rows: pd.DataFrame = field(default_factory=pd.DataFrame)
    conflicts: pd.DataFrame = field(default_factory=pd.DataFrame)

    @property
    def is_clean(self) -> bool:
        return (
            not self.schema_issues
            and self.duplicates.empty
            and self.ohlc_violations.empty
            and self.weekend_rows.empty
            and self.conflicts.empty
        )


def validate(
    df: pd.DataFrame,
    timeframe: Timeframe,
    other_source: tuple[pd.DataFrame, str, str] | None = None,
    check_weekend: bool = True,
) -> ValidationReport:
    """Run the full validation pass on one OHLCV frame.

    `other_source`, if given, is `(other_df, this_name, other_name)` --
    passed through to compare_sources() to add Section 31 conflict
    detection against a second source for the same symbol/timeframe.

    `check_weekend` should be False for symbols that legitimately trade
    through the weekend (e.g. BTCUSD); leave True for FX/CFD symbols
    like the current XAUUSD/EURUSD bootstrap set.
    """
    schema_issues = validate_schema(df)
    if schema_issues:
        empty = pd.DataFrame()
        return ValidationReport(schema_issues, empty, empty, empty, empty, empty)

    conflicts = pd.DataFrame()
    if other_source is not None:
        other_df, name_a, name_b = other_source
        conflicts = compare_sources(df, other_df, name_a, name_b)

    weekend_rows = find_weekend_rows(df) if check_weekend else pd.DataFrame()

    return ValidationReport(
        schema_issues=[],
        duplicates=find_duplicate_timestamps(df),
        ohlc_violations=find_ohlc_violations(df),
        gaps=find_gaps(df, timeframe, session_break_windows=XM_DAILY_ROLLOVER_WINDOW),
        weekend_rows=weekend_rows,
        conflicts=conflicts,
    )
