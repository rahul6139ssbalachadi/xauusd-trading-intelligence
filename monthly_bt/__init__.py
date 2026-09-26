"""Month-by-month backtest + visual signal layer for V11 / V12.

READ-ONLY RESEARCH MODULE. This package can never place an order:

  * it imports NO execution module that owns an order path
  * it never imports MetaTrader5
  * it opens the research DB in read-only mode
  * it writes to a SEPARATE database (db/backtests.db), never trading.db
  * it refuses to run if live_trading_enabled is anything other than False
    (see guard.py) — the module is backtest-only by construction.

Strategy logic is REUSED, never rewritten. V11 signals come from
`research.v11_d1_momentum`; V12 features come from
`execution.run_v12_hourly.build_h1_features` with the gate set proven
identical to `signal_on_last_closed_bar` by tests/test_monthly_bt.py.
"""
from __future__ import annotations

__all__ = ["strategies", "engine", "store", "report", "visual", "guard"]
