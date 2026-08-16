from __future__ import annotations

import pandas as pd
import pytest

from market_data.base import Timeframe
from market_data import validation as v

from .conftest import make_clean_ohlcv


# ---------------------------------------------------------------------------
# validate_schema
# ---------------------------------------------------------------------------


def test_schema_clean_frame_has_no_issues(clean_xauusd_h1):
    assert v.validate_schema(clean_xauusd_h1) == []


def test_schema_flags_missing_columns(clean_xauusd_h1):
    df = clean_xauusd_h1.drop(columns=["volume"])
    issues = v.validate_schema(df)
    assert any("missing columns" in i for i in issues)


def test_schema_flags_non_numeric_price_column(clean_xauusd_h1):
    df = clean_xauusd_h1.copy()
    df["close"] = df["close"].astype(str)
    issues = v.validate_schema(df)
    assert any("close" in i and "numeric" in i for i in issues)


def test_schema_flags_timezone_naive_timestamp(clean_xauusd_h1):
    df = clean_xauusd_h1.copy()
    df["timestamp"] = df["timestamp"].dt.tz_localize(None)
    issues = v.validate_schema(df)
    assert any("timezone-naive" in i for i in issues)


def test_schema_flags_non_utc_timezone(clean_xauusd_h1):
    df = clean_xauusd_h1.copy()
    df["timestamp"] = df["timestamp"].dt.tz_convert("America/New_York")
    issues = v.validate_schema(df)
    assert any("not UTC" in i for i in issues)


def test_schema_utc_timestamp_passes(clean_xauusd_h1):
    # Sanity check the positive case explicitly, not just via the
    # "clean frame has no issues" test above.
    assert clean_xauusd_h1["timestamp"].dt.tz is not None
    assert str(clean_xauusd_h1["timestamp"].dt.tz) == "UTC"


# ---------------------------------------------------------------------------
# duplicate timestamps
# ---------------------------------------------------------------------------


def test_find_duplicate_timestamps_none_on_clean_frame(clean_eurusd_m15):
    assert v.find_duplicate_timestamps(clean_eurusd_m15).empty


def test_find_duplicate_timestamps_detects_repeats(clean_eurusd_m15):
    df = pd.concat([clean_eurusd_m15, clean_eurusd_m15.iloc[[3]]], ignore_index=True)
    dups = v.find_duplicate_timestamps(df)
    assert len(dups) == 2  # both the original row and its repeat
    assert dups["timestamp"].nunique() == 1


# ---------------------------------------------------------------------------
# OHLC violations (structural + zero/negative/NaN prices)
# ---------------------------------------------------------------------------


def test_find_ohlc_violations_none_on_clean_frame(clean_xauusd_h1):
    assert v.find_ohlc_violations(clean_xauusd_h1).empty


@pytest.mark.parametrize(
    "field,value",
    [
        ("open", 0.0),
        ("close", -5.0),
        ("high", -0.01),
        ("low", 0.0),
    ],
)
def test_find_ohlc_violations_flags_zero_and_negative_prices(clean_xauusd_h1, field, value):
    df = clean_xauusd_h1.copy()
    df.loc[10, field] = value
    bad = v.find_ohlc_violations(df)
    assert 10 in bad.index


def test_find_ohlc_violations_flags_nan_price(clean_xauusd_h1):
    df = clean_xauusd_h1.copy()
    df.loc[5, "close"] = float("nan")
    bad = v.find_ohlc_violations(df)
    assert 5 in bad.index


def test_find_ohlc_violations_flags_high_below_low(clean_xauusd_h1):
    df = clean_xauusd_h1.copy()
    df.loc[7, "high"] = df.loc[7, "low"] - 1.0
    bad = v.find_ohlc_violations(df)
    assert 7 in bad.index


def test_find_ohlc_violations_flags_high_below_close(clean_xauusd_h1):
    df = clean_xauusd_h1.copy()
    df.loc[8, "high"] = df.loc[8, "close"] - 1.0
    bad = v.find_ohlc_violations(df)
    assert 8 in bad.index


# ---------------------------------------------------------------------------
# gaps
# ---------------------------------------------------------------------------


def test_find_gaps_none_on_clean_frame(clean_xauusd_h1):
    assert v.find_gaps(clean_xauusd_h1, Timeframe.H1).empty


def test_find_gaps_detects_missing_weekday_bar(clean_xauusd_h1):
    df = clean_xauusd_h1.drop(index=15).reset_index(drop=True)
    gaps = v.find_gaps(df, Timeframe.H1)
    assert len(gaps) == 1
    assert gaps.loc[0, "gap_minutes"] == 120


def test_find_gaps_session_break_windows_default_none_flags_recurring_gap():
    # Monday 22:00 -> Tuesday 00:00, a 120-min gap on a weekday (not a
    # weekend bridge). With no session_break_windows given, this must
    # still be flagged -- default behaviour is unchanged.
    df = make_clean_ohlcv("2024-01-01 00:00", 5, Timeframe.H1)  # Mon 00:00..04:00
    extra = pd.DataFrame(
        {
            "timestamp": [
                pd.Timestamp("2024-01-01 22:00", tz="UTC"),
                pd.Timestamp("2024-01-02 00:00", tz="UTC"),
            ],
            **{c: [1.0, 1.0] for c in ("open", "high", "low", "close", "volume")},
        }
    )
    df = pd.concat([df, extra], ignore_index=True)
    gaps = v.find_gaps(df, Timeframe.H1)
    assert pd.Timestamp("2024-01-02 00:00", tz="UTC") in set(gaps["gap_end"])


def test_find_gaps_session_break_windows_exempts_recurring_gap():
    from datetime import time

    df = make_clean_ohlcv("2024-01-01 00:00", 5, Timeframe.H1)  # Mon 00:00..04:00
    extra = pd.DataFrame(
        {
            "timestamp": [
                pd.Timestamp("2024-01-01 22:00", tz="UTC"),
                pd.Timestamp("2024-01-02 00:00", tz="UTC"),
            ],
            **{c: [1.0, 1.0] for c in ("open", "high", "low", "close", "volume")},
        }
    )
    df = pd.concat([df, extra], ignore_index=True)
    gaps = v.find_gaps(
        df, Timeframe.H1, session_break_windows=[(time(22, 0), time(0, 0))]
    )
    assert pd.Timestamp("2024-01-02 00:00", tz="UTC") not in set(gaps["gap_end"])


def test_find_gaps_does_not_flag_normal_weekend_bridge():
    # Friday 20:00 UTC -> Monday 00:00 UTC is a normal FX weekend
    # closure, not a data gap.
    friday = pd.Timestamp("2024-01-05 20:00", tz="UTC")
    monday = pd.Timestamp("2024-01-08 00:00", tz="UTC")
    df = make_clean_ohlcv("2024-01-01 00:00", 5, Timeframe.H1)
    df = pd.concat(
        [df, pd.DataFrame({"timestamp": [friday, monday], **{c: [1.0, 1.0] for c in ("open", "high", "low", "close", "volume")}})],
        ignore_index=True,
    )
    gaps = v.find_gaps(df, Timeframe.H1)
    assert monday not in set(gaps["gap_end"])


# ---------------------------------------------------------------------------
# weekend rows
# ---------------------------------------------------------------------------


def test_find_weekend_rows_none_on_clean_frame(clean_xauusd_h1, clean_eurusd_m15):
    assert v.find_weekend_rows(clean_xauusd_h1).empty
    assert v.find_weekend_rows(clean_eurusd_m15).empty


def test_find_weekend_rows_flags_saturday():
    df = pd.DataFrame(
        {
            "timestamp": [pd.Timestamp("2024-01-06 12:00", tz="UTC")],  # Saturday
            "open": [1.0], "high": [1.1], "low": [0.9], "close": [1.0], "volume": [1.0],
        }
    )
    assert len(v.find_weekend_rows(df)) == 1


def test_find_weekend_rows_flags_friday_after_close():
    # GOLD.i#'s actual observed close (confirmed via live MT5 fetch
    # 2026-08-16) is 23:55 UTC -- the same daily-rollover cutoff as every
    # other day -- not 21:00 UTC. So the open/closed boundary falls
    # between Friday 23:55 (still trading) and Saturday 00:00 (closed),
    # not at any Friday intraday hour.
    df = pd.DataFrame(
        {
            "timestamp": [
                pd.Timestamp("2024-01-05 23:55", tz="UTC"),  # Friday, actual last trading bar
                pd.Timestamp("2024-01-06 00:00", tz="UTC"),  # immediately after, market closed
            ],
            "open": [1.0, 1.0], "high": [1.1, 1.1], "low": [0.9, 0.9], "close": [1.0, 1.0], "volume": [1.0, 1.0],
        }
    )
    flagged_timestamps = set(v.find_weekend_rows(df)["timestamp"])
    assert flagged_timestamps == {pd.Timestamp("2024-01-06 00:00", tz="UTC")}


def test_find_weekend_rows_does_not_flag_friday_before_close():
    df = pd.DataFrame(
        {
            "timestamp": [pd.Timestamp("2024-01-05 20:00", tz="UTC")],  # Friday, before 21:00 UTC
            "open": [1.0], "high": [1.1], "low": [0.9], "close": [1.0], "volume": [1.0],
        }
    )
    assert v.find_weekend_rows(df).empty


def test_find_weekend_rows_flags_sunday_before_open():
    df = pd.DataFrame(
        {
            "timestamp": [pd.Timestamp("2024-01-07 10:00", tz="UTC")],  # Sunday, before 21:00 UTC
            "open": [1.0], "high": [1.1], "low": [0.9], "close": [1.0], "volume": [1.0],
        }
    )
    assert len(v.find_weekend_rows(df)) == 1


def test_find_weekend_rows_does_not_flag_sunday_after_open():
    df = pd.DataFrame(
        {
            "timestamp": [pd.Timestamp("2024-01-07 22:00", tz="UTC")],  # Sunday, after 21:00 UTC reopen
            "open": [1.0], "high": [1.1], "low": [0.9], "close": [1.0], "volume": [1.0],
        }
    )
    assert v.find_weekend_rows(df).empty


# ---------------------------------------------------------------------------
# compare_sources (Section 31 conflict detection)
# ---------------------------------------------------------------------------


def test_compare_sources_no_conflict_on_matching_data(clean_xauusd_h1):
    conflicts = v.compare_sources(clean_xauusd_h1, clean_xauusd_h1.copy(), "a", "b")
    assert conflicts.empty


def test_compare_sources_flags_diverging_close(clean_xauusd_h1):
    other = clean_xauusd_h1.copy()
    other.loc[3, "close"] *= 1.10  # 10% off, well past the 0.05% default tolerance
    conflicts = v.compare_sources(clean_xauusd_h1, other, "dukascopy", "histdata")
    assert len(conflicts) == 1
    assert conflicts.loc[0, "close_pct_diff"] > 0.05


# ---------------------------------------------------------------------------
# validate() end-to-end
# ---------------------------------------------------------------------------


def test_validate_clean_frame_is_clean(clean_xauusd_h1):
    report = v.validate(clean_xauusd_h1, Timeframe.H1)
    assert report.is_clean


def test_validate_clean_eurusd_m15_is_clean(clean_eurusd_m15):
    report = v.validate(clean_eurusd_m15, Timeframe.M15)
    assert report.is_clean


def test_validate_short_circuits_on_schema_issues(clean_xauusd_h1):
    df = clean_xauusd_h1.drop(columns=["volume"])
    report = v.validate(df, Timeframe.H1)
    assert not report.is_clean
    assert report.schema_issues
    assert report.duplicates.empty  # remaining checks skipped, not run

def test_validate_flags_weekend_rows_by_default(clean_xauusd_h1):
    df = clean_xauusd_h1.copy()
    saturday_row = df.iloc[[0]].copy()
    saturday_row["timestamp"] = pd.Timestamp("2024-01-06 12:00", tz="UTC")
    df = pd.concat([df, saturday_row], ignore_index=True)
    report = v.validate(df, Timeframe.H1)
    assert not report.is_clean
    assert len(report.weekend_rows) == 1


def test_validate_check_weekend_false_skips_weekend_check(clean_xauusd_h1):
    df = clean_xauusd_h1.copy()
    saturday_row = df.iloc[[0]].copy()
    saturday_row["timestamp"] = pd.Timestamp("2024-01-06 12:00", tz="UTC")
    df = pd.concat([df, saturday_row], ignore_index=True)
    report = v.validate(df, Timeframe.H1, check_weekend=False)
    assert report.weekend_rows.empty


def test_validate_surfaces_conflicts_with_other_source(clean_xauusd_h1):
    other = clean_xauusd_h1.copy()
    other.loc[0, "close"] *= 1.10
    report = v.validate(
        clean_xauusd_h1, Timeframe.H1, other_source=(other, "dukascopy", "histdata")
    )
    assert not report.is_clean
    assert len(report.conflicts) == 1
