"""Bootstrap ingestion of XAUUSD (GOLD.i#) for scalping timeframes.

Pulls the maximum history the demo terminal actually exposes:
  M1  : ~30 days
  M5  : ~180 days
  M15 : ~730 days
into db/trading.db via the existing market_data.insert_mt5_bars().

Read-only data fetch (no trading). Idempotent: re-running skips rows
already present for (symbol, timeframe, source='mt5', epoch).

Usage:
    ./.venv/Scripts/python.exe scripts/ingest_gold_scalp.py
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import MetaTrader5 as mt5
import sqlite3

from market_data import config as cfg
from market_data.base import Timeframe
from market_data.providers.mt5_provider import MT5AccountGuardError, MT5Provider
from market_data.storage import insert_mt5_bars

SYMBOL = "XAUUSD"
# (timeframe, days_of_history_to_request)
WINDOWS = [
    (Timeframe.M1, 35),
    (Timeframe.M5, 185),
    (Timeframe.M15, 735),
]
SCHEMA_PATH = cfg.PROJECT_ROOT / "db" / "schema" / "market_data.sql"


def ensure_db(db_path: Path) -> sqlite3.Connection:
    is_new = not db_path.exists()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    if is_new:
        conn.executescript(SCHEMA_PATH.read_text())
    return conn


def main() -> int:
    settings = cfg.load_settings()
    db_path = cfg.PROJECT_ROOT / settings["paths"]["db"]
    conn = ensure_db(db_path)

    provider = MT5Provider.from_config()
    try:
        provider.connect()
    except MT5AccountGuardError as exc:
        print(f"MT5 guard refused connection: {exc}", file=sys.stderr)
        return 1

    try:
        mt5_symbol = provider.symbol_map[SYMBOL]
        for tf, days in WINDOWS:
            start = datetime.now(timezone.utc) - timedelta(days=days)
            end = datetime.now(timezone.utc)
            rates = mt5.copy_rates_range(mt5_symbol, _mt5_tf(tf), start, end)
            if rates is None or len(rates) == 0:
                print(f"{SYMBOL} {tf.value}: no data returned")
                continue
            result = insert_mt5_bars(conn, SYMBOL, tf, rates)
            print(
                f"{SYMBOL} {tf.value}: fetched {len(rates)} bars "
                f"-> inserted {result.inserted}, skipped {result.skipped}"
            )
        return 0
    finally:
        provider.close()
        conn.close()


def _mt5_tf(tf: Timeframe):
    import MetaTrader5 as _mt5

    return {
        Timeframe.M1: _mt5.TIMEFRAME_M1,
        Timeframe.M5: _mt5.TIMEFRAME_M5,
        Timeframe.M15: _mt5.TIMEFRAME_M15,
    }[tf]


if __name__ == "__main__":
    raise SystemExit(main())
