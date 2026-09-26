"""WHY the uncle's M5 EA loses: diagnostic backtest, May-Aug 2026.

Runs the EA's real inputs (RR 1.1, fixed 0.01 lot, partial TP at 0.5R,
trailing start 100 points) and decomposes the loss:

  - R-multiple distribution (is the edge negative, or is it cost?)
  - MAE / MFE: does price go my way before the stop? (is the stop too tight?)
  - per-signal-type P&L (reversal vs support vs breakout)
  - cost as a share of stop distance
  - hour-of-day and day-of-week

READ-ONLY. No execution imports, no order_send.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from research.uncle_ea_m5_week import (  # noqa: E402
    DB, PIP, LOT_VALUE_PER_PIP, features, signals,
)

START_BALANCE = 10_000.0
FIXED_LOTS = 0.01          # the EA's InpLotSize
RR = 1.1                   # the EA's InpTP_RR
PARTIAL_TP_R = 0.50        # the EA's InpPartialTP_RR
PARTIAL_PCT = 0.50         # the EA's InpPartialPercent
SL_ATR_MULT = 1.5
SPREAD_PIPS = 30.0
SLIPPAGE_PIPS = 5.0
TRAIL_START_POINTS = 100.0


def load(frm, to):
    with sqlite3.connect(DB) as c:
        rows = c.execute(
            "SELECT ts_broker_epoch, open, high, low, close FROM market_data "
            "WHERE symbol='XAUUSD' AND timeframe='M5' AND ts_broker_epoch BETWEEN ? AND ? "
            "ORDER BY ts_broker_epoch", (int(frm.timestamp()), int(to.timestamp()))
        ).fetchall()
    df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close"])
    if df.empty:
        return df
    df.index = pd.to_datetime(df.pop("ts"), unit="s")
    df["spread"] = SPREAD_PIPS
    return features(df)


def run(df, variant="as_is", rr=None, fixed_sl_pips=None):
    """One position at a time, entry on the bar AFTER the signal, 50% out at
    0.5R, broker-side SL/TP assumption, round-trip spread+slippage charged.

    NOT modelled, and deliberately so: the EA's trailing stop
    (InpEnableTrailing / InpTrailStart) and its move-to-break-even after
    TP1. The break-even move means partial-then-stop trades are charged a
    pessimistic -0.5R here rather than ~0. Trailing is simply absent.

    variant:
      "as_is"   - the EA exactly as written (RR 1.1, stop 1.5xATR)
      "swap"    - RR 1.0 and the SL and TP levels EXCHANGED on the same
                  direction: the stop sits where the target was and the
                  target sits where the stop was. The stop therefore gets
                  WIDER (1.0xATR) and the target gets NEARER (1.5xATR).
      "invert"  - the signal's direction is REVERSED at RR 1.0. Tests
                  whether the candle patterns are predictive in reverse.

    fixed_sl_pips: if set, the stop is a FIXED pip distance instead of an
                  ATR multiple. Trailing is already absent from this model
                  (the EA's InpEnableTrailing has no effect here), so this
                  is the "fixed stop, no trailing" configuration.
    """
    # "swap"/"invert" DEFAULT to RR 1.0 as the user requested, but an explicit
    # rr must win. The previous `rr = rr or RR` on the line above made the
    # `rr is None` branch unreachable, so an RR sweep silently returned
    # identical numbers at every setting.
    if rr is None:
        rr = 1.0 if variant in ("swap", "invert") else RR
    sl_mult = SL_ATR_MULT if variant == "as_is" else 1.0   # swap: stop was the TP side
    o, h, l, c, atr = (df[k].to_numpy(float) for k in ("open", "high", "low", "close", "atr"))
    n = len(df)
    sigs = {s["i"]: s for s in signals(df, 60, True, RR, None)}   # look_ahead=True == EA as written

    trades = []
    pos = None
    for e in range(1, n):
        if pos is not None:
            long, sl, tp1, tp2 = pos["long"], pos["sl"], pos["tp1"], pos["tp2"]
            stop_dist = pos["stop_dist"]
            hit_sl = (l[e] <= sl) if long else (h[e] >= sl)
            hit_t1 = (h[e] >= tp1) if long else (l[e] <= tp1)
            hit_t2 = (h[e] >= tp2) if long else (l[e] <= tp2)
            # MAE / MFE in R, tracked continuously
            fav = ((h[e] - pos["entry"]) if long else (pos["entry"] - l[e])) / stop_dist
            adv = ((l[e] - pos["entry"]) if long else (pos["entry"] - h[e])) / stop_dist
            pos["mfe"] = max(pos["mfe"], fav)
            pos["mae"] = min(pos["mae"], adv)
            r = None
            if hit_sl:
                r = -0.5 if pos["t1"] else -1.0
            elif hit_t2:
                r = (0.5 + 0.5 * rr) if pos["t1"] else rr
            elif hit_t1:
                pos["t1"] = True
            if r is not None:
                rec = dict(pos["rec"], exit_idx=e, r=round(r, 3),
                           mfe=round(pos["mfe"], 3), mae=round(pos["mae"], 3))
                trades.append(rec)
                pos = None
            continue

        s = sigs.get(e - 1)
        if s is None:
            continue
        a = atr[e - 1]
        if not a or np.isnan(a) or a <= 0:
            continue
        long = s["dir"] == "BUY"
        if variant == "invert":
            long = not long
        entry = o[e] + (0.5 * SPREAD_PIPS * PIP) if long else o[e] - (0.5 * SPREAD_PIPS * PIP)
        # as_is: stop = 1.5xATR, target = stop x RR
        # swap  : stop = 1.0xATR, target = stop x 1.0  (levels exchanged)
        stop_dist = (fixed_sl_pips * PIP) if fixed_sl_pips else (sl_mult * a)
        tp2_dist = stop_dist * rr
        tp1_dist = stop_dist * PARTIAL_TP_R
        sl = entry - stop_dist if long else entry + stop_dist
        tp1 = entry + tp1_dist if long else entry - tp1_dist
        tp2 = entry + tp2_dist if long else entry - tp2_dist
        why = s["why"]
        kind = ("reversal" if "reversal" in why else
                "breakout" if "outbreak" in why or "breakdown" in why else "sr")
        rec = {
            "time": df.index[e], "dir": s["dir"], "why": s["why"],
            "kind": kind,
            "entry": round(entry, 2), "stop_pips": round(stop_dist / PIP),
            "hour": df.index[e].hour, "dow": df.index[e].day_name()[:3],
        }
        pos = {"long": long, "sl": sl, "tp1": tp1, "tp2": tp2, "entry": entry,
               "stop_dist": stop_dist, "t1": False, "mfe": 0.0, "mae": 0.0, "rec": rec}

    if pos is not None:                      # still open at window end
        mtm = ((c[-1] - pos["entry"]) if pos["long"] else (pos["entry"] - c[-1])) / pos["stop_dist"]
        rec = dict(pos["rec"], exit_idx=n - 1, r=round((0.5 if pos["t1"] else 1.0) * mtm, 3),
                   open_at_end=True, mfe=round(pos["mfe"], 3), mae=round(pos["mae"], 3))
        trades.append(rec)

    # money: FIXED 0.01 lot, exactly like the EA
    cost_pips = SPREAD_PIPS + 2 * SLIPPAGE_PIPS
    for t in trades:
        t["gross_pips"] = t["r"] * t["stop_pips"]
        t["cost_pips"] = cost_pips
        t["net_pips"] = t["gross_pips"] - cost_pips
        t["net_usd"] = t["net_pips"] * PIP * LOT_VALUE_PER_PIP * FIXED_LOTS
    return trades


def report(trades, title):
    n = len(trades)
    print("=" * 78)
    print(title)
    print("=" * 78)
    if not n:
        print("  no trades")
        return
    df = pd.DataFrame(trades)
    # `bal` must track the running balance, NOT add the running total again
    df["balance"] = START_BALANCE + df["net_usd"].cumsum()
    bal = float(df["balance"].iloc[-1])
    wins, losses = df[df.net_usd > 0], df[df.net_usd < 0]
    gp, gl = wins.net_usd.sum(), -losses.net_usd.sum()

    net = float(df.net_usd.sum())
    print(f"trades {n}   win% {len(wins)/n*100:.1f}   PF {gp/gl if gl else float('nan'):.2f}")
    print(f"net ${net:+.2f}  ({net/START_BALANCE*100:+.2f}%)   "
          f"end balance ${bal:,.2f}")
    print(f"avg R {df.r.mean():+.3f}   avg stop {df.stop_pips.mean():.0f} pips   "
          f"cost {df.cost_pips.iloc[0]:.0f} pips = "
          f"{df.cost_pips.mean()/df.stop_pips.mean()*100:.1f}% of the stop")

    print("\n-- R distribution (the edge itself) --")
    for label, lo, hi in [("<= -1R (stopped)", -9, -0.99), ("-1R..0R", -0.99, 0),
                          ("0R..+0.5R", 0, 0.5), ("+0.5R..+1R", 0.5, 1.0),
                          (">= +1.1R (target)", 1.0, 9)]:
        k = ((df.r >= lo) & (df.r < hi)).sum()
        print(f"   {label:22} {k:5}  {k/n*100:5.1f}%")

    print("\n-- MFE: did price ever go our way? (R units) --")
    print(f"   mean MFE {df.mfe.mean():+.2f}R   median {df.mfe.median():+.2f}R   "
          f"never +0.5R: {(df.mfe < 0.5).sum()} ({(df.mfe<0.5).mean()*100:.0f}%)")
    print(f"   mean MAE {df.mae.mean():+.2f}R   hit -1R before +0.5R: "
          f"{((df.mfe<0.5)&(df.mae<=-0.99)).sum()} ({(df.mfe<0.5).mean()*100:.0f}%)")

    print("\n-- by signal type --")
    g = df.groupby("kind").agg(n=("r", "size"), avg_r=("r", "mean"),
                               net_usd=("net_usd", "sum"),
                               win=("net_usd", lambda s: (s > 0).mean() * 100))
    print(g.to_string())

    print("\n-- by hour (broker time) --")
    g = df.groupby("hour").agg(n=("r", "size"), avg_r=("r", "mean"),
                               net_usd=("net_usd", "sum"))
    print(g.to_string())

    print("\n-- by weekday --")
    g = df.groupby("dow").agg(n=("r", "size"), avg_r=("r", "mean"),
                             net_usd=("net_usd", "sum"))
    print(g.to_string())
    print()


def _stats(d):
    """PF / win% / net from a trades frame. One definition, three callers."""
    w, l = d[d.net_usd > 0], d[d.net_usd < 0]
    gp, gl = w.net_usd.sum(), -l.net_usd.sum()
    return {"n": len(d), "win": len(w) / len(d) * 100,
            "pf": gp / gl if gl else float("nan"),
            "avg_r": d.r.mean(), "net": d.net_usd.sum(),
            "stop": d.stop_pips.mean(), "mfe": d.mfe.median(),
            "cost_share": d.cost_pips.mean() / d.stop_pips.mean() * 100}


def _row(win, variant):
    """Stats for a window under a variant, or None if it has no trades."""
    tr = run(win, variant)
    return _stats(pd.DataFrame(tr)) if tr else None


def _months(df, frm, to):
    """[(label, window)] for each calendar month in [frm, to)."""
    out, cur = [], frm.replace(day=1)
    while cur < to:
        nxt = (cur.replace(year=cur.year + 1, month=1) if cur.month == 12
               else cur.replace(month=cur.month + 1))
        out.append((cur.strftime("%Y-%m"), df[(df.index >= cur) & (df.index < nxt)]))
        cur = nxt
    return out


GEOM = {"as_is":  f"RR={RR}  SL={SL_ATR_MULT}xATR",
        "swap":   "RR=1.0  SL/TP EXCHANGED  SL=1.0xATR",
        "invert": "RR=1.0  direction INVERTED  SL=1.0xATR"}


def _fmt(label, r, bal=None):
    """One aligned stats row. `bal` adds the running-balance column."""
    if r is None:
        c = "".join(f"{'-':>6}{'-':>7}" for _ in range(1))
        return f"{label:9}{c}{'-':>8}{'-':>10}" + (f"{0:>12,.0f}" if bal is not None else "")
    extra = f"{bal:>12,.0f}" if bal is not None else ""
    return (f"{label:9}{r['n']:>6}{r['win']:>7.1f}{r['pf']:>7.2f}{r['avg_r']:>8.3f}"
            f"{r['net']:>10.2f}{r['net']/START_BALANCE*100:>8.2f}{r['stop']:>9.0f}"
            f"{r['mfe']:>9.2f}{r['cost_share']:>11.1f}{extra}")


def compare(a, b):
    """as_is vs swapped SL/TP vs inverted direction, month by month."""
    frm, to = datetime.strptime(a, "%Y-%m-%d"), datetime.strptime(b, "%Y-%m-%d")
    df = load(frm - timedelta(days=20), to)
    if df.empty:
        print(f"no data {frm.date()}..{to.date()}")
        return 2
    print(f"fixed {FIXED_LOTS} lot   balance ${START_BALANCE:,.0f}   "
          f"spread {SPREAD_PIPS}p + {SLIPPAGE_PIPS}p/side   "
          f"50% out at {PARTIAL_TP_R}R\n")
    for v in ("as_is", "swap", "invert"):
        print(f"== {v.upper():7} {GEOM[v]}")
        for label, win in _months(df, frm, to):
            print(_fmt(label, _row(win, v) if len(win) else None))
        if (tot := _row(df[df.index >= frm], v)):
            print(_fmt("TOTAL", tot), "\n")
    return 0


def monthly(a, b, variant="as_is"):
    """One row per month over [a, b) with a running balance column."""
    frm, to = datetime.strptime(a, "%Y-%m-%d"), datetime.strptime(b, "%Y-%m-%d")
    df = load(frm - timedelta(days=20), to)
    if df.empty:
        print(f"no data {frm.date()}..{to.date()}")
        return 2
    print(f"{variant.upper()}  {GEOM[variant]}  fixed {FIXED_LOTS} lot  "
          f"50% out at {PARTIAL_TP_R}R   balance ${START_BALANCE:,.0f}\n")
    print(f"{'month':9}{'n':>6}{'win%':>7}{'PF':>7}{'avgR':>8}{'net$':>10}"
          f"{'ret%':>8}{'avgStop':>9}{'medMFE':>9}{'cost%stop':>11}{'bal$':>12}")
    bal = START_BALANCE
    for label, win in _months(df, frm, to):
        r = _row(win, variant) if len(win) else None
        if r:
            bal += r["net"]
        print(_fmt(label, r, bal))
    return 0




def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="a", default="2026-05-01")
    ap.add_argument("--to", dest="b", default="2026-09-01")
    ap.add_argument("--monthly", action="store_true", help="one summary row per month")
    ap.add_argument("--variant", choices=["as_is", "swap", "invert"], default="as_is")
    ap.add_argument("--compare", action="store_true",
                    help="as_is vs swapped SL/TP vs inverted direction, per month")
    args = ap.parse_args()
    if args.monthly:
        return monthly(args.a, args.b, args.variant)
    if args.compare:
        return compare(args.a, args.b)
    frm, to = datetime.strptime(args.a, "%Y-%m-%d"), datetime.strptime(args.b, "%Y-%m-%d")
    df = load(frm - timedelta(days=20), to)
    if df.empty:
        print(f"no data {frm.date()}..{to.date()}")
        return 2
    win = df[df.index >= frm]
    print(f"window {frm.date()}..{to.date()}  bars={len(win)}  "
          f"warmup={len(df)-len(win)}\n"
          f"params: RR={RR}  lots={FIXED_LOTS} (FIXED)  SL={SL_ATR_MULT}xATR  "
          f"partial {PARTIAL_PCT*100:.0f}% at {PARTIAL_TP_R}R  "
          f"spread {SPREAD_PIPS}p + slip {SLIPPAGE_PIPS}p/side\n")
    report(run(win), "EA AS WRITTEN (look-ahead, fixed 0.01 lot, RR 1.1)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
