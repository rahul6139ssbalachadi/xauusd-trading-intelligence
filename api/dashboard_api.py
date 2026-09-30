"""Dashboard API: the main overview and per-strategy pages.

Read-only. Nothing here places an order, connects to MT5, or mutates
state. The main page is the owner's requested layout:

  header      Trading AI · MT5 state · account · engine · strategy
  money       balance, equity, today's P&L, total P&L, drawdown
  positions   symbol, direction, entry, SL, TP, current P&L
  recent      recent trades
  strategies  cards -> per-strategy pages (trades, win rate, PF, max DD,
               average trade, and the robustness evidence behind them)

Every number is either broker-confirmed or labelled as backtest. Nothing
is estimated, and a metric that was never measured renders as "—".
"""
from __future__ import annotations

from datetime import datetime, timezone

from api import strategy_stats as S
from api.live_status import read_health, read_latest_signals, read_open_positions


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _engine_state(reader: dict) -> dict:
    """Is the decision engine actually running?

    'running' is only claimed when the read service completed a cycle in
    the last five minutes. Anything else is reported honestly, because a
    dashboard that says RUNNING while nothing is evaluating signals is
    worse than one that admits it is down.
    """
    if not reader.get("running"):
        return {"state": "stopped", "label": "STOPPED",
                "detail": "read service has never run"}
    if not reader.get("connected"):
        return {"state": "offline", "label": "OFFLINE",
                "detail": reader.get("last_error") or "MT5 not connected"}
    if reader.get("stale"):
        return {"state": "stale", "label": "STALE",
                "detail": f"no cycle for {reader.get('stale_seconds')}s"}
    return {"state": "running", "label": "RUNNING",
            "detail": f"{reader.get('cycles')} cycles, last "
                      f"{reader.get('stale_seconds')}s ago"}


def overview() -> dict:
    """Everything the main dashboard renders, in one request."""
    reader = read_health()
    money = S.money_fields(reader)
    engine = _engine_state(reader)
    approved = S._approved_map()
    signals = read_latest_signals(limit=20)

    # Which strategy is currently "the" active one: an approved strategy
    # with a recent signal, else the first approved, else none.
    active = None
    for s in signals:
        if s.get("strategy_number"):
            active = s
            break
    if active is None and approved:
        first = next(iter(approved.values()))
        active = {"strategy": first.get("name"),
                  "strategy_number": first.get("version"),
                  "direction": "WAIT", "reason": "no signal recorded yet"}

    return {
        "generated_at": _now(),
        "header": {
            "app": "Trading AI",
            "mt5": "connected" if reader.get("connected") else "disconnected",
            "mt5_connected": bool(reader.get("connected")),
            "account": reader.get("login"),
            "server": reader.get("server"),
            "company": reader.get("company"),
            "engine": engine,
            "active_strategy": active,
            "stale": bool(reader.get("stale")),
            "stale_seconds": reader.get("stale_seconds"),
        },
        "money": money,
        "positions": read_open_positions(),
        "recent_signals": signals[:8],
        "recent_trades": S.recent_backtest_trades(15),
        "strategies": S.all_strategies(),
        "execution": {
            "mode": "demo",
            "live_money": False,
            "note": "Demo only. This build cannot send a real-money order.",
        },
    }


def strategy_page(version_or_name: str) -> dict:
    """One strategy's full page: headline metrics, robustness, periods."""
    m = S.strategy_metrics(version_or_name)
    if not m.get("found"):
        return {"found": False, "requested": version_or_name}

    # The live signal for this strategy, if the engine has produced one.
    live = None
    for s in read_latest_signals(limit=100):
        if s.get("strategy_number") == m.get("version"):
            live = s
            break

    return {
        "found": True,
        "generated_at": _now(),
        "strategy": m,
        "live_signal": live,
        "execution": {"mode": "demo", "live_money": False},
    }
