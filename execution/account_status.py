"""Read-only account + strategy status reporter, with optional Telegram send.

READ-ONLY: opens one MT5 connection, reads account_info / positions_get /
symbol_info, and evaluates the V11/V12 signal FUNCTIONS in-process. Never
calls order_send, never modifies or closes a position — manual mobile trades
are only read, for the report.

Usage:
  ./.venv/Scripts/python.exe execution/account_status.py
  ./.venv/Scripts/python.exe execution/account_status.py --telegram
  ./.venv/Scripts/python.exe execution/account_status.py --json
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from market_data import config as cfg
from execution.mt5_gateway import MT5Gateway

BROKER_SYMBOL = "GOLD.i#"
# Bot token lives in exactly one place: the existing Telegram bridge script.
# Read at call time, never printed, never committed.
BRIDGE = Path(r"D:\rahul_ai\hermes\hermes_telegram_bridge.py")


def _load_creds() -> tuple[str, str]:
    src = BRIDGE.read_text(encoding="utf-8", errors="ignore")
    tok = re.search(r'BOT_TOKEN\s*=\s*"([^"]+)"', src)
    cid = re.search(r'CHAT_ID\s*=\s*"([^"]+)"', src)
    if not tok or not cid:
        raise SystemExit("could not read Telegram credentials from bridge script")
    return tok.group(1), cid.group(1)


def send_telegram(text: str) -> tuple[bool, str]:
    token, chat_id = _load_creds()
    data = urllib.parse.urlencode(
        {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    ).encode()
    try:
        with urllib.request.urlopen(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=data, timeout=25,
        ) as r:
            body = json.loads(r.read().decode())
        return bool(body.get("ok")), str(body.get("description", ""))
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"


def _strategy_state(label: str, load, signal) -> str:
    """Evaluate a runner's signal function in-process (read-only, no order)."""
    try:
        sig = signal(load())
    except Exception as exc:  # noqa: BLE001
        return f"{label} : ERROR {type(exc).__name__}: {exc}"
    if sig is None:
        return f"{label} : WAIT (no signal on last closed bar)"
    return (f"{label} : BUY SIGNAL (body_pct={sig['body_pct']:.2f}, "
            f"ATR={sig['atr']:.1f})")


def collect() -> tuple[str, dict]:
    import MetaTrader5 as mt5

    from execution.run_v11_daily import load_d1_recent, signal_on_last_closed_bar as v11_sig
    from execution.run_v12_hourly import load_h1_recent, signal_on_last_closed_bar as v12_sig

    mt5_cfg = cfg.load_mt5_config()
    gw = MT5Gateway(
        terminal_path=mt5_cfg["terminal_path"],
        allowed_login=mt5_cfg["allowed_login"],
    )
    gw.connect()
    try:
        acc = gw.account_info()
        positions = list(mt5.positions_get(symbol=BROKER_SYMBOL) or [])
        sym = mt5.symbol_info(BROKER_SYMBOL)

        lines = [
            "<b>XAUUSD ACCOUNT STATUS</b>",
            f"<i>{datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC</i>",
            "",
            f"Login      : {acc.login} "
            f"({'DEMO' if acc.trade_mode == mt5.ACCOUNT_TRADE_MODE_DEMO else 'LIVE'})",
            f"Server     : {acc.server}",
            f"Balance    : ${float(acc.balance):,.2f}",
            f"Equity     : ${float(acc.equity):,.2f}",
            f"Free margin: ${float(acc.margin_free):,.2f}  (1:{acc.leverage})",
        ]
        if positions:
            lines += ["", f"<b>OPEN POSITIONS ({len(positions)})</b>"]
            for p in positions:
                kind = "BUY" if p.type == mt5.POSITION_TYPE_BUY else "SELL"
                lines.append(
                    f"#{p.ticket} {kind} {float(p.volume):.2f}L @{float(p.price_open):.2f} "
                    f"SL {float(p.sl):.2f} TP {float(p.tp):.2f} P/L ${float(p.profit):+.2f}")
            lines.append(f"Floating total: ${sum(float(p.profit) for p in positions):+.2f}")
        else:
            lines += ["", "Open positions: NONE (flat)"]

        lines += [
            "",
            f"Gold spot  : {float(sym.bid):.2f} / {float(sym.ask):.2f} "
            f"(spread {int(sym.spread)} pts)",
            "",
            "<b>STRATEGIES (live, demo)</b>",
            _strategy_state("V11 D1", lambda: load_d1_recent(gateway=gw), v11_sig),
            _strategy_state("V12 H1", lambda: load_h1_recent(gateway=gw), v12_sig),
            "",
            "Risk 1% (V11) / 2% (V12) per trade. SL+TP always attached.",
        ]
        return "\n".join(lines), {
            "login": acc.login,
            "balance": float(acc.balance),
            "equity": float(acc.equity),
            "positions": len(positions),
            "bid": float(sym.bid),
            "ask": float(sym.ask),
        }
    finally:
        gw.disconnect()


def deliver(text: str, label: str) -> int:
    """Send `text` to Telegram and echo a one-line result to stdout.

    Returns a PROCESS EXIT CODE, not a bool: a failed send must surface as
    non-zero so cron records an alert. A silently undelivered trade update is
    worse than a noisy failure. Never raises — the caller's own message has
    already been printed by then.
    """
    try:
        ok, desc = send_telegram(text)
    except Exception as exc:  # noqa: BLE001
        print(f"{label} Telegram send failed: {type(exc).__name__}: {exc}")
        return 1
    print(f"{label} Telegram: {'sent' if ok else 'FAILED ' + desc}")
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--telegram", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    text, raw = collect()
    print(json.dumps(raw, indent=2) if args.json else text)
    if args.telegram:
        ok, desc = send_telegram(text)
        print(f"TELEGRAM: {'SENT' if ok else 'FAILED ' + desc}")
        return 0 if ok else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
