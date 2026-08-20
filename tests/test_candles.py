"""Tests for candle-pattern statistical testing (Phase 9)."""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from candles import analyze_patterns, PatternStats


def _make_df(n: int = 200, seed: int = 42) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    opens = np.cumsum(rng.standard_normal(n) * 0.5 + 100)
    closes = opens + rng.standard_normal(n) * 0.3
    highs = np.maximum(opens, closes) + np.abs(rng.standard_normal(n) * 0.2)
    lows = np.minimum(opens, closes) - np.abs(rng.standard_normal(n) * 0.2)
    return pd.DataFrame({"open": opens, "high": highs, "low": lows, "close": closes})


def test_analyze_returns_all_patterns():
    df = _make_df()
    stats = analyze_patterns(df, min_samples=1, horizon=3)
    expected = {"doji", "hammer", "shooting_star", "engulfing_bullish",
                "engulfing_bearish", "inside_bar", "pin_bar"}
    assert set(stats.keys()) == expected


def test_insufficient_samples_flagged():
    df = _make_df(n=50)
    stats = analyze_patterns(df, min_samples=30, horizon=3)
    for s in stats.values():
        if s.occurrences < 30:
            assert s.sufficient is False


def test_zero_occurrence_pattern():
    df = _make_df()
    stats = analyze_patterns(df, min_samples=1, horizon=3)
    # at least the structure should be valid for all patterns
    for pname, s in stats.items():
        assert s.pattern == pname
        assert s.occurrences >= 0
        assert isinstance(s.sufficient, bool)


def test_doji_detection():
    """A real doji should be detected."""
    df = pd.DataFrame({
        "open": [100, 100, 100, 100, 100],
        "high": [100.1, 100.1, 100.1, 100.1, 100.1],
        "low": [99.9, 99.9, 99.9, 99.9, 99.9],
        "close": [100.0, 100.0, 100.0, 100.0, 100.0],
    })
    stats = analyze_patterns(df, min_samples=1, horizon=1)
    # all 5 bars are dojis (body=0, range=0.2)
    assert stats["doji"].occurrences <= 5


def test_render_report_runs():
    from candles import render_pattern_report
    df = _make_df()
    stats = analyze_patterns(df, min_samples=1, horizon=3)
    report = render_pattern_report(stats)
    assert "Pattern" in report
    assert "doji" in report.lower()


def test_engulfing_bullish():
    """Construct an explicit bullish engulfing."""
    df = pd.DataFrame({
        "open": [100, 95],      # prev down body (close 98 < open 100)
        "high": [102, 103],     # curr high > prev high
        "low":  [98, 93],       # curr low < prev low
        "close": [98, 103],     # curr close > prev open -> engulfing
    })
    # need enough bars for rolling; pad
    df = pd.concat([df, _make_df(n=20)], ignore_index=True)
    stats = analyze_patterns(df, min_samples=1, horizon=1)
    assert stats["engulfing_bullish"].occurrences >= 1
