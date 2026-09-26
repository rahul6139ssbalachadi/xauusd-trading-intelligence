"""Backtest-only safety guard.

The monthly backtester must be INCAPABLE of trading, not merely choosing
not to trade. Two layers:

1. STRUCTURAL (always on, not configurable)
   - monthly_bt never imports MetaTrader5, and never references
     `order_send`, `MT5Gateway` or `positions_get` (asserted statically by
     tests/test_monthly_bt.py::TestNoOrderPath).
   - the research DB is opened read-only (SQLite URI mode=ro).
   - results are written to a SEPARATE database (db/backtests.db).
   - `execution.run_v12_hourly` is imported ONLY for its pure-pandas
     feature builder `build_h1_features`. That module's order code lives
     inside `main()` and is never reached from here.

2. ENVIRONMENT (configurable, off by default and loud when used)
   `check_live_switch(require_disarmed=...)` reports whether
   `config/settings.toml:live_trading_enabled` is armed. With
   require_disarmed=True (CLI `--require-disarmed`) it fails CLOSED.
   It is OFF by default on purpose: that switch is the account owner's
   deliberate, unrelated setting, and this backtester neither reads nor
   writes it. The status is recorded in every run's provenance so a report
   always states the environment it ran in.
"""
from __future__ import annotations

import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class BacktestGuardError(RuntimeError):
    """Raised when a backtest-only environment precondition is violated."""


def live_switch_armed() -> bool:
    """True if config/settings.toml has live_trading_enabled = true."""
    with (ROOT / "config" / "settings.toml").open("rb") as fh:
        return bool(tomllib.load(fh).get("live_trading_enabled", False))


def check_live_switch(require_disarmed: bool = False) -> dict:
    """Report (and optionally enforce) that the account is disarmed.

    Returns a status dict recorded in every run's provenance.
    """
    armed = live_switch_armed()
    if armed and require_disarmed:
        raise BacktestGuardError(
            "live_trading_enabled=true in config/settings.toml and "
            "--require-disarmed was given. This backtester is simulation-only "
            "(no order path, read-only DB, separate output DB) but it refuses "
            "to run while the account is armed. Disarm the switch, or drop "
            "the flag."
        )
    return {
        "live_trading_enabled": armed,
        "enforced_disarmed": require_disarmed,
        "order_capable": False,
        "note": "monthly_bt has no order path; the switch is reported, "
                "never modified",
    }
