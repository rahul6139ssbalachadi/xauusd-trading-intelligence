"""V11 D1 paper trading run.

SIMULATION-ONLY. Does NOT touch MT5 in trade mode and NEVER places or modifies
any trade on the user's demo account. This script:

  1. Runs the V11 D1 momentum signal logic over the full 10-year D1 dataset
     (from db/trading.db, read-only).
  2. Journals every bar (BUY signal or WAIT) to paper/journal_v11_d1.jsonl
     with bias + reasons + planned entry/stop/target/lots/risk.
  3. Summarizes the paper-trade decisions (BUY/SELL/WAIT counts, planned risk).
  4. Prints a read-only summary report.

Usage:
    ./.venv/Scripts/python.exe research/v11_paper_trade.py
    ./.venv/Scripts/python.exe research/v11_paper_trade.py --max-entries 300
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from paper import JournalStore, summarize_journal
from paper.d1_paper import paper_run_d1
from reporting import ExperimentRegistry

# V11 best params (from research/v11_d1_momentum.py research, documented in
# the strategy def JSON). Uses 1% risk per trade per user instruction.
# R:R 1:2 means 2% reward target of total balance per trade.
V11_PARAMS = {
    "body_pct_threshold": 0.95,
    "trend_filter": True,
    "min_atr_pct": 0.005,
    "rr": 2.0,
    "atr_mult_stop": 1.5,
    "max_holding_d1": 8,
}
V11_RISK_PCT = 0.01  # 1% risk per trade (user instruction)


def main():
    ap = argparse.ArgumentParser(description="V11 D1 paper-trade (simulation-only)")
    ap.add_argument("--max-entries", type=int, default=None,
                    help="cap number of journal bars (for quick smoke runs)")
    ap.add_argument("--equity", type=float, default=10_000.0,
                    help="simulated paper equity in USD")
    ap.add_argument("--journal", type=str, default="paper/journal_v11_d1.jsonl",
                    help="journal output path")
    args = ap.parse_args()

    reg = ExperimentRegistry()
    eid = reg.mint("PAPER", "V11 D1 Momentum paper-trade (simulation-only)")
    print(f"Experiment ID: {eid}")

    journal = JournalStore(args.journal)

    # Clear any prior journal so this run is a clean audit trail
    journal_path = Path(args.journal)
    if journal_path.exists():
        journal_path.unlink()
        print(f"Cleared prior journal: {journal_path}")

    print(f"\nV11 paper_run (SIMULATION-ONLY, no MT5 writes, no live trading)")
    print(f"Params: {V11_PARAMS}")
    print(f"Equity: ${args.equity:,.2f}")
    print(f"Journal: {journal_path}")
    print("-" * 60)

    sized = paper_run_d1(
        V11_PARAMS,
        journal,
        equity=args.equity,
        max_entries=args.max_entries,
    )

    summary = summarize_journal(journal)
    print(f"\n{'='*60}")
    print(f"  V11 PAPER-TRADE SUMMARY (simulation-only)")
    print(f"{'='*60}")
    print(f"  Total bars journaled : {summary['total']}")
    print(f"  BUY decisions        : {summary['buys']}")
    print(f"  SELL decisions       : {summary['sells']}")
    print(f"  WAIT decisions       : {summary['waits']}")
    print(f"  Planned risk (total) : ${summary['planned_risk_usd']:.2f}")
    print(f"\n  Sized trades: {len(sized)}")
    if sized:
        wins = [t for t in sized if t.net_usd > 0]
        print(f"  Wins: {len(wins)} / {len(sized)} "
              f"({len(wins)/len(sized)*100:.1f}%)")
        total_net = sum(t.net_usd for t in sized)
        print(f"  Total paper P&L    : ${total_net:,.2f}")
        print(f"  Per-trade avg P&L  : ${total_net/len(sized):.2f}")
        if wins:
            avg_win = sum(t.net_usd for t in wins) / len(wins)
            print(f"  Avg win            : ${avg_win:.2f}")
        losses = [t for t in sized if t.net_usd <= 0]
        if losses:
            avg_loss = sum(t.net_usd for t in losses) / len(losses)
            print(f"  Avg loss           : ${avg_loss:.2f}")

    print(f"\n{'='*60}")
    print("  REMINDERS:")
    print("  - This is a SIMULATION/audit trail. No MT5 trades were placed.")
    print("  - live_trading_enabled in config/settings.toml remains False.")
    print("  - V11 requires extended paper-trading before any live consideration.")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
