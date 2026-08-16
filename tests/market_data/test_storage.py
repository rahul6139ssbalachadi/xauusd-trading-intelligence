from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from market_data.base import Timeframe
from market_data.storage import IngestResult, insert_mt5_bars

_SCHEMA_PATH = Path(__file__).resolve().parents[2] / "db" / "schema" / "market_data.sql"


@pytest.fixture
def conn():
    connection = sqlite3.connect(":memory:")
    connection.executescript(_SCHEMA_PATH.read_text())
    yield connection
    connection.close()


def _rate(time, open_, high, low, close, tick_volume=100, spread=20):
    return {
        "time": time,
        "open": open_,
        "high": high,
        "low": low,
        "close": close,
        "tick_volume": tick_volume,
        "spread": spread,
    }


def test_insert_is_idempotent_on_rerun(conn):
    rates = [
        _rate(1704067200, 2050.0, 2051.0, 2049.5, 2050.5),
        _rate(1704067500, 2050.5, 2052.0, 2050.0, 2051.8),
    ]
    first = insert_mt5_bars(conn, "XAUUSD", Timeframe.M5, rates)
    assert first == IngestResult(inserted=2, skipped=0)

    # Re-running the identical fetch must not duplicate rows.
    second = insert_mt5_bars(conn, "XAUUSD", Timeframe.M5, rates)
    assert second == IngestResult(inserted=0, skipped=2)

    total = conn.execute("SELECT COUNT(*) FROM market_data").fetchone()[0]
    assert total == 2


def test_insert_stores_raw_epoch_and_source_unconverted(conn):
    insert_mt5_bars(conn, "XAUUSD", Timeframe.M5, [_rate(1704067200, 2050.0, 2051.0, 2049.5, 2050.5)])
    row = conn.execute(
        "SELECT ts_broker_epoch, source, timeframe FROM market_data"
    ).fetchone()
    assert row == (1704067200, "mt5", "M5")  # exact raw int, literal source, plain string timeframe


def test_check_violation_raises_and_does_not_silently_drop(conn):
    # high < low -- violates CHECK (high >= low). A naive "INSERT OR
    # IGNORE" would silently swallow this like a PK conflict; it must
    # raise instead.
    bad_row = _rate(1704067200, 2050.0, 2049.0, 2049.5, 2050.5)
    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
        insert_mt5_bars(conn, "XAUUSD", Timeframe.M5, [bad_row])

    total = conn.execute("SELECT COUNT(*) FROM market_data").fetchone()[0]
    assert total == 0  # nothing was silently inserted


def test_check_violation_rolls_back_whole_batch(conn):
    # A CHECK violation anywhere in the batch must not leave earlier,
    # otherwise-valid rows from the same call committed -- the batch
    # is one transaction, not a partial best-effort insert.
    rates = [
        _rate(1704067200, 2050.0, 2051.0, 2049.5, 2050.5),  # valid
        _rate(1704067500, 2050.0, 2049.0, 2049.5, 2050.5),  # high < low
    ]
    with pytest.raises(sqlite3.IntegrityError):
        insert_mt5_bars(conn, "XAUUSD", Timeframe.M5, rates)

    total = conn.execute("SELECT COUNT(*) FROM market_data").fetchone()[0]
    assert total == 0
