"""V13 BTCUSD DQR — LAST WEEK replay on M30 and D1, $1,000 account, RR 1.5-4.0.

WHAT THIS DOES
  Runs the FROZEN V13 config (no re-tuning of the signal side) on BTCUSD M30
  and D1 over the last complete trading week, sweeping only the R:R exit
  parameter across 1.5 / 2.0 / 3.0 / 4.0 at a fixed 1% risk on $1,000.

  The owner asked specifically for R:R up to 3.0-4.0. That is the one knob
  being varied. Everything upstream of entry — D1 swing levels, the 0.25 ATR
  deep-penetration sweep, close_position 0.70, the 0.80 ATR impulse
  confirmation, 1.5x ATR stop — is frozen from the V13 def.

  IMPORTANT AND DELIBERATE: on M30 the V13 def already recorded that raising
  R:R does not rescue the trade, because M30's measured PF was only 1.09 —
  a wider target wins less often. This script measures that rather than
  assuming it, and it reports the best RR honestly even when "best" still
  loses money.

WHY THE NEAR-MISS REPORT EXISTS
  A "0 signals" week is uninformative on its own: it does not distinguish
  "the thesis is dead" from "the market was quiet". So when no setup fires,
  this script walks every D1 level that was in range on each bar and reports
  how close price came to satisfying each gate, i.e. the actual distance in
  ATR that blocked the trade. That converts a zero into evidence.

WINDOW
  Last COMPLETE week before the run: Monday..Friday, ending at the last
  stored bar. BTC trades weekends, so the window is a rolling 7 calendar
  days ending at the newest stored bar, and the exact timestamps are printed.
  Override with --start/--end.

ACCOUNT MODEL ($1,000, BTCUSD on XMGlobal-MT5)
  1 lot = 1 BTC, so $1 of price = $1 per lot. 1% of equity = $10 risked.
  lots = risk_usd / stop_distance, floored DOWN to the 0.01 lot step. A signal
  whose correct size is under 0.01 lot is reported as UNTRADEABLE rather than
  silently resized, because resizing it upward would breach the 1% cap and
  misrepresent what the account can carry.

Usage:
    ./.venv/Scripts/python.exe research/v13_btc_lastweek_m30_d1.py
    ./.venv/Scripts/python.exe research/v13_btc_lastweek_m30_d1.py --start 2026-09-21
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from research import dqr_quality_reversal as dqr

SYMBOL = "BTCUSD"
BALANCE = 1000.0
RISK_PCT = 0.01
MIN_LOTS = 0.01
LOT_STEP = 0.01
RR_GRID = [1.5, 2.0, 3.0, 4.0]

# Frozen signal side, verbatim from strategy/defs/BTCUSD_DQR_SWING_REVERSAL_V13.json
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
ATR_MULT_STOP = 1.5
COOLDOWN_BARS = 4
MAX_HOLD = {"M30": 32, "D1": 32}


def size_and_pnl(trades, bars: pd.DataFrame, pip: float):
    """Apply 1%-of-equity sizing to the pips trade list; return money rows."""
    ts = pd.to_datetime(bars["ts"].to_numpy())
    rows = []
    equity = BALANCE
    peak = BALANCE
    max_dd = 0.0
    for t in trades:
        stop_dist = abs(t.entry_price - t.stop)
        risk_usd = RISK_PCT * equity
        exact = risk_usd / stop_dist if stop_dist > 0 else 0.0
        lots = int(exact / LOT_STEP) * LOT_STEP
        if lots < MIN_LOTS:
            rows.append(dict(trade=t, tradeable=False, exact_lots=exact,
                             stop_dist=stop_dist, lots=0.0,
                             reason=f"needs {exact:.4f} lot, min is {MIN_LOTS}"))
            continue
        net_usd = t.net_pips * pip * lots
        equity += net_usd
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
        rows.append(dict(trade=t, tradeable=True, exact_lots=exact,
                         stop_dist=stop_dist, lots=lots,
                         risk_usd=stop_dist * lots, net_usd=net_usd,
                         entry_ts=ts[t.entry_bar], exit_ts=ts[t.exit_bar],
                         equity=equity))
    return rows, equity, max_dd


def near_miss_report(win: pd.DataFrame, d1: pd.DataFrame, counter, levels,
                     label: str) -> None:
    """When nothing fires, show how close each gate came, per D1 level.

    Walks every bar in the window against every level that was usable and in
    range, and records which gate blocked the trade first. This is the
    difference between "no signal" and "no signal, and the market came within
    0.11 ATR of one".
    """
    atr = win["atr14"].to_numpy()
    lo = win["low"].to_numpy()
    hi = win["high"].to_numpy()
    op = win["open"].to_numpy()
    cl = win["close"].to_numpy()
    ts = pd.to_datetime(win["ts"].to_numpy())
    rng = (hi - lo)
    rng[rng <= 0] = np.nan
    close_pos = np.where(rng > 0, (cl - lo) / np.where(rng > 0, rng, 1), 0.5)
    body_dir = np.sign(cl - op)

    d1_last = d1["ts"].iloc[-1]
    blocked = {}
    closest = []

    for lv in levels:
        kf = lv.get("known_from")
        if kf is None or pd.Timestamp(kf) > d1_last:
            continue
        price_l = float(lv["price"])
        ltype = lv.get("kind")
        for i in range(len(win)):
            a = atr[i]
            if not np.isfinite(a) or a <= 0:
                continue
            dist_atr = abs(cl[i] - price_l) / a
            if dist_atr > FROZEN["max_level_dist_atr"]:
                continue
            # LONG candidate = support swept from below; SHORT = resistance
            if ltype == "S":
                pen = (price_l - lo[i]) / a
                if pen < 0:
                    pen = np.nan
                side = "BUY"
            else:
                pen = (hi[i] - price_l) / a
                if pen < 0:
                    pen = np.nan
                side = "SELL"
            if not np.isfinite(pen):
                continue
            if pen < FROZEN["min_sweep_atr"]:
                gate = f"sweep {pen:.2f} < {FROZEN['min_sweep_atr']} ATR"
            else:
                cp = close_pos[i] if side == "BUY" else 1 - close_pos[i]
                bd = body_dir[i]
                if not ((side == "BUY" and bd > 0) or (side == "SELL" and bd < 0)):
                    gate = f"body {('bullish' if side == 'BUY' else 'bearish')} required"
                elif (side == "BUY" and cl[i] <= price_l) or (side == "SELL" and cl[i] >= price_l):
                    gate = "no close back beyond level"
                elif cp < FROZEN["close_pos_min"]:
                    gate = f"close_pos {cp:.2f} < {FROZEN['close_pos_min']}"
                else:
                    gate = "sweep+rejection OK, impulse bar missing"
            blocked[gate] = blocked.get(gate, 0) + 1
            closest.append((FROZEN["min_sweep_atr"] - pen, ts[i], side,
                            price_l, pen, gate))

    if not blocked:
        print(f"    near-miss  : no D1 level was within "
              f"{FROZEN['max_level_dist_atr']} ATR of price at any point in the "
              f"window — the level filter alone blocked everything.")
        return
    print(f"    near-miss  : level/bar pairs evaluated in range: {len(closest)}")
    print(f"    blocking gate counts:")
    for gate, n in sorted(blocked.items(), key=lambda kv: -kv[1]):
        print(f"        {n:>4}x  {gate}")

    # Rank by how CLOSE the bar came to satisfying the sweep gate, and split
    # the two populations. A bar that pierced far PAST the level is not a
    # near-miss at all -- it means price ran through the level and never
    # closed back, which is the "no close back beyond level" failure. Mixing
    # those together (as an earlier version did) put 7-ATR blow-throughs at
    # the top of a list labelled "near-miss", which is exactly backwards.
    under = [c for c in closest if c[0] > 0]        # short of the gate
    over = [c for c in closest if c[0] <= 0]        # blew straight through
    under.sort(key=lambda x: x[0])
    over.sort(key=lambda x: -x[4])
    if under:
        print(f"    closest genuine near-misses (short of the "
              f"{FROZEN['min_sweep_atr']} ATR sweep gate):")
        for short, t, side, plvl, pen, gate in under[:3]:
            print(f"        {t:%Y-%m-%d %H:%M}  {side} @ level {plvl:,.2f}  "
                  f"penetrated {pen:.3f} ATR  SHORT BY {short:.3f} ATR  ({gate})")
    if over:
        print(f"    largest blow-throughs (level swept but no close back -- "
              f"NOT near-misses, listed so the distinction is explicit):")
        for short, t, side, plvl, pen, gate in over[:2]:
            print(f"        {t:%Y-%m-%d %H:%M}  {side} @ level {plvl:,.2f}  "
                  f"penetrated {pen:.2f} ATR  ({gate})")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default=SYMBOL)
    ap.add_argument("--tfs", default="M30,D1")
    ap.add_argument("--start", default=None)
    ap.add_argument("--end", default=None)
    ap.add_argument("--days", type=int, default=7,
                    help="rolling calendar days back from newest bar (default 7)")
    ap.add_argument("--balance", type=float, default=BALANCE)
    args = ap.parse_args()

    spec = dqr.SYMBOLS[args.symbol]
    pip = spec["pip"]
    spr = spec["fallback_spread_pips"]
    slip = spec["fallback_slippage_pips"]

    now = pd.Timestamp.now(tz="UTC").tz_localize(None)
    print("=" * 78)
    print(f"V13 BTCUSD DQR — LAST WEEK REPLAY — M30 / D1 — ${args.balance:,.2f}")
    print("=" * 78)
    print(f"run at (UTC)   : {now:%Y-%m-%d %H:%M}")
    print(f"RR sweep       : {', '.join(f'{r}' for r in RR_GRID)}  (only the exit varies)")
    print(f"stop           : {ATR_MULT_STOP} x ATR, fixed")
    print(f"risk/trade     : {RISK_PCT*100:.0f}% of equity, min {MIN_LOTS} lot, "
          f"step {LOT_STEP}")
    print()

    d1 = dqr.build_exec_features(dqr.load("D1", args.symbol))
    counter = dqr.D1TouchCounter(d1, touch_atr=0.30)
    levels = dqr.enrich_levels(
        d1, dqr.build_d1_levels(d1, dqr.FAMILY_GRID["swings"]),
        min_touches=1, touch_atr=0.30)
    print(f"D1 level frame : {len(d1)} bars "
          f"({d1['ts'].iloc[0].date()} .. {d1['ts'].iloc[-1].date()})")
    print(f"usable swing levels at newest bar: {len(levels)}")
    print(f"costs          : round trip {spr + slip:,.0f} pips = "
          f"${(spr+slip)*pip:,.2f}/lot")
    print()

    results = []
    for tf in [t for t in args.tfs.split(",") if t]:
        full = dqr.build_exec_features(dqr.load(tf, args.symbol))
        if full.empty:
            print(f"-- {tf}: no data in db, skipped\n")
            results.append((tf, None, None))
            continue
        newest = full["ts"].iloc[-1]
        end = pd.Timestamp(args.end) if args.end else newest + pd.Timedelta(minutes=1)
        start = (pd.Timestamp(args.start) if args.start
                 else end - pd.Timedelta(days=args.days))
        win = full[(full["ts"] >= start) & (full["ts"] < end)].reset_index(drop=True)
        # Minimum is 20 bars EXCEPT for D1, where a week is only 7 bars by
        # construction. D1 is still run at 7 bars (it just yields little), and
        # a --days 90 window gives it 90 bars, which is enough to compare RR.
        if len(win) < (7 if tf == "D1" else 20):
            print(f"-- {tf}: only {len(win)} bars in window, skipped\n")
            results.append((tf, None, None))
            continue

        print("=" * 78)
        print(f"EXECUTION TF {tf}   window {win['ts'].iloc[0]} .. {win['ts'].iloc[-1]}"
              f"   ({len(win)} bars)")
        print("=" * 78)
        o, h, l, c = (win["open"].to_numpy(), win["high"].to_numpy(),
                      win["low"].to_numpy(), win["close"].to_numpy())
        print(f"  open  {o[0]:,.2f}   high {h.max():,.2f}   low {l.min():,.2f}   "
              f"close {c[-1]:,.2f}   move {(c[-1]/o[0]-1)*100:+.2f}%")

        sigs = dqr.detect_signals(win, levels, d1=d1,
                                  touch_counter=counter, **FROZEN)
        print(f"  raw signal candidates: {len(sigs)}")
        if not sigs:
            near_miss_report(win, d1, counter, levels, tf)
            print()
            results.append((tf, 0, None))
            continue

        for rr in RR_GRID:
            tr, m = dqr.backtest_signals(
                win, sigs, spread_pips=spr, slippage_pips=slip, pip=pip,
                rr=rr, atr_mult_stop=ATR_MULT_STOP,
                max_holding_bars=MAX_HOLD[tf], cooldown_bars=COOLDOWN_BARS)
            if not tr:
                print(f"    RR {rr:.1f}: 0 trades (all cooled down or truncated)")
                results.append((tf, rr, None))
                continue
            rows, eq, dd = size_and_pnl(tr, win, pip)
            live = [r for r in rows if r["tradeable"]]
            untr = len(rows) - len(live)
            net_live = sum(r["net_usd"] for r in live)
            print(f"    RR {rr:.1f}: {len(tr):>2} signals | pips net "
                  f"{m['net_pips']:>9,.1f} PF {m['profit_factor']:.2f} "
                  f"win {m['win_rate']*100:>3.0f}% | $1,000 acct: "
                  f"{len(live)} tradeable, {untr} untradeable at 0.01 lot, "
                  f"net ${net_live:+,.2f}, final ${eq:,.2f}, maxDD ${dd:,.2f}")
            for r in rows:
                t = r["trade"]
                if not r["tradeable"]:
                    print(f"          {t.side:<4} {pd.to_datetime(win['ts'].iloc[t.entry_bar]):%Y-%m-%d %H:%M}"
                          f"  UNTRADEABLE: {r['reason']}  stop {r['stop_dist']:,.0f} pts")
                else:
                    print(f"          {t.side:<4} {r['entry_ts']:%Y-%m-%d %H:%M} -> "
                          f"{r['exit_ts']:%Y-%m-%d %H:%M} ({t.duration_bars} bars)  "
                          f"entry {t.entry_price:>10,.2f} stop {t.stop:>10,.2f} "
                          f"exit {t.exit_price:>10,.2f} ({t.exit_reason})  "
                          f"{r['lots']:.2f} lots  risk ${r['risk_usd']:,.2f}  "
                          f"net ${r['net_usd']:+,.2f}  equity ${r['equity']:,.2f}")
            results.append((tf, rr, dict(n=len(tr), pf=m["profit_factor"],
                                         net_pips=m["net_pips"], eq=eq, dd=dd,
                                         live=len(live), untr=untr,
                                         net_live=net_live)))
        print()

    print("=" * 78)
    print(f"SUMMARY — last week, {args.symbol}, ${args.balance:,.2f}, RR sweep")
    print("=" * 78)
    print(f"{'TF':<5}{'RR':>6}{'signals':>9}{'PF':>7}{'net pips':>12}"
          f"{'tradeable':>11}{'untradeable':>13}{'final $':>11}")
    any_sig = False
    for tf, rr, r in results:
        if rr is None or (r is None and rr == 0):
            print(f"{tf:<5}{'-':>6}{0:>9}{'no signals':>18}")
            continue
        any_sig = any_sig or r is not None
        if r is None:
            continue
        print(f"{tf:<5}{rr:>6.1f}{r['n']:>9}{r['pf']:>7.2f}"
              f"{r['net_pips']:>12,.1f}{r['live']:>11}{r['untr']:>13}"
              f"{r['eq']:>11,.2f}")
    print()
    if not any_sig:
        print("VERDICT: no DQR setup satisfied the frozen gates on either M30 or")
        print("D1 in this window. The strategy is built to be flat when no D1")
        print("level is swept, so an empty week is the designed behaviour, not a")
        print("bug. It also means R:R cannot help: the RR sweep only changes the")
        print("exit of trades that never opened.")
        print()
        print("To widen the sample, replay a longer window, e.g.")
        print("  ./.venv/Scripts/python.exe research/v13_btc_lastweek_m30_d1.py --days 90")
    else:
        print("NOTE: a positive week is a DESCRIPTIVE REPLAY of 7 days on a")
        print("low-frequency strategy, not a validation. V13 remains RESEARCH and")
        print("is NOT in execution/approved.json.")
    print()
    print("Read-only. No MT5 writes, no orders, no fabricated numbers.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
