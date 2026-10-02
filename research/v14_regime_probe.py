"""V14 PROBE: EMA12/EMA200 regime states and pullback behaviour.

DESCRIPTIVE ONLY. No grid search, no verdict. This answers three
questions BEFORE any backtest is written:

  Q1  How often is price ABOVE BOTH / BELOW BOTH / MIXED?
  Q2  After a cross, does price FOLLOW (continue) or REVERT?
  Q3  Does a pullback to EMA12 / EMA200 inside the regime offer a
      trackable re-entry (the "market repeats" hypothesis)?

Each state is reported with the cost floor of that timeframe, because
every prior reject in this repo (V1-V13) was a cost-floor problem, not
a signal problem.

Read-only vs db/trading.db.

Usage:
    ./.venv/Scripts/python.exe research/v14_regime_probe.py
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from market_data import config as cfg
from indicators import ema, atr

DB = cfg.PROJECT_ROOT / cfg.load_settings()["paths"]["db"]
# XAUUSD round trip ~0.11%: 2x slippage + 2x median spread. Used only as the
# yardstick for "is this drift big enough to trade" -- never as a P&L figure.
COST_PCT = 0.11
HORIZONS = (3, 5, 10, 20)


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


def regimes(df: pd.DataFrame, fast: int = 12, slow: int = 200) -> pd.DataFrame:
    d = df.copy()
    d["e12"] = ema(d["close"], fast)
    d["e200"] = ema(d["close"], slow)
    d["atr14"] = atr(d["high"], d["low"], d["close"], 14)

    gap = d["e12"] - d["e200"]
    prev = gap.shift(1)
    d["cross_up"] = (prev <= 0) & (gap > 0)
    d["cross_dn"] = (prev >= 0) & (gap < 0)

    above12 = d["close"] > d["e12"]
    above200 = d["close"] > d["e200"]
    d["state"] = np.select(
        [above12 & above200, ~above12 & ~above200],
        ["ABOVE_BOTH", "BELOW_BOTH"], default="MIXED")

    # bars since the last cross -> "regime age"
    idx = pd.Series(np.arange(len(d), dtype=float))
    any_cross = (d["cross_up"] | d["cross_dn"]).astype(float)
    any_cross[any_cross == 0] = np.nan
    d["bars_since_cross"] = idx.where(any_cross.notna()).ffill()

    # pullback: bar trades back to a band around an EMA, measured in ATRs
    # (a percentage band is meaningless here - 0.25% == the entire bar range)
    band = 0.35 * d["atr14"]
    d["pb200"] = ((d["close"] - d["e200"]).abs() <= band) & \
                 (d["e12"] > d["e200"]) & (d["close"] > d["e200"])
    d["pb12"] = ((d["close"] - d["e12"]).abs() <= band) & \
                (d["e12"] > d["e200"]) & (d["close"] > d["e12"])
    # mirrored bearish versions: price below both, trading up into the EMA
    d["pb200s"] = ((d["close"] - d["e200"]).abs() <= band) & \
                  (d["e12"] < d["e200"]) & (d["close"] < d["e200"])
    d["pb12s"] = ((d["close"] - d["e12"]).abs() <= band) & \
                 (d["e12"] < d["e200"]) & (d["close"] < d["e12"])
    return d


def fwd(d: pd.DataFrame) -> pd.DataFrame:
    d = d.copy()
    for h in HORIZONS:
        d[f"f{h}"] = d["close"].pct_change(h).shift(-h)
    return d


def state_table(d: pd.DataFrame, label: str) -> None:
    print(f"\n  Q1  regime distribution + unconditional forward drift [{label}]")
    tot = d["state"].value_counts(normalize=True) * 100
    for st in ("ABOVE_BOTH", "MIXED", "BELOW_BOTH"):
        share = tot.get(st, 0.0)
        row = d[d["state"] == st]
        bits = [f"{st:11s} {share:5.1f}%  n={len(row):6d}"]
        for h in HORIZONS:
            r = row[f"f{h}"].dropna()
            if len(r) < 30:
                bits.append(f"h{h}:n/a")
                continue
            a = r.mean() * 100
            bits.append(f"h{h}:{a:+.3f}%" if abs(a) > COST_PCT / 3 else f"h{h}:~0")
        print("    " + "  ".join(bits))


def cross_table(d: pd.DataFrame, label: str) -> None:
    print(f"\n  Q2  does price FOLLOW or REVERT after a cross? [{label}]")
    for nm, col, sgn in (("GOLDEN", "cross_up", 1), ("DEATH", "cross_dn", -1)):
        for h in HORIZONS:
            r = (d.loc[d[col], f"f{h}"].dropna() * sgn * 100)
            if len(r) < 5:
                print(f"    {nm:7s} h={h:2d}  n={len(r):3d}  (too few)")
                continue
            win = (r > 0).mean() * 100
            verdict = "FOLLOW" if r.mean() > COST_PCT / 3 else (
                "REVERT" if r.mean() < -COST_PCT / 3 else "FLAT")
            print(f"    {nm:7s} h={h:2d}  n={len(r):3d}  dir-win%={win:5.1f}  "
                  f"avg={r.mean():+.3f}%  med={r.median():+.3f}%  -> {verdict}")


def pullback_table(d: pd.DataFrame, label: str) -> None:
    print(f"\n  Q3  pullback-to-EMA re-entry, direction = side of EMA200 [{label}]")
    rows = (("PB->EMA200 long", "pb200", 1),
            ("PB->EMA12  long", "pb12", 1),
            ("PB->EMA200 short", "pb200s", -1),
            ("PB->EMA12  short", "pb12s", -1))
    for nm, col, sgn in rows:
        sel = d[col]
        bits = [f"    {nm:18s} n={int(sel.sum()):5d}"]
        for h in HORIZONS:
            r = d.loc[sel, f"f{h}"].dropna() * sgn * 100
            if len(r) < 30:
                bits.append(f"h{h}:n/a")
                continue
            bits.append(f"h{h}:{(r > 0).mean() * 100:4.0f}%/{r.mean():+.3f}%")
        print("  ".join(bits))


def report(symbol: str, tf: str) -> None:
    df = load(symbol, tf)
    print("\n" + "=" * 78)
    print(f"{symbol} {tf}   EMA12 / EMA200 regime probe")
    print("=" * 78)
    if df.empty:
        print("  NO DATA")
        return
    d = fwd(regimes(df))
    print(f"  {len(d)} bars  {d['ts'].iloc[0].date()} -> {d['ts'].iloc[-1].date()}")
    print(f"  crosses: golden={int(d['cross_up'].sum())} "
          f"death={int(d['cross_dn'].sum())}  "
          f"(~{(d['cross_up'].sum() + d['cross_dn'].sum()) / max(len(d) / 1000, 1):.1f}/1k bars)")
    a = d["atr14"].median()
    print(f"  median ATR14 = {a:.2f} price units; cost floor = {COST_PCT:.3f}%/RT")
    print(f"  cost as % of one ATR move = {COST_PCT / (a / d['close'].median() * 100):.1f}%")

    state_table(d, f"{symbol} {tf}")
    cross_table(d, f"{symbol} {tf}")
    pullback_table(d, f"{symbol} {tf}")


def main() -> None:
    for sym, tfs in (("XAUUSD", ["D1", "H4", "H1", "M15"]),
                     ("BTCUSD", ["D1", "H4", "H1"])):
        for tf in tfs:
            report(sym, tf)
    print("\nDescriptive only. No verdict, no grid search. "
          "Read-only vs db/trading.db.")


if __name__ == "__main__":
    main()