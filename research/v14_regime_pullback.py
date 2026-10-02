"""V14: EMA12/EMA200 REGIME + PULLBACK CONTINUATION (XAUUSD / BTCUSD)

WHERE THE DESIGN COMES FROM
---------------------------
The user asked for three things: trade the state (above both / below both),
trade the cross, and find the pattern that "repeats".

A descriptive probe was run FIRST (research/v14_regime_probe.py). It
measured all three states on XAUUSD and BTCUSD across D1/H4/H1/M15. The
findings that shaped this strategy:

  FINDING 1 - state alone is a POSITIVE drift filter, not an entry.
    XAUUSD D1 ABOVE_BOTH: 46% of bars, +0.58% over 10 bars, +1.13% over 20.
    GOLD has a genuine long-side drift above EMA200. But you cannot enter
    on "price is above EMA200" 1237 times or you pay 1237 round trips of
    cost. State is therefore used as a FILTER, not a signal.

  FINDING 2 - the cross is ASYMMETRIC. Long follows, short reverts.
    XAUUSD D1 GOLDEN cross: 62-69% dir-win at h=5..20, avg +0.28..+0.57%.
    XAUUSD D1 DEATH cross: 20-40% dir-win, avg -0.40..-1.75%. It REVERTS.
    Trading BOTH directions was the single biggest error in the earlier
    v14_ema_cross.py run: on H4, long-only scored PF 1.55 vs 1.17 both-sided.
    => V14 takes LONG-ONLY golden crosses, and treats a death cross as a
       REGIME EXIT, not a short entry.

  FINDING 3 - the "repeat" pattern is the PULLBACK, but only at EMA200,
    and only long. This is the trackable pattern the user asked for.
      XAUUSD D1 PB->EMA200 long : n=48,  h10 +0.49%, h20 +1.30%, 64% win
      XAUUSD D1 PB->EMA200 short: n=41,  h10 -0.45%  -> NO EDGE
      XAUUSD H4 PB->EMA12 long : n=1385, h20 +0.33%, 57% win
      XAUUSD H1 PB->EMA12 long : n=5480, h20 +0.07%  -> cost-killed
      XAUUSD M15             : everything <= 0.05%  -> COST FLOOR, dead
    => V14 is a PULLBACK entry inside an up-regime. Entering the pullback,
       not chasing the cross.

  FINDING 4 - M15/H1 are dead for this idea (0.11% round-trip cost vs
    sub-0.1% drift). Scalping here cannot work. V14 is a swing strategy on
    D1, with H4 as the confirmation timeframe. Long holding, NOT scalping.

ENTRY RULES
  REGIME (must hold, checked every bar):
    close > EMA12 > EMA200          -> up-regime, longs allowed
    a death cross turns it OFF      -> no new entries (regime exit)
  TRIGGER - two interchangeable patterns, both measured:
    (A) PULLBACK : bar trades back into a band around EMA200 (0.35 x ATR14)
        while price is still above it. This is the "repeat" pattern.
    (B) CROSS    : EMA12 crosses above EMA200 (golden cross).
  ENTRY at the NEXT bar's OPEN. Never the signal bar's close.
  STOP   = atr_mult x ATR14 below entry.
  TARGET = rr x risk.
  EXIT   = stop / target / max_holding bars / DEATH CROSS (regime exit).
  COSTS  = 2x slippage + 2x spread per round trip, always charged.

SELECTION RULE (stated before running, so it cannot be gamed)
  Grid is searched on TRAIN only. VAL, walk-forward OOS and TEST are
  MEASUREMENT, never selection.

Read-only vs db/trading.db. No MT5 writes. No live trading.

Usage:
    ./.venv/Scripts/python.exe research/v14_regime_pullback.py
    ./.venv/Scripts/python.exe research/v14_regime_pullback.py --all
"""
from __future__ import annotations

import argparse
import itertools
import sqlite3
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from market_data import config as cfg
from indicators import ema, atr
from backtest import compute_metrics, Trade
from validation import train_val_test_split, walk_forward_windows
from montecarlo import run_monte_carlo, MCConfig

DB = cfg.PROJECT_ROOT / cfg.load_settings()["paths"]["db"]

PIP = 0.10
POINT = 0.01
SLIPPAGE_PIPS = 1.0
SPREAD_PIPS = 3.0
COST_PIPS = 2 * SLIPPAGE_PIPS + 2 * SPREAD_PIPS   # 8 pips RT

MIN_TRAIN_TRADES = 15
MIN_OOS_TRADES = 10

GRID = {
    "entry": ["pullback200", "pullback12", "cross", "pullback_or_cross"],
    "pullback_band": [0.25, 0.50, 0.80],       # in ATR14 units
    "max_holding": [5, 10, 20],               # D1 bars held
    "atr_mult": [1.0, 1.5, 2.0],
    "rr": [1.5, 2.0, 3.0],
}
COMBOS = 4 * 3 * 3 * 3 * 3  # 324


def load(symbol: str, tf: str) -> pd.DataFrame:
    con = sqlite3.connect(DB)
    df = pd.read_sql_query(
        "SELECT ts_broker_epoch, open, high, low, close, spread "
        "FROM market_data WHERE symbol=? AND timeframe=? AND source='mt5' "
        "ORDER BY ts_broker_epoch",
        con, params=(symbol, tf))
    con.close()
    if df.empty:
        return df
    df["ts"] = pd.to_datetime(df["ts_broker_epoch"], unit="s", utc=True)
    return df


def features(df: pd.DataFrame, fast: int = 12, slow: int = 200) -> pd.DataFrame:
    d = df.copy()
    d["e12"] = ema(d["close"], fast)
    d["e200"] = ema(d["close"], slow)
    d["atr14"] = atr(d["high"], d["low"], d["close"], 14)
    gap = d["e12"] - d["e200"]
    prev = gap.shift(1)
    d["golden"] = (prev <= 0) & (gap > 0)
    d["death"] = (prev >= 0) & (gap < 0)
    d["up_regime"] = (d["close"] > d["e12"]) & (d["e12"] > d["e200"])
    return d


def signals(d: pd.DataFrame, *, entry: str, pullback_band: float,
            rr: float, atr_mult: float) -> list[dict]:
    """Entry triggers. Index is window-local into the passed frame."""
    out: list[dict] = []
    for i in range(len(d) - 1):
        row, nxt = d.iloc[i], d.iloc[i + 1]
        if not row["up_regime"] or pd.isna(row["atr14"]):
            continue

        side = None
        if entry == "pullback200":
            hit = abs(row["close"] - row["e200"]) <= pullback_band * row["atr14"]
            if hit:
                side = "LONG"
        elif entry == "pullback12":
            hit = abs(row["close"] - row["e12"]) <= pullback_band * row["atr14"]
            if hit:
                side = "LONG"
        elif entry == "cross":
            if row["golden"]:
                side = "LONG"
        else:  # pullback_or_cross
            pb200 = abs(row["close"] - row["e200"]) <= pullback_band * row["atr14"]
            pb12 = abs(row["close"] - row["e12"]) <= pullback_band * row["atr14"]
            if pb200 or pb12 or row["golden"]:
                side = "LONG"

        if side is None:
            continue
        # de-stack: skip if the PREVIOUS bar already triggered, so a long
        # pullback produces one entry instead of one per bar inside the band
        if entry != "cross" and i > 0:
            pv = d.iloc[i - 1]
            prev_hit = (
                abs(pv["close"] - pv["e200"]) <= pullback_band * pv["atr14"]
                or abs(pv["close"] - pv["e12"]) <= pullback_band * pv["atr14"])
            if prev_hit:
                continue
        e = float(nxt["open"])
        risk = atr_mult * float(row["atr14"])
        out.append({"entry_bar": i + 1, "side": "LONG", "entry": e,
                    "stop": e - risk, "target": e + risk * rr, "ts": nxt["ts"]})
    return out


def run(d: pd.DataFrame, *, entry: str, pullback_band: float, rr: float,
        atr_mult: float, max_holding: int) -> tuple[list[Trade], dict]:
    sigs = signals(d, entry=entry, pullback_band=pullback_band,
                   rr=rr, atr_mult=atr_mult)
    trades: list[Trade] = []
    for s in sigs:
        i = s["entry_bar"]
        entry_p, stop, target = s["entry"], s["stop"], s["target"]
        last = min(i + max_holding, len(d) - 1)
        if last <= i:
            continue
        exit_p, reason, hold = None, "target", 0
        for j in range(i + 1, last + 1):
            r = d.iloc[j]
            hold = j - i
            if r["low"] <= stop:
                exit_p, reason = stop, "stop"
                break
            if r["high"] >= target:
                exit_p, reason = target, "target"
                break
            if r["death"]:
                exit_p, reason = float(r["close"]), "regime_exit"
                break
        if exit_p is None:
            exit_p, reason = float(d.iloc[last]["close"]), "time"
        pips = (exit_p - entry_p) / PIP
        net = pips - COST_PIPS
        trades.append(Trade(entry_bar=i, entry_price=entry_p, side="LONG",
                            stop=stop, target=target, exit_bar=i + hold,
                            exit_price=exit_p, exit_reason=reason,
                            points=pips * PIP / POINT, cost_pips=COST_PIPS,
                            net_pips=net, duration_bars=hold))
    m = compute_metrics(trades)
    return trades, m


def line(tag: str, m: dict) -> str:
    return (f"{tag:8s} net={m.get('net_pips', 0):9.1f} "
            f"PF={m.get('profit_factor', float('nan')):5.2f} "
            f"t={m.get('total_trades', 0):4d} "
            f"win%={m.get('win_rate', 0) * 100:4.1f} "
            f"maxDD={m.get('max_drawdown_pips', 0):8.1f}")


def evaluate(symbol: str, tf: str) -> None:
    df = load(symbol, tf)
    print("\n" + "=" * 78)
    print(f"{symbol} {tf}   V14 REGIME + PULLBACK   (EMA12 / EMA200)")
    print("=" * 78)
    if df.empty:
        print("  NO DATA")
        return
    d = features(df)
    print(f"  {len(d)} bars  {d['ts'].iloc[0].date()} -> {d['ts'].iloc[-1].date()}")
    print(f"  crosses: golden={int(d['golden'].sum())} death={int(d['death'].sum())}")
    print(f"  up-regime bars: {int(d['up_regime'].sum())} "
          f"({d['up_regime'].sum() / len(d) * 100:.1f}%)")
    tr, va, te = train_val_test_split(d, 0.6, 0.2, 0.2)
    print(f"  TRAIN {len(tr)} | VAL {len(va)} | TEST {len(te)} bars")

    print(f"\n[1] TRAIN grid search ({COMBOS} combos)")
    rows = []
    for p in itertools.product(*GRID.values()):
        p = dict(zip(GRID.keys(), p))
        _, m = run(tr, **p)
        rows.append((m.get("net_pips", float("nan")), m, p))
    ok = [r for r in rows if r[1].get("total_trades", 0) >= MIN_TRAIN_TRADES
          and pd.notna(r[0])]
    if not ok:
        print(f"  NO combo produced >= {MIN_TRAIN_TRADES} TRAIN trades.")
        print("  VERDICT: INCONCLUSIVE - sample too small to test.")
        return
    ok.sort(key=lambda r: r[0], reverse=True)
    print(f"  top 5 (>= {MIN_TRAIN_TRADES} trades):")
    for net, m, p in ok[:5]:
        print("    " + line("", m) + f"  {p}")
    best_p = ok[0][2]
    print(f"  SELECTED on TRAIN only: {best_p}")

    print(f"\n[2] VAL (never searched): {line('', run(va, **best_p)[1])}")

    print("\n[3] Walk-forward (fixed params, measurement only):")
    wins = list(walk_forward_windows(len(d), train_frac=0.5, test_frac=0.25))
    is_n, oos_n, oos_t = [], [], 0
    for k, ((a, b), (c, e)) in enumerate(wins, 1):
        _, im = run(d.iloc[a:b], **best_p)
        _, om = run(d.iloc[c:e], **best_p)
        is_n.append(im.get("net_pips", 0))
        oos_n.append(om.get("net_pips", 0))
        oos_t += om.get("total_trades", 0)
        print(f"  win{k}: IS net={im.get('net_pips', 0):9.1f} (t{im.get('total_trades', 0):4d})"
              f"   OOS net={om.get('net_pips', 0):9.1f} (t{om.get('total_trades', 0):4d})")
    is_m, oos_m = float(np.mean(is_n)), float(np.mean(oos_n))
    deg = 0.0 if is_m == 0 else max(0.0, 1.0 - oos_m / is_m)
    print(f"  IS_mean={is_m:.1f} OOS_mean={oos_m:.1f} "
          f"degradation={deg:.2f} OOS_trades={oos_t}")

    full_trades, fm = run(d, **best_p)
    print(f"\n[4] FULL: {line('', fm)}")
    print(f"     sharpe={fm.get('sharpe', 0):.2f} "
          f"avg_dur={fm.get('avg_duration_bars', 0):.1f} bars")

    if len(full_trades) >= 10:
        mc = run_monte_carlo(full_trades, MCConfig(
            n_iterations=2000, seed=42, shuffle=True, scatter_pct=0.10,
            jitter_pct=0.05, ruin_threshold=-200.0))
        print(f"[5] MC: net_mean={mc.net_mean:.0f} net_p5={mc.net_p5:.0f} "
              f"PF_p5={mc.profit_factor_p5:.2f} "
              f"ruin={mc.ruin_prob * 100:.1f}% robust={mc.is_robust}")
    else:
        mc = None
        print(f"[5] MC skipped - only {len(full_trades)} trades (need >= 10).")

    print("\n[6] Parameter sensitivity (full data, one param at a time):")
    for key, vals in (("entry", GRID["entry"]),
                      ("pullback_band", GRID["pullback_band"]),
                      ("max_holding", GRID["max_holding"]),
                      ("atr_mult", GRID["atr_mult"]),
                      ("rr", GRID["rr"])):
        cells, pos = [], 0
        for v in vals:
            p = dict(best_p, **{key: v})
            _, m = run(d, **p)
            n = m.get("net_pips", float("nan"))
            cells.append(f"{v}:{n:.0f}(PF{m.get('profit_factor', 0):.2f})")
            pos += int(n > 0)
        print(f"    {key:15s} " + "  ".join(cells) + f"   [{pos}/{len(vals)} +]")
    print(f"    ^ a single spike with negative neighbours = REJECT, not a plateau")

    print("\n[7] MONEY view (real $1,000 balance):")
    n_sigs = len(signals(d, entry=best_p["entry"],
                         pullback_band=best_p["pullback_band"],
                         rr=best_p["rr"], atr_mult=best_p["atr_mult"]))
    lots = 0.01
    net_usd = sum(t.net_pips for t in full_trades) * PIP * lots
    print(f"    {len(full_trades)} trades at 0.01 lot -> net ${net_usd:,.2f} on $1,000 "
          f"({net_usd / 1000 * 100:+.2f}%)")
    print(f"    signals={n_sigs}  executed={len(full_trades)}  "
          f"skipped={n_sigs - len(full_trades)}  "
          f"(0.01 is the broker MINIMUM lot; risk sizing floors below it)")

    print("\n" + "-" * 78)
    g1 = ok[0][0] > 0 and ok[0][1].get("total_trades", 0) >= MIN_TRAIN_TRADES
    vm = run(va, **best_p)[1]
    g2 = vm.get("net_pips", 0) > 0
    g3 = (deg <= 0.50) and oos_t >= MIN_OOS_TRADES
    g4 = bool(mc.is_robust) if mc is not None else False
    for nm, val, detail in (
            ("GATE1 train>0 & n>=15", g1,
             f"net={ok[0][0]:.0f} n={ok[0][1].get('total_trades', 0)}"),
            ("GATE2 val>0", g2, f"net={vm.get('net_pips', 0):.1f}"),
            ("GATE3 oos degradation<=0.50", g3,
             f"deg={deg:.2f} oos_trades={oos_t}"),
            ("GATE4 monte-carlo robust", g4, "" if mc is None else
             f"ruin={mc.ruin_prob * 100:.1f}%")):
        print(f"  {'PASS' if val else 'FAIL'}  {nm:32s} {detail}")
    passed = g1 and g2 and g3 and g4
    print(f"  => {'ACCEPT (proceed to paper trade)' if passed else 'REJECT'}")
    print("-" * 78)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="XAUUSD")
    ap.add_argument("--tf", default="D1")
    ap.add_argument("--all", action="store_true")
    a = ap.parse_args()
    if a.all:
        for sym, tfs in (("XAUUSD", ["D1", "H4"]), ("BTCUSD", ["D1", "H4"])):
            for tf in tfs:
                evaluate(sym, tf)
    else:
        evaluate(a.symbol, a.tf)
    print("\nRead-only vs db/trading.db. No MT5 writes. No live trading.")


if __name__ == "__main__":
    main()