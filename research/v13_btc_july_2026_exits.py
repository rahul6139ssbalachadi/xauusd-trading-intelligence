"""V13 BTCUSD DQR — JULY 2026 across M5/M15/M30/H1/H4/D1, with SL and TP hit counts.

WHY JULY IS THE INTERESTING MONTH
  August ran +25.27%, so almost every losing trade there was a fade of a level
  that had just broken -- the trend beat the mean-reversion thesis. July was
  the month BEFORE that run. Splitting the two tests the thesis far more
  cleanly: a sweep-reversal that only works in a rising market is not an edge,
  and if July shows the mirror image (stopped out on the way down) that is the
  same conclusion from the other side.

EXIT-TAXONOMY COUNTS (what the owner asked for)
  target  the trade reached TP. The good case.
  stop    the trade hit SL. The thesis was wrong. Counted separately from
          time-exits because a stop is information and a time-exit is not.
  end     hit the holding cap, exited on the bar close. AMBIGUOUS by
          construction -- it could be a winner that stalled or a loser that
          never resolved -- so it is never counted as a win.

  SL hit rate and TP hit rate are reported as a share of CLOSED trades, and
  the residual (time-exits) is shown explicitly so the two rates can never be
  mistaken for summing to 100%.

Sizing/exit/signal decisions are imported from v13_btc_august_2026_report.py
so this report cannot drift from the August one. Same $1,000 balance, same
frozen signal side, same R:R-per-TF selected on June-July (July is therefore
IN-SAMPLE for the R:R choice -- flagged loudly below).

Usage:
    ./.venv/Scripts/python.exe research/v13_btc_july_2026_exits.py
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from research import dqr_quality_reversal as dqr
from research import v13_btc_august_2026_report as rep
from research.v13_btc_august_2026_all_tf import slice_tf

TFS = ["M5", "M15", "M30", "H1", "H4", "D1"]
JUL_START, JUL_END = "2026-07-01", "2026-08-01"


def exit_census(rows):
    """Count SL / TP / time-exit outcomes over TRADED rows only."""
    c = Counter(r["reason"] for r in rows if r["traded"])
    tp, sl, end = c.get("target", 0), c.get("stop", 0), c.get("end", 0)
    closed = tp + sl + end
    return dict(tp=tp, sl=sl, end=end, closed=closed,
                tp_rate=(tp / closed) if closed else 0.0,
                sl_rate=(sl / closed) if closed else 0.0,
                end_rate=(end / closed) if closed else 0.0,
                pnl_at_sl=sum(r["net_usd"] for r in rows
                              if r["traded"] and r["reason"] == "stop"),
                pnl_at_tp=sum(r["net_usd"] for r in rows
                              if r["traded"] and r["reason"] == "target"),
                pnl_at_end=sum(r["net_usd"] for r in rows
                               if r["traded"] and r["reason"] == "end"))


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
    print(f"V13 BTCUSD DQR — JULY 2026 — SL / TP HIT COUNTS — ${args.balance:,.2f}")
    print("=" * 78)
    print(f"report generated (UTC) : {now:%Y-%m-%d %H:%M:%S}")
    print(f"month                  : JULY 2026  ({JUL_START} .. {JUL_END})")
    print(f"timeframes             : {', '.join(tfs)}")
    print()
    print("*** JULY IS IN-SAMPLE FOR THE R:R CHOICE ***")
    print("    R:R is selected on June-July and August is held out, so a July")
    print("    number is NOT an out-of-sample result. It is a diagnostic for the")
    print("    exit behaviour only. Do not read it as validation.")
    print()
    print(f"  lot {rep.LOT} fixed | risk cap ${rep.MAX_RISK_USD:,.2f} | "
          f"SL {rep.ATR_MULT_STOP}x ATR(14) | hold cap {rep.BARS_PER_RR} bars/R | "
          f"frozen signal")
    print()

    rows = []
    for tf in tfs:
        full = dqr.build_exec_features(dqr.load(tf, rep.SYMBOL))
        if full.empty:
            print(f"-- {tf}: no data, skipped")
            continue
        sel = slice_tf(full, rep.SEL_START, rep.SEL_END)
        jul = slice_tf(full, JUL_START, JUL_END)
        if len(sel) < 20 or len(jul) < 5:
            print(f"-- {tf}: sel {len(sel)} / jul {len(jul)} bars, too thin, skipped")
            continue

        sel_stats = {rr: rep.money_stats(
            rep.ledger(rep.run_window(sel, levels, d1, counter, rr, spr, slip,
                                      pip)[0], sel, pip))
            for rr in rep.RR_GRID}
        rr = max(sel_stats, key=lambda r: sel_stats[r]["net"])

        tr, _ = rep.run_window(jul, levels, d1, counter, rr, spr, slip, pip)
        lg = rep.ledger(tr, jul, pip)
        tot = rep.money_stats(lg)
        cx = exit_census(lg)
        o, c = jul["open"].to_numpy(), jul["close"].to_numpy()
        rows.append(dict(tf=tf, rr=rr, bars=len(jul),
                         move=(c[-1] / o[0] - 1) * 100, n=tot["n"],
                         skip=tot["skipped"], net=tot["net"], final=tot["final"],
                         dd=tot["max_dd"], wr=tot["win_rate"],
                         pf=("inf" if tot["pf"] == float("inf")
                             else f"{tot['pf']:.2f}"), cx=cx, lg=lg))

    # ---- exit census summary --------------------------------------------
    print("=" * 78)
    print("SL / TP HIT COUNTS — JULY 2026 (traded trades only)")
    print("=" * 78)
    print(f"{'TF':<5}{'R:R':>6}{'trades':>8}{'TP':>5}{'SL':>5}{'TIME':>6}"
          f"{'TP%':>8}{'SL%':>8}{'TIME%':>8}{'net $':>10}{'PF':>7}")
    for r in rows:
        x = r["cx"]
        print(f"{r['tf']:<5}{r['rr']:>6.1f}{r['n']:>8}{x['tp']:>5}{x['sl']:>5}"
              f"{x['end']:>6}{x['tp_rate']*100:>7.0f}%{x['sl_rate']*100:>7.0f}%"
              f"{x['end_rate']*100:>7.0f}%{r['net']:>+10,.2f}{r['pf']:>7}")
    print()
    print("  TP%  = share of closed trades that reached target")
    print("  SL%  = share that hit stop        TIME% = hit the holding cap")
    print("  the three always sum to 100% of CLOSED trades; skipped-by-risk-cap")
    print("  trades are excluded from all three.")
    print()

    # ---- per-timeframe detail -------------------------------------------
    for r in rows:
        x = r["cx"]
        print("=" * 78)
        print(f"{r['tf']}   R:R {r['rr']}   JULY 2026   ({r['bars']} bars, "
              f"BTC {r['move']:+.2f}% that month)")
        print("=" * 78)
        for t in r["lg"]:
            tag = "TRADED " if t["traded"] else "SKIPPED "
            print(f"  {tag}{t['side']:<4} {t['entry_ts']:%Y-%m-%d %H:%M} -> "
                  f"{t['exit_ts']:%Y-%m-%d %H:%M} ({t['duration']:>3} bars)  "
                  f"SL {t['stop']:>10,.2f}  TP {t['target']:>10,.2f}  "
                  f"exit {t['exit']:>10,.2f} ({t['reason'].upper():<6})  "
                  f"risk ${t['risk_usd']:>5,.2f}  net ${t['net_usd']:>+7,.2f}")
            if not t["traded"]:
                print(f"         {t['reject']}")
        print(f"  exits: {x['tp']} TP (${x['pnl_at_tp']:+,.2f})  "
              f"{x['sl']} SL (${x['pnl_at_sl']:+,.2f})  "
              f"{x['end']} TIME (${x['pnl_at_end']:+,.2f})")
        print(f"  total: {r['n']} trades, win {r['wr']*100:.1f}%, "
              f"net ${r['net']:+,.2f}, PF {r['pf']}, maxDD ${r['dd']:,.2f}")
        print()

    # ---- week by week ----------------------------------------------------
    weeks = [("Jul 1-5", "2026-07-01", "2026-07-06"),
             ("Jul 6-12", "2026-07-06", "2026-07-13"),
             ("Jul 13-19", "2026-07-13", "2026-07-20"),
             ("Jul 20-26", "2026-07-20", "2026-07-27"),
             ("Jul 27-31", "2026-07-27", "2026-08-01")]
    rep.week_grid(rows, weeks, "WEEK BY WEEK — JULY 2026 — net $ per timeframe")

    # ---- reading ---------------------------------------------------------
    print("=" * 78)
    print("BOTTOM LINE — JULY vs AUGUST")
    print("=" * 78)
    tot_tp = sum(r["cx"]["tp"] for r in rows)
    tot_sl = sum(r["cx"]["sl"] for r in rows)
    tot_end = sum(r["cx"]["end"] for r in rows)
    print(f"  across all timeframes: {tot_tp} TP hits, {tot_sl} SL hits, "
          f"{tot_end} time-exits")
    losers = [r for r in rows if r["net"] < 0]
    if losers:
        detail = ", ".join(f"{r['tf']} ${r['net']:+,.2f}" for r in losers)
        print(f"  losing timeframes   : {detail}")
        sl_heavy = [r for r in rows if r["cx"]["sl_rate"] > r["cx"]["tp_rate"]]
        if sl_heavy:
            print(f"  SL outnumbers TP on : "
                  f"{', '.join(r['tf'] for r in sl_heavy)}")
    print()
    print("  July was the month BEFORE BTC's +25.27% August run. If the thesis")
    print("  were sound it should not depend on which side of that move it sat.")
    print("  Compare the TP%/SL% columns here against August: a strategy whose")
    print("  exits flip wholesale between two adjacent months is regime-dependent,")
    print("  and regime-dependent is not the same thing as having an edge.")
    print()
    print("  V13 remains RESEARCH. Not in execution/approved.json. No orders.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
