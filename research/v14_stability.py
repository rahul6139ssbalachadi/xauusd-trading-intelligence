"""V14 GATE STABILITY: does the ACCEPT survive a change of history?

check_gate_stability.py is hardwired to V12. V12's verdict was found to
FLIP (ACCEPT -> REJECT) purely by prepending 4 months of bars, which means
its in-sample metrics are sample-sensitive rather than robust.

This does the same test for V14, plus the harsher version: truncate the
FRONT of the history and re-measure. A robust strategy keeps its verdict.
A lucky parameter pick loses its edge as soon as the sample moves.

Usage:
    ./.venv/Scripts/python.exe research/v14_stability.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import research.v14_regime_pullback as v14
from research.v14_stress import run_one_position

BEST = {"entry": "pullback_or_cross", "pullback_band": 0.5,
        "max_holding": 20, "atr_mult": 1.5, "rr": 1.5}


def measure(d: pd.DataFrame, label: str) -> dict:
    """One-position-at-a-time, the realistic model, over a data span."""
    trades, m = run_one_position(d, **BEST)
    n = len(trades)
    wins = sum(1 for t in trades if t.net_pips > 0)
    net = m.get("net_pips", 0)
    print(f"  {label:26s} t={n:4d} net={net:9.1f} "
          f"PF={m.get('profit_factor', float('nan')):5.2f} "
          f"win%={(wins / n * 100) if n else 0:4.1f} "
          f"maxDD={m.get('max_drawdown_pips', 0):8.1f}")
    return {"n": n, "net": net, "pf": m.get("profit_factor", 0.0)}


def main() -> None:
    raw = v14.load("XAUUSD", "D1")
    d = v14.features(raw)
    print("=" * 78)
    print("V14 GATE STABILITY  XAUUSD D1")
    print("=" * 78)
    print(f"  {len(d)} bars  {d['ts'].iloc[0].date()} -> {d['ts'].iloc[-1].date()}")
    print(f"  params (frozen from TRAIN): {BEST}")
    print("  model: ONE position at a time\n")

    print("[1] FULL span (the reported result)")
    measure(d, "full 2016-2026")

    print("\n[2] Truncate the FRONT (drop oldest history)")
    for keep in (0.90, 0.80, 0.70, 0.60):
        sub = d.iloc[int(len(d) * (1 - keep)):]
        measure(sub, f"last {keep * 100:.0f}% "
                     f"({sub['ts'].iloc[0].date()})")

    print("\n[3] Drop the BEST year (2024) - does the edge survive without it?")
    yrs = d["ts"].dt.year
    for drop in (2024, 2025):
        measure(d[yrs != drop], f"all except {drop}")

    print("\n[4] Split halves (independent periods, same params)")
    h = len(d) // 2
    measure(d.iloc[:h], f"first half ({d['ts'].iloc[0].date()})")
    measure(d.iloc[h:], f"second half ({d['ts'].iloc[h].date()})")

    print("\n[5] Verdict stability")
    full = measure(d, "full")
    p75 = measure(d.iloc[int(len(d) * 0.25):], "drop oldest 25%")
    h1 = measure(d.iloc[:h], "first half")
    h2 = measure(d.iloc[h:], "second half")
    verdicts = [v["net"] > 0 for v in (full, p75, h1, h2)]
    stable = all(verdicts)
    print(f"  profitable in every span: {verdicts}  -> "
          f"{'STABLE' if stable else 'UNSTABLE (sample-sensitive)'}")
    if not stable:
        print("  A verdict that flips when history changes is NOT robustness.")
        print("  V14 would be REJECT under the same standard applied to V12.")

    print("\nRead-only. No MT5 writes. No live trading.")


if __name__ == "__main__":
    main()