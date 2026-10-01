"""Tests for scripts/ingest_common.py — the MT5 history-ingestion helpers.

The property worth pinning is the bar-cap guard. `copy_rates_range` fills
from the START of a window forward and refuses requests above ~100k bars,
so an oversized window truncates the END: the newest bars never arrive and a
live feed looks stale. BTCUSD M1/M5/M15 sat at 2026-09-28 for exactly that
reason while M30/H1/H4/D1 on the same symbol were current. Nothing raised,
nothing logged, the script printed "skipped 96786" and looked healthy.

So: a window that would be silently truncated must fail LOUDLY at startup,
and a shrinking fetch must never return a silent gap.

No MT5 import here — `fetch_with_shrink` takes the fetch callable, so the
whole module is testable without a terminal.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from ingest_common import (  # noqa: E402
    BAR_CAP,
    BAR_CAP_SAFE,
    BARS_PER_DAY,
    SESSION_FACTOR,
    assert_windows_safe,
    coverage_pct,
    ensure_db,
    estimate_bars,
    fetch_with_shrink,
    find_gaps,
    invalid_ohlc_rows,
    window_is_safe,
)
from market_data.base import Timeframe  # noqa: E402


class TestBarCapGuard:
    """A window that would be silently truncated must raise."""

    def test_full_history_btc_m1_window_is_unsafe(self):
        # The exact window that caused the bug: BTC trades 24/7, so
        # 68d x 1440 = 97,920 bars -- over BAR_CAP entirely.
        assert estimate_bars("M1", 68, "BTCUSD") == 97_920
        assert estimate_bars("M1", 68, "BTCUSD") > BAR_CAP_SAFE
        assert not window_is_safe("M1", 68, "BTCUSD")

    def test_gold_m1_same_window_is_fine_because_it_has_sessions(self):
        # Gold closes for the weekend: the same 68d is ~70k bars, not 98k.
        # This asymmetry is why BTCUSD hit the cap and XAUUSD did not.
        assert estimate_bars("M1", 68, "XAUUSD") < estimate_bars("M1", 68, "BTCUSD")
        assert window_is_safe("M1", 68, "XAUUSD")

    def test_session_factor_is_conservative_for_gold(self):
        # 5/7 (0.714) must stay ABOVE the real ratio, or the guard misses
        # truncation. Measured: XAUUSD H1 3,800d = 61,854 bars = 0.678.
        real = 61_854 / (3_800 * BARS_PER_DAY["H1"])
        assert real < SESSION_FACTOR["XAUUSD"] < 1.0

    def test_recent_topup_windows_are_safe(self):
        for tf, days in [("M1", 5), ("M5", 20), ("M15", 60)]:
            assert window_is_safe(tf, days, "BTCUSD"), f"{tf} {days}d should be safe"

    def test_assert_raises_and_names_the_offender(self):
        with pytest.raises(ValueError) as e:
            assert_windows_safe([(Timeframe.M1, 68), (Timeframe.D1, 3800)],
                                symbol="BTCUSD")
        msg = str(e.value)
        assert "M1 68d" in msg
        # D1 3800d = 3,800 bars, comfortably safe — must not be blamed.
        assert "D1" not in msg

    def test_assert_accepts_the_real_full_history_plans(self):
        # Both plans that actually ran successfully this session.
        assert_windows_safe([
            (Timeframe.M1, 40), (Timeframe.M5, 200), (Timeframe.M15, 800),
            (Timeframe.H1, 3800), (Timeframe.H4, 3800), (Timeframe.D1, 3800),
        ], symbol="XAUUSD")
        assert_windows_safe([
            (Timeframe.M1, 5), (Timeframe.M5, 20), (Timeframe.M15, 60),
        ], symbol="BTCUSD")

    def test_accepts_plain_strings_and_enum_members(self):
        assert window_is_safe("H1", 3800, "XAUUSD")
        assert window_is_safe(Timeframe.H1, 3800, "XAUUSD")
        assert_windows_safe([("H1", 3800), (Timeframe.D1, 3800)], symbol="XAUUSD")

    def test_unknown_symbol_falls_back_to_session_default(self):
        # An unregistered symbol is treated as session-traded rather than
        # crashing. EURUSD behaves like gold: 68d of M1 = 69,942 bars.
        assert estimate_bars("M1", 68, "EURUSD") == 69_942
        assert window_is_safe("H1", 3800, "EURUSD")
        assert window_is_safe("M1", 68, "EURUSD")
        # ...but a genuinely 24/7-sized window is still caught.
        assert not window_is_safe("M1", 120, "EURUSD")

    def test_unknown_timeframe_is_a_loud_keyerror(self):
        # Better to blow up than to silently skip the guard.
        with pytest.raises(KeyError):
            window_is_safe("W1", 100)

    def test_cap_constants_are_ordered(self):
        assert BAR_CAP_SAFE < BAR_CAP


class TestFetchWithShrink:
    def test_returns_first_non_empty_result(self):
        calls = []

        def fetch(d):
            calls.append(d)
            return [1, 2, 3]

        rates, used = fetch_with_shrink(fetch, 100)
        assert rates == [1, 2, 3]
        assert used == 100
        assert calls == [100]  # no needless shrinking

    def test_halves_until_non_empty(self):
        calls = []

        def fetch(d):
            calls.append(d)
            return None if d > 25 else [1]

        rates, used = fetch_with_shrink(fetch, 100)
        assert rates == [1]
        assert used == 25
        assert calls == [100, 50, 25]

    def test_empty_sequence_also_triggers_shrink(self):
        # MT5 returns an empty array, not always None — both must shrink.
        def fetch(d):
            return [] if d > 10 else [7]

        rates, used = fetch_with_shrink(fetch, 40)
        assert rates == [7]
        assert used == 10

    def test_all_empty_returns_none_and_zero(self):
        rates, used = fetch_with_shrink(lambda d: None, 8)
        assert rates is None
        assert used == 0

    def test_never_returns_a_silent_gap(self):
        """(None, 0) is explicit; an empty result is never passed through."""
        rates, used = fetch_with_shrink(lambda d: [], 64)
        assert (rates, used) == (None, 0)

    def test_respects_min_days(self):
        calls = []

        def fetch(d):
            calls.append(d)
            return None

        rates, used = fetch_with_shrink(fetch, 64, min_days=16)
        assert (rates, used) == (None, 0)
        assert calls == [64, 32, 16]  # stops at min_days, never reaches 0

    def test_min_days_one_reaches_one(self):
        calls = []

        def fetch(d):
            calls.append(d)
            return [1] if d == 1 else None

        rates, used = fetch_with_shrink(fetch, 8)
        assert used == 1
        assert calls == [8, 4, 2, 1]


class TestEnsureDb:
    def test_creates_fresh_db_from_schema(self, tmp_path):
        schema = tmp_path / "s.sql"
        schema.write_text(
            "CREATE TABLE market_data (symbol TEXT, timeframe TEXT, "
            "source TEXT, ts_broker_epoch INTEGER, "
            "PRIMARY KEY (symbol, timeframe, source, ts_broker_epoch));"
        )
        conn = ensure_db(tmp_path / "new.db", schema)
        try:
            names = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            assert "market_data" in names
        finally:
            conn.close()

    def test_existing_db_is_not_re_schemed(self, tmp_path):
        db = tmp_path / "x.db"
        sqlite3.connect(db).close()
        schema = tmp_path / "s.sql"
        # A schema that would fail if applied to an existing table.
        schema.write_text("CREATE TABLE market_data (a);")
        conn = ensure_db(db, schema)
        try:
            conn.execute("CREATE TABLE marker (v)")
            conn.commit()
        finally:
            conn.close()
        # Re-open: schema must NOT be re-applied (no "table already exists").
        conn = ensure_db(db, schema)
        try:
            names = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            assert "marker" in names
        finally:
            conn.close()


class TestFindGaps:
    """A missing stretch must be visible; an expected closure must not."""

    def test_regular_series_has_no_gaps(self):
        hourly = [i * 3600 for i in range(500)]
        assert find_gaps(hourly, "H1", "XAUUSD") == []

    def test_weekend_closure_is_not_a_gap_for_gold(self):
        # 50h dead = a normal gold weekend. Must not fire.
        epochs = [0, 3600, 3600 + 50 * 3600, 3600 + 51 * 3600]
        assert find_gaps(epochs, "H1", "XAUUSD") == []

    def test_christmas_closure_is_not_a_gap_for_gold(self):
        # Measured 2020-12-24 H4 gap = 80h; 2016-12-23 D1 gap = 96h.
        epochs = [0, 86400, 86400 + 96 * 3600, 86400 + 97 * 3600]
        assert find_gaps(epochs, "D1", "XAUUSD") == []

    def test_multi_week_hole_is_a_gap_for_gold(self):
        epochs = [0, 3600, 3600 + 137 * 3600]
        gaps = find_gaps(epochs, "H1", "XAUUSD")
        assert len(gaps) == 1
        assert gaps[0] == (3600, 137 * 3600)

    def test_crypto_is_held_to_a_tighter_threshold(self):
        # 50h is fine for gold, a real hole for a 24/7 symbol.
        epochs = [0, 3600, 3600 + 50 * 3600]
        assert find_gaps(epochs, "H1", "XAUUSD") == []
        assert len(find_gaps(epochs, "H1", "BTCUSD")) == 1

    def test_reports_the_epoch_before_each_gap(self):
        # Threshold for gold is 120h, so use 150h holes.
        epochs = [0, 3600, 3600 + 150 * 3600, 3600 + 300 * 3600]
        assert find_gaps(epochs, "H1", "XAUUSD") == [
            (3600, 150 * 3600), (3600 + 150 * 3600, 150 * 3600)]

    def test_a_gap_at_the_threshold_is_not_reported(self):
        # 120h is exactly the limit; only EXCEEDING it is a gap.
        epochs = [0, 3600, 3600 + 120 * 3600]
        assert find_gaps(epochs, "H1", "XAUUSD") == []
        epochs = [0, 3600, 3600 + 121 * 3600]
        assert len(find_gaps(epochs, "H1", "XAUUSD")) == 1

    def test_unsorted_input_is_the_caller_s_problem_but_still_terminates(self):
        # Documented contract: sorted ascending. Unsorted yields nothing
        # rather than an exception, so the caller sees "no gaps" not a crash.
        assert find_gaps([3600, 0, 7200], "H1", "XAUUSD") == []


class TestCoverage:
    WEEK = 168 * 3600  # one calendar week

    def test_gold_week_with_a_weekend_closure_reads_as_100(self):
        # A week is 168h; gold trades ~5/7 of it = 120 hourly bars. That IS
        # complete coverage for a session-traded symbol.
        cov = coverage_pct(120, [0, self.WEEK], "H1", "XAUUSD")
        assert 99 < cov < 101

    def test_same_week_for_crypto_reads_as_incomplete(self):
        # Crypto trades through the weekend, so the identical 120 bars out
        # of 168 expected is a 29% hole.
        cov = coverage_pct(120, [0, self.WEEK], "H1", "BTCUSD")
        assert 70 < cov < 73

    def test_unadjusted_would_misread_gold_as_missing_a_third(self):
        # The bug this guards: without the session factor a healthy gold
        # series reads 71%, which looks like a third of the data is gone.
        epochs = [i * 86400 for i in range(100)]
        assert coverage_pct(70, epochs, "D1", "XAUUSD") > 90

    def test_short_series_is_zero_not_a_divide_by_zero(self):
        assert coverage_pct(1, [0], "D1", "XAUUSD") == 0.0
        assert coverage_pct(0, [], "D1", "XAUUSD") == 0.0


class TestInvalidOhlc:
    def test_valid_rows_pass(self):
        rows = [(0, 100.0, 110.0, 90.0, 105.0),
                (60, 105.0, 105.0, 105.0, 105.0)]  # flat bar is legal
        assert invalid_ohlc_rows(rows) == []

    def test_high_below_low_is_caught(self):
        assert len(invalid_ohlc_rows([(0, 100.0, 90.0, 110.0, 100.0)])) == 1

    def test_high_below_the_body_is_caught(self):
        assert len(invalid_ohlc_rows([(0, 100.0, 99.0, 90.0, 105.0)])) == 1

    def test_low_above_the_body_is_caught(self):
        assert len(invalid_ohlc_rows([(0, 100.0, 110.0, 101.0, 105.0)])) == 1

    def test_non_positive_price_is_caught(self):
        assert len(invalid_ohlc_rows([(0, 0.0, 0.0, 0.0, 0.0)])) == 1
        assert len(invalid_ohlc_rows([(0, -1.0, 1.0, -2.0, 0.0)])) == 1
