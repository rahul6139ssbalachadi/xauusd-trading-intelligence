"""Read-only probe: check how much history GOLD24-7.i# (24/7 gold) exposes
per timeframe, so we can decide whether to extend the M1 dataset.

Uses the guarded MT5Provider (demo + allowed_login verified).
Read-only: only copy_rates_range. No trading functions called.

Usage:
    ./.venv/Scripts/python.exe scripts/probe_gold24.py
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import MetaTrader5 as mt5
from market_data.providers.mt5_provider import MT5AccountGuardError, MT5Provider


def probe(provider: MT5Provider, symbol: str):
    """Probe history depth for a symbol across timeframes."""
    end = datetime.now(timezone.utc)
    for tf_const, tf_name in [
        (mt5.TIMEFRAME_M1, "M1"),
        (mt5.TIMEFRAME_M5, "M5"),
        (mt5.TIMEFRAME_M15, "M15"),
        (mt5.TIMEFRAME_H1, "H1"),
        (mt5.TIMEFRAME_D1, "D1"),
    ]:
        # Max days per MT5 history limit — probe progressively
        for days in [90, 180, 365, 730, 1095, 1460, 1825, 3650]:
            start = end - timedelta(days=days)
            rates = mt5.copy_rates_range(symbol, tf_const, start, end)
            if rates is not None and len(rates) > 0:
                first = datetime.fromtimestamp(rates[0]["time"], tz=timezone.utc)
                last = datetime.fromtimestamp(rates[-1]["time"], tz=timezone.utc)
                print(f"  {tf_name} {days:4d}d: {len(rates):>8,} bars  "
                      f"({first.date()} -> {last.date()})")
            else:
                print(f"  {tf_name} {days:4d}d: 0 bars")
                break  # terminal returns 0 for larger ranges too
        print()


def main() -> int:
    provider = MT5Provider.from_config()
    try:
        provider.connect()
    except MT5AccountGuardError as exc:
        print(f"CONNECTION GUARD FAILED: {exc}")
        return 1

    try:
        acc = mt5.account_info()
        print(f"Connected: login={acc.login} trade_mode={acc.trade_mode} "
              f"server={acc.server}\n")

        for sym in ["GOLD.i#", "GOLD24-7.i#"]:
            info = mt5.symbol_info(sym)
            if info is None:
                print(f"{sym}: NOT FOUND on terminal\n")
                continue
            print(f"=== {sym} (visible={info.visible}, digits={info.digits}) ===")
            probe(provider, sym)
    finally:
        provider.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
