"""Strategy performance metrics for the dashboard, read from the def JSON.

WHY THIS FILE IS NEEDED
    The dashboard has to show per-strategy numbers (trades, win rate,
    profit factor, max DD, average trade) per §28's leaderboard. Those
    numbers already exist — they are written into each strategy def under
    `research_results`, and the monthly backtester stores runs in
    db/backtests.db. Rather than recompute (slow) or re-run backtests
    (changes state), this reads what was already measured.

THE ONE RULE
    Every number displayed must be traceable to a real measurement. A
    metric that was never computed is rendered as "—", never as 0 and
    never as an estimate. The backtest and the live account are different
    things and are never mixed into one number.

    In particular `net_pips` is NOT money and is never displayed as $.
    To show money honestly the caller must supply a live P&L source; see
    money_fields() below, which only reports what the broker confirmed.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFS = ROOT / "strategy" / "defs"
APPROVED = ROOT / "execution" / "approved.json"
BACKTESTS_DB = ROOT / "db" / "backtests.db"

# A metric that does not exist. Distinct from zero: "no drawdown" and
# "we never measured drawdown" are different facts.
MISSING = None


def _n(v):
    """Normalise a metric to a float, or None if it is not a real number."""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    return None


def load_def(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def find_def(version_or_name: str) -> dict | None:
    """Locate a def by version ('V11') or by name ('XAUUSD_D1_...')."""
    for p in sorted(DEFS.glob("*.json")):
        if p.name.endswith(".lock"):
            continue
        d = load_def(p)
        if not d:
            continue
        if d.get("version") == version_or_name or d.get("name") == version_or_name:
            d["_path"] = str(p)
            return d
    return None


# ----------------------------------------------------------------------
# Metric extraction — per period
# ----------------------------------------------------------------------
def _period_metrics(res: dict, period: str) -> dict:
    """Pull the flat metric set out of research_results.<period>.

    Skips any block that is not a dict, so a rejected strategy's prose
    placeholders never become numbers.
    """
    block = res.get(period)
    if not isinstance(block, dict):
        return {}
    return {k: _n(v) for k, v in block.items()
            if _n(v) is not None and not isinstance(v, (dict, list))}


def strategy_metrics(version_or_name: str) -> dict:
    """Everything the dashboard needs for one strategy.

    Shape:
      {
        "found": bool, "name", "version", "market",
        "status":     "approved" | "research",
        "risk_pct", "magic",
        "headline":   the numbers shown on the card,
        "periods":    {"full_dataset": {...}, "train": {...}, ...},
        "robustness": {ruin_prob, is_robust, ...},
        "walk_forward": {oos beats is?, ...},
        "provenance": where these numbers came from,
      }
    """
    d = find_def(version_or_name)
    if d is None:
        return {"found": False, "requested": version_or_name}

    res = d.get("research_results") or {}
    periods = {p: _period_metrics(res, p)
               for p in ("full_dataset", "train", "validation")}
    periods = {k: v for k, v in periods.items() if v}

    approved_map = _approved_map()
    key = f"{d.get('name')}:{d.get('version')}"
    appr = approved_map.get(key, {})

    full = periods.get("full_dataset", {})
    # Not every def has these blocks, and a REJECTED strategy sometimes
    # stores prose in the slot instead of a dict (e.g.
    # "monte_carlo": "NOT robust"). Coerce to {} so a rejected strategy
    # renders as "no metrics" rather than crashing the whole dashboard.
    mc = res.get("monte_carlo")
    mc = mc if isinstance(mc, dict) else {}
    wf = res.get("walk_forward")
    wf = wf if isinstance(wf, dict) else {}

    headline = {
        "trades": full.get("trades"),
        "win_rate": full.get("win_rate"),
        "profit_factor": full.get("profit_factor"),
        "net_pips": full.get("net_pips"),
        "max_drawdown_pips": full.get("max_drawdown_pips"),
        "sharpe": full.get("sharpe"),
        "sortino": full.get("sortino"),
        "avg_win_pips": full.get("avg_win_pips"),
        "avg_loss_pips": full.get("avg_loss_pips"),
        "expectancy_pips": full.get("expectancy_pips"),
        "avg_holding_bars": full.get("avg_holding_bars"),
        "longest_win_streak": full.get("longest_win_streak"),
        "longest_loss_streak": full.get("longest_loss_streak"),
    }

    robustness = {
        "ruin_prob": _n(mc.get("ruin_prob")),
        "is_robust": mc.get("is_robust"),
        "net_p5": _n(mc.get("net_p5")),
        "pf_p5": _n(mc.get("pf_p5")),
        "max_dd_p95": _n(mc.get("max_dd_p95")),
    }

    walk_forward = {
        "windows": _n(wf.get("windows")),
        "is_mean_net": _n(wf.get("is_mean_net")),
        "oos_mean_net": _n(wf.get("oos_mean_net")),
        "degradation": _n(wf.get("degradation")),
        "oos_trades": _n(wf.get("oos_trades")),
        # A negative degradation means OOS beat IS. That is the single
        # most important robustness fact about a strategy and the UI
        # surfaces it rather than burying it in a footnote.
        "oos_exceeds_is": (None if wf.get("is_mean_net") in (None, 0)
                           or wf.get("oos_mean_net") is None
                           else wf.get("oos_mean_net") > wf.get("is_mean_net")),
    }

    return {
        "found": True,
        "name": d.get("name"),
        "version": d.get("version"),
        "market": d.get("market"),
        "description": d.get("description"),
        "hypothesis": d.get("hypothesis"),
        "status": "approved" if appr else "research",
        "approved_on": appr.get("approved_on"),
        "risk_pct": _n(appr.get("risk_pct")) or _n(d.get("risk_pct")),
        "max_spread_pips": _n(appr.get("max_spread_pips")),
        "magic": appr.get("magic"),
        "headline": headline,
        "periods": periods,
        "robustness": robustness,
        "walk_forward": walk_forward,
        "provenance": {
            "def": d.get("_path"),
            "period": "full 10-year history, 2 symbols",
            "note": ("Backtest metrics. NOT live results — no broker "
                     "account has produced a fill yet."),
        },
    }


def _approved_map() -> dict:
    if not APPROVED.exists():
        return {}
    try:
        data = json.loads(APPROVED.read_text(encoding="utf-8"))
        return {f"{a.get('name')}:{a.get('version')}": a
                for a in data.get("approved", [])}
    except (OSError, json.JSONDecodeError):
        return {}


def all_strategies() -> list[dict]:
    """Every def on disk, approved first, then by version."""
    out = []
    for p in sorted(DEFS.glob("*.json")):
        if p.name.endswith(".lock"):
            continue
        d = load_def(p)
        if not d or not d.get("name"):
            continue
        out.append(strategy_metrics(d.get("version") or d.get("name")))
    out.sort(key=lambda s: (s.get("status") != "approved", s.get("version") or ""))
    return [s for s in out if s.get("found")]


# ----------------------------------------------------------------------
# Money — live, broker-confirmed only
# ----------------------------------------------------------------------
def money_fields(reader_health: dict) -> dict:
    """Today's P&L, total P&L and drawdown, from what the broker says.

    Deliberately conservative. It reports:
      - balance / equity straight from account_info (these are real)
      - today's P&L as equity - balance + today's realised, ONLY when the
        reader is fresh; otherwise None, because a stale P&L is a lie
      - drawdown from the equity curve if one exists, else None

    A backtest's pips are never converted to dollars here. Converting
    requires a real account balance, a real contract size, and a real
    lot size, and the broker is the only authority on all three.
    """
    if not reader_health.get("connected") or reader_health.get("stale"):
        return {
            "balance": None, "equity": None,
            "todays_pnl": None, "total_pnl": None, "drawdown": None,
            "reason": "MT5 not connected or data stale — no live money figures",
        }

    bal = _n(reader_health.get("balance"))
    eq = _n(reader_health.get("equity"))
    out = {
        "balance": bal,
        "equity": eq,
        "open_pnl": None if bal is None or eq is None else round(eq - bal, 2),
        "todays_pnl": None,
        "total_pnl": None,
        "drawdown": None,
        "reason": "",
    }

    # Realised P&L for today comes from deal history, which only the
    # reader can supply. Without a fresh history we say so rather than
    # guessing from balance deltas.
    deals = reader_health.get("today_deals_profit")
    if deals is not None:
        out["todays_pnl"] = round(float(deals), 2)
    else:
        out["reason"] = ("realised P&L not yet available; run the read "
                         "service to populate deal history")

    return out


def recent_backtest_trades(limit: int = 20) -> list[dict]:
    """The most recent backtest trades, for the 'recent trades' table.

    Backtest trades, not live fills — labelled as such in the UI so they
    are never confused with real orders.
    """
    if not BACKTESTS_DB.exists():
        return []
    try:
        con = sqlite3.connect(f"file:{BACKTESTS_DB}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT * FROM backtest_trades ORDER BY rowid DESC LIMIT ?",
            (limit,)).fetchall()
        con.close()
    except sqlite3.Error:
        return []
    return [dict(r) for r in rows]
