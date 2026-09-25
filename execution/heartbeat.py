"""5-minute account/trade heartbeat for Telegram.

Read-only. Reports balance, equity, open positions and current signal state.
Uses the real recorded spread from the last bar rather than a hardcoded
assumption, because on BTC the spread dominates the cost picture.

Prints nothing to stdout unless there is something to say: the caller
decides whether silence or a message is correct. Telegram is sent only when
--telegram is passed.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import MetaTrader5 as mt5
from market_data import config as cfg
from execution.mt5_gateway import MT5Gateway

SYMBOL = "GOLD.i#"


def snapshot() -> str:
    c = cfg.load_mt5_config()
    gw = MT5Gateway(terminal_path=c["terminal_path"],
                    allowed_login=c["allowed_login"])
    gw.connect()
    try:
        a = gw.account_info()
        pos = list(mt5.positions_get() or [])
        tick = mt5.symbol_info_tick(SYMBOL)
        spread = f"{(tick.ask - tick.bid):.2f}" if tick else "n/a"
        bid = f"{tick.bid:.2f}" if tick else "n/a"

        lines = [
            "<b>XAUUSD HEARTBEAT</b>",
            f"<i>{datetime.now(timezone.utc):%H:%M:%S} UTC</i>",
            f"Equity  ${float(a.equity):,.2f}",
            f"Balance ${float(a.balance):,.2f}",
        ]
        if pos:
            pl = sum(float(p.profit) for p in pos)
            lines.append(f"<b>OPEN: {len(pos)}</b>  P/L ${pl:+.2f}")
            for p in pos:
                k = "BUY" if p.type == mt5.POSITION_TYPE_BUY else "SELL"
                lines.append(f"  #{p.ticket} {k} {float(p.volume):.2f}L "
                             f"@{float(p.price_open):.2f} ${float(p.profit):+.2f}")
        else:
            lines.append("Positions: flat")
        lines.append(f"Gold {bid}  spread {spread}")
        return "\n".join(lines)
    finally:
        gw.disconnect()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--telegram", action="store_true")
    args = ap.parse_args()

    msg = snapshot()
    if args.telegram:
        from execution.account_status import send_telegram
        ok, desc = send_telegram(msg)
        if not ok:
            print(f"heartbeat Telegram FAILED: {desc}")
            return 1
    print(msg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
