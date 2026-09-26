"""Robustness check on the study's top candidates — RESEARCH ONLY.

Takes the configurations the sweep flagged as interesting and subjects
them to the checks that have killed 12 of the 14 strategies in this repo:
Monte Carlo on the OUT-OF-SAMPLE trades, and a parameter-neighbourhood
scan to see whether the optimum is a plateau or a spike.

Read-only. Places no orders. Writes only to research/study/.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from monthly_bt import data as bt_data
from monthly_bt.engine import SimConfig
from monthly_bt.strategies import STRATEGIES
from montecarlo import MCConfig, run_monte_carlo
from research.v11_v12_param_study import features, run, signals, stats

OUT = ROOT / "research" / "study"

CANDIDATES = [
    # (label, tf, strategy, bp, min_atr, atr_mult, rr, hold, risk_pct)
    ("V11 D1 deployed      ", "D1", "V11", 0.95, 0.005, 1.5, 2.0, 8, 0.01),
    ("V11 D1 hold=2d       ", "D1", "V11", 0.95, 0.005, 1.5, 2.0, 2, 0.01),
    ("V11 D1 hold=3d rr1.5 ", "D1", "V11", 0.95, 0.005, 1.5, 1.5, 3, 0.01),
    ("V12 H1 deployed      ", "H1", "V12", 0.95, 0.004, 0.8, 3.0, 8, 0.02),
    ("V12 H1 hold=16h      ", "H1", "V12", 0.95, 0.004, 0.8, 3.0, 16, 0.02),
    ("V12 H1 hold=16h rr2.5", "H1", "V12", 0.95, 0.004, 0.8, 2.5, 16, 0.02),
    ("V12 H1 hold=32h rr2.5", "H1", "V12", 0.95, 0.004, 0.8, 2.5, 32, 0.02),
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--test-start", default="2025-01-01")
    args = ap.parse_args()
    split = pd.Timestamp(args.test_start, tz="UTC")
    sim = SimConfig(initial_balance=100_000.0, slippage_pips=1.0,
                    spread_multiplier=1.0, max_positions=1)

    cache = {}
    print("=" * 112)
    print(f"MONTE CARLO on OUT-OF-SAMPLE trades only ({args.test_start}+), "
          f"$100k balance, 2000 shuffles")
    print("=" * 112)
    print(f"{'candidate':<24}{'n':>5}{'net$':>10}{'PF':>7}"
          f"{'MC p5 net$':>12}{'MC p5 PF':>10}{'ruin%':>8}{'robust':>8}")
    print("-" * 112)

    rows = []
    for (label, tf, strat, bp, ma, am, rr, hold, rp) in CANDIDATES:
        if tf not in cache:
            cache[tf] = features(bt_data.load("XAUUSD", tf), 21, 55, 60)
        feats = cache[tf]
        spec = STRATEGIES[strat]
        sigs = signals(feats, body_pct=bp, min_atr=ma, atr_mult=am, rr=rr)
        oos = [s for s in sigs if feats.iloc[s["entry_bar"]]["ts"] >= split]
        trades = run(feats, oos, spec, hold, sim, rp)
        st = stats(trades)
        if not trades:
            print(f"{label:<24}{0:>5}{'-':>10}{'-':>7}")
            continue
        # montecarlo needs objects with net_pips / net_usd
        class T:
            def __init__(s, t):
                s.net_pips, s.net_usd = t["net_pips"], t["net_pnl"]
        mc = run_monte_carlo([T(t) for t in trades],
                             MCConfig(n_iterations=2000, seed=42, shuffle=True,
                                      scatter_pct=0.10, jitter_pct=0.05))
        robust = "yes" if mc.is_robust else "NO"
        pf_s = f"{st['pf']:.2f}" if st["pf"] else "n/a"
        print(f"{label:<24}{st['n']:>5}{st['net']:>10,.0f}{pf_s:>7}"
              f"{mc.net_p5:>12,.0f}{mc.profit_factor_p5:>10.2f}"
              f"{mc.ruin_prob*100:>8.1f}{robust:>8}")
        rows.append({"label": label.strip(), "n": st["n"], "net": st["net"],
                     "pf": st["pf"], "mc_p5": mc.net_p5,
                     "mc_pf_p5": mc.profit_factor_p5,
                     "ruin": mc.ruin_prob, "robust": mc.is_robust})

    print("=" * 112)
    print("robust = the 5th-percentile Monte Carlo run is still profitable.")
    print("ruin% = probability of a >$2,000 loss draw. A non-zero ruin on a")
    print("        1% risk strategy would be alarming; here it reflects the")
    print("        ruin_threshold passed to MCConfig, not account risk.")

    # ---- neighbourhood: is the optimum a plateau or a spike? ----
    print()
    print("=" * 112)
    print("NEIGHBOURHOOD SCAN (OOS net $) — a spike is overfitting, a plateau is real")
    print("=" * 112)
    for tf, strat, base_rp in (("D1", "V11", 0.01), ("H1", "V12", 0.02)):
        feats = cache.get(tf)
        if feats is None:
            feats = features(bt_data.load("XAUUSD", tf), 21, 55, 60)
            cache[tf] = feats
        spec = STRATEGIES[strat]
        ma = 0.005 if strat == "V11" else 0.004
        am = 1.5 if strat == "V11" else 0.8
        print(f"\n{strat} {tf}: rr across rows, max_hold across columns")
        holds = (2, 3, 5, 8, 13, 16) if tf == "D1" else (8, 12, 16, 24, 32)
        rrs = (1.0, 1.5, 2.0, 2.5, 3.0)
        print("rr\\hold  " + "".join(f"{h:>12}" for h in holds))
        for rr in rrs:
            line = f"{rr:<10}"
            for hold in holds:
                sigs = signals(feats, body_pct=0.95, min_atr=ma,
                               atr_mult=am, rr=rr)
                oos = [s for s in sigs if feats.iloc[s["entry_bar"]]["ts"] >= split]
                st = stats(run(feats, oos, spec, hold, sim, base_rp))
                mark = "" if st["n"] >= 12 else "*"
                line += f"{st['net']:>10,.0f}{mark:>1}"
            print(line)
    print()
    print("* = fewer than 12 OOS trades (insufficient; treat as noise)")

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "robustness.json").write_text(
        json.dumps(rows, indent=2, default=str), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
