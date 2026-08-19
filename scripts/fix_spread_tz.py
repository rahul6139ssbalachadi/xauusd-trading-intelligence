"""Fix-verification BEFORE patching analyze_gold.py.

Read-only. Queries the live terminal for:
  1. GOLD.i# point size / digits / tick_size -> correct spread->$ and
     spread->pip conversion (the 1164.9% number was a unit bug).
  2. Broker server-time vs UTC offset (currently UNVERIFIED in schema).

Prints the values so we can hardcode the correct conversion + record tz.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import MetaTrader5 as mt5
from market_data.providers.mt5_provider import MT5Provider


def main() -> int:
    provider = MT5Provider.from_config()
    provider.connect()
    try:
        sym = provider.symbol_map["XAUUSD"]
        info = mt5.symbol_info(sym)
        print(f"symbol          : {sym}")
        print(f"digits          : {info.digits}")
        print(f"point           : {info.point}")
        print(f"trade_tick_size : {info.trade_tick_size}")
        print(f"trade_tick_value: {info.trade_tick_value}")
        print(f"spread (points) : {info.spread}")

        # Correct conversions
        pt = info.point
        spread_pts = float(info.spread)
        spread_usd = spread_pts * pt
        # pip convention for gold = 0.10 (10 points) when point=0.01
        pip = 0.10 if pt <= 0.01 else pt * 10
        spread_pips = spread_usd / pip
        print(f"spread in $     : {spread_usd:.4f}")
        print(f"spread in pips  : {spread_pips:.2f}")

        # Mark the DB-spread reading. copy_rates_range 'spread' is in points.
        print(f"\nDB stores spread as points; to get $ multiply by point={pt}.")

        # --- Broker timezone offset ---
        # tick.time is in broker SERVER time (epoch). Compare to UTC now.
        tick = mt5.symbol_info_tick(sym)
        server_now = datetime.fromtimestamp(tick.time, tz=timezone.utc)
        utc_now = datetime.now(timezone.utc)
        delta = (server_now - utc_now).total_seconds() / 3600.0
        print(f"\nTICK time (epoch={tick.time}) -> {server_now.isoformat()}")
        print(f"UTC now                      -> {utc_now.isoformat()}")
        print(f"server - UTC offset (hours)  : {delta:+.2f}")
        # Classify
        if abs(delta - 2) < 1.5:
            tz = "EET (UTC+2) / EEST (UTC+3) - typical MetaQuotes broker"
        elif abs(delta - 0) < 0.5:
            tz = "UTC"
        else:
            tz = f"~UTC{delta:+.0f}"
        print(f"inferred broker tz           : {tz}")
        return 0
    finally:
        provider.close()


if __name__ == "__main__":
    raise SystemExit(main())
