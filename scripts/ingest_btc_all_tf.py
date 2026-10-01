"""Bootstrap ingestion of BTCUSD (BTCUSD#) across ALL timeframes.

Pulls the maximum history the demo terminal actually exposes for BTCUSD#,
probed 2026-09-28 against the live terminal (hard ~100k bar cap per request,
so each window below is set just under that limit and verified after fetch):

  M1   ~68 days    (bar cap, NOT a terminal retention limit)
  M5   ~340 days   (bar cap)
  M15  ~2 years
  M30  ~4 years
  H1   ~10 years
  H4   ~13.7 years
  D1   ~13.7 years  <-- this is the timeframe DQR levels depend on

Writes into db/trading.db via the existing market_data.insert_mt5_bars().
Read-only with respect to the BROKER: this only reads bars, it never
trades. Idempotent: re-running skips rows already present for
(symbol, timeframe, source='mt5', epoch).

If a window returns 0 bars the script shrinks the window geometrically and
retries rather than silently storing a gap, and it prints what it actually
got so the caller can see the real depth instead of the requested depth.

Usage:
    ./.venv/Scripts/python.exe scripts/ingest_btc_all_tf.py
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

SYMBOL = "BTCUSD"
BAR_CAP = 100_000          # MT5 terminal refuses requests above this

# (timeframe, days_of_history_to_request) -- probed depths, not guesses
WINDOWS = [
    (Timeframe.M1, 68),
    (Timeframe.M5, 340),
    (Timeframe.M15, 730),
    (Timeframe.M30, 1460),
    (Timeframe.H1, 3650),
    (Timeframe.H4, 5000),
    (Timeframe.D1, 5000),
]

_MTF = {
    Timeframe.M1: mt5.TIMEFRAME_M1,
    Timeframe.M5: mt5.TIMEFRAME_M5,
    Timeframe.M15: mt5.TIMEFRAME_M15,
    Timeframe.M30: mt5.TIMEFRAME_M30,
    Timeframe.H1: mt5.TIMEFRAME_H1,
    Timeframe.H4: mt5.TIMEFRAME_H4,
    Timeframe.D1: mt5.TIMEFRAME_D1,
}

SCHEMA_PATH = cfg.PROJECT_ROOT / "db" / "schema" / "market_data.sql"


def ensure_db(db_path: Path) -> sqlite3.Connection:
    is_new = not db_path.exists()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    if is_new:
        conn.executescript(SCHEMA_PATH.read_text())
    return conn


def fetch_with_shrink(mt5_symbol: str, tf: Timeframe, days: int):
    """Fetch up to `days` of bars, halving the window until the request is
    accepted. Returns (rates, actual_days). Never returns a silent gap."""
    d = days
    while d >= 1:
        start = datetime.now(timezone.utc) - timedelta(days=d)
        rates = mt5.copy_rates_range(mt5_symbol, _MTF[tf], start,
                                     datetime.now(timezone.utc))
        if rates is not None and len(rates) > 0:
            return rates, d
        d = d // 2
    return None, 0


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
        mt5_symbol = provider.symbol_map.get(SYMBOL)
        if mt5_symbol is None:
            print(f"{SYMBOL} is not mapped in config/symbols.toml",
                  file=sys.stderr)
            return 1
        print(f"canonical {SYMBOL} -> broker symbol {mt5_symbol!r}\n")

        for tf, days in WINDOWS:
            rates, used = fetch_with_shrink(mt5_symbol, tf, days)
            if rates is None:
                print(f"{SYMBOL} {tf.value}: NO DATA at any window size")
                continue
            first = datetime.utcfromtimestamp(int(rates['time'][0])).date()
            last = datetime.utcfromtimestamp(int(rates['time'][-1])).date()
            result = insert_mt5_bars(conn, SYMBOL, tf, rates)
            print(f"{SYMBOL} {tf.value:>3}: requested {days:>5}d, used {used:>5}d "
                  f"| {len(rates):>6d} bars  {first} .. {last} "
                  f"| inserted {result.inserted}, skipped {result.skipped}")
        return 0
    finally:
        provider.close()
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
