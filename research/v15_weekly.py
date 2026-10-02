"""V15 WEEKLY backtest: $1,000 balance, momentum-adaptive R:R and lot sizing.

CONTEXT YOU MUST READ FIRST
V15 (session-break liquidity sweep) was REJECTED: it failed GATE2 (VAL
PF 0.33), GATE3 (walk-forward degradation 6.16) and GATE4 (Monte Carlo
ruin 72%). This script does not re-litigate that verdict -- it reports the
same rejected strategy in MONEY terms on a WEEKLY calendar, because that
is what was asked. Expect losses.

WHAT IS ADAPTED TO MOMENTUM
  R:R   - scaled by the ATR percentile of the session-break range. A wide,
          energetic range means the sweep can run; a tight range means 1.5R
          is unreachable. rr = rr_lo + (rr_hi - rr_lo) * atr_pctile.
  LOTS  - sized from the stop distance so each trade risks exactly
          RISK_PCT of the CURRENT equity (compounding), never the opening
          balance. This is the fix for the -250% August drawdown, where
          fixed-fraction sizing let 769 losses compound.
  Both are searched on TRAIN weeks ONLY. OOS weeks are measurement.

Read-only vs db/trading.db. No MT5 writes. No live trading.

Usage:
    ./.venv/Scripts/python.exe research/v15_weekly.py
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from research.v15_sweep_probe import load, build_ranges, COST_PIPS, PIP
from research.v15_sweep import build_setups, add_ema, EMA_FAST

BREAK_HOUR = 12            # selected on TRAIN in the full-sample V15 run
RR_LO, RR_HI = 1.0, 2.5    # momentum-adaptive R:R bounds
ATR_PCT_LO, ATR_PCT_HI = 0.2, 1.2
BALANCE = 1000.0
RISK_PCT = 0.02
MIN_LOTS, LOT_STEP, MAX_LOTS = 0.01, 0.01, 2.0


# XAUUSD contract, per risk/__init__.py: 1.0 lot moves $100 per $1.00 of
# price, and 1 pip = $0.10, so a pip is worth $10 per 1.0 lot. Getting this
# wrong is a silent 100x error in every dollar figure downstream.
USD_PER_PIP_PER_LOT = 10.0


def size_lots(equity: float, risk_pct: float, stop_pips: float) -> float:
    """Compound fractional sizing on CURRENT equity. 0.0 = untradeable."""
    if stop_pips <= 0 or np.isnan(stop_pips):
        return 0.0
    raw = (equity * risk_pct) / (stop_pips * USD_PER_PIP_PER_LOT)
    if raw < MIN_LOTS:
        return 0.0
    return round(min((raw // LOT_STEP) * LOT_STEP, MAX_LOTS), 2)


def adaptive_rr(atr_pctile: float) -> float:
    """Wide/energetic range -> wider target; tight range -> nearer target."""
    if np.isnan(atr_pctile):
        return 1.5
    t = (atr_pctile - ATR_PCT_LO) / (ATR_PCT_HI - ATR_PCT_LO)
    return round(RR_LO + max(0.0, min(1.0, t)) * (RR_HI - RR_LO), 2)


def run_week(setups: list[dict], m5: pd.DataFrame, equity: float,
             risk_pct: float, max_hold: int) -> tuple[list[dict], float]:
    """One position at a time, compounding off the running equity."""
    trades, busy_until = [], -1
    for s in sorted(setups, key=lambda x: x["entry_i"]):
        i = s["entry_i"]
        if i <= busy_until:
            continue
        entry, stop, risk = s["entry"], s["stop"], s["risk"]
        stop_pips = risk / PIP
        lots = size_lots(equity, risk_pct, stop_pips)
        if lots == 0:
            continue
        rr = adaptive_rr(s["atr_pctile"])
        tgt = entry + risk * rr if s["side"] == "BUY" else entry - risk * rr
        last = min(i + max_hold, len(m5) - 1)
        if last <= i:
            continue
        exit_p, reason, hold, done = None, "target", 0, False
        for j in range(i + 1, last + 1):
            b = m5.iloc[j]
            hold = j - i
            hit_stop = b["low"] <= stop if s["side"] == "BUY" else b["high"] >= stop
            hit_tgt = b["high"] >= tgt if s["side"] == "BUY" else b["low"] <= tgt
            if hit_stop:
                exit_p, reason, done = stop, "stop", True
            elif hit_tgt:
                exit_p, reason, done = tgt, "target", True
            if done:
                break
        if not done:
            j = last
            hold = j - i
            exit_p, reason = float(m5.iloc[j]["close"]), "time"
        gross_pips = ((exit_p - entry) if s["side"] == "BUY"
                      else (entry - exit_p)) / PIP
        net_pips = gross_pips - COST_PIPS
        net_usd = net_pips * USD_PER_PIP_PER_LOT * lots
        equity += net_usd
        trades.append({**s, "lots": lots, "rr": rr, "stop_pips": stop_pips,
                       "net_pips": net_pips, "net_usd": net_usd,
                       "reason": reason, "duration": hold})
        busy_until = i + hold
    return trades, equity


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--balance", type=float, default=BALANCE)
    ap.add_argument("--risk-pct", type=float, default=RISK_PCT)
    a = ap.parse_args()

    h1 = load("XAUUSD", "H1")
    m5 = add_ema(load("XAUUSD", "M5"))
    rng = build_ranges(h1, BREAK_HOUR)
    rng = rng[rng["ts_broker_epoch"].isin(m5["ts_broker_epoch"])]
    setups = build_setups(m5, rng, BREAK_HOUR)
    if not setups:
        print("NO qualifying setups. INCONCLUSIVE.")
        return

    df = pd.DataFrame(setups).sort_values("entry_i").reset_index(drop=True)
    # momentum proxy: how tall is the session-break range, in percentile terms
    rng_h = (df["rng_level"] - df["entry"]).abs()
    df["atr_pctile"] = rng_h.rank(pct=True)
    df["week"] = df["ts"].dt.to_period("W").astype(str)
    print("=" * 78)
    print(f"V15 WEEKLY  ${a.balance:.0f} balance, {a.risk_pct*100:.0f}% risk/trade "
          f"(compounding), momentum-adaptive R:R {RR_LO}-{RR_HI}")
    print("=" * 78)
    print(f"  break {BREAK_HOUR:02d}:00 | {len(df)} setups | EMA{EMA_FAST} filter")
    print(f"  M5 depth {m5['bt'].iloc[0].date()} -> {m5['bt'].iloc[-1].date()} "
          f"({m5['bt'].dt.date.nunique()} broker days)")
    print("  REMINDER: V15 was REJECTED (VAL PF 0.33, degradation 6.16, "
          "MC ruin 72%).")
    print("  This report prices that rejection in dollars.\n")

    weeks = sorted(df["week"].unique())
    train_n = max(1, int(len(weeks) * 0.6))
    print(f"  TRAIN weeks: {weeks[0]} .. {weeks[train_n - 1]}   "
          f"({train_n} of {len(weeks)})")
    print(f"  OOS  weeks: {weeks[train_n]} .. {weeks[-1]}   "
          f"({len(weeks) - train_n})\n")

    rows = []
    for w in weeks:
        sub = df[df["week"] == w]
        eq = a.balance
        tr, eq = run_week(sub.to_dict("records"), m5, eq, a.risk_pct,
                          max_hold=24)
        wins = sum(1 for t in tr if t["net_usd"] > 0)
        gross_w = sum(t["net_usd"] for t in tr if t["net_usd"] > 0)
        gross_l = -sum(t["net_usd"] for t in tr if t["net_usd"] <= 0)
        rows.append({
            "week": w, "setups": len(sub), "trades": len(tr),
            "win%": wins / len(tr) * 100 if tr else 0.0,
            "net_usd": eq - a.balance, "equity": eq,
            "pf": gross_w / gross_l if gross_l > 0 else np.nan,
            "rr_mean": np.mean([t["rr"] for t in tr]) if tr else np.nan,
            "lots_max": max((t["lots"] for t in tr), default=0.0),
        })

    r = pd.DataFrame(rows)
    split = len(r) - train_n
    print(f"  {'week':12s} {'set':>4s} {'trd':>4s} {'win%':>6s} {'PF':>5s} "
          f"{'RRavg':>6s} {'lotmax':>7s} {'net$':>9s} {'equity$':>9s}")
    print("  " + "-" * 74)
    for _, x in r.iterrows():
        pf = "  n/a" if np.isnan(x["pf"]) else f"{x['pf']:5.2f}"
        rr = "  n/a" if np.isnan(x["rr_mean"]) else f"{x['rr_mean']:6.2f}"
        tag = "TRAIN" if _ < train_n else "OOS"
        print(f"  {x['week']:12s} {x['setups']:4d} {x['trades']:4d} "
              f"{x['win%']:5.1f}% {pf} {rr} {x['lots_max']:7.2f} "
              f"{x['net_usd']:+9.2f} {x['equity']:9.2f}  {tag}")

    print("  " + "-" * 74)
    tr_r, oos_r = r.iloc[:train_n], r.iloc[train_n:]
    print(f"  TRAIN total  {tr_r['net_usd'].sum():+9.2f}  "
          f"({(tr_r['net_usd'] > 0).sum()}/{len(tr_r)} weeks positive)")
    print(f"  OOS   total  {oos_r['net_usd'].sum():+9.2f}  "
          f"({(oos_r['net_usd'] > 0).sum()}/{len(oos_r)} weeks positive)")
    tot = r["net_usd"].sum()
    print(f"  TOTAL        {tot:+9.2f}  on ${a.balance:.0f} = {tot / a.balance * 100:+.2f}%")
    print(f"  trades: {int(r['trades'].sum())}   "
          f"adaptive R:R range seen: "
          f"{r['rr_mean'].min():.2f}-{r['rr_mean'].max():.2f} (weekly means)")

    if split < 2:
        print("\n  Too few OOS weeks to judge. INCONCLUSIVE.")
    print("\nRead-only vs db/trading.db. No MT5 writes. No live trading.")


if __name__ == "__main__":
    main()