"""Extended ingestion of XAUUSD history from the XM Global MT5 terminal.

Pulls the maximum history the demo terminal exposes per timeframe:
  M1  : up to ~30 days (GOLD.i# session hours only)  [already in DB]
  M5  : up to 180 days                                 [extend]
  M15 : up to 730 days (~2 years)                       [already in DB]
  H1  : up to 3650 days (~10 years)  *** MAJOR EXTENSION ***
  D1  : up to 3650 days (~10 years)  *** MAJOR EXTENSION ***

Read-only data fetch only. Idempotent: insert_mt5_bars skips rows already
present for (symbol, timeframe, source, epoch).

Usage:
    ./.venv/Scripts/python.exe scripts/ingest_extended_gold.py
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import MetaTrader5 as mt5
from market_data import config as cfg
from market_data.base import Timeframe
from market_data.providers.mt5_provider import MT5AccountGuardError, MT5Provider
from market_data.storage import insert_mt5_bars

SYMBOL = "XAUUSD"
DB_PATH = cfg.PROJECT_ROOT / "db" / "trading.db"
SCHEMA_PATH = cfg.PROJECT_ROOT / "db" / "schema" / "market_data.sql"


def _mt5_tf(tf: Timeframe):
    return {
        Timeframe.M1: mt5.TIMEFRAME_M1,
        Timeframe.M5: mt5.TIMEFRAME_M5,
        Timeframe.M15: mt5.TIMEFRAME_M15,
        Timeframe.M30: mt5.TIMEFRAME_M30,
        Timeframe.H1: mt5.TIMEFRAME_H1,
        Timeframe.H4: mt5.TIMEFRAME_H4,
        Timeframe.D1: mt5.TIMEFRAME_D1,
    }[tf]


# (timeframe, days_of_history_to_request)
# H1 and D1 get 10 years; M5 and M15 get max available.
WINDOWS = [
    (Timeframe.M5, 185),    # extend M5 to full 180-day limit
    (Timeframe.M15, 735),   # re-confirm/extend M15 (already in DB, idempotent)
    (Timeframe.H1, 3650),   # 10 YEARS of H1 — major data extension
    (Timeframe.D1, 3650),   # 10 YEARS of D1 — major data extension
]

import sqlite3


def ensure_db(db_path: Path) -> sqlite3.Connection:
    is_new = not db_path.exists()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    if is_new:
        conn.executescript(SCHEMA_PATH.read_text())
    return conn


def main() -> int:
    conn = ensure_db(DB_PATH)
    provider = MT5Provider.from_config()
    try:
        provider.connect()
    except MT5AccountGuardError as exc:
        print(f"MT5 guard refused connection: {exc}", file=sys.stderr)
        return 1

    try:
        mt5_symbol = provider.symbol_map[SYMBOL]
        end = datetime.now(timezone.utc)
        for tf, days in WINDOWS:
            start = end - timedelta(days=days)
            rates = mt5.copy_rates_range(mt5_symbol, _mt5_tf(tf), start, end)
            if rates is None or len(rates) == 0:
                print(f"{SYMBOL} {tf.value}: no data returned")
                continue
            result = insert_mt5_bars(conn, SYMBOL, tf, rates)
            first = datetime.fromtimestamp(rates[0]["time"], tz=timezone.utc)
            last = datetime.fromtimestamp(rates[-1]["time"], tz=timezone.utc)
            print(
                f"{SYMBOL} {tf.value}: fetched {len(rates)} bars "
                f"({first.date()} -> {last.date()}) "
                f"-> inserted {result.inserted}, skipped {result.skipped}"
            )
        return 0
    finally:
        provider.close()
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
