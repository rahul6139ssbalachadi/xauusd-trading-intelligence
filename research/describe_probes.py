"""Descriptive probes across higher timeframes to find new signal hypotheses.

Runs read-only against db/trading.db. NO trading, NO MT5.
"""
import sqlite3
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from market_data import config as cfg
from indicators import ema, rsi, atr

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


def resample(df: pd.DataFrame, rule: str) -> pd.DataFrame:
    out = df.set_index("ts").resample(rule).apply(
        {"open": "first", "high": "max", "low": "min", "close": "last",
         "tick_volume": "sum", "spread": "mean"})
    out = out.dropna().reset_index()
    return out


def main():
    m5 = load("M5")
    m15 = load("M15")
    h1 = resample(m15, "1h")
    h4 = resample(m15, "4h")
    d1 = resample(m15, "D")

    print(f"M5: {len(m5)} bars  ({m5.ts.iloc[0]} -> {m5.ts.iloc[-1]})")
    print(f"M15: {len(m15)} bars  ({m15.ts.iloc[0]} -> {m15.ts.iloc[-1]})")
    print(f"H1:  {len(h1)} bars  ({h1.ts.iloc[0]} -> {h1.ts.iloc[-1]})")
    print(f"H4:  {len(h4)} bars  ({h4.ts.iloc[0]} -> {h4.ts.iloc[-1]})")
    print(f"D1:  {len(d1)} bars  ({d1.ts.iloc[0]} -> {d1.ts.iloc[-1]})")
    print()

    # --- Momentum continuation across timeframes ---
    for name, df in [("M5", m5), ("M15", m15), ("H1", h1), ("H4", h4)]:
        df = df.copy()
        df["ret"] = df["close"].pct_change()
        df["bar_ret"] = (df["close"] - df["open"]) / df["open"]
        thr = df["bar_ret"].quantile(0.95)
        up = df[df["bar_ret"] >= thr]
        thr_lo = df["bar_ret"].quantile(0.05)
        dn = df[df["bar_ret"] <= thr_lo]
        print(f"=== {name}: top-5% up bars n={len(up)}, bot-5% down bars n={len(dn)} ===")
        for n in [1, 2, 3, 5, 8, 13]:
            if n >= len(df):
                continue
            ret_up = df["ret"].shift(-n).reindex(up.index).dropna()
            ret_dn = df["ret"].shift(-n).reindex(dn.index).dropna()
            wr_up = (ret_up > 0).mean() if len(ret_up) else 0
            wr_dn = (ret_dn > 0).mean() if len(ret_dn) else 0
            print(f"  next-{n:2d}: up avg={ret_up.mean()*1e2:+.2f}% win%={wr_up*100:.1f}  "
                  f"dn avg={ret_dn.mean()*1e2:+.2f}% win%={wr_dn*100:.1f}")
        print()

    # --- Volume analysis ---
    print("=== Volume analysis (M15) ===")
    m15v = m15.copy()
    m15v["vol_ma20"] = m15v["tick_volume"].rolling(20).mean()
    m15v["vol_ratio"] = m15v["tick_volume"] / m15v["vol_ma20"]
    m15v["ret1"] = m15v["close"].pct_change().shift(-1)
    hi_vol = m15v[m15v["vol_ratio"] > 1.5]
    lo_vol = m15v[m15v["vol_ratio"] < 0.7]
    print(f"  high vol (>1.5x MA20): n={len(hi_vol)} ({len(hi_vol)/len(m15v)*100:.1f}%)")
    print(f"    next-bar ret: avg={m15v.loc[hi_vol.index, 'ret1'].mean()*1e2:+.3f}%  "
          f"win%={(m15v.loc[hi_vol.index, 'ret1']>0).mean()*100:.1f}")
    print(f"  low vol (<0.7x MA20): n={len(lo_vol)} ({len(lo_vol)/len(m15v)*100:.1f}%)")
    print(f"    next-bar ret: avg={m15v.loc[lo_vol.index, 'ret1'].mean()*1e2:+.3f}%  "
          f"win%={(m15v.loc[lo_vol.index, 'ret1']>0).mean()*100:.1f}")
    print()

    # --- EMA pullback entry analysis ---
    print("=== EMA(34) pullback entries (M15) ===")
    for span in [21, 34, 55, 89]:
        m15v[f"ema{span}"] = ema(m15v["close"], span)
        # Pullback: price near EMA then continues in trend direction
        # Uptrend pullback: price drops to within 1 ATR below EMA, then bounces up
        m15v["atr14"] = atr(m15v["high"], m15v["low"], m15v["close"], 14)
        m15v["dist"] = (m15v["close"] - m15v[f"ema{span}"]) / m15v["atr14"]
        # Near EMA: |dist| < 0.5 ATR
        near = m15v[(m15v["dist"].abs() < 0.5) & (m15v["dist"].notna()) & m15v[f"ema{span}"].notna()]
        print(f"  EMA{span} pullback: n={len(near)} near-bars, "
              f"next-bar win%={(m15v['ret1'].reindex(near.index)>0).mean()*100:.1f}, "
              f"avg_next_ret={m15v['ret1'].reindex(near.index).mean()*1e2:+.2f}%")
        # Directional pullback: in uptrend (price > EMA), pullback near, then continue up
        up_near = m15v[(m15v["dist"] > -0.5) & (m15v["dist"] < 0.5) & (m15v[f"ema{span}"] < m15v["close"])]
        dn_near = m15v[(m15v["dist"] > -0.5) & (m15v["dist"] < 0.5) & (m15v[f"ema{span}"] > m15v["close"])]
        if len(up_near) > 0:
            print(f"    uptrend pullback: n={len(up_near)}, win%={(m15v['ret1'].reindex(up_near.index)>0).mean()*100:.1f}, avg={m15v['ret1'].reindex(up_near.index).mean()*1e2:+.2f}%")
        if len(dn_near) > 0:
            print(f"    downtrend pullback: n={len(dn_near)}, win%={(m15v['ret1'].reindex(dn_near.index)>0).mean()*100:.1f}, avg={m15v['ret1'].reindex(dn_near.index).mean()*1e2:+.2f}%")

    print()

    # --- Opening range analysis ---
    print("=== Opening range breakout (M5, first bar of each session) ===")
    m5["hour"] = m5["ts"].dt.hour
    m5["session"] = np.select([
        (m5["hour"] >= 0) & (m5["hour"] < 7),
        (m5["hour"] >= 7) & (m5["hour"] < 13),
        (m5["hour"] >= 13) & (m5["hour"] < 21),
    ], ["asia", "london", "newyork"], default="quiet")
    m5["ret1"] = m5["close"].pct_change().shift(-1)
    # Session change detection
    m5["sess_change"] = m5["session"].astype(str) != m5["session"].astype(str).shift(1)
    for sess in ["london", "newyork"]:
        sc = m5[(m5["session"].astype(str) == sess) & m5["sess_change"]]
        if len(sc) == 0:
            continue
        # First 3 bars of session
        idxs = []
        for ts in sc["ts"].values:
            mask = (m5["ts"] >= pd.Timestamp(ts, tz="UTC")) & (m5["session"].astype(str) == sess)
            sess_bars = m5[mask].head(5)
            idxs.extend(sess_bars.index[:3])
        first3 = m5.loc[idxs]
        first_bar = first3.groupby("ts").first()
        print(f"  {sess} session open ({len(sc)} sessions):")
        print(f"      first-bar win%={(m5['ret1'].reindex(first_bar.index)>0).mean()*100:.1f}, avg={m5['ret1'].reindex(first_bar.index).mean()*1e2:+.2f}%")

    print()
    print("PROBE COMPLETE.")


if __name__ == "__main__":
    main()
