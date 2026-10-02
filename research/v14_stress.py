"""V14 STRESS TEST: does the ACCEPT survive realistic execution?

The plain v14_regime_pullback.py run PASSED all four gates. This file tries
to KILL that result, because a strategy that passes everything on the first
try is usually hiding a modelling error rather than finding an edge.

Five independent attacks:
  A. OVERLAP  - the base run() takes every signal independently, so several
     positions can be open at once. Real money cannot do that with one
     position. Re-run with ONE position at a time (skip new signals while
     a trade is live). This is the single most likely source of fake profit.
  B. SPREAD STRESS - re-run at 1.5x and 2.0x real spread. Anything that
     dies here was living on cost dilution (the failure mode of V1-V10).
  C. COST CAP - what if slippage is worse than modelled?
  D. YEARLY BREAKDOWN - is the edge spread across years, or made by one?
  E. LONG-ONLY TREND GATE - is V14 secretly just "gold goes up"?

Usage:
    ./.venv/Scripts/python.exe research/v14_stress.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import research.v14_regime_pullback as v14

BEST = {"entry": "pullback_or_cross", "pullback_band": 0.5,
        "max_holding": 20, "atr_mult": 1.5, "rr": 1.5}


def run_one_position(d: pd.DataFrame, **p) -> tuple[list, dict]:
    """Same signals as v14.run(), but strictly ONE position at a time."""
    sigs = v14.signals(d, entry=p["entry"], pullback_band=p["pullback_band"],
                       rr=p["rr"], atr_mult=p["atr_mult"])
    trades = []
    busy_until = -1
    for s in sigs:
        i = s["entry_bar"]
        if i <= busy_until:
            continue
        entry_p, stop, target = s["entry"], s["stop"], s["target"]
        last = min(i + p["max_holding"], len(d) - 1)
        if last <= i:
            continue
        exit_p, reason, hold, done = None, "target", 0, False
        for j in range(i + 1, last + 1):
            r = d.iloc[j]
            hold = j - i
            if r["low"] <= stop:
                exit_p, reason = stop, "stop"
                done = True
                break
            if r["high"] >= target:
                exit_p, reason = target, "target"
                done = True
                break
            if r["death"]:
                exit_p, reason = float(r["close"]), "regime_exit"
                done = True
                break
        if not done:
            j = last
            hold = j - i
            exit_p, reason = float(d.iloc[j]["close"]), "time"
        pips = (exit_p - entry_p) / v14.PIP
        trades.append(v14.Trade(
            entry_bar=i, entry_price=entry_p, side="LONG", stop=stop,
            target=target, exit_bar=i + hold, exit_price=exit_p,
            exit_reason=reason, points=pips * v14.PIP / v14.POINT,
            cost_pips=v14.COST_PIPS, net_pips=pips - v14.COST_PIPS,
            duration_bars=hold))
        busy_until = i + hold
    return trades, v14.compute_metrics(trades)


def attack_overlap(symbol: str, tf: str, d: pd.DataFrame) -> None:
    print("\n" + "-" * 78)
    print("ATTACK A: one position at a time (no overlapping trades)")
    print("-" * 78)
    _, base = v14.run(d, **BEST)
    base_t, _ = v14.run(d, **BEST)
    ov = sum(1 for a in range(len(base_t)) for b in range(a + 1, len(base_t))
             if base_t[a].entry_bar < base_t[b].exit_bar
             and base_t[b].entry_bar < base_t[a].exit_bar)
    one_t, one = run_one_position(d, **BEST)
    print(f"  base (overlapping allowed): {v14.line('', base)}")
    print(f"  one-position-at-a-time    : {v14.line('', one)}")
    print(f"  overlapping pairs in base : {ov}")
    print(f"  trades dropped by the 1-position rule: {len(base_t) - len(one_t)}")
    keep = one.get("net_pips", 0) > 0
    print(f"  VERDICT: {'SURVIVES' if keep else 'KILLED - profit was overlap'}")

    # same test inside TRAIN only, since that is what was selected on
    tr = d.iloc[:int(len(d) * 0.6)]
    _, tr_base = v14.run(tr, **BEST)
    _, tr_one = run_one_position(tr, **BEST)
    print(f"  TRAIN base  {v14.line('', tr_base)}")
    print(f"  TRAIN 1-pos {v14.line('', tr_one)}")


def attack_spread(symbol: str, tf: str, d: pd.DataFrame) -> None:
    print("\n" + "-" * 78)
    print("ATTACK B: spread / slippage stress (cost dilution check)")
    print("-" * 78)
    orig = v14.COST_PIPS
    try:
        for mult in (1.0, 1.5, 2.0, 3.0):
            v14.COST_PIPS = orig * mult
            _, m = v14.run(d, **BEST)
            print(f"  cost x{mult:.1f} ({orig * mult:4.1f} pips RT): "
                  f"{v14.line('', m)}")
    finally:
        v14.COST_PIPS = orig


def attack_yearly(symbol: str, tf: str, d: pd.DataFrame) -> None:
    print("\n" + "-" * 78)
    print("ATTACK D: yearly breakdown (is the edge spread or concentrated?)")
    print("-" * 78)
    trades, _ = v14.run(d, **BEST)
    # entry_bar is a positional index, not a timestamp -> rebuild from frame
    ts = d["ts"].iloc[[t.entry_bar for t in trades]].reset_index(drop=True)
    rows = []
    for y, grp in ts.groupby(ts.dt.year):
        nets = [t.net_pips for t, x in zip(trades, ts) if x.year == y]
        wins = sum(1 for n in nets if n > 0)
        rows.append((y, len(nets), sum(nets), wins / len(nets)))
    print(f"  {'year':6s} {'trades':>7s} {'net_pips':>10s} {'win%':>7s}")
    for y, n, net, wr in rows:
        print(f"  {y:<6d} {n:7d} {net:10.1f} {wr * 100:6.1f}%")
    pos = sum(1 for _, _, net, _ in rows if net > 0)
    print(f"  profitable years: {pos}/{len(rows)}")
    print(f"  VERDICT: {'SPREAD' if pos >= len(rows) * 0.6 else 'CONCENTRATED'}"
          f"  (a single good year out of many = overfit)")


def attack_trend_only(symbol: str, tf: str, d: pd.DataFrame) -> None:
    print("\n" + "-" * 78)
    print("ATTACK E: is V14 secretly just 'gold goes up'?")
    print("-" * 78)
    buys = [t for t in v14.run(d, **BEST)[0] if t.net_pips > 0]
    sells = [t for t in v14.run(d, **BEST)[0] if t.net_pips <= 0]
    if buys and sells:
        b = np.mean([t.net_pips for t in buys])
        s = np.mean([t.net_pips for t in sells])
        print(f"  winners  n={len(buys):3d} avg={b:+8.1f} pips")
        print(f"  losers   n={len(sells):3d} avg={s:+8.1f} pips")
        print(f"  asymmetry: winners/losses = {b / abs(s):.2f}x")
        print("  A pure up-drift strategy would show avg loss ~= -stop and")
        print("  winners clustered at +target. Check the exit reasons:")
        reasons = pd.Series([t.exit_reason for t in v14.run(d, **BEST)[0]])
        for k, v in reasons.value_counts().items():
            print(f"    {k:14s} {v:4d}  ({v / len(reasons) * 100:.0f}%)")


def main() -> None:
    for sym, tf in (("XAUUSD", "D1"),):
        raw = v14.load(sym, tf)
        d = v14.features(raw)
        print("=" * 78)
        print(f"V14 STRESS TEST  {sym} {tf}   params={BEST}")
        print("=" * 78)
        print(f"  {len(d)} bars  {d['ts'].iloc[0].date()} -> {d['ts'].iloc[-1].date()}")
        attack_overlap(sym, tf, d)
        attack_spread(sym, tf, d)
        attack_yearly(sym, tf, d)
        attack_trend_only(sym, tf, d)
    print("\nRead-only. No MT5 writes. No live trading.")


if __name__ == "__main__":
    main()