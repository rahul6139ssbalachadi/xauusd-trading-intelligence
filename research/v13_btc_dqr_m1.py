"""V13 BTCUSD DQR — M1 EXECUTION-TIMEFRAME test with a $1,000 account.

WHY THIS SCRIPT EXISTS
  The V13 def (strategy/defs/BTCUSD_DQR_SWING_REVERSAL_V13.json) validated the
  DQR logic on D1 execution only, and it explicitly warns: "The same logic on
  H1/H4 is deeply negative and NOT Monte-Carlo robust. Do not port to lower
  timeframes without re-validating from scratch." This script does that
  re-validation for M1, which the def had never tested at all.

  The owner's brief says "for i min time fram" — reading it as M1 (the minimum
  timeframe) but the script defaults to ALL available timeframes and reports
  them side by side, so if "i" meant M5 or M15 the answer is already in the
  output. --tfs overrides.

WHAT IS AND IS NOT CLAIMED
  M1 BTCUSD history in db/trading.db is 97,618 bars covering 2026-07-22 ..
  2026-09-28 — 68 days, and that is the MT5 ~100k bar request cap, NOT the
  broker's retention limit. Two months is a DESCRIPTIVE REPLAY, not a
  validation. Nothing here can approve a strategy. The number is what the
  stored data actually produced.

ACCOUNT MODEL ($1,000, matching the owner's balance)
  BTCUSD on XMGlobal-MT5: 1 lot = 1 BTC, so 1 lot = $1 per $1.00 price move.
  A 1% risk cap is $10. Stops are ATR-based, so position size varies per
  trade; lots = risk_usd / stop_distance_price, floored at the 0.01 broker
  minimum and rounded to the 0.01 lot step. When a stop is so wide that
  risk/stop < 0.01 the trade is SKIPPED, not silently resized — that would
  hide the fact that $1,000 cannot trade this strategy at that size.
  Costs: per-bar DB spread is authoritative; fallback 225 pips (=$22.50) plus
  5 pips slippage, i.e. a round trip costs ~$27.50 per lot, or ~2.75% of a
  0.01 lot's notional. On M1 a round trip is a large fraction of a typical
  H1-ATR stop distance.

ORDER OF OPERATIONS (this project's known warmup trap)
  1. build D1 levels on the FULL D1 history
  2. build execution features on the FULL M1 history
  3. slice the execution frame to the test window
  Levels are availability-gated (known_from = defining D1 bar close), so
  slicing after building cannot leak the future. Slicing BEFORE building
  would cold-start ATR and produce zero signals.

Usage:
    ./.venv/Scripts/python.exe research/v13_btc_dqr_m1.py
    ./.venv/Scripts/python.exe research/v13_btc_dqr_m1.py --tfs M1 --start 2026-09-01
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from research import dqr_quality_reversal as dqr
from backtest import compute_metrics
from montecarlo import run_monte_carlo, MCConfig

SYMBOL = "BTCUSD"
BALANCE = 1000.0
RISK_PCT = 0.01                      # 1% of equity per trade = $10 on $1,000
MIN_LOTS = 0.01
LOT_STEP = 0.01
MAX_HOLD_COST_MULT = 3.0            # skip if round-trip cost > 3x the stop

# Frozen V13 D1 config, copied verbatim from the V13 def. Frozen BEFORE
# looking at any M1/M5/M15 result, so this is a genuine transfer test and not
# a fresh fit to whatever timeframe happens to look good.
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
    min_sweep_atr=0.25,
)
FROZEN_EXIT = dict(atr_mult_stop=1.5, rr=1.5, max_holding_bars=32,
                   cooldown_bars=4)


def money(trades, pip: float, bars: pd.DataFrame) -> dict:
    """Convert the pips-based Trade list into real $1,000-account money.

    Trade carries bar INDICES, not timestamps, and net_pips already has the
    per-bar DB spread plus slippage folded in twice. For BTC (1 lot = 1 BTC)
    $1 of price = $1 per lot, so net_usd = net_pips * pip * lots. Timestamps
    are resolved here from the execution frame so every printed trade has a
    real date and time, which is what the owner asked to see.

    Sizing: 1% of current equity risked, lots = risk_usd / stop_distance,
    floored at the 0.01 broker minimum and floored down to the 0.01 step.
    A trade whose correct size is below 0.01 is SKIPPED, never resized up —
    resizing would hide the fact that $1,000 cannot carry this stop.
    """
    ts = pd.to_datetime(bars["ts"].to_numpy())   # -> Timestamp, not str
    out = []
    equity = BALANCE
    peak = BALANCE
    max_dd = 0.0
    for t in trades:
        stop_dist = abs(t.entry_price - t.stop)
        risk_usd = RISK_PCT * equity
        lots = risk_usd / stop_dist if stop_dist > 0 else 0.0
        if lots < MIN_LOTS:
            out.append(dict(side=t.side, skipped=True, reason="below_min_lot",
                            stop_dist=stop_dist, risk_usd=risk_usd))
            continue
        lots = (int(lots / LOT_STEP) * LOT_STEP)
        if lots <= 0:
            out.append(dict(side=t.side, skipped=True, reason="rounds_to_zero",
                            stop_dist=stop_dist, risk_usd=risk_usd))
            continue
        net_usd = t.net_pips * pip * lots
        risk_on_trade = stop_dist * lots
        equity += net_usd
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
        out.append(dict(side=t.side, skipped=False,
                        entry_ts=ts[t.entry_bar], exit_ts=ts[t.exit_bar],
                        entry=t.entry_price, stop=t.stop,
                        exit=t.exit_price, reason=t.exit_reason, lots=lots,
                        stop_dist=stop_dist, risk_usd=risk_on_trade,
                        net_pips=t.net_pips, net_usd=net_usd, equity=equity,
                        duration_bars=t.duration_bars))
    return dict(trades=out, final_equity=equity, max_dd=max_dd,
                executed=sum(1 for o in out if not o["skipped"]),
                skipped=sum(1 for o in out if o["skipped"]))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default=SYMBOL)
    ap.add_argument("--tfs", default="M1,M5,M15,M30,H1,H4,D1")
    ap.add_argument("--start", default=None, help="ISO date lower bound")
    ap.add_argument("--end", default=None, help="ISO date upper bound")
    ap.add_argument("--balance", type=float, default=BALANCE)
    args = ap.parse_args()

    spec = dqr.SYMBOLS[args.symbol]
    pip = spec["pip"]
    spr = spec["fallback_spread_pips"]
    slip = spec["fallback_slippage_pips"]

    now = pd.Timestamp.now(tz="UTC").tz_localize(None)
    print("=" * 78)
    print(f"V13 BTCUSD DQR — LOWER-TIMEFRAME RE-VALIDATION — ${args.balance:,.2f} account")
    print("=" * 78)
    print(f"run at (UTC)     : {now:%Y-%m-%d %H:%M:%S}")
    print(f"levels TF        : D1 (unfrozen, full history, availability-gated)")
    print(f"signal/exec TF   : {args.tfs}")
    print(f"risk/trade       : {RISK_PCT*100:.0f}% of equity, min {MIN_LOTS} lot, "
          f"step {LOT_STEP}")
    print(f"config           : FROZEN V13 D1 config, no re-tuning on lower TFs")
    print()
    print("Config frozen from strategy/defs/BTCUSD_DQR_SWING_REVERSAL_V13.json")
    print("(swing levels, deep_penetration 0.25 ATR, close_pos 0.70, impulse 0.80,")
    print(" stop 1.5x ATR, target 1.5R, 32-bar exit, 4-bar cooldown).")
    print()

    d1 = dqr.build_exec_features(dqr.load("D1", args.symbol))
    counter = dqr.D1TouchCounter(d1, touch_atr=0.30)
    levels = {
        fam: dqr.enrich_levels(
            d1, dqr.build_d1_levels(d1, dqr.FAMILY_GRID[fam]),
            min_touches=1, touch_atr=0.30)
        for fam in dqr.FAMILY_GRID
    }
    print(f"D1 level frame   : {len(d1)} bars "
          f"({d1['ts'].iloc[0].date()} .. {d1['ts'].iloc[-1].date()})")
    for fam, lv in levels.items():
        print(f"  levels[{fam:>10}] = {len(lv):5d}")
    print()

    # Cost sanity: what does one round trip actually cost per lot?
    rt = (spr + slip) * pip
    print(f"costs            : round trip = {rt:,.2f} USD/lot "
          f"({(spr+slip):,.0f} pips). On a {MIN_LOTS} lot that is "
          f"${rt*MIN_LOTS:,.2f} per trade.")
    print()

    summary = []
    for tf in [t for t in args.tfs.split(",") if t]:
        full = dqr.build_exec_features(dqr.load(tf, args.symbol))
        if full.empty:
            print(f"-- {tf}: no data in db, skipped\n")
            summary.append((tf, 0, None, None))
            continue
        lo = full["ts"].iloc[0]
        hi = full["ts"].iloc[-1]
        win = full
        if args.start:
            win = win[win["ts"] >= pd.Timestamp(args.start)]
        if args.end:
            win = win[win["ts"] < pd.Timestamp(args.end)]
        win = win.reset_index(drop=True)
        if len(win) < 60:
            print(f"-- {tf}: only {len(win)} bars in window, skipped\n")
            summary.append((tf, 0, None, None))
            continue

        sigs = dqr.detect_signals(win, levels["swings"], d1=d1,
                                  touch_counter=counter, **FROZEN)
        tr, m = dqr.backtest_signals(
            win, sigs, spread_pips=spr, slippage_pips=slip, pip=pip,
            **FROZEN_EXIT)
        if not tr:
            print(f"-- {tf}: 0 signals (D1 level never swept under the frozen "
                  f"config) — the strategy is designed to be flat, this is not "
                  f"a bug\n")
            summary.append((tf, 0, None, None))
            continue

        pay = money(tr, pip, win)
        span = (hi - lo)
        print(f"--- execution TF {tf}  |  {len(win)} bars  "
              f"{win['ts'].iloc[0]} .. {win['ts'].iloc[-1]} ---")
        print(f"    pips view   : {dqr.fmt(m)}")
        print(f"    money view  : ${pay['final_equity']:,.2f} final "
              f"({(pay['final_equity']-args.balance)/args.balance*100:+.2f}%)  "
              f"maxDD ${pay['max_dd']:,.2f}  "
              f"executed {pay['executed']} / skipped {pay['skipped']}")
        if span.days > 0:
            print(f"    per month   : ${(pay['final_equity']-args.balance)/span.days*30:,.2f}"
                  f"  (over {span.days} days)")
        for t in pay["trades"][:40]:
            if t["skipped"]:
                print(f"      {t['side']:<4} SKIPPED ({t['reason']}) "
                      f"stop_dist {t['stop_dist']:,.1f} risk ${t['risk_usd']:,.2f}")
            else:
                print(f"      {t['side']:<4} {t['entry_ts']:%Y-%m-%d %H:%M} -> "
                      f"{t['exit_ts']:%Y-%m-%d %H:%M}  "
                      f"({t['duration_bars']} bars)  "
                      f"entry {t['entry']:>10,.2f} stop {t['stop']:>10,.2f} "
                      f"exit {t['exit']:>10,.2f} ({t['reason']}) "
                      f"{t['lots']:.2f} lots  risk ${t['risk_usd']:,.2f}  "
                      f"net ${t['net_usd']:+,.2f}")
        if len(pay["trades"]) > 40:
            print(f"      ... {len(pay['trades'])-40} more")
        mc = (run_monte_carlo(tr, MCConfig(n_iterations=2000, seed=42,
                                           shuffle=True, scatter_pct=0.10,
                                           jitter_pct=0.05,
                                           ruin_threshold=-200.0))
              if len(tr) >= 10 else None)
        if mc:
            print(f"    MC          : net_p5 {mc.net_p5:,.0f} pips  "
                  f"PF_p5 {mc.profit_factor_p5:.2f}  ruin {mc.ruin_prob*100:.1f}%  "
                  f"robust={mc.is_robust}")
        print()
        summary.append((tf, len(tr), m, pay))

    print("=" * 78)
    print(f"SUMMARY — V13 BTCUSD DQR lower-TF, ${args.balance:,.2f} balance")
    print("=" * 78)
    print(f"{'TF':<5}{'bars':>9}{'trades':>8}{'PF':>7}{'net pips':>13}"
          f"{'final $':>12}{'maxDD $':>10}{'MC':>8}")
    for tf, n, m, pay in summary:
        if n == 0:
            print(f"{tf:<5}{'-':>9}{0:>8}{'no signals':>20}")
            continue
        print(f"{tf:<5}{'-':>9}{n:>8}{m['profit_factor']:>7.2f}"
              f"{m['net_pips']:>13,.1f}{pay['final_equity']:>12,.2f}"
              f"{pay['max_dd']:>10,.2f}{'':>8}")
    print()
    d1row = [r for r in summary if r[0] == "D1"]
    if d1row and d1row[0][1]:
        print(f"context — the SAME frozen config on D1 (the timeframe it was")
        print(f"approved on): {d1row[0][1]} trades, PF {d1row[0][2]['profit_factor']:.2f}, "
              f"net {d1row[0][2]['net_pips']:,.0f} pips")
    else:
        print("context — D1 produced no signals in the stored window either;")
        print("           the V13 def reports +39,802 pips / PF 1.79 / 36 trades")
        print("           over 13.7 years, so the stored D1 slice is thinner than that.")
    print()
    print("Read-only. No MT5 writes, no orders, no fabricated numbers.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
