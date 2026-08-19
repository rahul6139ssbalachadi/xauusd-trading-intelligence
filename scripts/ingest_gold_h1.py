"""One-off ingestion runner: last 500 XAUUSD H1 bars, MT5 -> db/trading.db.

Creates db/trading.db from db/schema/market_data.sql if it doesn't exist
yet, connects to MT5 via MT5Provider (demo-account guarded), fetches the
500 most recent H1 bars for XAUUSD with mt5.copy_rates_from_pos (raw
rates -- NOT MT5Provider.get_ohlcv()'s converted output, per
storage.insert_mt5_bars()'s contract), and inserts them.

Usage:
    python scripts/ingest_gold_h1.py
"""
from __future__ import annotations

import sqlite3
import sys

import MetaTrader5 as mt5

from market_data import config as cfg
from market_data.base import Timeframe
from market_data.providers.mt5_provider import MT5AccountGuardError, MT5Provider
from market_data.storage import insert_mt5_bars

SYMBOL = "XAUUSD"
TIMEFRAME = Timeframe.H1
BAR_COUNT = 500
SCHEMA_PATH = cfg.PROJECT_ROOT / "db" / "schema" / "market_data.sql"


def ensure_db(db_path) -> sqlite3.Connection:
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
        print(f"MT5 connection refused: {exc}", file=sys.stderr)
        return 1

    try:
        recon = provider.reconcile_symbols().get(SYMBOL)
        if recon is None or not recon["exists"] or not recon["visible"]:
            print(f"{SYMBOL!r} not available on this MT5 terminal: {recon}", file=sys.stderr)
            return 1

        mt5_symbol = provider.symbol_map[SYMBOL]
        rates = mt5.copy_rates_from_pos(mt5_symbol, mt5.TIMEFRAME_H1, 0, BAR_COUNT)
        if rates is None or len(rates) == 0:
            code, desc = mt5.last_error()
            print(f"copy_rates_from_pos returned no data: [{code}] {desc}", file=sys.stderr)
            return 1

        result = insert_mt5_bars(conn, SYMBOL, TIMEFRAME, rates)
        print(
            f"{SYMBOL} {TIMEFRAME.value}: fetched {len(rates)} bars, "
            f"inserted {result.inserted}, skipped {result.skipped} (already present)"
        )
        return 0
    finally:
        provider.close()
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
