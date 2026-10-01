"""DQR — BTCUSD LAST-WEEK report with $1,000 balance, adaptive lots and R:R.

Answers one question: if the DQR spec had been traded over the last complete
Monday-Sunday week, what would it have done on a $1,000 account?

WINDOW
  Last COMPLETE Monday-Sunday week in the DB. A partial current week is never
  used, because an unfinished week cannot have a real result.

SIZING AND R:R ARE ADAPTIVE, NOT CONSTANTS
  The request was to choose lot size and R:R "according to the market
  momentum". Done objectively, not by tuning until the week looks good:

   * stop distance  = atr_mult x ATR(14) of the execution TF
   * R:R multiple   = higher when the post-entry trend is strong
                      (realised move in the trade direction over the last
                      `mom_lookback` bars, normalised by ATR), lower when
                      momentum is absent. Clamped to [1.0, 3.0].
   * risk per trade = risk_pct of RUNNING equity (compounding), capped by the
                      project's own RiskConfig hard limit of 1%
   * lots           = project risk.compute_lot_size, floor-rounded to lot_step

  Every one of these is printed per trade so the sizing is auditable.

CONTRACT SIZING (BTCUSD)
  BTCUSD point = 0.01, 1 lot = 1 BTC, so USD P&L per lot = price move in USD.
  The project's risk/ module defaults to GOLD (multiplier 100, pip 0.10);
  the BTC multiplier is passed explicitly rather than editing that module.
  $1,000 equity with a $500-2,000 stop distance means the account can only
  afford the 0.01 minimum lot on most trades. That is a real constraint and
  is reported, not hidden by inflating the position.

HONESTY
  One week is 5-7 D1 bars. This is a descriptive replay, NOT a validation.
  It is printed next to the multi-year result for the same config. If the
  week disagrees with the multi-year numbers, the week is noise.

Read-only: queries db/trading.db only. No MT5, no orders, no fabrication.

Usage:
    ./.venv/Scripts/python.exe research/dqr_btc_lastweek.py
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from research import dqr_quality_reversal as dqr
from backtest import compute_metrics, Trade
from risk import RiskConfig, compute_lot_size, risk_per_trade_usd

SYMBOL = "BTCUSD"
# 1 BTC per lot on BTCUSD
BTC_MULTIPLIER = 1.0


def last_complete_week(d1: pd.DataFrame) -> tuple[pd.Timestamp, pd.Timestamp, str]:
    """Most recent FULL Monday..Sunday in the D1 series.

    The last D1 bar is only considered complete if the series ends before
    today; we anchor on the last bar, walk back to its Monday, and require
    that a Sunday exists after it.
    """
    last = pd.Timestamp(d1["ts"].iloc[-1])
    # Monday of the week containing the LAST bar
    monday = last - pd.Timedelta(days=last.dayofweek)
    # a full week requires the following Sunday to exist in the data
    sunday = monday + pd.Timedelta(days=6)
    if sunday > last:
        # last bar's week is incomplete -> use the previous week
        monday = monday - pd.Timedelta(days=7)
        sunday = sunday - pd.Timedelta(days=7)
    return monday, sunday + pd.Timedelta(days=1), f"{monday.date()} .. {(sunday).date()}"


def momentum_rr(direction: int, seg_c: np.ndarray, atr: float,
                lo: float = 1.0, hi: float = 3.0) -> float:
    """Adaptive R:R from measured momentum in the trade direction.

    direction: +1 long, -1 short. Uses the drift of the last N CLOSED bars
    (seg_c, supplied by the caller) in ATR units:
      strong tailwind -> wider target (up to hi); no momentum -> lo.
    """
    if atr <= 0 or len(seg_c) == 0:
        return lo
    units = ((seg_c[-1] - seg_c[0]) * direction) / atr
    return round(lo + float(np.clip(units / 3.0, 0.0, 1.0)) * (hi - lo), 2)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default=SYMBOL)
    ap.add_argument("--tf", default="H1", help="execution timeframe")
    ap.add_argument("--balance", type=float, default=1000.0)
    ap.add_argument("--risk-pct", type=float, default=0.01,
                    help="fraction of RUNNING equity risked per trade")
    ap.add_argument("--atr-mult", type=float, default=1.5)
    ap.add_argument("--mom-lookback", type=int, default=8)
    ap.add_argument("--rr-min", type=float, default=1.0)
    ap.add_argument("--rr-max", type=float, default=3.0)
    ap.add_argument("--max-hold", type=int, default=48, help="bars")
    ap.add_argument("--cooldown", type=int, default=4)
    ap.add_argument("--day", default=None,
                    help="YYYY-MM-DD: run a SINGLE closed day instead of the "
                         "last complete Mon-Sun week")
    args = ap.parse_args()

    spec = dqr.SYMBOLS[args.symbol]
    pip = spec["pip"]
    spr = spec["fallback_spread_pips"]
    slip = spec["fallback_slippage_pips"]
    rcfg = RiskConfig(max_risk_per_trade_pct=0.01, max_lots=100.0,
                      min_lots=0.01, lot_step=0.01)

    print("=" * 78)
    print(f"DQR :: {args.symbol} LAST COMPLETE WEEK :: ${args.balance:,.2f} account")
    print("=" * 78)

    d1 = dqr.build_exec_features(dqr.load("D1", args.symbol))
    full = dqr.build_exec_features(dqr.load(args.tf, args.symbol))
    if args.day:
        d = pd.Timestamp(args.day)
        w_start, w_end = d, d + pd.Timedelta(days=1)
        w_label = f"{d.date()} (single closed day)"
    else:
        w_start, w_end, w_label = last_complete_week(d1)
    print(f"window      : {w_label}")
    print(f"execution TF: {args.tf}")
    print(f"risk/trade  : {args.risk_pct*100:.2f}% of RUNNING equity "
          f"(hard cap {rcfg.max_risk_per_trade_pct*100:.0f}%)")
    print(f"stop        : {args.atr_mult} x ATR14   "
          f"R:R adaptive {args.rr_min}..{args.rr_max} from measured momentum")
    print(f"contract    : 1 lot = 1 BTC  (multiplier {BTC_MULTIPLIER})")
    print()

    counter = dqr.D1TouchCounter(d1, touch_atr=0.30)
    levels = {fam: dqr.enrich_levels(
        d1, dqr.build_d1_levels(d1, dqr.FAMILY_GRID[fam]),
        min_touches=1, touch_atr=0.30) for fam in dqr.FAMILY_GRID}

    # slice execution to the week, but keep enough pre-week bars for ATR warmup
    warm = 200   # ATR(14) needs ~14 closed bars; 200 is ample headroom
    pre = full[full["ts"] < w_start].tail(warm)
    win = full[(full["ts"] >= w_start) & (full["ts"] < w_end)]
    if win.empty:
        print(f"NO {args.tf} BARS in {w_label} — cannot report.")
        return 1
    exec_win = pd.concat([pre, win], ignore_index=True)
    week_lo = len(pre)
    print(f"bars in week: {len(win)}  (+{len(pre)} warmup bars, not traded)")
    print(f"week close  : {win['close'].iloc[0]:.2f} -> {win['close'].iloc[-1]:.2f} "
          f"({(win['close'].iloc[-1]/win['close'].iloc[0]-1)*100:+.2f}%)")
    print()

    o = exec_win["open"].to_numpy(); h = exec_win["high"].to_numpy()
    l = exec_win["low"].to_numpy(); c = exec_win["close"].to_numpy()
    a = exec_win["atr14"].to_numpy()
    sp_pts = exec_win["spread"].to_numpy()

    equity = args.balance
    rows = []
    for fam in dqr.FAMILY_GRID:
        sigs = dqr.detect_signals(
            exec_win, levels[fam], d1=d1, touch_counter=counter,
            level_tol_atr=0.10, close_pos_min=0.70, impulse_atr=0.80,
            sweep_lookback=1, break_lookback=0, break_atr=0.50,
            max_level_age_d1=120, max_level_dist_atr=3.0,
            min_level_touches=1, min_sweep_atr=0.25)

        fam_trades = []
        last_entry = -10**9
        for s in sigs:
            i = s["entry_bar"]
            if i < week_lo:              # only trades opened inside the week
                continue
            if i - last_entry < args.cooldown:
                continue
            side, ep = s["side"], s["entry_price"]
            atrv = a[i - 1] if not np.isnan(a[i - 1]) else a[max(0, i - 10)]
            if np.isnan(atrv) or atrv <= 0:
                continue
            dist = args.atr_mult * atrv
            stop = ep - dist if side == "BUY" else ep + dist
            d = 1 if side == "BUY" else -1
            seg = c[max(0, i - args.mom_lookback):i]
            rr = momentum_rr(d, seg, atrv, args.rr_min, args.rr_max)
            target = ep + d * rr * dist

            lots = compute_lot_size(equity, args.risk_pct, dist,
                                    BTC_MULTIPLIER, rcfg)
            risk_usd = risk_per_trade_usd(lots, dist, BTC_MULTIPLIER) if lots else 0.0

            maxj = min(i + args.max_hold, len(exec_win) - 1)
            exit_price = reason = None
            for j in range(i + 1, maxj + 1):
                if side == "BUY":
                    sh, th = l[j] <= stop, h[j] >= target
                else:
                    sh, th = h[j] >= stop, l[j] <= target
                # same-bar collision: stop is always the nearer level
                # (rr >= rr_min = 1.0), so pessimistically assume stop first
                if sh:
                    exit_price, reason = stop, "stop"
                elif th:
                    exit_price, reason = target, "target"
                else:
                    continue
                break
            if exit_price is None:
                j = maxj
                exit_price, reason = c[j], "end"

            gross_price = (exit_price - ep) if side == "BUY" else (ep - exit_price)
            spr_price = (sp_pts[i] * 0.01) if not np.isnan(sp_pts[i]) else spr * pip
            cost_price = 2 * spr_price + 2 * slip * pip
            pnl_usd = lots * (gross_price - cost_price)
            equity += pnl_usd

            t = Trade(entry_bar=i, entry_price=ep, side=side, stop=stop,
                      target=target, exit_bar=j, exit_price=exit_price,
                      exit_reason=reason, points=gross_price / 0.01,
                      cost_pips=cost_price / pip,
                      net_pips=(gross_price - cost_price) / pip,
                      duration_bars=j - i, lots=lots, risk_usd=risk_usd,
                      net_usd=pnl_usd)
            fam_trades.append(t)
            rows.append(dict(
                family=fam, side=side,
                entry_ts=pd.Timestamp(exec_win["ts"].iloc[i]),
                level=s["level_price"], pen_atr=s["penetration_atr"],
                entry=ep, stop=stop, target=target, exit=exit_price,
                reason=reason, atr=atrv, stop_dist=dist, rr=rr, lots=lots,
                risk_usd=risk_usd, cost_usd=round(lots * cost_price, 2),
                pnl_usd=pnl_usd, pnl_r=(pnl_usd / risk_usd) if risk_usd else 0.0,
                equity=equity, bars=j - i))
            last_entry = i

        m = compute_metrics(fam_trades)
        print(f"--- family {fam}: {len(fam_trades)} trades")
        if not fam_trades:
            print("    no DQR setup qualified in this week\n")
            continue
        for r in rows:
            if r["family"] != fam:
                continue
            print(f"    {r['entry_ts']} {r['side']:<4} entry {r['entry']:>9.1f} "
                  f"stop {r['stop']:>9.1f} tgt {r['target']:>9.1f} "
                  f"R:R {r['rr']:.2f} stopdist {r['stop_dist']:>7.1f} "
                  f"lots {r['lots']:.2f} risk ${r['risk_usd']:.2f} "
                  f"-> {r['reason']:<6} net ${r['pnl_usd']:+.2f} "
                  f"({r['pnl_r']:+.2f}R) eq ${r['equity']:.2f}")
        print(f"    {dqr.fmt(m)}")
        print()

    # ---- report ----
    print("=" * 78)
    print(f"WEEK REPORT — {args.symbol} {w_label}")
    print("=" * 78)
    if not rows:
        print("  NO DQR SETUP FIRED THIS WEEK on any level family.")
        print()
        print("  This is a legitimate outcome, not an error. The spec requires a")
        print("  real liquidity sweep of a D1 level plus momentum confirmation;")
        print("  in a quiet week that simply does not happen. The strategy is")
        print("  designed to be flat.")
        print()
        print("  A week with zero trades tells you nothing about edge. For the")
        print("  multi-year evidence see research/dqr_btc_output.txt:")
        print("    D1 + swing levels: net=+39802 pips, PF=1.79, 36 trades,")
        print("    Monte Carlo robust=True  (the only robust config found)")
        print("    H1/H4 same config:   deeply negative, NOT robust")
        print()
        print("=" * 78)
        print(f"balance ${args.balance:,.2f} -> ${args.balance:,.2f} (unchanged)")
        print("=" * 78)
        return 0

    rows.sort(key=lambda r: r["entry_ts"])
    print(f"{'#':>2} {'time':<17}{'dir':<5}{'entry':>9}{'stop':>9}{'tgt':>9}"
          f"{'R:R':>5}{'lots':>6}{'risk$':>7}{'exit':<7}{'pnl$':>9}{'R':>7}")
    print("-" * 100)
    for n, r in enumerate(rows, 1):
        print(f"{n:>2} {str(r['entry_ts'])[:16]:<17}{r['side']:<5}"
              f"{r['entry']:>9.1f}{r['stop']:>9.1f}{r['target']:>9.1f}"
              f"{r['rr']:>5.2f}{r['lots']:>6.2f}{r['risk_usd']:>7.2f}"
              f"{r['reason']:<7}{r['pnl_usd']:>+9.2f}{r['pnl_r']:>+7.2f}")

    net = sum(r["pnl_usd"] for r in rows)
    wins = [r for r in rows if r["pnl_usd"] > 0]
    losses = [r for r in rows if r["pnl_usd"] <= 0]
    gw = sum(r["pnl_usd"] for r in wins)
    gl = -sum(r["pnl_usd"] for r in losses)
    avg_r = np.mean([r["pnl_r"] for r in rows]) if rows else 0
    eq_curve = [args.balance] + [r["equity"] for r in rows]
    peak = np.maximum.accumulate(eq_curve)
    dd = float(np.max(np.array(peak) - np.array(eq_curve)))
    tot_risk = sum(r["risk_usd"] for r in rows)

    print("-" * 100)
    print(f"trades            : {len(rows)}")
    print(f"win rate          : {len(wins)/len(rows)*100:.1f}%  "
          f"({len(wins)}W / {len(losses)}L)")
    print(f"profit factor     : {(gw/gl if gl else float('inf')):.2f}")
    print(f"net profit        : ${net:+,.2f}  "
          f"({net/args.balance*100:+.2f}% of balance)")
    print(f"gross win / loss  : ${gw:+,.2f} / ${gl:,.2f}")
    print(f"avg R per trade   : {avg_r:+.2f}R")
    print(f"total risk used   : ${tot_risk:,.2f} "
          f"({tot_risk/args.balance*100:.1f}% of starting balance)")
    print(f"max drawdown      : ${dd:,.2f} ({dd/args.balance*100:.2f}%)")
    print(f"balance           : ${args.balance:,.2f} -> ${equity:,.2f}")
    print()
    print("SIZING AUDIT (why these lots)")
    print(f"  risk/trade = {args.risk_pct*100:.1f}% of RUNNING equity, floor to "
          f"{rcfg.lot_step} lot")
    print(f"  lots = risk$ / (stop_dist x 1 BTC/lot)")
    for r in rows:
        print(f"    stop_dist {r['stop_dist']:>8.1f} -> lots {r['lots']:.2f} "
              f"= risk ${r['risk_usd']:.2f}")
    print()
    print("=" * 78)
    print("READ THIS BEFORE TRUSTING THE NUMBERS")
    print("=" * 78)
    print("  * ONE WEEK IS NOT A BACKTEST. 5-7 D1 bars, 1 week of H1. It is a")
    print("    descriptive replay, not validation. Any win rate computed from a")
    print("    handful of trades has an error bar wider than the rate itself.")
    print("  * The lots are TINY because $1,000 against a BTC stop of several")
    print("    hundred dollars forces the 0.01 minimum. The P&L numbers are")
    print("    therefore mostly a statement about position sizing arithmetic,")
    print("    not about how much the strategy earns.")
    print("  * Adaptive R:R and ATR stops are reasonable defaults, but they were")
    print("    chosen by me, not validated. A different R:R cap would change")
    print("    every number above.")
    print("  * This is NOT approved for live trading. DQR is RESEARCH status.")
    print("    The only multi-year-robust config found is D1 + swing levels,")
    print("    which may well produce zero trades in any given week — by design.")
    print()
    print("  Multi-year reference (research/dqr_btc_output.txt):")
    print("    D1  swings : net +39802 pips  PF 1.79  36 trades  MC robust=True")
    print("    H1  swings : net -213956 pips  PF 0.45 372 trades  MC robust=False")
    print("    H4  swings : net  -12623 pips  PF 0.93 149 trades  MC robust=False")
    print()
    print("  If the week above disagrees with those numbers, the week is noise.")
    print("  Read-only. No MT5 writes, no orders, no fabricated numbers.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
