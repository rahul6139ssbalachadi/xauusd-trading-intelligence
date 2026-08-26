"""V10 hypothesis probe: H4 trend + M15 momentum breakout.

Tests whether H4 uptrend (EMA34 > EMA89) + M15 strong up-bar breakout
produces a tradable edge with positive expectancy after costs.
Read-only against db/trading.db.
"""
import sqlite3
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from market_data import config as cfg
from indicators import ema, atr, rsi

DB = cfg.PROJECT_ROOT / cfg.load_settings()["paths"]["db"]


def load(tf):
    con = sqlite3.connect(DB)
    df = pd.read_sql_query(
        "SELECT ts_broker_epoch, open, high, low, close, tick_volume, spread "
        "FROM market_data WHERE symbol='XAUUSD' AND timeframe=? AND source='mt5' "
        "ORDER BY ts_broker_epoch", con, params=(tf,))
    con.close()
    df["ts"] = pd.to_datetime(df["ts_broker_epoch"], unit="s", utc=True)
    return df


def resample(df, rule):
    out = df.set_index("ts").resample(rule).apply(
        {"open": "first", "high": "max", "low": "min", "close": "last",
         "tick_volume": "sum", "spread": "mean"}).dropna().reset_index()
    return out


def main():
    m15 = load("M15")
    h1 = resample(m15, "1h")
    h4 = resample(m15, "4h")

    # H4 trend: EMA34 vs EMA89
    h4["ema34"] = ema(h4["close"], 34)
    h4["ema89"] = ema(h4["close"], 89)
    h4["trend"] = np.where(h4["ema34"] > h4["ema89"], "up",
                    np.where(h4["ema34"] < h4["ema89"], "down", "flat"))

    # Merge H4 trend to M15 (as-of)
    h4_trend = h4[["ts", "trend"]].sort_values("ts")
    m15 = m15.sort_values("ts").reset_index(drop=True)
    m15_merged = pd.merge_asof(m15, h4_trend, on="ts", direction="backward")
    m15["h4_trend"] = m15_merged["trend"]

    # M15 features
    close = m15["close"]
    high = m15["high"]
    low = m15["low"]
    m15["ema13"] = ema(close, 13)
    m15["ema34"] = ema(close, 34)
    m15["ema89"] = ema(close, 89)
    m15["rsi14"] = rsi(close, 14)
    m15["atr14"] = atr(high, low, close, 14)

    # Momentum breakout: M15 close breaks above ema13 with momentum
    m15["body"] = (m15["close"] - m15["open"]) / m15["open"]
    m15["bar_range"] = (m15["high"] - m15["low"]) / m15["close"]

    # Next-bar forward return
    m15["ret1"] = m15["close"].pct_change().shift(-1)
    m15["ret3"] = m15["close"].pct_change(3).shift(-3)
    m15["ret5"] = m15["close"].pct_change(5).shift(-5)

    # Spread cost (2x entry + exit, ~2 pips each way = ~4 pips = 0.4% for XAUUSD at ~1000... actually 4pips/10000)
    avg_price = m15["close"].mean()
    spread_cost = m15["spread"].mean() * 0.01 * 2  # round trip in $
    spread_pips = m15["spread"].mean() * 0.01 / 0.10
    print(f"Avg M15 price: {avg_price:.2f}, avg spread: {spread_pips:.2f} pips, round-trip cost: ${spread_cost:.3f}")
    # At ~$2700/oz, 1 pip = $0.10/lot but for direction: 4 pips ~$0.40 per lot, ~0.015%
    print(f"  4-pip round trip as % of price: {4/avg_price*100:.4f}%")

    print()

    # Signal: H4 uptrend + M15 breakout up (close > ema13, body > 0.3% of price, RSI < 70)
    buy_signal = (
        (m15["h4_trend"] == "up") &
        (m15["close"] > m15["ema13"]) &
        (m15["body"] > 0.003) &  # >0.3% body
        (m15["rsi14"] < 70)
    )
    sell_signal = (
        (m15["h4_trend"] == "down") &
        (m15["close"] < m15["ema13"]) &
        (m15["body"] < -0.003) &
        (m15["rsi14"] > 30)
    )

    for horizon, col in [(1, "ret1"), (3, "ret3"), (5, "ret5")]:
        print(f"=== {horizon}-bar holding period ===")
        for sig_name, sig, direction in [("BUY (H4-up momentum)", buy_signal, "up"),
                                          ("SELL (H4-down momentum)", sell_signal, "dn")]:
            n = sig.sum()
            if n == 0:
                print(f"  {sig_name}: no signals")
                continue
            r = m15.loc[sig, col].dropna()
            wr = (r > 0).mean()
            avg = r.mean()
            # Average return per trade in $ (approx, per ounce)
            avg_usd = avg * avg_price
            print(f"  {sig_name}: n={n}, win%={wr*100:.1f}, avg_ret={avg*100:.4f}% (= ${avg_usd:+.2f}/oz), "
                  f"median_ret={r.median()*100:.4f}%")
            # Check if edge survives 4-pip cost
            net_avg = avg - (4 / avg_price)  # subtract round-trip cost as fraction of price
            print(f"    after 4-pip cost: net_avg_ret={net_avg*100:.4f}%  ({'EDGE' if net_avg > 0 else 'NO EDGE'})")

    print()
    # Also check: does breakout size matter? Stronger breakout = better follow-through?
    buy_strong = buy_signal & (m15["body"] > 0.006)
    buy_weak = buy_signal & (m15["body"] <= 0.006)
    print("=== Breakout magnitude split (BUY, 3-bar horizon) ===")
    for name, mask in [("STRONG (>0.6% body)", buy_strong), ("WEAK (0.3-0.6% body)", buy_weak)]:
        n = mask.sum()
        if n == 0:
            continue
        r = m15.loc[mask, "ret3"].dropna()
        print(f"  {name}: n={n}, win%={(r>0).mean()*100:.1f}, avg={r.mean()*100:.4f}%, "
              f"after cost avg={(r.mean() - 4/avg_price)*100:.4f}%")

    print()
    # Volatility regime: high ATR vs low ATR breakout
    m15["atr_percentile"] = m15["atr14"] / m15["atr14"].rolling(200).median()
    buy_highvol = buy_signal & (m15["atr_percentile"] > 1.0)
    buy_lowvol = buy_signal & (m15["atr_percentile"] <= 1.0)
    print("=== Volatility split for BUY (3-bar horizon) ===")
    for name, mask in [("HIGH vol (ATR > median)", buy_highvol), ("LOW vol (ATR < median)", buy_lowvol)]:
        n = mask.sum()
        if n == 0:
            continue
        r = m15.loc[mask, "ret3"].dropna()
        print(f"  {name}: n={n}, win%={(r>0).mean()*100:.1f}, avg={r.mean()*100:.4f}%, "
              f"after cost avg={(r.mean() - 4/avg_price)*100:.4f}%")

    print()
    print("PROBE COMPLETE")


if __name__ == "__main__":
    main()
