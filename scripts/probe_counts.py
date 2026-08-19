"""Read-only: report how many bars MT5 will return per timeframe for
given date ranges, so we can size the database before ingesting.

Usage:
    ./.venv/Scripts/python.exe scripts/probe_counts.py
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import MetaTrader5 as mt5
from market_data.providers.mt5_provider import MT5Provider


def probe(symbol: str, tf_const, tf_name: str, days: int):
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    rates = mt5.copy_rates_range(symbol, tf_const, start, end)
    if rates is None or len(rates) == 0:
        print(f"  {tf_name:4s} {days:4d}d : 0 bars")
        return
    first = datetime.fromtimestamp(rates[0]["time"], tz=timezone.utc)
    last = datetime.fromtimestamp(rates[-1]["time"], tz=timezone.utc)
    print(
        f"  {tf_name:4s} {days:4d}d : {len(rates):>9,} bars  "
        f"span {first.date()} -> {last.date()}"
    )


def main() -> int:
    provider = MT5Provider.from_config()
    provider.connect()
    try:
        account = mt5.account_info()
        print(f"connected demo login={account.login}")

        # Find the real gold symbol name
        sym = provider.symbol_map.get("XAUUSD")
        if sym is None:
            print("XAUUSD not mapped in config/symbols.toml")
            return 1
        print(f"gold symbol on terminal: {sym}\n")

        print("AVAILABLE HISTORY (read-only probe):")
        probe(sym, mt5.TIMEFRAME_M1, "M1", 30)
        probe(sym, mt5.TIMEFRAME_M1, "M1", 90)
        probe(sym, mt5.TIMEFRAME_M1, "M1", 180)
        probe(sym, mt5.TIMEFRAME_M5, "M5", 180)
        probe(sym, mt5.TIMEFRAME_M5, "M5", 365)
        probe(sym, mt5.TIMEFRAME_M15, "M15", 365)
        probe(sym, mt5.TIMEFRAME_M15, "M15", 730)
        return 0
    finally:
        provider.close()


if __name__ == "__main__":
    raise SystemExit(main())
