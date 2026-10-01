"""Supplemental catch-up for low timeframes where the full-history window
hits MT5's ~100k bar cap.

`mt5.copy_rates_range` fills from the START of the window forward and
refuses requests above ~100k bars, so an oversized window truncates the END
and the newest bars never arrive. BTCUSD M1/M5/M15 sat at 2026-09-28 for
exactly this reason while M30/H1/H4/D1 on the same symbol were current -- a
request-size artifact, not a data gap or a stale feed.

This asks for a SHORT recent window (well under the cap) so the tail is
always delivered. Pair it with the full-history ingest, and run it on a
schedule. Idempotent.

Usage:
    ./.venv/Scripts/python.exe scripts/ingest_recent_topup.py
    ./.venv/Scripts/python.exe scripts/ingest_recent_topup.py --symbol XAUUSD
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import MetaTrader5 as mt5

from ingest_common import assert_windows_safe, ensure_db
from market_data import config as cfg
from market_data.base import Timeframe
from market_data.providers.mt5_provider import MT5AccountGuardError, MT5Provider
from market_data.storage import insert_mt5_bars

# (symbol, [(timeframe, days)]) -- each window is a small fraction of the cap.
# M1 is the binding one: 5d x 1440 = 7,200 bars.
PLAN = {
    "BTCUSD": [(Timeframe.M1, 5), (Timeframe.M5, 20), (Timeframe.M15, 60)],
    "XAUUSD": [(Timeframe.M1, 5), (Timeframe.M5, 20), (Timeframe.M15, 60)],
}

_MTF = {
    Timeframe.M1: mt5.TIMEFRAME_M1,
    Timeframe.M5: mt5.TIMEFRAME_M5,
    Timeframe.M15: mt5.TIMEFRAME_M15,
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default=None,
                    help="subset, e.g. --symbol BTCUSD (default: all)")
    args = ap.parse_args()

    for sym, windows in PLAN.items():
        assert_windows_safe(windows, symbol=sym)

    symbols = [args.symbol] if args.symbol else list(PLAN)
    conn = ensure_db(cfg.PROJECT_ROOT / cfg.load_settings()["paths"]["db"],
                     cfg.PROJECT_ROOT / "db" / "schema" / "market_data.sql")

    provider = MT5Provider.from_config()
    try:
        provider.connect()
    except MT5AccountGuardError as exc:
        print(f"MT5 guard refused connection: {exc}", file=sys.stderr)
        return 1

    try:
        now = datetime.now(timezone.utc)
        for symbol in symbols:
            sym = provider.symbol_map.get(symbol)
            if sym is None:
                print(f"{symbol} is not mapped in config/symbols.toml", file=sys.stderr)
                continue
            for tf, days in PLAN[symbol]:
                rates = mt5.copy_rates_range(sym, _MTF[tf],
                                             now - timedelta(days=days), now)
                if rates is None or len(rates) == 0:
                    print(f"{symbol} {tf.value}: no data")
                    continue
                first = datetime.utcfromtimestamp(int(rates["time"][0])).date()
                last = datetime.utcfromtimestamp(int(rates["time"][-1])).date()
                res = insert_mt5_bars(conn, symbol, tf, rates)
                conn.commit()
                print(f"{symbol} {tf.value:>3}: {days:>3}d window | fetched "
                      f"{len(rates):>6d} bars  {first} .. {last} | inserted "
                      f"{res.inserted}, skipped {res.skipped}")
        return 0
    finally:
        provider.close()
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
