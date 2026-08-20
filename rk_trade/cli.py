"""CLI interface for RK Trade multi-user live analysis platform.

Commands:
  register         — create a new user account
  list-users       — list all users
  add-strategy     — register a strategy for a user
  list-strategies  — list a user's strategies
  live             — stream live signals (analysis-only, no execution)
  signal           — compute single signal for a user+strategy
  journal          — show recent journal entries

SAFETY: All commands are READ-ONLY. No live trading, no MT5 order calls.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from rk_trade.users import UserManager
from rk_trade.strategies import StrategyRegistry
from rk_trade.signal_engine import SignalEngine
from rk_trade.journal import DecisionJournal


def cmd_register(args):
    um = UserManager()
    try:
        user = um.register(args.username, notes=args.notes,
                           default_symbol=args.symbol)
        print(f"User registered: {user.username}")
        print(f"  API key    : {user.api_key}")
        print(f"  Default    : {user.default_symbol} / {user.default_timeframe}")
        print(f"  Journal    : {um.journal_path(args.username)}")
        print(f"  Strategies : {um.strategy_dir(args.username)}")
    except FileExistsError:
        print(f"ERROR: user '{args.username}' already exists")
        sys.exit(1)


def cmd_list_users(args):
    um = UserManager()
    users = um.list_users()
    if not users:
        print("No users registered yet.")
        print("  Run: python -m rk_trade.cli register --username <name>")
        return
    print(f"{'Username':<20} {'Created':<28} {'Symbol':<10}")
    print("-" * 60)
    for u in users:
        print(f"{u.username:<20} {u.created_at[:25]:<28} {u.default_symbol:<10}")


def cmd_add_strategy(args):
    um = UserManager()
    if not um.get(args.username):
        print(f"ERROR: user '{args.username}' not found")
        sys.exit(1)
    sr = StrategyRegistry(um)
    dest = sr.register(args.username, args.name, args.file, is_active=args.activate)
    print(f"Strategy registered: {args.name} -> {dest.name}")
    print(f"  Source: {args.file}")
    print(f"  Active: {args.activate}")


def cmd_list_strategies(args):
    um = UserManager()
    sr = StrategyRegistry(um)
    if not um.get(args.username):
        print(f"ERROR: user '{args.username}' not found")
        sys.exit(1)
    strats = sr.list(args.username)
    if not strats:
        print(f"No strategies for user '{args.username}'.")
        return
    print(f"Strategies for '{args.username}':")
    for s in strats:
        active = "ACTIVE" if s["active"] else "inactive"
        print(f"  [{active}] {s['name']:<20} <- {s['file']}")


def cmd_signal(args):
    um = UserManager()
    sr = StrategyRegistry(um)
    if not um.get(args.username):
        print(f"ERROR: user '{args.username}' not found")
        sys.exit(1)
    engine = SignalEngine(um, sr, account_equity=args.equity)
    try:
        d = engine.compute_signal(args.username, args.strategy, args.symbol)
        print(json.dumps(d.to_dict(), indent=2, default=str))
    except Exception as e:
        print(f"ERROR: {e}")
        sys.exit(1)


def cmd_live(args):
    """Stream live signals for a user's active strategies."""
    um = UserManager()
    sr = StrategyRegistry(um)
    if not um.get(args.username):
        print(f"ERROR: user '{args.username}' not found")
        sys.exit(1)
    engine = SignalEngine(um, sr, account_equity=args.equity)
    journal = DecisionJournal(um.journal_path(args.username))

    active = sr.get_active(args.username)
    if not active:
        print(f"No active strategies for '{args.username}'.")
        print(f"  Run: python -m rk_trade.cli add-strategy --username {args.username} ...")
        return

    print(f"[RK TRADE] Live analysis for user '{args.username}'")
    print(f"  Active strategies: {[a[0] for a in active]}")
    print(f"  Symbol: {args.symbol}  Equity: ${args.equity}")
    print(f"  (ANALYSIS-ONLY — no live trading, no MT5 orders)")
    print(f"  Press Ctrl+C to stop.\n")

    try:
        while True:
            decisions = engine.compute_all(args.username, args.symbol)
            ts = decisions[0].timestamp if decisions else time.strftime("%Y-%m-%d %H:%M:%S")
            print(f"[{ts}]")
            for d in decisions:
                journal.append(d)
                status = "WAIT" if d.signal == "WAIT" else \
                         f"{d.signal} (conf {d.confidence:.0f}%, " \
                         f"lots={d.lots}, risk=${d.risk_usd:.2f})"
                print(f"  {d.strategy_name} V{d.strategy_version}: {status}")
                if d.signal != "WAIT" and d.reasons:
                    for r in d.reasons[:3]:
                        print(f"    reason: {r}")
            summary = journal.summarize(limit=50)
            print(f"  Journal: {summary}")
            print()
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nLive analysis stopped.")
    except Exception as e:
        print(f"\nLive analysis error (connection lost): {e}")
        print("Restart with: python -m rk_trade.cli live --username {args.username}")


def cmd_journal(args):
    um = UserManager()
    if not um.get(args.username):
        print(f"ERROR: user '{args.username}' not found")
        sys.exit(1)
    journal = DecisionJournal(um.journal_path(args.username))
    entries = journal.read(limit=args.limit)
    if not entries:
        print(f"No journal entries for '{args.username}'.")
        return
    print(f"Journal for '{args.username}' ({len(entries)} entries):")
    for e in entries:
        sig = e.get("signal", "?")
        conf = e.get("confidence", 0)
        strat = e.get("strategy", "?")
        ts = e.get("timestamp", "?")
        print(f"  [{ts}] {strat}: {sig} (conf {conf:.0f}%)")
    print()
    print("Summary:", json.dumps(journal.summarize(args.limit), indent=2))


def main():
    parser = argparse.ArgumentParser(
        prog="rk_trade", description="RK Trade — multi-user live analysis (read-only)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("register", help="create a new user account")
    p.add_argument("--username", required=True)
    p.add_argument("--notes", default="")
    p.add_argument("--symbol", default="XAUUSD")

    sub.add_parser("list-users", help="list all users")

    p = sub.add_parser("add-strategy", help="register a strategy for a user")
    p.add_argument("--username", required=True)
    p.add_argument("--name", required=True)
    p.add_argument("--file", required=True)
    p.add_argument("--activate", action="store_true", default=True)

    p = sub.add_parser("list-strategies", help="list a user's strategies")
    p.add_argument("--username", required=True)

    p = sub.add_parser("signal", help="compute a single signal")
    p.add_argument("--username", required=True)
    p.add_argument("--strategy", required=True)
    p.add_argument("--symbol", default="XAUUSD")
    p.add_argument("--equity", type=float, default=10000.0)

    p = sub.add_parser("live", help="stream live signals (Ctrl+C to stop)")
    p.add_argument("--username", required=True)
    p.add_argument("--symbol", default="XAUUSD")
    p.add_argument("--equity", type=float, default=10000.0)
    p.add_argument("--interval", type=float, default=10.0)

    p = sub.add_parser("journal", help="show recent journal entries")
    p.add_argument("--username", required=True)
    p.add_argument("--limit", type=int, default=20)

    args = parser.parse_args()
    handlers = {
        "register": cmd_register,
        "list-users": cmd_list_users,
        "add-strategy": cmd_add_strategy,
        "list-strategies": cmd_list_strategies,
        "signal": cmd_signal,
        "live": cmd_live,
        "journal": cmd_journal,
    }
    handlers[args.cmd](args)


if __name__ == "__main__":
    main()