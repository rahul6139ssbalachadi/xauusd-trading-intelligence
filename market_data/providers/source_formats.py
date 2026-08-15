from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class SourceFormat:
    """Describes how to parse one vendor's raw CSV export.

    `filename_glob` is a template with `{symbol}` and `{timeframe}`
    placeholders, matched against files in data/raw/<source>/. Multiple
    matches (e.g. one file per year) are concatenated.

    `column_map` maps the standardized field name to the column name
    (or 0-based index, if the file has no header) in the raw file.
    """

    name: str
    filename_glob: str
    delimiter: str
    has_header: bool
    column_map: dict[str, str | int]
    timestamp_format: str | None  # None = let pandas infer
    timestamp_tz: str  # tz the raw timestamps are already in
    timeframe_tokens: dict[str, str] = field(default_factory=dict)


# NOTE: these are best-effort defaults based on each vendor's publicly
# documented export format. Neither has been checked against an actual
# downloaded file yet (per instruction, no data has been downloaded).
# Verify column_map / delimiter / timestamp_format against the first
# real file before trusting an ingestion run, and adjust here if they
# don't match -- do not assume this guess is correct.

DUKASCOPY = SourceFormat(
    name="dukascopy",
    # Dukascopy's CSV export names files like:
    #   EURUSD_Candlestick_1_Hour_BID_01.01.2018-31.12.2018.csv
    # Exact naming varies by export batch size, so this glob is
    # intentionally loose -- narrow it once real filenames are known.
    filename_glob="{symbol}_Candlestick_*{timeframe}*.csv",
    delimiter=",",
    has_header=True,
    column_map={
        "timestamp": "Local time",
        "open": "Open",
        "high": "High",
        "low": "Low",
        "close": "Close",
        "volume": "Volume",
    },
    timestamp_format="%d.%m.%Y %H:%M:%S.%f",
    timestamp_tz="UTC",  # Dukascopy exports as "GMT+0000" -- treat as UTC
    timeframe_tokens={"M15": "15_Min", "H1": "1_Hour"},
)

HISTDATA = SourceFormat(
    name="histdata",
    # HistData.com "Generic ASCII" export, e.g.:
    #   DAT_ASCII_EURUSD_M15_2018.csv
    filename_glob="DAT_ASCII_{symbol}_{timeframe}_*.csv",
    delimiter=";",
    has_header=False,
    column_map={
        "timestamp": 0,
        "open": 1,
        "high": 2,
        "low": 3,
        "close": 4,
        "volume": 5,
    },
    timestamp_format="%Y%m%d %H%M%S",
    timestamp_tz="UTC",  # HistData publishes in EST/EDT for M1; assumed
    # pre-converted to UTC for H1/M15 generic ASCII -- VERIFY.
    timeframe_tokens={"M15": "M15", "H1": "H1"},
)

REGISTRY: dict[str, SourceFormat] = {
    DUKASCOPY.name: DUKASCOPY,
    HISTDATA.name: HISTDATA,
}
