from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any

import pandas as pd

from market_data.base import Timeframe

_INSERT_SQL = """
INSERT INTO market_data
    (symbol, timeframe, source, ts_broker_epoch, open, high, low, close, tick_volume, spread)
VALUES (?, ?, 'mt5', ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT (symbol, timeframe, source, ts_broker_epoch) DO NOTHING
"""


@dataclass
class IngestResult:
    inserted: int
    skipped: int


def insert_mt5_bars(
    conn: sqlite3.Connection,
    symbol: str,
    timeframe: Timeframe | str,
    rates: Any,
) -> IngestResult:
    """Insert raw MT5 rate rows -- as returned by mt5.copy_rates_range /
    copy_rates_from_pos, NOT MT5Provider.get_ohlcv()'s converted output
    -- into market_data. `rates` may be a numpy structured array, a
    list of dicts, or a DataFrame -- anything pd.DataFrame() accepts,
    with time/open/high/low/close/tick_volume columns (spread optional).

    Idempotent: re-running the same fetch inserts nothing new for rows
    already present for (symbol, timeframe, source='mt5',
    ts_broker_epoch) -- that composite-PK conflict is silently skipped.

    ts_broker_epoch is written as the exact raw `time` integer MT5
    returned -- no timezone conversion, shift, or localization (see
    db/schema/market_data.sql for why).

    A row that violates a CHECK constraint (e.g. non-positive price,
    high < low) raises sqlite3.IntegrityError -- it is never silently
    dropped. Plain "INSERT OR IGNORE" was deliberately NOT used:
    SQLite's OR IGNORE swallows CHECK violations exactly like PK
    conflicts (verified empirically -- see conversation), which would
    silently drop bad data. Targeting the composite PK explicitly with
    ON CONFLICT (...) DO NOTHING skips only an actual PK conflict;
    CHECK violations still raise normally.

    The whole batch runs inside a single transaction: commits once at
    the end, rolls back entirely (nothing from this call persists) if
    any row raises.
    """
    tf = timeframe.value if isinstance(timeframe, Timeframe) else timeframe
    df = pd.DataFrame(rates)
    has_spread = "spread" in df.columns

    inserted = 0
    skipped = 0
    with conn:
        for row in df.itertuples(index=False):
            spread = None
            if has_spread and not pd.isna(row.spread):
                spread = int(row.spread)
            cur = conn.execute(
                _INSERT_SQL,
                (
                    symbol,
                    tf,
                    int(row.time),
                    float(row.open),
                    float(row.high),
                    float(row.low),
                    float(row.close),
                    int(row.tick_volume),
                    spread,
                ),
            )
            if cur.rowcount == 1:
                inserted += 1
            else:
                skipped += 1
    return IngestResult(inserted=inserted, skipped=skipped)
