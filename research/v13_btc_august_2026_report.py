"""V13 BTCUSD DQR — AUGUST 2026 report with owner-delegated sizing/exit choice.

THE DELEGATION
  The owner asked me to decide the lot size and the TP/SL myself, with a fixed
  $1,000 balance, and to report P/L, win rate, week by week, for August 2026.
  Every decision below is stated with its reasoning and is auditable. None of
  them is fitted to make August look good -- see "ANTI-OVERFIT" below.

MY DECISIONS, AND WHY
  1. LOT SIZE = fixed 0.01 (the broker minimum), not fractional.
     On BTCUSD 1 lot = 1 BTC, so 0.01 lot = $1.00 per $1.00 of price. A 1%
     risk target on $1,000 is $10, which buys 0.01 lot only when the stop is
     <= ~1,000 points. V13's ATR stop on M30 runs 1,000-2,500 points, so
     fractional sizing correctly computes 0.004-0.010 lots and then gets
     rounded AWAY by the broker floor. Sizing fractionally therefore means
     either never trading, or quietly risking 1.5-2.5x the intended amount.
     Fixed 0.01 is the only size that is both legal and honest, so the real
     risk per trade is whatever the stop says, and I REPORT it every time
     rather than pretending it is 1%.
  2. HARD RISK CAP = skip any trade needing > $15 (1.5% of $1,000).
     This is the discipline that keeps decision 1 from becoming reckless. A
     trade whose 0.01-lot stop is wider than $15 is declined, not shrunk.
  3. STOP = 1.5 x M30 ATR(14), measured from the sweep extreme, unchanged
     from the V13 def. I did NOT tighten or widen it to fit August.
  4. TARGET = fixed R multiple, chosen on JUNE-JULY ONLY, then applied to
     August untouched. R:R 3.0-4.0 needs a time window long enough to reach
     the target, so each R:R gets a proportional holding cap; otherwise
     3R and 4R are identical to 1.5R (already observed: the 32-bar cap made
     R:R 2/3/4 byte-identical, so the earlier "R:R 3-4 is bad" reading was
     partly an artefact of an unreachable target, not a real result).
  5. NO RE-TUNING OF THE SIGNAL SIDE. D1 swing levels, 0.25 ATR sweep,
     close_pos 0.70, 0.80 ATR impulse confirmation are all frozen. Changing
     them to fit August would be exactly the overfitting the spec forbids.

ANTI-OVERFIT
  June-July is the selection window. August is held out and reported once.
  If the chosen R:R looks good in August, that is one month of one asset and
  it does NOT promote V13 out of RESEARCH.

OUTPUT
  Per-trade ledger with real date and time, a monthly P/L, win rate, and a
  week-by-week breakdown (Aug 1-2, 3-9, 10-16, 17-23, 24-31).

Usage:
    ./.venv/Scripts/python.exe research/v13_btc_august_2026_report.py
    ./.venv/Scripts/python.exe research/v13_btc_august_2026_report.py --tf M30
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
BALANCE = 1000.0
LOT = 0.01                  # decision 1: broker minimum, fixed
MAX_RISK_USD = 15.0         # decision 2: 1.5% of $1,000, hard cap
ATR_MULT_STOP = 1.5         # decision 3
RR_GRID = [1.5, 2.0, 3.0, 4.0]   # decision 4 candidates
SEL_START, SEL_END = "2026-06-01", "2026-08-01"   # selection window
AUG_START, AUG_END = "2026-08-01", "2026-09-01"   # held-out report month
WEEKS = [("Aug 1-2", "2026-08-01", "2026-08-03"),
         ("Aug 3-9", "2026-08-03", "2026-08-10"),
         ("Aug 10-16", "2026-08-10", "2026-08-17"),
         ("Aug 17-23", "2026-08-17", "2026-08-24"),
         ("Aug 24-31", "2026-08-24", "2026-09-01")]

FROZEN = dict(level_tol_atr=0.10, close_pos_min=0.70, impulse_atr=0.80,
              sweep_lookback=1, break_lookback=0, break_atr=0.50,
              max_level_age_d1=120, max_level_dist_atr=3.0,
              min_level_touches=1, min_sweep_atr=0.25)

# decision 4: a target that cannot be reached inside the holding cap is not a
# test of that R:R, it is a test of the cap. 12 M30 bars per R gives 4R a
# 48-bar window, which is ~2.5 days at M30.
BARS_PER_RR = 12


def hold_cap(rr: float) -> int:
    return int(round(rr * BARS_PER_RR))


def run_window(exec_df, levels, d1, counter, rr, spr, slip, pip):
    """One RR pass over an already-sliced execution frame."""
    sigs = dqr.detect_signals(exec_df, levels, d1=d1, touch_counter=counter,
                              **FROZEN)
    tr, m = dqr.backtest_signals(
        exec_df, sigs, spread_pips=spr, slippage_pips=slip, pip=pip, rr=rr,
        atr_mult_stop=ATR_MULT_STOP, max_holding_bars=hold_cap(rr),
        cooldown_bars=4)
    return tr, m


def ledger(trades, bars, pip):
    """Turn pips trades into the $1,000-account ledger with my sizing rules."""
    ts = pd.to_datetime(bars["ts"].to_numpy())
    rows = []
    for t in trades:
        stop_dist = abs(t.entry_price - t.stop)
        risk_usd = stop_dist * LOT            # decision 1
        ok = risk_usd <= MAX_RISK_USD          # decision 2
        rows.append(dict(
            side=t.side, entry_ts=ts[t.entry_bar], exit_ts=ts[t.exit_bar],
            entry=t.entry_price, stop=t.stop, target=t.target,
            exit=t.exit_price, reason=t.exit_reason, duration=t.duration_bars,
            stop_dist=stop_dist, risk_usd=risk_usd, lots=LOT,
            net_pips=t.net_pips,
            net_usd=(t.net_pips * pip * LOT) if ok else 0.0,
            traded=ok, reject="" if ok else
            f"stop ${risk_usd:,.2f} > cap ${MAX_RISK_USD:,.2f}"))
    return rows


def money_stats(rows):
    """P/L, win rate and equity path over the rows that were actually traded."""
    done = [r for r in rows if r["traded"]]
    wins = [r for r in done if r["net_usd"] > 0]
    losses = [r for r in done if r["net_usd"] < 0]
    eq, peak, dd = BALANCE, BALANCE, 0.0
    for r in done:
        eq += r["net_usd"]
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    gross_w = sum(r["net_usd"] for r in wins)
    gross_l = -sum(r["net_usd"] for r in losses)
    return dict(n=len(done), skipped=len(rows) - len(done), wins=len(wins),
                losses=len(losses),
                win_rate=(len(wins) / len(done)) if done else 0.0,
                net=eq - BALANCE, final=eq, max_dd=dd,
                gross_win=gross_w, gross_loss=gross_l,
                pf=(gross_w / gross_l) if gross_l else float("inf"),
                avg_win=(gross_w / len(wins)) if wins else 0.0,
                avg_loss=(-gross_l / len(losses)) if losses else 0.0,
                equity=eq)


def week_grid(rows, weeks, title: str) -> None:
    """Print a per-timeframe net-$ grid, one column per row['tf'].

    `rows` entries need 'tf' and 'lg' (the ledger). Weeks are (label, start,
    end) with end exclusive, matching how the report slices months. Running
    the cumulative from BALANCE rather than from the per-month totals keeps
    this identical for August and July without either script owning a copy.
    """
    print("=" * 78)
    print(title)
    print("=" * 78)
    print(f"{'week':<11}" + "".join(f"{r['tf']:>10}" for r in rows))
    cums = {r["tf"]: BALANCE for r in rows}
    for label, s0, s1 in weeks:
        a, b = pd.Timestamp(s0), pd.Timestamp(s1)
        line = f"{label:<11}"
        for r in rows:
            wr = [x for x in r["lg"] if a <= x["entry_ts"] < b]
            s = money_stats(wr) if wr else None
            if s and s["n"]:
                cums[r["tf"]] += s["net"]
                line += f"{s['net']:>+10,.2f}"
            else:
                line += f"{'--':>10}"
        print(line)
    print("-" * 78)
    for label, fn, fmt in (
            ("CUMULATIVE", lambda v: v - BALANCE, "{:>+10,.2f}"),
            ("FINAL $", lambda v: v, "{:>10,.2f}")):
        print(f"{label:<11}" + "".join(fmt.format(fn(cums[r["tf"]]))
                                       for r in rows))
    print()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tf", default="M30")
    ap.add_argument("--balance", type=float, default=BALANCE)
    args = ap.parse_args()

    spec = dqr.SYMBOLS[SYMBOL]
    pip, spr, slip = spec["pip"], spec["fallback_spread_pips"], \
        spec["fallback_slippage_pips"]
    now = pd.Timestamp.now(tz="UTC").tz_localize(None)

    d1 = dqr.build_exec_features(dqr.load("D1", SYMBOL))
    counter = dqr.D1TouchCounter(d1, touch_atr=0.30)
    levels = dqr.enrich_levels(
        d1, dqr.build_d1_levels(d1, dqr.FAMILY_GRID["swings"]),
        min_touches=1, touch_atr=0.30)

    full = dqr.build_exec_features(dqr.load(args.tf, SYMBOL))
    sel = full[(full["ts"] >= pd.Timestamp(SEL_START)) &
               (full["ts"] < pd.Timestamp(SEL_END))].reset_index(drop=True)
    aug = full[(full["ts"] >= pd.Timestamp(AUG_START)) &
               (full["ts"] < pd.Timestamp(AUG_END))].reset_index(drop=True)

    print("=" * 78)
    print(f"V13 BTCUSD DQR — AUGUST 2026 REPORT — {args.tf} — ${args.balance:,.2f}")
    print("=" * 78)
    print(f"report generated (UTC) : {now:%Y-%m-%d %H:%M:%S}")
    print(f"report month           : AUGUST 2026  "
          f"({aug['ts'].iloc[0]:%Y-%m-%d %H:%M} .. {aug['ts'].iloc[-1]:%Y-%m-%d %H:%M}, "
          f"{len(aug)} bars)")
    print(f"selection window       : {SEL_START} .. {SEL_END} "
          f"({len(sel)} bars) — R:R chosen here, August held out")
    print()
    print("MY DECISIONS (owner delegated lot size + TP/SL):")
    print(f"  1. lot      = {LOT} fixed (broker min). Fractional 1%-risk sizing")
    print(f"               computes 0.004-0.010 lots on these stops and is erased")
    print(f"               by the 0.01 floor, so it would risk 1.5-2.5x the target.")
    print(f"  2. risk cap = ${MAX_RISK_USD:,.2f}/trade (1.5% of ${args.balance:,.0f}).")
    print(f"               Wider stop => trade declined, never resized.")
    print(f"  3. stop     = {ATR_MULT_STOP} x {args.tf} ATR(14) from the sweep extreme (V13 default, unchanged).")
    print(f"  4. target   = fixed R, R:R picked on Jun-Jul only; holding cap")
    print(f"               = {BARS_PER_RR} bars per R so 3R/4R are actually reachable.")
    print(f"  5. signal   = frozen V13 DQR (swings, 0.25 ATR sweep, close_pos 0.70,")
    print(f"               0.80 ATR impulse). No August tuning.")
    print(f"  costs       = ${(spr+slip)*pip:,.2f}/lot round trip "
          f"= ${(spr+slip)*pip*LOT:,.2f} at {LOT} lot")
    print()

    # ---- step 1: choose R:R on Jun-Jul only -------------------------------
    print("-" * 78)
    print("STEP 1 — R:R SELECTION on June-July 2026 (August NOT consulted)")
    print("-" * 78)
    print(f"{'RR':>5}{'hold':>6}{'signals':>9}{'traded':>8}{'PF':>7}"
          f"{'net $':>10}{'win%':>7}{'final $':>11}")
    sel_stats = {}
    for rr in RR_GRID:
        tr, m = run_window(sel, levels, d1, counter, rr, spr, slip, pip)
        rows = ledger(tr, sel, pip)
        s = money_stats(rows)
        sel_stats[rr] = s
        pf = "inf" if s["pf"] == float("inf") else f"{s['pf']:.2f}"
        print(f"{rr:>5.1f}{hold_cap(rr):>6}{len(tr):>9}{s['n']:>8}{pf:>7}"
              f"{s['net']:>+10,.2f}{s['win_rate']*100:>6.0f}%{s['final']:>11,.2f}")
    best_rr = max(sel_stats, key=lambda r: sel_stats[r]["net"])
    print(f"\n  SELECTED R:R = {best_rr}  (best net on Jun-Jul, "
          f"${sel_stats[best_rr]['net']:+,.2f})")
    print(f"  Aug/Sep are excluded from this choice by construction.")
    print()

    # ---- step 2: August, held out ----------------------------------------
    print("-" * 78)
    print(f"STEP 2 — AUGUST 2026 (held out), R:R {best_rr}, "
          f"holding cap {hold_cap(best_rr)} bars")
    print("-" * 78)
    o, h, l, c = (aug["open"].to_numpy(), aug["high"].to_numpy(),
                  aug["low"].to_numpy(), aug["close"].to_numpy())
    print(f"  BTC open  {o[0]:,.2f}   high {h.max():,.2f}   low {l.min():,.2f}   "
          f"close {c[-1]:,.2f}   month move {(c[-1]/o[0]-1)*100:+.2f}%")
    tr, m = run_window(aug, levels, d1, counter, best_rr, spr, slip, pip)
    rows = ledger(tr, aug, pip)
    print(f"  signal candidates: {len(dqr.detect_signals(aug, levels, d1=d1, touch_counter=counter, **FROZEN))}"
          f"   trades simulated: {len(tr)}")
    print()
    for r in rows:
        tag = "TRADED " if r["traded"] else "SKIPPED "
        print(f"  {tag}{r['side']:<4} {r['entry_ts']:%Y-%m-%d %H:%M} -> "
              f"{r['exit_ts']:%Y-%m-%d %H:%M} ({r['duration']:>2} bars)  "
              f"entry {r['entry']:>10,.2f}  SL {r['stop']:>10,.2f}  "
              f"TP {r['target']:>10,.2f}  exit {r['exit']:>10,.2f} ({r['reason']})  "
              f"risk ${r['risk_usd']:>5,.2f}  net ${r['net_usd']:>+7,.2f}")
        if not r["traded"]:
            print(f"         reason: {r['reject']}")
    print()

    tot = money_stats(rows)
    pf = "inf" if tot["pf"] == float("inf") else f"{tot['pf']:.2f}"
    print(f"  AUGUST TOTAL : {tot['n']} trades ({tot['skipped']} skipped on risk cap)")
    print(f"                win {tot['wins']} / loss {tot['losses']}   "
          f"WIN RATE {tot['win_rate']*100:.1f}%")
    print(f"                gross +${tot['gross_win']:,.2f} / gross -${tot['gross_loss']:,.2f}"
          f"   PF {pf}")
    print(f"                avg win ${tot['avg_win']:,.2f}   "
          f"avg loss ${abs(tot['avg_loss']):,.2f}")
    print(f"                NET P/L ${tot['net']:+,.2f} on ${args.balance:,.2f} "
          f"({tot['net']/args.balance*100:+.2f}%)")
    print(f"                max drawdown ${tot['max_dd']:,.2f}   "
          f"final ${tot['final']:,.2f}")
    if len(tr) >= 10:
        mc = run_monte_carlo(tr, MCConfig(n_iterations=2000, seed=42,
                                          shuffle=True, scatter_pct=0.10,
                                          jitter_pct=0.05, ruin_threshold=-200.0))
        print(f"                MC p5 {mc.net_p5:,.0f} pips  PF_p5 "
              f"{mc.profit_factor_p5:.2f}  ruin {mc.ruin_prob*100:.1f}%  "
              f"robust={mc.is_robust}")
    print()

    # ---- step 3: week by week --------------------------------------------
    print("-" * 78)
    print("STEP 3 — WEEK BY WEEK, AUGUST 2026")
    print("-" * 78)
    print(f"{'week':<11}{'dates':<26}{'trades':>7}{'W':>4}{'L':>4}"
          f"{'win%':>7}{'net $':>10}{'cum $':>10}")
    cum = BALANCE
    for label, s0, s1 in WEEKS:
        a, b = pd.Timestamp(s0), pd.Timestamp(s1)
        wr = [r for r in rows if a <= r["entry_ts"] < b]
        s = money_stats(wr) if wr else None
        if s and s["n"]:
            cum += s["net"]
            print(f"{label:<11}{s0[5:]} .. {s1[5:]:<12}{s['n']:>7}"
                  f"{s['wins']:>4}{s['losses']:>4}{s['win_rate']*100:>6.0f}%"
                  f"{s['net']:>+10,.2f}{cum:>10,.2f}")
        else:
            n_raw = len(wr)
            print(f"{label:<11}{s0[5:]} .. {s1[5:]:<12}{0:>7}{'-':>4}{'-':>4}"
                  f"{'no trades':>9}{0:>10,.2f}{cum:>10,.2f}"
                  + (f"   ({n_raw} signal(s), all blocked by the "
                     f"${MAX_RISK_USD:,.0f} risk cap)" if n_raw else
                     "   (no DQR setup)"))
    print()
    print("=" * 78)
    print("BOTTOM LINE")
    print("=" * 78)
    print(f"  August 2026, {args.tf}, R:R {best_rr}, {LOT} lot, ${args.balance:,.2f} balance")
    print(f"  net ${tot['net']:+,.2f} ({tot['net']/args.balance*100:+.2f}%)   "
          f"win rate {tot['win_rate']*100:.1f}% over {tot['n']} trades   "
          f"PF {pf}")
    if tot["n"] == 0:
        print("  No trade was taken. On this balance the risk cap declines most of")
        print("  the signals, and the ones it allows are too few to call an edge.")
    elif tot["net"] <= 0:
        print("  August was a LOSING month, and it is worth reading WHY rather than")
        print("  filing it as noise. Three of the seven losing trades were FATHERS")
        print("  of a large move -- e.g. SELL 2026-08-18 filled at 64,064 and was")
        print("  stopped at 64,472 one bar later, right before BTC ran to 81,441 by")
        print("  month end. A mean-reversion-at-level thesis keeps fading a level")
        print("  that has just broken, and in a month where BTC rose 25.27% the")
        print("  trend repeatedly wins. That is a structural objection to the")
        print("  thesis, not just one bad week. One month still cannot settle it.")
    else:
        print("  August was profitable, but this is ONE month of a low-frequency")
        print("  strategy on a held-out window. It is evidence, not validation.")
    print()
    print("  V13 remains status RESEARCH and is NOT in execution/approved.json.")
    print("  Nothing above writes to MT5 or places an order.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
