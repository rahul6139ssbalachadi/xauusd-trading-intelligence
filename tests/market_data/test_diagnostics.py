from __future__ import annotations

import pandas as pd
import pytest

from market_data.base import Timeframe
from market_data import diagnostics as d


def test_gap_minutes_series_matches_consecutive_diffs(clean_xauusd_h1):
    gaps = d.gap_minutes_series(clean_xauusd_h1)
    assert len(gaps) == len(clean_xauusd_h1) - 1
    assert (gaps == 60.0).all()  # clean H1 fixture has uniform hourly spacing


def test_gap_minutes_series_empty_on_single_row():
    df = pd.DataFrame(
        {
            "timestamp": [pd.Timestamp("2024-01-01 00:00", tz="UTC")],
            "open": [1.0], "high": [1.0], "low": [1.0], "close": [1.0], "volume": [1.0],
        }
    )
    assert d.gap_minutes_series(df).empty


def test_print_gap_histogram_reports_uniform_spacing(clean_xauusd_h1, capsys):
    d.print_gap_histogram(clean_xauusd_h1, Timeframe.H1)
    out = capsys.readouterr().out
    assert "gap histogram" in out
    assert "100.0%" in out  # every gap falls in the single 60-min bucket


def test_print_gap_histogram_empty_frame_prints_message(capsys):
    df = pd.DataFrame(
        {
            "timestamp": [pd.Timestamp("2024-01-01 00:00", tz="UTC")],
            "open": [1.0], "high": [1.0], "low": [1.0], "close": [1.0], "volume": [1.0],
        }
    )
    d.print_gap_histogram(df, Timeframe.H1)
    out = capsys.readouterr().out
    assert "no bars to compare" in out
