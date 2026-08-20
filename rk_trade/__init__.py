"""RK Trade — Multi-user live analysis platform.

A read-only market analysis platform where multiple users can:
  - Create accounts (local JSON store, no external auth)
  - Register custom strategies (JSON definitions)
  - Receive live BUY/SELL/WAIT signals computed from their strategies
  - View real-time market data and signal history

SAFETY: This platform is ANALYSIS-ONLY. It connects to MT5 in read-only
mode (symbol_info, copy_rates_range, copy_ticks_range). It NEVER calls
order_send, order_check, positions_get, or any trading function.
live_trading_enabled stays false. Users execute manually if they choose.

Architecture:
  rk_trade/
    __init__.py          — package marker
    users.py             — user account management (JSON store)
    strategies.py        — per-user strategy registration + lookup
    live_feed.py         — read-only live MT5 data streaming
    signal_engine.py     — real-time signal computation per user/strategy
    journal.py           — per-user decision journal (JSONL)
    cli.py               — CLI interface for users

Run:
  ./.venv/Scripts/python.exe -m rk_trade.cli register --username alice
  ./.venv/Scripts/python.exe -m rk_trade.cli add-strategy --username alice --name MY_STRAT --file strategy/defs/XAUUSD_STRUCTURE_BREAK_V1.json
  ./.venv/Scripts/python.exe -m rk_trade.cli live --username alice --symbol XAUUSD
"""
from __future__ import annotations

from pathlib import Path

# Base paths
ROOT = Path(__file__).resolve().parents[1]
RK_TRADE_DIR = ROOT / "rk_trade"
USERS_DIR = RK_TRADE_DIR / "users"
STRATEGIES_DIR = RK_TRADE_DIR / "strategies"
JOURNAL_DIR = RK_TRADE_DIR / "journals"
CONFIG_DIR = ROOT / "config"

# Ensure directories exist
for d in (USERS_DIR, STRATEGIES_DIR, JOURNAL_DIR):
    d.mkdir(parents=True, exist_ok=True)

# Re-export key components
from rk_trade.users import UserManager, User
from rk_trade.strategies import StrategyRegistry
from rk_trade.live_feed import LiveFeed
from rk_trade.signal_engine import SignalEngine
from rk_trade.journal import DecisionJournal

__all__ = [
    "UserManager", "User", "StrategyRegistry",
    "LiveFeed", "SignalEngine", "DecisionJournal",
]
