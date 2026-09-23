"""Debug H1 signal conditions for August 2026."""
import sqlite3, sys
from pathlib import Path
import numpy as np, pandas as pd
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from market_data import config as cfg
from indicators import ema, atr

DB = cfg.PROJECT_ROOT / cfg.load_settings()["paths"]["db"]
con = sqlite3.connect(DB)
df = pd.read_sql_query(
    "SELECT ts_broker_epoch, open, high, low, close FROM market_data "
    "WHERE symbol='XAUUSD' AND timeframe='H1' AND source='mt5' ORDER BY ts_broker_epoch",
    con)
con.close()
df["ts"] = pd.to_datetime(df["ts_broker_epoch"], unit="s", utc=True)
df["ts_broker"] = df["ts"] + pd.Timedelta(hours=3)

# Build features on full dataset
df["ema21"] = ema(df["close"], 21)
df["ema55"] = ema(df["close"], 55)
df["atr14"] = atr(df["high"], df["low"], df["close"], 14)
df["body"] = (df["close"] - df["open"]) / df["open"]
df["body_abs"] = df["body"].abs()
df["body_pct"] = df["body_abs"].rolling(60, min_periods=20).rank(pct=True)
df["d1_trend"] = np.where(df["ema21"] > df["ema55"], "bull",
    np.where(df["ema21"] < df["ema55"], "bear", "flat"))

# Slice August 2026
aug = df[(df["ts_broker"] >= "2026-08-01") & (df["ts_broker"] < "2026-09-01")].reset_index(drop=True)
print(f"August 2026 H1: {len(aug)} bars")
print(f"  d1_trend counts: {aug['d1_trend'].value_counts().to_dict()}")
print(f"  body_pct non-null: {aug['body_pct'].notna().sum()}")
print(f"  body_pct >= 0.95: {(aug['body_pct'] >= 0.95).sum()}")
print(f"  body > 0: {(aug['body'] > 0).sum()}")
print(f"  atr14/close >= 0.004: {(aug['atr14']/aug['close'] >= 0.004).sum()}")
print(f"  atr14/close stats: min={aug['atr14'].min()/aug['close'].min():.5f}, "
      f"mean={aug['atr14'].mean()/aug['close'].mean():.5f}, "
      f"max={aug['atr14'].max()/aug['close'].max():.5f}")

# All conditions combined
mask = ((aug["d1_trend"] == "bull") &
        (aug["body_pct"] >= 0.95) &
        (aug["body"] > 0) &
        (aug["atr14"]/aug["close"] >= 0.004))
print(f"  ALL conditions: {mask.sum()}")

# Show a few bars with high body_pct
print(f"\n  Top 5 body_pct bars in Aug 2026:")
top = aug.nlargest(5, "body_pct")[["ts_broker", "body", "body_pct", "d1_trend", "atr14", "close"]]
for _, r in top.iterrows():
    print(f"    {r['ts_broker']}: body={r['body']:.5f} pct={r['body_pct']:.3f} trend={r['d1_trend']} atr={r['atr14']:.2f} close={r['close']:.2f}")
