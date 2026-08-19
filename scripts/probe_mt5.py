"""Read-only MT5 connectivity + symbol reconciliation probe.

Does NOT fetch data and CANNOT trade. Verifies:
  1. MT5 terminal initializes
  2. account guard passes (demo + allowed_login match)
  3. which Gold symbol name actually exists on the terminal

Usage:
    ./.venv/Scripts/python.exe scripts/probe_mt5.py
"""
from __future__ import annotations

import sys
from pathlib import Path

# Make project root importable when run as a bare script
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from market_data.providers.mt5_provider import MT5AccountGuardError, MT5Provider


def main() -> int:
    provider = MT5Provider.from_config()
    try:
        provider.connect()
    except MT5AccountGuardError as exc:
        print("CONNECTION GUARD FAILED:")
        print(f"  {exc}")
        print("\nFix: open the XM Global MT5 terminal, log into your DEMO account,")
        print("then make sure config/mt5.toml allowed_login matches that account.")
        return 1

    try:
        import MetaTrader5 as mt5

        acc = mt5.account_info()
        print("CONNECTION OK")
        print(f"  login      : {acc.login}")
        print(f"  trade_mode : {acc.trade_mode} (1 = DEMO)")
        print(f"  server     : {acc.server}")
        print(f"  balance    : {acc.balance}")

        print("\nSYMBOL RECONCILIATION (config xm names -> real terminal):")
        rep = provider.reconcile_symbols()
        for canon, info in rep.items():
            status = "OK" if (info["exists"] and info["visible"]) else "MISSING"
            print(f"  {canon:10s} -> {info['configured_name']:12s} exists={info['exists']} visible={info['visible']}  [{status}]")

        # Show what gold-like symbols actually exist so we can pick the right one
        print("\nGOLD-LIKE SYMBOLS ON TERMINAL:")
        all_syms = mt5.symbols_get()
        gold = [s.name for s in all_syms if "GOLD" in s.name.upper() or "XAU" in s.name.upper()]
        for name in sorted(gold)[:40]:
            print(f"  {name}")
        return 0
    finally:
        provider.close()


if __name__ == "__main__":
    raise SystemExit(main())
