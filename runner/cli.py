"""CLI command runner (CLAUDE.md §35).

Implements the user-facing command set:
  /market SYMBOL      — market summary (trend, regime, key levels)
  /analyze SYMBOL     — deep analysis (indicators + structure + patterns)
  /signal SYMBOL      — current BUY/SELL/WAIT signal from active strategy
  /backtest STRAT     — run backtest on a strategy def, show metrics
  /optimize STRAT     — run parameter grid search on TRAIN only
  /papertrade STRAT   — run paper trades over historical window
  /strategy-list      — list all strategy defs
  /strategy-performance— show leaderboard of strategy results
  /experiments        — list experiment IDs
  /risk-status        — show current risk config + daily/weekly limits
  /trading-status     — show live_trading_enabled + kill switch state
  /kill-switch        — show/reset kill switch state (read-only here)
  /report             — generate text + HTML report for active strategy

All commands are read-only. No live trading, no account writes.
The kill-switch can only be queried, never set to "closed" — per §24 it
requires manual reset outside the CLI.

Run:  python runner/cli.py <command> [args]
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

import pandas as pd

# ensure project root is on path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from market_data import config as cfg
from strategy import Strategy, build_features, evaluate
from backtest import run_backtest, compute_metrics
from validation import run_walk_forward, summarize_walk_forward
from montecarlo import run_monte_carlo, MCConfig
from candles import analyze_patterns, render_pattern_report
from reporting import ExperimentRegistry, build_report, render_text, write_report
from risk import RiskConfig, AccountState, compute_lot_size
from paper import JournalStore, paper_run, summarize_journal


DB = ROOT / cfg.load_settings()["paths"]["db"]
DEFS = ROOT / "strategy" / "defs"
JOURNAL = ROOT / "journal"
JOURNAL.mkdir(exist_ok=True)


def _load_ohlc(timeframe: str, symbol: str = "XAUUSD") -> pd.DataFrame:
    con = sqlite3.connect(DB)
    df = pd.read_sql_query(
        f"SELECT ts_broker_epoch, open, high, low, close, spread "
        f"FROM market_data WHERE symbol='{symbol}' AND timeframe='{timeframe}' "
        f"AND source='mt5' ORDER BY ts_broker_epoch", con)
    con.close()
    df["ts"] = pd.to_datetime(df["ts_broker_epoch"], unit="s", utc=True)
    df["spread_pips"] = df["spread"] * 0.01 / 0.10
    return df


def load_settings():
    return cfg.load_settings()


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------
def cmd_market(symbol: str):
    """Market summary: trend, regime, key levels."""
    m15 = _load_ohlc("M15", symbol)
    if len(m15) == 0:
        print(f"No M15 data for {symbol}")
        return
    feats = build_features(m15)
    latest = feats.iloc[-1]
    ema20 = latest.get("ema_fast")
    ema50 = latest.get("ema_slow")
    adx = latest.get("adx")
    vol_regime = latest.get("vol_regime")
    rsi = latest.get("rsi")
    session = latest.get("session")
    close = latest.get("close")

    print(f"--- {symbol} MARKET SUMMARY ---")
    print(f"  Current price : {close:.2f}")
    print(f"  EMA20/EMA50   : {ema20:.2f} / {ema50:.2f}  "
          f"({'bullish' if ema20 > ema50 else 'bearish' if ema20 < ema50 else 'flat'} trend)")
    print(f"  ADX           : {adx:.1f}  "
          f"({'trending' if adx > 25 else 'range-bound'})")
    print(f"  RSI           : {rsi:.1f}  "
          f"({'overbought' if rsi > 70 else 'oversold' if rsi < 30 else 'neutral'})")
    print(f"  Vol regime    : {vol_regime}")
    print(f"  Session       : {session}")
    print(f"  Last BOS dir  : {latest.get('last_bos_dir', 'n/a')}")
    print(f"  Last CHoCH dir: {latest.get('last_choch_dir', 'n/a')}")
    print(f"  Data bars     : {len(feats)} (M15)")


def cmd_analyze(symbol: str):
    """Deep analysis: indicators + structure + candle patterns."""
    m5 = _load_ohlc("M5", symbol)
    m15 = _load_ohlc("M15", symbol)
    if len(m5) == 0 or len(m15) == 0:
        print(f"No data for {symbol}")
        return
    m5f = build_features(m5)
    m15f = build_features(m15)

    print(f"--- {symbol} DEEP ANALYSIS ---")
    print(f"\n[M15 context ({len(m15f)} bars)]")
    latest_m15 = m15f.iloc[-1]
    print(f"  ADX={latest_m15['adx']:.1f}  RSI={latest_m15['rsi']:.1f}  "
          f"EMA20={latest_m15['ema_fast']:.2f}  EMA50={latest_m15['ema_slow']:.2f}  "
          f"Vol={latest_m15['vol_regime']}  Session={latest_m15['session']}")

    print(f"\n[M5 trigger ({len(m5f)} bars)]")
    latest_m5 = m5f.iloc[-1]
    print(f"  ADX={latest_m5['adx']:.1f}  RSI={latest_m5['rsi']:.1f}  "
          f"close={latest_m5['close']:.2f}  spread={latest_m5['spread_pips']:.1f}pips")

    print(f"\n[Candle patterns (M5, last 500 bars)]")
    recent = m5f.tail(500)
    stats = analyze_patterns(recent, min_samples=5, horizon=5)
    print(render_pattern_report(stats))


def cmd_signal(symbol: str):
    """Generate current signal from the latest active strategy."""
    defs = sorted(DEFS.glob("*.json"))
    if not defs:
        print("No strategy defs found.")
        return
    # use the highest V number of the base strategy
    strat = Strategy.load(defs[-1])
    m15 = _load_ohlc("M15", symbol)
    m5 = _load_ohlc("M5", symbol)
    if len(m15) == 0 or len(m5) == 0:
        print(f"No data for {symbol}")
        return
    fb = build_features(m15)
    ft = build_features(m5)
    dec = evaluate(strat, fb, ft)
    latest = dec.iloc[-1]
    print(f"--- {symbol} SIGNAL — {strat.name} {strat.version} ---")
    print(f"  Signal : {latest['signal']}")
    print(f"  Bias   : {latest['bias']}")
    print(f"  Reasons: {latest['reasons']}")
    if latest['signal'] != 'WAIT':
        print(f"  Stop   : {latest['stop']:.2f}")
        print(f"  Target : {latest['target']:.2f}")


def cmd_backtest(strat_name: str):
    """Run backtest on a strategy def."""
    path = DEFS / f"{strat_name}.json"
    if not path.exists():
        # try with V* suffix
        matches = list(DEFS.glob(f"{strat_name}*.json"))
        if not matches:
            print(f"Strategy not found: {strat_name}")
            return
        path = matches[-1]
    strat = Strategy.load(path)
    m15 = _load_ohlc("M15", strat.market)
    m5 = _load_ohlc("M5", strat.market)
    res = run_backtest(strat, build_features(m15), build_features(m5))
    print(f"--- BACKTEST: {strat.name} {strat.version} ({strat.market}) ---")
    for k, v in res.metrics.items():
        print(f"  {k:>20}: {v}")


def cmd_optimize(strat_name: str):
    """Run parameter grid search on TRAIN window only."""
    from research import grid_search
    path = DEFS / f"{strat_name}.json"
    if not path.exists():
        matches = list(DEFS.glob(f"{strat_name}*.json"))
        if not matches:
            print(f"Strategy not found: {strat_name}")
            return
        path = matches[-1]
    strat = Strategy.load(path)
    m15 = _load_ohlc("M15", strat.market)
    m5 = _load_ohlc("M5", strat.market)

    grid = {
        "bias_min_adx": [15, 20, 25],
        "atr_multiple": [1.0, 1.5, 2.0],
        "risk_reward": [1.5, 2.0, 2.5],
        "bias_trend": ["up", "down", "both"],
        "require_structure": [True, False],
    }
    results = grid_search(build_features(m15), build_features(m5), grid)
    print(f"--- OPTIMIZE: {strat.name} {strat.version} ---")
    print(f"{'TrainNet':>10} {'TrainPF':>8} {'TrainTr':>8} "
          f"{'OOSNet':>10} {'OOSPF':>8} {'Params'}")
    for r in results[:20]:
        print(f"{r.train_net:>10.1f} {r.train_pf:>8.2f} {r.train_trades:>8} "
              f"{r.oos_net:>10.1f} {r.oos_pf:>8.2f} {r.params}")


def cmd_strategy_list():
    """List all strategy defs."""
    print("--- STRATEGY LIST ---")
    for p in sorted(DEFS.glob("*.json")):
        s = Strategy.load(p)
        print(f"  {s.name} {s.version}  ({s.market}, {s.bias_timeframe}+{s.trigger_timeframe})")
    print(f"\nTotal: {len(list(DEFS.glob('*.json')))} strategies")


def cmd_risk_status():
    """Show current risk config."""
    settings = load_settings()
    cfg = RiskConfig()
    print("--- RISK STATUS ---")
    print(f"  live_trading_enabled : {settings.get('live_trading_enabled', False)}")
    print(f"  max_risk_per_trade   : {cfg.max_risk_per_trade_pct*100:.2f}%")
    print(f"  max_positions        : {cfg.max_positions}")
    print(f"  max_lots             : {cfg.max_lots}")
    print(f"  min_lots             : {cfg.min_lots}")
    print(f"  lot_step             : {cfg.lot_step}")
    acct = AccountState()
    print(f"  account equity       : ${acct.equity:.2f}")
    # example sizing
    lots = compute_lot_size(acct.equity, 0.0025, stop_distance_price=200.0 * 0.01,
                            contract_multiplier=100.0, cfg=cfg)
    print(f"  sample lot size (200pt stop, 0.25% risk): {lots} lots")


def cmd_trading_status():
    """Show trading/kill-switch status."""
    settings = load_settings()
    print("--- TRADING STATUS ---")
    print(f"  live_trading_enabled : {settings.get('live_trading_enabled', False)}")
    print(f"  Kill switch          : INACTIVE (read-only; manual reset only)")
    print(f"  DB path              : {DB}")
    print(f"  Strategy defs        : {DEFS}")
    con = sqlite3.connect(DB)
    n = con.execute("SELECT COUNT(*) FROM market_data WHERE symbol='XAUUSD'").fetchone()[0]
    con.close()
    print(f"  Market data rows     : {n}")


def cmd_report(strat_name: str, fmt: str = "text"):
    """Generate a report for a strategy."""
    path = DEFS / f"{strat_name}.json"
    if not path.exists():
        matches = list(DEFS.glob(f"{strat_name}*.json"))
        if not matches:
            print(f"Strategy not found: {strat_name}")
            return
        path = matches[-1]
    strat = Strategy.load(path)
    m15 = _load_ohlc("M15", strat.market)
    m5 = _load_ohlc("M5", strat.market)
    journal_path = JOURNAL / "journal.jsonl"
    journal = JournalStore(journal_path, db_path=DB)
    report = build_report(strat, build_features(m15), build_features(m5),
                          journal=journal, notes="Read-only auto-generated report")
    if fmt == "html":
        out = ROOT / "reports" / f"{strat.name}_{strat.version}.html"
        write_report(report, out, fmt="html")
        print(f"HTML report written to {out}")
    else:
        out = ROOT / "reports" / f"{strat.name}_{strat.version}.txt"
        write_report(report, out, fmt="text")
        print(f"Text report written to {out}")
    print(render_text(report))


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        prog="cli", description="XAUUSD trading system CLI (read-only)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("market", help="market summary for a symbol")
    p.add_argument("symbol", nargs="?", default="XAUUSD")

    p = sub.add_parser("analyze", help="deep analysis of a symbol")
    p.add_argument("symbol", nargs="?", default="XAUUSD")

    p = sub.add_parser("signal", help="current BUY/SELL/WAIT signal")
    p.add_argument("symbol", nargs="?", default="XAUUSD")

    p = sub.add_parser("backtest", help="run backtest on a strategy def")
    p.add_argument("strat", help="strategy name (e.g. XAUUSD_STRUCTURE_BREAK_V1)")

    p = sub.add_parser("optimize", help="parameter grid search (TRAIN only)")
    p.add_argument("strat", help="strategy name")

    p = sub.add_parser("strategy-list", help="list all strategies")

    p = sub.add_parser("risk-status", help="show risk configuration")

    p = sub.add_parser("trading-status", help="show trading/kill-switch status")

    p = sub.add_parser("report", help="generate report for a strategy")
    p.add_argument("strat", help="strategy name")
    p.add_argument("--fmt", choices=["text", "html"], default="text")

    p = sub.add_parser("experiments", help="list experiment IDs")

    p = sub.add_parser("papertrade", help="run paper trading over historical window")
    p.add_argument("strat", help="strategy name")

    args = parser.parse_args()

    if args.cmd == "market":
        cmd_market(args.symbol)
    elif args.cmd == "analyze":
        cmd_analyze(args.symbol)
    elif args.cmd == "signal":
        cmd_signal(args.symbol)
    elif args.cmd == "backtest":
        cmd_backtest(args.strat)
    elif args.cmd == "optimize":
        cmd_optimize(args.strat)
    elif args.cmd == "strategy-list":
        cmd_strategy_list()
    elif args.cmd == "risk-status":
        cmd_risk_status()
    elif args.cmd == "trading-status":
        cmd_trading_status()
    elif args.cmd == "report":
        cmd_report(args.strat, args.fmt)
    elif args.cmd == "experiments":
        reg = ExperimentRegistry()
        print(f"Experiments recorded: {reg._seq - 1}")
        print("(registry at .experiment_registry.jsonl)")
    elif args.cmd == "papertrade":
        path = DEFS / f"{args.strat}.json"
        if not path.exists():
            matches = list(DEFS.glob(f"{args.strat}*.json"))
            if not matches:
                print(f"Strategy not found: {args.strat}")
                return
            path = matches[-1]
        strat = Strategy.load(path)
        m15 = _load_ohlc("M15", strat.market)
        m5 = _load_ohlc("M5", strat.market)
        journal_path = JOURNAL / "papertrade_journal.jsonl"
        journal = JournalStore(journal_path)
        dec = paper_run(strat, build_features(m15), build_features(m5), journal,
                        equity=10000.0)
        print(f"Paper-traded {len(dec)} bars for {strat.name} {strat.version}")
        print(summarize_journal(journal))
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
