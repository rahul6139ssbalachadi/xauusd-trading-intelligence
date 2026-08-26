"""V9 hypothesis probe: H4 trend as filter for M15 pullback entries.

Checks whether H4 directional bias + M15 EMA/pullback context produces
any edge. Read-only against db/trading.db.
"""
import sqlite3
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from market_data import config as cfg

DB = cfg.PROJECT_ROOT / cfg.load_settings()["paths"]["db"]


def load(tf: str):
    con = sqlite3.connect(DB)
    df = pd.read_sql_query(
        "SELECT ts_broker_epoch, open, high, low, close, tick_volume, spread "
        "FROM market_data WHERE symbol='XAUUSD' AND timeframe=? AND source='mt5' "
        "ORDER BY ts_broker_epoch",
        con, params=(tf,))
    con.close()
    df["ts"] = pd.to_datetime(df["ts_broker_epoch"], unit="s", utc=True)
    return df


def resample(df, rule):
    out = df.set_index("ts").resample(rule).apply(
        {"open": "first", "high": "max", "low": "min", "close": "last",
         "tick_volume": "sum", "spread": "mean"}).dropna().reset_index()
    return out


def ema(s, span):
    return s.ewm(span=span, adjust=False, min_periods=span).mean()


def main():
    m15 = load("M15")
    h1 = resample(m15, "1h")
    h4 = resample(m15, "4h")

    # H4 EMA trend direction
    h4["ema34"] = ema(h4["close"], 34)
    h4["ema89"] = ema(h4["close"], 89)
    h4["trend"] = np.where(h4["ema34"] > h4["ema89"], "up", "down")

    # M15 EMA
    m15["ema34"] = ema(m15["close"], 34)
    m15["ema89"] = ema(m15["close"], 89)
    m15["atr14"] = (m15["high"].rolling(14).max() - m15["low"].rolling(14).min()) / 14  # rough
    # proper ATR
    prev_close = m15["close"].shift(1)
    tr = pd.concat([
        (m15["high"] - m15["low"]),
        (m15["high"] - prev_close).abs(),
        (m15["low"] - prev_close).abs()
    ], axis=1).max(axis=1)
    m15["atr14"] = tr.ewm(alpha=1/14, adjust=False, min_periods=14).mean()

    # Merge H4 trend to M15 by timestamp (as-of)
    h4_trend = h4[["ts", "trend"]].sort_values("ts")
    m15 = m15.sort_values("ts")
    merged = pd.merge_asof(m15, h4_trend, on="ts", direction="backward")

    m15["h4_trend"] = merged["trend"]

    # Pullback signal: M15 price near EMA34 (within 0.5*ATR), direction follows H4 trend
    m15["dist_to_ema"] = (m15["close"] - m15["ema34"]) / m15["atr14"]
    m15["ret1"] = m15["close"].pct_change().shift(-1)

    # Buy setup: H4 up, M15 pullback (dist between -0.5 and 0.5, i.e. price near EMA)
    buy = m15[(m15["h4_trend"] == "up") & (m15["dist_to_ema"] > -0.5) & (m15["dist_to_ema"] < 0.5) & (m15["ema34"] < m15["close"])]
    sell = m15[(m15["h4_trend"] == "down") & (m15["dist_to_ema"] > -0.5) & (m15["dist_to_ema"] < 0.5) & (m15["ema34"] > m15["close"])]

    for name, sub in [("BUY (H4 up pullback)", buy), ("SELL (H4 down pullback)", sell)]:
        if len(sub) == 0:
            print(f"{name}: 0 signals")
            continue
        r = sub["ret1"].dropna()
        print(f"{name}: n={len(sub)}, next-bar win%={(r>0).mean()*100:.1f}, avg_ret={r.mean()*1e2:+.3f}%")

    # Also: H4 trend continuation — does H4 trend hold for N bars?
    for n in [1, 2, 3, 5, 8, 13, 21, 34]:
        ret = m15["ret1"].copy()
        up_mask = (m15["h4_trend"] == "up").values
        dn_mask = (m15["h4_trend"] == "down").values
        # Check n-bar forward return
        ret_n = m15["close"].pct_change(n).shift(-n)
        up_ret = ret_n[up_mask].dropna()
        dn_ret = ret_n[dn_mask].dropna()
        print(f"H4-up {n:2d}-bar fwd: win%={(up_ret>0).mean()*100:.1f} avg={up_ret.mean()*1e2:+.3f}%  "
              f"H4-down {n:2d}-bar: win%={(dn_ret>0).mean()*100:.1f} avg={dn_ret.mean()*1e2:+.3f}%")

    print()
    print("=== H4 daily seasonality ===")
    m15["dow"] = m15["ts"].dt.dayofweek  # 0=Mon
    for d in range(5):
        day_df = m15[m15["dow"] == d]
        dr = day_df["ret1"].dropna()
        print(f"  {['Mon','Tue','Wed','Thu','Fri'][d]}: n={len(day_df)}, win%={(dr>0).mean()*100:.1f}, avg={dr.mean()*1e2:+.3f}%")

    print()
    print("=== H4 session volatility ===")
    m15["hour"] = m15["ts"].dt.hour
    # Broker hour
    m15["broker_hour"] = (m15["hour"] - 3) % 24
    vol_by_hour = m15.groupby("broker_hour")["atr14"].mean()
    print("M15 ATR by broker hour (top 5):")
    print(vol_by_hour.sort_values(ascending=False).head(5).to_string())


if __name__ == "__main__":
    main()
