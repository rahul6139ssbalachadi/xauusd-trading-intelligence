"""Run the 24/7 MT5 read service as a supervised process.

  python -m execution.read_service            # foreground, logs to console
  python -m execution.read_service --once     # one cycle, then exit (health probe)
  python -m execution.read_service --status   # print last known health and exit

This is the DATA service only. It reads the broker and writes the local
database. It cannot place an order — there is no order_send call in
this module or in execution/mt5_reader.py.

DEPLOYMENT
    Intended to run as a Windows service or a NSSM/schtasks job on the
    machine that has MT5 installed natively, not in Docker: MT5 is a
    Windows GUI application and the Python API needs a real terminal
    process attached to it.

    Typical task-scheduler entry (runs at start, restarts on failure):
      schtasks /Create /SC ONSTART /TN "TradingMT5Reader" ^
        /TR "C:\\path\\to\\.venv\\Scripts\\python.exe -m execution.read_service" ^
        /RL HIGHEST /F
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from market_data import config as cfg          # noqa: E402
from execution.mt5_reader import MT5Reader     # noqa: E402

HEALTH = ROOT / "run" / "mt5reader_health.json"

# What to keep fresh. D1 and H1 are the two approved strategies' own
# timeframes; M15/M5 are the structure timeframes the analysis uses.
DEFAULT_JOBS = [
    ("XAUUSD", "D1", 2000),
    ("XAUUSD", "H1", 3000),
    ("XAUUSD", "M15", 5000),
    ("XAUUSD", "M5", 5000),
]


def _write_health(health) -> None:
    HEALTH.parent.mkdir(parents=True, exist_ok=True)
    HEALTH.write_text(json.dumps(health.to_dict(), indent=2), encoding="utf-8")


def build_reader() -> MT5Reader:
    mt5_cfg = cfg.load_mt5_config()
    return MT5Reader(
        terminal_path=mt5_cfg["terminal_path"],
        allowed_login=int(mt5_cfg["allowed_login"]),
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="24/7 MT5 read service (no orders)")
    ap.add_argument("--once", action="store_true",
                    help="run a single cycle and exit")
    ap.add_argument("--status", action="store_true",
                    help="print the last recorded health and exit")
    ap.add_argument("--interval", type=int, default=60,
                    help="seconds between cycles (default 60)")
    ap.add_argument("--symbol", default=None,
                    help="restrict ingestion to one canonical symbol")
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")

    if args.status:
        if not HEALTH.exists():
            print("no health file yet — service has never run")
            return 1
        print(HEALTH.read_text(encoding="utf-8"))
        return 0

    # Owner decision 2026-09-30: demo only, never real money. Refuse to
    # start rather than start and quietly do nothing.
    mode = (cfg.load_settings().get("trading_mode") or "demo").lower()
    if mode != "demo":
        print(f"REFUSED: TRADING_MODE={mode!r}. This service is demo-only.",
              file=sys.stderr)
        return 2

    try:
        reader = build_reader()
    except FileNotFoundError:
        print("REFUSED: config/mt5.toml is missing. It holds the terminal "
              "path and the allowed demo login, and is git-ignored by design.",
              file=sys.stderr)
        return 2

    jobs = [j for j in DEFAULT_JOBS if not args.symbol or j[0] == args.symbol]

    if args.once:
        h = reader.cycle(jobs)
        _write_health(h)
        print(json.dumps(h.to_dict(), indent=2))
        # Decide the exit code from a snapshot: disconnect() clears
        # health.connected, so reading h.connected afterwards would always
        # report failure even on a fully successful cycle.
        ok = bool(h.connected and not h.last_error)
        reader.disconnect()
        return 0 if ok else 1

    logging.info("starting 24/7 read service: %d jobs, %ds interval",
                 len(jobs), args.interval)
    try:
        reader.run_forever(jobs, interval=args.interval)
    except KeyboardInterrupt:
        logging.info("stopped by user")
    finally:
        reader.disconnect()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
