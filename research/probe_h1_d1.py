"""Descriptive probes on H1 and D1 timeframes.

The M5/M15 analysis showed no edge because the ~0.11% round-trip cost
floor swallows all forward drift. On H1 and D1, price moves are much larger
in absolute terms, so the same cost structure may leave room for edge.

Tests:
  1. H1 momentum continuation: next-N-bar forward return after top/bottom
     5% momentum bars
  2. D1 momentum continuation: same at daily scale
  3. H1 EMA pullback entries
  4. D1 directional drift (long bias / short bias)
  5. H1 session volatility profile

Read-only: queries db/trading.db only.
"""
import sqlite3
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from market_data import config as cfg
from indicators import ema, atr

DB = cfg.PROJECT_ROOT / cfg.load_settings()["paths"]["db"]


def load(tf: str) -> pd.DataFrame:
    con = sqlite3.connect(DB)
    df = pd.read_sql_query(
        "SELECT ts_broker_epoch, open, high, low, close, tick_volume, spread "
        "FROM market_data WHERE symbol='XAUUSD' AND timeframe=? AND source='mt5' "
        "ORDER BY ts_broker_epoch",
        con, params=(tf,))
    con.close()
    df["ts"] = pd.to_datetime(df["ts_broker_epoch"], unit="s", utc=True)
    return df


def momentum_probe(df: pd.DataFrame, name: str, horizons: list[int]):
    """Test forward returns after extreme momentum bars."""
    df = df.copy()
    df["ret"] = df["close"].pct_change()
    df["bar_ret"] = (df["close"] - df["open"]) / df["open"]

    avg_price = df["close"].mean()

    print(f"\n=== {name}: momentum continuation ===")
    print(f"  {len(df)} bars, avg price={avg_price:.2f}")

    # Top/bottom 5% bars by body return
    thr_hi = df["bar_ret"].quantile(0.95)
    thr_lo = df["bar_ret"].quantile(0.05)
    up = df[df["bar_ret"] >= thr_hi]
    dn = df[df["bar_ret"] <= thr_lo]

    print(f"  Top 5% up bars: n={len(up)}, threshold={thr_hi*100:.2f}%")
    print(f"  Bot 5% down bars: n={len(dn)}, threshold={thr_lo*100:.2f}%")

    for n in horizons:
        if n >= len(df):
            continue
        fwd_ret = df["close"].pct_change(n).shift(-n)
        r_up = fwd_ret.reindex(up.index).dropna()
        r_dn = fwd_ret.reindex(dn.index).dropna()
        wr_up = (r_up > 0).mean() if len(r_up) else 0
        wr_dn = (r_dn > 0).mean() if len(r_dn) else 0
        avg_up = r_up.mean() * 1e2 if len(r_up) else 0
        avg_dn = r_dn.mean() * 1e2 if len(r_dn) else 0

        # Cost estimate: ~2x spread as fraction of price
        avg_spread = df["spread"].mean()  # in points
        cost_frac = avg_spread * 2 * 0.01 / avg_price  # round-trip as frac
        cost_pct = cost_frac * 100

        net_up = avg_up - cost_pct
        net_dn = avg_dn - cost_pct

        print(f"  next-{n:2d}: up win%={wr_up*100:.1f} avg={avg_up:+.3f}% "
              f"net_after_cost={net_up:+.3f}%  |  "
              f"dn win%={wr_dn*100:.1f} avg={avg_dn:+.3f}% "
              f"net_after_cost={net_dn:+.3f}%  "
              f"(cost={cost_pct:.3f}%)")


def ema_pullback_probe(df: pd.DataFrame, name: str, spans: list[int]):
    """Test EMA pullback entries."""
    df = df.copy()
    df["close"] = df["close"]
    df["high"] = df["high"]
    df["low"] = df["low"]
    avg_price = df["close"].mean()
    avg_spread = df["spread"].mean()
    cost_frac = avg_spread * 2 * 0.01 / avg_price
    cost_pct = cost_frac * 100

    print(f"\n=== {name}: EMA pullback entries ===")

    for span in spans:
        df[f"ema{span}"] = ema(df["close"], span)
        df["atr14"] = atr(df["high"], df["low"], df["close"], 14)
        # Near EMA: |close - ema| / atr < 0.5
        df["dist"] = (df["close"] - df[f"ema{span}"]) / df["atr14"]
        near = df[(df["dist"].abs() < 0.5) & df["dist"].notna() & df[f"ema{span}"].notna()]
        n = len(near)
        if n == 0:
            continue
        # Forward 3-bar return
        fwd = df["close"].pct_change(3).shift(-3).reindex(near.index).dropna() * 100
        fwd1 = df["close"].pct_change().shift(-1).reindex(near.index).dropna() * 100
        win3 = (fwd > 0).mean() * 100
        avg3 = fwd.mean() if len(fwd) else 0
        net3 = avg3 - cost_pct
        win1 = (fwd1 > 0).mean() * 100
        avg1 = fwd1.mean() if len(fwd1) else 0
        net1 = avg1 - cost_pct
        print(f"  EMA{span}: n={n}, 3-bar: win%={win3:.1f} avg={avg3:+.3f}% "
              f"net={net3:+.3f}% | 1-bar: win%={win1:.1f} avg={avg1:+.3f}% net={net1:+.3f}%")


def directional_drift_probe(df: pd.DataFrame, name: str):
    """Test long/short directional bias."""
    df = df.copy()
    df["ret"] = df["close"].pct_change()
    df["bar_ret"] = (df["close"] - df["open"]) / df["open"]
    avg_price = df["close"].mean()
    avg_spread = df["spread"].mean()
    cost_pct = avg_spread * 2 * 0.01 / avg_price * 100

    print(f"\n=== {name}: directional drift ===")

    # Overall up/down ratio
    up_days = (df["bar_ret"] > 0).sum()
    dn_days = (df["bar_ret"] < 0).sum()
    print(f"  Up bars: {up_days} ({up_days/len(df)*100:.1f}%)  "
          f"Down bars: {dn_days} ({dn_days/len(df)*100:.1f}%)")

    # Drift over various horizons
    for n in [1, 2, 3, 5, 8, 13, 21, 34]:
        fwd = df["ret"].shift(-n).dropna()
        avg = fwd.mean() * 100
        win = (fwd > 0).mean() * 100
        net = avg - cost_pct
        print(f"  {n:2d}-bar fwd: avg={avg:+.3f}% win%={win:.1f} net_after_cost={net:+.3f}% "
              f"{'EDGE' if net > 0 else 'NO EDGE'}")


def main():
    h1 = load("H1")
    d1 = load("D1")
    m15 = load("M15")

    # Resample H1 to get cleaner picture
    print("=" * 70)
    print("EXTENDED DATA PROBES — H1 (10yr) + D1 (10yr) + M15 (2yr)")
    print("=" * 70)

    momentum_probe(h1, "H1", [1, 2, 3, 5, 8, 13, 21, 34])
    momentum_probe(d1, "D1", [1, 2, 3, 5, 8, 13])

    ema_pullback_probe(h1, "H1", [21, 34, 55, 89, 144, 233])
    ema_pullback_probe(d1, "D1", [5, 10, 20, 30, 50, 89])

    directional_drift_probe(h1, "H1")
    directional_drift_probe(d1, "D1")
    directional_drift_probe(m15, "M15")

    # Volatility regime: is volatility clustered?
    print("\n=== H1 volatility regime ===")
    h1["atr14"] = atr(h1["high"], h1["low"], h1["close"], 14)
    h1["atr_pct"] = h1["atr14"] / h1["close"] * 100
    print(f"  ATR14/mean(close) = {h1['atr_pct'].mean():.3f}% "
          f"(median {h1['atr_pct'].median():.3f}%, "
          f"std {h1['atr_pct'].std():.3f}%)")
    print(f"  Low vol (pct < 25th): {h1['atr_pct'].quantile(0.25):.3f}%")
    print(f"  High vol (pct > 75th): {h1['atr_pct'].quantile(0.75):.3f}%")

    # Volatility clustering: does high vol follow high vol?
    h1["vol_z"] = (h1["atr_pct"] - h1["atr_pct"].mean()) / h1["atr_pct"].std()
    h1["vol_next"] = h1["vol_z"].shift(-1)
    high_vol = h1[h1["vol_z"] > 1.0]
    low_vol = h1[h1["vol_z"] < -1.0]
    print(f"\n  High vol (z>1) next-bar: n={len(high_vol)}, "
          f"avg_next_vol_z={h1['vol_z'].reindex(high_vol.index).mean():.3f}")
    print(f"  Low vol (z<-1) next-bar: n={len(low_vol)}, "
          f"avg_next_vol_z={h1['vol_z'].reindex(low_vol.index).mean():.3f}")
    # Mean reversion in volatility?
    print(f"  Vol mean-reversion (lag-1 autocorr): {h1['vol_z'].autocorr(1):.3f}")

    print("\nPROBE COMPLETE")


if __name__ == "__main__":
    main()
