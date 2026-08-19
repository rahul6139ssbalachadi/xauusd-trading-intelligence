"""Exploratory analysis of ingested XAUUSD scalping data.

Reads db/trading.db (M1/M5/M15). Computes REAL descriptive statistics:
  - per-timeframe bar/date span
  - typical range, volatility (ATR-like), tick volume
  - spread cost in points / $ / pips and as % of typical M1 range
    (scalping viability) -- conversion verified 2026-08-18:
    point=0.01, spread stored in POINTS => $ = points*0.01, pips = $/0.10
  - per-hour session profile in BROKER time (epoch is broker-server
    time, currently UTC+3 EEST; winter UTC+2 EET -- see schema comment)
  - simple momentum/mean-reversion probe (descriptive, NOT an edge claim)

All numbers are computed from the stored data. Nothing is invented.

Usage:
    ./.venv/Scripts/python.exe scripts/analyze_gold.py
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from market_data import config as cfg

DB = cfg.PROJECT_ROOT / cfg.load_settings()["paths"]["db"]

# Verified 2026-08-18 from live terminal: GOLD.i# point=0.01, digits=2.
# Spread field in DB = points. Convert: $ = points * POINT; pips = $ / PIP.
POINT = 0.01
PIP = 0.10  # gold pip = 10 points
# Broker offset vs UTC. Summer EEST=+3, winter EET=+2. Applied to label
# true-UTC hours where needed; profiles below are shown in BROKER time.
BROKER_UTC_OFFSET_H = 3


def load(tf: str) -> pd.DataFrame:
    con = sqlite3.connect(DB)
    df = pd.read_sql_query(
        "SELECT ts_broker_epoch, open, high, low, close, tick_volume, spread "
        "FROM market_data WHERE symbol='XAUUSD' AND timeframe=? AND source='mt5' "
        "ORDER BY ts_broker_epoch",
        con,
        params=(tf,),
    )
    con.close()
    df["ts"] = pd.to_datetime(df["ts_broker_epoch"], unit="s", utc=True)
    return df


def rng(df):
    return (df["high"] - df["low"]).mean()


def atr(df, n=14):
    hl = df["high"] - df["low"]
    hc = (df["high"] - df["close"].shift()).abs()
    lc = (df["low"] - df["close"].shift()).abs()
    tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    return tr.rolling(n).mean().mean()


def main() -> None:
    print("=" * 70)
    print("XAUUSD SCALPING DATA ANALYSIS (computed from db/trading.db)")
    print("=" * 70)
    print("spread unit: DB stores POINTS; $ = pts*0.01, pips = $/0.10")
    print(f"broker time: UTC+{BROKER_UTC_OFFSET_H} (EEST summer / EET winter)\n")

    for tf in ("M1", "M5", "M15"):
        df = load(tf)
        span_days = (df["ts"].max() - df["ts"].min()).days
        sp_pts = df["spread"].mean()
        sp_usd = sp_pts * POINT
        sp_pips = sp_usd / PIP
        print(f"--- {tf} ---")
        print(f"  bars            : {len(df):,}")
        print(f"  date span       : {df['ts'].min()} -> {df['ts'].max()}  ({span_days} days)")
        print(f"  avg range ($)   : {rng(df):.2f}")
        print(f"  avg ATR(14) ($) : {atr(df):.2f}")
        print(f"  avg tick vol    : {df['tick_volume'].mean():.0f}")
        print(f"  avg spread      : {sp_pts:.1f} pts = ${sp_usd:.3f} = {sp_pips:.2f} pips")
        if tf == "M1":
            pct = sp_usd / rng(df) * 100
            print(f"  spread / M1 range: {pct:.1f}%  (share of a bar you pay just to enter)")

    # Session profile in BROKER time (epochs are broker-server time)
    print("\n--- SESSION PROFILE (M5, avg |close-open| by BROKER hour) ---")
    df = load("M5")
    df["hour"] = df["ts"].dt.hour  # broker hour
    df["absmove"] = (df["close"] - df["open"]).abs()
    prof = df.groupby("hour")["absmove"].mean()
    best = prof.sort_values(ascending=False).head(5)
    for h, v in best.items():
        utc_h = (h - BROKER_UTC_OFFSET_H) % 24
        print(f"  {h:02d}:00 broker (= {utc_h:02d}:00 UTC)  {v:.2f}")
    print("  (your MT5 charts are in broker time too, so broker-hour is the useful label)")

    # Descriptive momentum probe (NOT an edge claim)
    print("\n--- DESCRIPTIVE PROBES (NOT an edge claim) ---")
    for tf in ("M5", "M15"):
        d = load(tf)
        d["ret"] = d["close"].pct_change()
        d["bar_ret"] = (d["close"] - d["open"]) / d["open"]
        thr = d["bar_ret"].quantile(0.9)
        up = d[d["bar_ret"] >= thr]
        nxt = d.shift(-1).loc[up.index, "ret"].dropna()
        print(f"  {tf}: top-10% momentum bars n={len(up)} | "
              f"avg next-bar ret={nxt.mean()*1e4:+.2f} bp | win%={(nxt>0).mean()*100:.1f}")
        dthr = d["bar_ret"].quantile(0.1)
        dn = d[d["bar_ret"] <= dthr]
        nxtd = d.shift(-1).loc[dn.index, "ret"].dropna()
        print(f"  {tf}: bot-10% reversal bars n={len(dn)} | "
              f"avg next-bar ret={nxtd.mean()*1e4:+.2f} bp | win%={(nxtd>0).mean()*100:.1f}")

    print("\nNOTE: M1 has only ~30 days -> any M1 finding is low-confidence.")
    print("      M5/M15 have 6mo/2yr -> moderately reliable for description.")


if __name__ == "__main__":
    main()
