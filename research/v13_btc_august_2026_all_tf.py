"""V13 BTCUSD DQR — AUGUST 2026 across M5, M15, M30, H1, H4, D1, $1,000 account.

Reuses the exact sizing/exit decisions and the frozen signal config from
research/v13_btc_august_2026_report.py (imported, not re-implemented, so the
two reports cannot drift apart).

  decision 1  lot      = 0.01 fixed (broker minimum)
  decision 2  risk cap = $15.00/trade, wider stop => declined, never resized
  decision 3  stop     = 1.5 x TF ATR(14) from the sweep extreme
  decision 4  target   = fixed R, R:R selected per-TF on Jun-Jul ONLY,
                         holding cap = 12 bars per R so 3R/4R are reachable
  decision 5  signal   = frozen V13 DQR, no August tuning

Each timeframe gets its OWN R:R choice from June-July, then August is scored
once, held out. R:R is not compared across timeframes, because an M5 stop and
a D1 stop are not the same distance; only the money column is comparable.

DATA DEPTH NOTE: BTCUSD history in db/trading.db is not uniform.
  M5  from 2025-10-23, M15 from 2024-09-28, M30 from 2022-09-29,
  H1  from 2016-10-03, H4/D1 from 2013-01-21.
  All six cover June-August 2026, so the comparison is fair for THIS window.
  It says nothing about behaviour over a full cycle on the shorter frames.

Usage:
    ./.venv/Scripts/python.exe research/v13_btc_august_2026_all_tf.py
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from research import dqr_quality_reversal as dqr
from research import v13_btc_august_2026_report as rep
from montecarlo import run_monte_carlo, MCConfig

TFS = ["M5", "M15", "M30", "H1", "H4", "D1"]


def slice_tf(full: pd.DataFrame, a: str, b: str) -> pd.DataFrame:
    return full[(full["ts"] >= pd.Timestamp(a)) &
                (full["ts"] < pd.Timestamp(b))].reset_index(drop=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tfs", default=",".join(TFS))
    ap.add_argument("--balance", type=float, default=rep.BALANCE)
    args = ap.parse_args()

    spec = dqr.SYMBOLS[rep.SYMBOL]
    pip, spr, slip = (spec["pip"], spec["fallback_spread_pips"],
                      spec["fallback_slippage_pips"])
    now = pd.Timestamp.now(tz="UTC").tz_localize(None)
    tfs = [t for t in args.tfs.split(",") if t]

    d1 = dqr.build_exec_features(dqr.load("D1", rep.SYMBOL))
    counter = dqr.D1TouchCounter(d1, touch_atr=0.30)
    levels = dqr.enrich_levels(
        d1, dqr.build_d1_levels(d1, dqr.FAMILY_GRID["swings"]),
        min_touches=1, touch_atr=0.30)

    print("=" * 78)
    print(f"V13 BTCUSD DQR — AUGUST 2026 — ALL TIMEFRAMES — ${args.balance:,.2f}")
    print("=" * 78)
    print(f"report generated (UTC) : {now:%Y-%m-%d %H:%M:%S}")
    print(f"timeframes             : {', '.join(tfs)}")
    print(f"signal selection       : Jun-Jul 2026 (August held out, per-TF R:R)")
    print()
    print("SHARED DECISIONS (owner delegated lot size + TP/SL):")
    print(f"  lot {rep.LOT} fixed (broker min) | risk cap ${rep.MAX_RISK_USD:,.2f}/trade,")
    print(f"  wider stop => declined | stop {rep.ATR_MULT_STOP} x TF ATR(14) |")
    print(f"  target fixed R, R:R per-TF from Jun-Jul, hold cap "
          f"{rep.BARS_PER_RR} bars/R | signal frozen V13 DQR")
    print(f"  costs ${(spr+slip)*pip:,.2f}/lot round trip = "
          f"${(spr+slip)*pip*rep.LOT:,.2f} at {rep.LOT} lot")
    print()

    rows = []
    for tf in tfs:
        full = dqr.build_exec_features(dqr.load(tf, rep.SYMBOL))
        if full.empty:
            print(f"-- {tf}: no data in db, skipped")
            continue
        sel = slice_tf(full, rep.SEL_START, rep.SEL_END)
        aug = slice_tf(full, rep.AUG_START, rep.AUG_END)
        if len(sel) < 20 or len(aug) < 5:
            print(f"-- {tf}: sel {len(sel)} / aug {len(aug)} bars, too thin, skipped")
            continue

        # ---- R:R selection on Jun-Jul only ------------------------------
        sel_stats = {}
        for rr in rep.RR_GRID:
            tr, _ = rep.run_window(sel, levels, d1, counter, rr, spr, slip, pip)
            sel_stats[rr] = rep.money_stats(rep.ledger(tr, sel, pip))
        best_rr = max(sel_stats, key=lambda r: sel_stats[r]["net"])

        # ---- August, held out -------------------------------------------
        tr, _ = rep.run_window(aug, levels, d1, counter, best_rr, spr, slip, pip)
        lg = rep.ledger(tr, aug, pip)
        tot = rep.money_stats(lg)
        pf = "inf" if tot["pf"] == float("inf") else f"{tot['pf']:.2f}"
        mc = (run_monte_carlo(tr, MCConfig(n_iterations=2000, seed=42,
                                          shuffle=True, scatter_pct=0.10,
                                          jitter_pct=0.05, ruin_threshold=-200.0))
              if len(tr) >= 10 else None)

        o, h, l, c = (aug["open"].to_numpy(), aug["high"].to_numpy(),
                      aug["low"].to_numpy(), aug["close"].to_numpy())
        rows.append(dict(tf=tf, bars=len(aug), span=f"{aug['ts'].iloc[0]:%m-%d}"
                        f"..{aug['ts'].iloc[-1]:%m-%d}",
                        move=(c[-1] / o[0] - 1) * 100, rr=best_rr,
                        sigs=len(dqr.detect_signals(aug, levels, d1=d1,
                                                    touch_counter=counter,
                                                    **rep.FROZEN)),
                        n=tot["n"], skip=tot["skipped"], w=tot["wins"],
                        l=tot["losses"], wr=tot["win_rate"], net=tot["net"],
                        final=tot["final"], dd=tot["max_dd"], pf=pf,
                        mc=(f"p5 {mc.net_p5:,.0f} ruin {mc.ruin_prob*100:.0f}%"
                            if mc else "n<10"),
                        robust=(mc.is_robust if mc else None), lg=lg))
        print(f"  {tf:<4} R:R {best_rr:<4} august {tot['n']:>3} trades  "
              f"win {tot['win_rate']*100:>5.1f}%  net ${tot['net']:+7,.2f}  "
              f"PF {pf}")
    print()

    # ---- detail per timeframe -------------------------------------------
    for r in rows:
        print("=" * 78)
        print(f"{r['tf']}   R:R {r['rr']}   August 2026   "
              f"({r['bars']} bars, BTC {r['move']:+.2f}% that month)")
        print("=" * 78)
        for x in r["lg"]:
            tag = "TRADED " if x["traded"] else "SKIPPED "
            print(f"  {tag}{x['side']:<4} {x['entry_ts']:%Y-%m-%d %H:%M} -> "
                  f"{x['exit_ts']:%Y-%m-%d %H:%M} ({x['duration']:>3} bars)  "
                  f"SL {x['stop']:>10,.2f}  TP {x['target']:>10,.2f}  "
                  f"exit {x['exit']:>10,.2f} ({x['reason']})  "
                  f"risk ${x['risk_usd']:>5,.2f}  net ${x['net_usd']:>+7,.2f}")
            if not x["traded"]:
                print(f"         {x['reject']}")
        print(f"  -> {r['n']} trades, win {r['w']}/{r['l']} = {r['wr']*100:.1f}%, "
              f"net ${r['net']:+,.2f}, PF {r['pf']}, MC {r['mc']}")
        print()

    # ---- week by week, every timeframe ----------------------------------
    rep.week_grid(rows, rep.WEEKS, "WEEK BY WEEK — AUGUST 2026 — net $ per timeframe")

    # ---- summary ---------------------------------------------------------
    print("=" * 78)
    print("SUMMARY — AUGUST 2026, $1,000 balance, V13 DQR frozen signal side")
    print("=" * 78)
    print(f"{'TF':<5}{'bars':>7}{'R:R':>6}{'sigs':>6}{'trades':>8}{'skip':>6}"
          f"{'win%':>7}{'PF':>7}{'net $':>10}{'final $':>11}{'maxDD $':>9}  MC")
    for r in rows:
        print(f"{r['tf']:<5}{r['bars']:>7}{r['rr']:>6.1f}{r['sigs']:>6}"
              f"{r['n']:>8}{r['skip']:>6}{r['wr']*100:>6.1f}%{r['pf']:>7}"
              f"{r['net']:>+10,.2f}{r['final']:>11,.2f}{r['dd']:>9,.2f}  {r['mc']}")
    print()
    best = max(rows, key=lambda r: r["net"]) if rows else None
    if best:
        print(f"  best timeframe this month: {best['tf']}  "
              f"net ${best['net']:+,.2f}  win {best['wr']*100:.1f}%  "
              f"PF {best['pf']}")
    mc_robust = [r["tf"] for r in rows if r["robust"]]
    print(f"  Monte Carlo robust      : {', '.join(mc_robust) if mc_robust else 'none'}")
    print()
    print("  READ THIS CAREFULLY:")
    print("  - The signal side is IDENTICAL across all six rows. Only the execution")
    print("    timeframe changed. A timeframe that looks better here has not been")
    print("    promoted; it has been re-measured on one month.")
    print("  - 0-7 trades per row means every win rate above is 1-3 trades wide.")
    print("    Do not rank timeframes off this table.")
    print("  - V13's own def already measured M1/M5/M15 as NOT robust over their")
    print("    full stored history, and D1 as the only robust execution TF. This")
    print("    August table is consistent with that, not a new discovery.")
    print("  - V13 remains RESEARCH. Not in execution/approved.json. No orders.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
