from __future__ import annotations

import pytest

from market_data import validation as v
from market_data.base import Timeframe
from market_data.providers.csv_provider import CSVProvider
from market_data.providers.source_formats import REGISTRY

DUKASCOPY_HEADER = "Local time,Open,High,Low,Close,Volume\n"
# 01:00 through 04:00 on a Monday -- H1 bars, dukascopy timestamp format.
DUKASCOPY_ROWS_H1 = [
    "01.01.2024 01:00:00.000,2050.00,2050.80,2049.60,2050.20,120.5\n",
    "01.01.2024 02:00:00.000,2050.20,2051.00,2049.90,2050.90,131.2\n",
    "01.01.2024 03:00:00.000,2050.90,2051.40,2050.10,2050.30,98.7\n",
    "01.01.2024 04:00:00.000,2050.30,2050.70,2049.80,2050.10,142.0\n",
]

HISTDATA_ROWS_M15 = [
    "20240101 010000;2050.00;2050.30;2049.80;2050.10;80.0\n",
    "20240101 011500;2050.10;2050.40;2049.90;2050.25;75.5\n",
    "20240101 013000;2050.25;2050.60;2050.00;2050.40;90.2\n",
]


def _write(path, lines, header=None):
    with path.open("w") as f:
        if header:
            f.write(header)
        f.writelines(lines)


@pytest.fixture
def dukascopy_provider(tmp_path):
    _write(
        tmp_path / "EURUSD_Candlestick_1_Hour_BID_01.01.2024-07.01.2024.csv",
        DUKASCOPY_ROWS_H1,
        header=DUKASCOPY_HEADER,
    )
    return CSVProvider.from_config("dukascopy", raw_dir=tmp_path)


@pytest.fixture
def histdata_provider(tmp_path):
    _write(
        tmp_path / "DAT_ASCII_XAUUSD_M15_202401.csv",
        HISTDATA_ROWS_M15,
    )
    return CSVProvider.from_config("histdata", raw_dir=tmp_path)


# ---------------------------------------------------------------------------
# available_symbols / from_config
# ---------------------------------------------------------------------------


def test_from_config_symbol_map_matches_symbols_toml(dukascopy_provider):
    # config/symbols.toml maps BTCUSD, EURUSD and XAUUSD for dukascopy;
    # inactive symbols (GBPUSD etc.) have no dukascopy key and must not appear.
    # BTCUSD became active 2026-09-28 (confirmed broker symbol 'BTCUSD#').
    assert dukascopy_provider.available_symbols() == ["BTCUSD", "EURUSD", "XAUUSD"]


# ---------------------------------------------------------------------------
# get_ohlcv: dukascopy (header, comma, DD.MM.YYYY timestamp)
# ---------------------------------------------------------------------------


def test_get_ohlcv_returns_standard_schema(dukascopy_provider):
    df = dukascopy_provider.get_ohlcv("EURUSD", Timeframe.H1)
    assert list(df.columns) == ["timestamp", "open", "high", "low", "close", "volume"]
    assert len(df) == 4


def test_get_ohlcv_parses_values_correctly(dukascopy_provider):
    df = dukascopy_provider.get_ohlcv("EURUSD", Timeframe.H1)
    first = df.iloc[0]
    assert first["open"] == pytest.approx(2050.00)
    assert first["close"] == pytest.approx(2050.20)
    assert first["timestamp"] == pd_timestamp("2024-01-01 01:00:00")


def test_get_ohlcv_timestamp_is_utc(dukascopy_provider):
    df = dukascopy_provider.get_ohlcv("EURUSD", Timeframe.H1)
    assert str(df["timestamp"].dt.tz) == "UTC"


def test_get_ohlcv_sorted_ascending(dukascopy_provider):
    df = dukascopy_provider.get_ohlcv("EURUSD", Timeframe.H1)
    assert df["timestamp"].is_monotonic_increasing


def test_get_ohlcv_passes_validation_cleanly(dukascopy_provider):
    df = dukascopy_provider.get_ohlcv("EURUSD", Timeframe.H1)
    report = v.validate(df, Timeframe.H1)
    assert report.is_clean


def test_get_ohlcv_unmapped_symbol_raises(dukascopy_provider):
    with pytest.raises(ValueError, match="not mapped"):
        dukascopy_provider.get_ohlcv("GBPUSD", Timeframe.H1)


def test_get_ohlcv_no_matching_files_raises(tmp_path):
    provider = CSVProvider.from_config("dukascopy", raw_dir=tmp_path)
    with pytest.raises(FileNotFoundError):
        provider.get_ohlcv("EURUSD", Timeframe.H1)


def test_get_ohlcv_start_end_filtering(dukascopy_provider):
    df = dukascopy_provider.get_ohlcv(
        "EURUSD", Timeframe.H1, start=None, end=None
    )
    assert len(df) == 4
    narrowed = dukascopy_provider.get_ohlcv(
        "EURUSD", Timeframe.H1,
        start=__import__("datetime").date(2024, 1, 1),
        end=__import__("datetime").date(2024, 1, 1),
    )
    # end=2024-01-01 becomes 2024-01-01T00:00 UTC, before all 01:00-04:00
    # bars, so nothing should pass through.
    assert narrowed.empty


def test_get_ohlcv_concatenates_multiple_files_and_dedups(tmp_path):
    fmt = REGISTRY["dukascopy"]
    _write(
        tmp_path / "EURUSD_Candlestick_1_Hour_BID_part1.csv",
        DUKASCOPY_ROWS_H1[:2],
        header=DUKASCOPY_HEADER,
    )
    # Second file re-includes the last row of the first (overlapping
    # export batches) with a different close, to prove keep="first" wins
    # and the row isn't duplicated.
    overlap_row = "01.01.2024 02:00:00.000,2050.20,2051.00,2049.90,9999.99,999\n"
    _write(
        tmp_path / "EURUSD_Candlestick_1_Hour_BID_part2.csv",
        [overlap_row] + DUKASCOPY_ROWS_H1[2:],
        header=DUKASCOPY_HEADER,
    )
    provider = CSVProvider(fmt=fmt, raw_dir=tmp_path, symbol_map={"EURUSD": "EURUSD"})
    df = provider.get_ohlcv("EURUSD", Timeframe.H1)
    assert len(df) == 4  # not 5 -- the overlapping timestamp collapsed
    dup_row = df[df["timestamp"] == pd_timestamp("2024-01-01 02:00:00")]
    assert dup_row.iloc[0]["close"] == pytest.approx(2050.90)  # first file's value kept


# ---------------------------------------------------------------------------
# get_ohlcv: histdata (no header, semicolon, YYYYMMDD HHMMSS timestamp)
# ---------------------------------------------------------------------------


def test_histdata_get_ohlcv_parses_correctly(histdata_provider):
    df = histdata_provider.get_ohlcv("XAUUSD", Timeframe.M15)
    assert len(df) == 3
    assert df.iloc[0]["timestamp"] == pd_timestamp("2024-01-01 01:00:00")
    assert df.iloc[0]["close"] == pytest.approx(2050.10)


def test_histdata_get_ohlcv_passes_validation_cleanly(histdata_provider):
    df = histdata_provider.get_ohlcv("XAUUSD", Timeframe.M15)
    report = v.validate(df, Timeframe.M15)
    assert report.is_clean


# ---------------------------------------------------------------------------
# cross-source validation (Section 31): dukascopy vs histdata for EURUSD
# ---------------------------------------------------------------------------


def test_cross_source_matching_data_has_no_conflict(tmp_path):
    dukascopy_dir = tmp_path / "dukascopy"
    histdata_dir = tmp_path / "histdata"
    dukascopy_dir.mkdir()
    histdata_dir.mkdir()

    _write(
        dukascopy_dir / "EURUSD_Candlestick_1_Hour_BID_01.01.2024.csv",
        DUKASCOPY_ROWS_H1,
        header=DUKASCOPY_HEADER,
    )
    # Same bars, same values, histdata format/timestamps.
    histdata_rows = [
        "20240101 010000;2050.00;2050.80;2049.60;2050.20;120.5\n",
        "20240101 020000;2050.20;2051.00;2049.90;2050.90;131.2\n",
        "20240101 030000;2050.90;2051.40;2050.10;2050.30;98.7\n",
        "20240101 040000;2050.30;2050.70;2049.80;2050.10;142.0\n",
    ]
    _write(histdata_dir / "DAT_ASCII_EURUSD_H1_202401.csv", histdata_rows)

    duka = CSVProvider.from_config("dukascopy", raw_dir=dukascopy_dir)
    hist = CSVProvider.from_config("histdata", raw_dir=histdata_dir)
    df_a = duka.get_ohlcv("EURUSD", Timeframe.H1)
    df_b = hist.get_ohlcv("EURUSD", Timeframe.H1)

    report = v.validate(df_a, Timeframe.H1, other_source=(df_b, "dukascopy", "histdata"))
    assert report.is_clean


def test_cross_source_diverging_data_flags_conflict(tmp_path):
    dukascopy_dir = tmp_path / "dukascopy"
    histdata_dir = tmp_path / "histdata"
    dukascopy_dir.mkdir()
    histdata_dir.mkdir()

    _write(
        dukascopy_dir / "EURUSD_Candlestick_1_Hour_BID_01.01.2024.csv",
        DUKASCOPY_ROWS_H1,
        header=DUKASCOPY_HEADER,
    )
    # First bar's close is significantly different between sources.
    histdata_rows = [
        "20240101 010000;2050.00;2050.80;2049.60;2300.00;120.5\n",
        "20240101 020000;2050.20;2051.00;2049.90;2050.90;131.2\n",
        "20240101 030000;2050.90;2051.40;2050.10;2050.30;98.7\n",
        "20240101 040000;2050.30;2050.70;2049.80;2050.10;142.0\n",
    ]
    _write(histdata_dir / "DAT_ASCII_EURUSD_H1_202401.csv", histdata_rows)

    duka = CSVProvider.from_config("dukascopy", raw_dir=dukascopy_dir)
    hist = CSVProvider.from_config("histdata", raw_dir=histdata_dir)
    df_a = duka.get_ohlcv("EURUSD", Timeframe.H1)
    df_b = hist.get_ohlcv("EURUSD", Timeframe.H1)

    report = v.validate(df_a, Timeframe.H1, other_source=(df_b, "dukascopy", "histdata"))
    assert not report.is_clean
    assert len(report.conflicts) == 1


def pd_timestamp(s):
    import pandas as pd

    return pd.Timestamp(s, tz="UTC")
