"""DQR — BTCUSD AUGUST 2026 month-scoped backtest.

Runs the DQR spec (research/dqr_quality_reversal.py) over a single calendar
month and reports EVERY parameter family, not just the best one, so a good
August number cannot be presented as if it were the strategy's merit.

CRITICAL ORDER OF OPERATIONS (per the known warmup bug in this project):
  1. build features + D1 levels on the FULL history
  2. slice the EXECUTION frame to the target month
  Levels are availability-gated (known_from = defining D1 bar close), so a
  level from 2013 is legitimately usable in 2026, and slicing after building
  cannot leak the future. Building AFTER slicing would produce 0 signals
  because ATR would cold-start inside the month.

WHAT THIS IS AND IS NOT
  A single month is NOT a validation. It is a descriptive replay. 31 daily
  bars on D1, ~744 on H1. Any result here is statistically meaningless on its
  own and is reported next to the same strategy's full-history result for
  contrast. Month-scoped numbers that contradict the multi-year numbers are
  noise, and are labelled as such.

Usage:
    ./.venv/Scripts/python.exe research/dqr_btc_august.py
    ./.venv/Scripts/python.exe research/dqr_btc_august.py --month 2026-08 --tfs H1
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from research import dqr_quality_reversal as dqr
from backtest import compute_metrics
from montecarlo import run_monte_carlo, MCConfig

SYMBOL = "BTCUSD"
# The single most defensible config found in the full-history BTC D1 run
# (swings / deep_penetration). Frozen here BEFORE looking at August, so the
# month replay is a genuine out-of-sample replay rather than a fresh fit.
FROZEN = dict(
    level_tol_atr=0.10,
    close_pos_min=0.70,
    impulse_atr=0.80,
    sweep_lookback=1,
    break_lookback=0,
    break_atr=0.50,
    max_level_age_d1=120,
    max_level_dist_atr=3.0,
    min_level_touches=1,
    min_sweep_atr=0.25,          # deep_penetration
)
FROZEN_EXIT = dict(atr_mult_stop=1.5, rr=1.5, max_holding_bars=32,
                   cooldown_bars=4)


def month_bounds(month: str) -> tuple[pd.Timestamp, pd.Timestamp]:
    y, m = (int(x) for x in month.split("-"))
    start = pd.Timestamp(y, m, 1)
    end = pd.Timestamp(y + 1, 1, 1) if m == 12 else pd.Timestamp(y, m + 1, 1)
    return start, end


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default=SYMBOL)
    ap.add_argument("--month", default="2026-08")
    ap.add_argument("--tfs", default="H1,D1,H4",
                    help="execution timeframes. M15/M5 exist for Aug 2026 but "
                         "are too short to carry D1 levels meaningfully.")
    ap.add_argument("--full-context", action="store_true",
                    help="also print the same config over full history")
    args = ap.parse_args()

    spec = dqr.SYMBOLS[args.symbol]
    pip = spec["pip"]
    spr = spec["fallback_spread_pips"]
    slip = spec["fallback_slippage_pips"]
    start, end = month_bounds(args.month)

    print("=" * 78)
    print(f"DQR — {args.symbol} BACKTEST :: {args.month} (single month)")
    print("=" * 78)
    print(f"window: {start.date()} .. {(end - pd.Timedelta(days=1)).date()}")
    print(f"costs : spread<={spr}pips slippage={slip}pips (per-bar DB spread "
          f"used when present), round-trip in pips")
    print()
    print("WARNING: one month is a DESCRIPTIVE REPLAY, not a validation.")
    print("         31 D1 bars / ~744 H1 bars. Read alongside the multi-year")
    print("         numbers, never instead of them.")
    print()

    d1 = dqr.build_exec_features(dqr.load("D1", args.symbol))
    counter = dqr.D1TouchCounter(d1, touch_atr=0.30)
    levels = {fam: dqr.enrich_levels(
        d1, dqr.build_d1_levels(d1, dqr.FAMILY_GRID[fam]),
        min_touches=1, touch_atr=0.30) for fam in dqr.FAMILY_GRID}
    print(f"D1 level frame: {len(d1)} bars "
          f"({d1['ts'].iloc[0].date()} .. {d1['ts'].iloc[-1].date()})")
    for fam, lv in levels.items():
        print(f"  levels[{fam:>10}] = {len(lv):5d}")
    print()

    rows = []
    for tf in [t for t in args.tfs.split(",") if t]:
        full = dqr.build_exec_features(dqr.load(tf, args.symbol))
        if full.empty:
            print(f"-- {tf}: no data, skipped\n")
            continue
        month = full[(full["ts"] >= start) & (full["ts"] < end)].reset_index(drop=True)
        if len(month) < 30:
            print(f"-- {tf}: only {len(month)} bars in {args.month}, skipped\n")
            continue

        print(f"--- execution TF {tf} ({len(month)} bars in month) ---")
        for fam in dqr.FAMILY_GRID:
            sigs_m = dqr.detect_signals(month, levels[fam], d1=d1,
                                         touch_counter=counter, **FROZEN)
            tr, m = dqr.backtest_signals(
                month, sigs_m, spread_pips=spr, slippage_pips=slip,
                pip=pip, **FROZEN_EXIT)
            if not tr:
                print(f"    {fam:>10}: 0 signals in {args.month}")
                rows.append((tf, fam, 0, None))
                continue
            nb = sum(1 for t in tr if t.side == "BUY")
            print(f"    {fam:>10}: {m['total_trades']:>3} trades  {dqr.fmt(m)}"
                  f"  BUY={nb} SELL={m['total_trades']-nb}")
            for t in tr:
                print(f"        {t.side:<4} entry {t.entry_price:>10.2f} "
                      f"stop {t.stop:>10.2f} exit {t.exit_price:>10.2f} "
                      f"({t.exit_reason}) net {t.net_pips:>9.1f}p")
            rows.append((tf, fam, m["total_trades"], m))
        print()

    if args.full_context:
        print("=" * 78)
        print("CONTEXT — same frozen config over FULL history (not a month)")
        print("=" * 78)
        for tf in [t for t in args.tfs.split(",") if t]:
            full = dqr.build_exec_features(dqr.load(tf, args.symbol))
            if full.empty:
                continue
            for fam in dqr.FAMILY_GRID:
                sigs = dqr.detect_signals(full, levels[fam], d1=d1,
                                          touch_counter=counter, **FROZEN)
                tr, m = dqr.backtest_signals(
                    full, sigs, spread_pips=spr, slippage_pips=slip,
                    pip=pip, **FROZEN_EXIT)
                if not tr:
                    print(f"  {tf:>3} {fam:>10}: 0 trades")
                    continue
                mc = (run_monte_carlo(tr, MCConfig(n_iterations=2000, seed=42,
                                                   shuffle=True, scatter_pct=0.10,
                                                   jitter_pct=0.05,
                                                   ruin_threshold=-200.0))
                      if len(tr) >= 10 else None)
                print(f"  {tf:>3} {fam:>10}: {dqr.fmt(m)}"
                      + (f"  MC p5={mc.net_p5:.0f} PF_p5={mc.profit_factor_p5:.2f} "
                         f"robust={mc.is_robust}" if mc else ""))
        print()

    print("=" * 78)
    print(f"AUGUST SUMMARY — {args.symbol} {args.month}")
    print("=" * 78)
    any_trade = False
    for tf, fam, n, m in rows:
        if n == 0:
            print(f"  {tf:>3} {fam:>10}: no signals")
            continue
        any_trade = True
        print(f"  {tf:>3} {fam:>10}: {dqr.fmt(m)}")
    if not any_trade:
        print("  NO DQR setup fired in this month on any tested family.")
        print("  That is a legitimate outcome (the spec requires a real sweep),")
        print("  not a bug — but it means the month cannot confirm or deny")
        print("  the hypothesis either way.")
    print()
    print("Read-only. No MT5 writes, no orders, no fabricated numbers.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
