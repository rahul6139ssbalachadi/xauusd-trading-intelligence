"""Trade-log persistence — a SEPARATE database.

db/trading.db is the research market-data store and is never written here.
db/backtests.db is created on demand and holds:

  backtest_runs     one row per run, with the full reproducibility manifest
  backtest_signals  one row per generated signal (even if no trade opened)
  backtest_trades   one row per SIMULATED trade

SIGNAL and TRADE are separate tables on purpose: the spec requires the
chart/report to distinguish "the strategy said BUY" from "the simulator
actually opened a position" (a signal can be rejected for sizing, or
because a position was already open).
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "db" / "backtests.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS backtest_runs (
    run_id TEXT PRIMARY KEY,
    run_ts_utc TEXT NOT NULL,
    strategy TEXT, strategy_name TEXT, strategy_version TEXT,
    logic_source TEXT, params_json TEXT,
    symbol TEXT, timeframe TEXT, period TEXT,
    data_json TEXT, sim_config_json TEXT, guard_json TEXT,
    initial_balance REAL,
    git_commit TEXT,
    signals INTEGER, trades INTEGER,
    ending_balance REAL, metrics_json TEXT
);
CREATE TABLE IF NOT EXISTS backtest_signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    strategy TEXT, symbol TEXT, timeframe TEXT,
    period TEXT,
    signal TEXT,
    signal_bar INTEGER, entry_bar INTEGER,
    signal_ts TEXT, entry_ts TEXT,
    entry REAL, sl REAL, tp REAL,
    risk_pips REAL, atr REAL, body_pct REAL,
    simulated INTEGER,           -- 1 if a trade was opened for it
    skip_reason TEXT,
    UNIQUE(run_id, strategy, entry_bar)
);
CREATE TABLE IF NOT EXISTS backtest_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    strategy TEXT, symbol TEXT, timeframe TEXT,
    period TEXT,
    signal TEXT,
    signal_bar INTEGER, entry_bar INTEGER, exit_bar INTEGER,
    signal_ts TEXT, entry_ts TEXT, exit_ts TEXT,
    entry REAL, sl REAL, tp REAL, exit REAL,
    exit_reason TEXT,
    lots REAL, commission REAL, swap REAL,
    spread_pips REAL, slippage_pips REAL, cost_pips REAL,
    gross_pips REAL, net_pips REAL, r_multiple REAL,
    gross_pnl REAL, fees REAL, net_pnl REAL,
    balance_after REAL,
    UNIQUE(run_id, strategy, entry_bar)
);
"""


def new_run_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    return f"MBT-{stamp}-{uuid.uuid4().hex[:6]}"


def connect() -> sqlite3.Connection:
    DB.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB)
    con.executescript(SCHEMA)
    return con


def save_run(manifest: dict, results: list[dict], con: sqlite3.Connection | None = None) -> str:
    """Persist one run: manifest + every signal + every simulated trade."""
    own = con is None
    con = con or connect()
    run_id = new_run_id()
    agg = aggregate(results)
    con.execute(
        "INSERT INTO backtest_runs (run_id, run_ts_utc, strategy, strategy_name,"
        " strategy_version, logic_source, params_json, symbol, timeframe, period,"
        " data_json, sim_config_json, guard_json, initial_balance, git_commit,"
        " signals, trades, ending_balance, metrics_json)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (run_id, manifest["run_ts_utc"], manifest["strategy"],
         manifest["strategy_name"], manifest["strategy_version"],
         manifest["logic_source"], json.dumps(manifest["params"]),
         manifest["symbol"], manifest["timeframe"], manifest["period"],
         json.dumps(manifest["data"], default=str),
         json.dumps(manifest["sim_config"]),
         json.dumps(manifest["guard"]),
         manifest["initial_balance"], manifest["git_commit"],
         agg["signals"], agg["trades"], agg["ending_balance"],
         json.dumps(agg["metrics"], default=str)))
    con.commit()
    if own:
        con.close()
    return run_id


def save_results(run_id: str, results: list[dict], con: sqlite3.Connection | None = None) -> None:
    """Signals and trades for every period of a run."""
    own = con is None
    con = con or connect()
    traded = {(r["strategy"], r["entry_bar"]) for res in results
              for r in res.get("trade_rows", [])}
    for res in results:
        period = res.get("period", "")
        strategy = res.get("strategy") or _strategy_of(res)
        for s in res.get("signals", []):
            con.execute(
                "INSERT OR IGNORE INTO backtest_signals (run_id, strategy, symbol,"
                " timeframe, period, signal, signal_bar, entry_bar, signal_ts,"
                " entry_ts, entry, sl, tp, risk_pips, atr, body_pct, simulated,"
                " skip_reason) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (run_id, s["strategy"], s["symbol"], s["timeframe"], period,
                 s["signal"], s["signal_bar"], s["entry_bar"],
                 str(s.get("signal_ts")), str(s.get("entry_ts")),
                 s["entry"], s["sl"], s["tp"], s["risk_pips"], s["atr"],
                 s["body_pct"], int((s["strategy"], s["entry_bar"]) in traded),
                 _skip_reason(res, s)))
        for t in res.get("trade_rows", []):
            con.execute(
                "INSERT OR IGNORE INTO backtest_trades (run_id, strategy, symbol,"
                " timeframe, period, signal, signal_bar, entry_bar, exit_bar,"
                " signal_ts, entry_ts, exit_ts, entry, sl, tp, exit, exit_reason,"
                " lots, commission, swap, spread_pips, slippage_pips, cost_pips,"
                " gross_pips, net_pips, r_multiple, gross_pnl, fees, net_pnl,"
                " balance_after) VALUES (" + ",".join(["?"] * 30) + ")",
                (run_id, t["strategy"], t["symbol"], t["timeframe"], period,
                 t["signal"], t["signal_bar"], t["entry_bar"], t["exit_bar"],
                 str(t.get("signal_ts")), str(t.get("entry_ts")), t["exit_ts"],
                 t["entry"], t["sl"], t["tp"], t["exit"], t["exit_reason"],
                 t["lots"], t["commission"], t["swap"], t["spread_pips"],
                 t["slippage_pips"], t["cost_pips"], t["gross_pips"],
                 t["net_pips"], t["r_multiple"], t["gross_pnl"], t["fees"],
                 t["net_pnl"], t["balance_after"]))
    con.commit()
    if own:
        con.close()


def _strategy_of(res: dict) -> str:
    rows = res.get("trade_rows") or res.get("signals") or []
    return rows[0]["strategy"] if rows else "?"


def _skip_reason(res: dict, sig: dict) -> str | None:
    for sk in res.get("skipped", []):
        if sk["entry_bar"] == sig["entry_bar"]:
            return sk["skip_reason"]
    return None


def aggregate(results: list[dict]) -> dict:
    """Whole-run totals across every period."""
    trades = [t for res in results for t in res.get("trade_rows", [])]
    signals = [s for res in results for s in res.get("signals", [])]
    nets = [t["net_pnl"] for t in trades]
    equity = 0.0
    peak, max_dd = 0.0, 0.0
    for n in nets:
        equity += n
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
    gp = sum(n for n in nets if n > 0)
    gl = -sum(n for n in nets if n <= 0)
    return {
        "signals": len(signals),
        "trades": len(trades),
        "ending_balance": trades[-1]["balance_after"] if trades else None,
        "metrics": {
            "net_pnl": sum(nets),
            "gross_profit": gp, "gross_loss": gl,
            "profit_factor": (gp / gl) if gl > 0 else None,
            "max_drawdown": max_dd,
        },
    }


def fetch_run(run_id: str) -> dict:
    con = connect()
    run = con.execute("SELECT * FROM backtest_runs WHERE run_id=?",
                      (run_id,)).fetchone()
    cols = [d[0] for d in con.execute(
        "SELECT * FROM backtest_runs WHERE run_id=?", (run_id,)).description]
    con.close()
    return dict(zip(cols, run)) if run else {}
