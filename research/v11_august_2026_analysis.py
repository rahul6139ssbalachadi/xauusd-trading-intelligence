"""V11 August 2026 analysis for user query.

User parameters:
- $1000 starting balance
- Fixed 0.05 lot size
- Win ratio 1:3 (risk:reward — profit = 3x risk on winners)
- Analyze August 2026 (all timeframes where applicable)

Uses the V11 D1 momentum strategy signals, runs them through the
backtester with fixed 0.05 lots, and reports August 2026 results.

Read-only: queries db/trading.db only.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from datetime import datetime

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from market_data import config as cfg
from backtest import compute_metrics

DB = cfg.PROJECT_ROOT / cfg.load_settings()["paths"]["db"]
PIP = 0.10  # XAUUSD 1 pip = 0.10 USD


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


def build_d1_features(df: pd.DataFrame) -> pd.DataFrame:
    from indicators import ema, atr
    df = df.copy()
    df["ema21"] = ema(df["close"], 21)
    df["ema55"] = ema(df["close"], 55)
    df["atr14"] = atr(df["high"], df["low"], df["close"], 14)
    df["body"] = (df["close"] - df["open"]) / df["open"]
    df["body_abs"] = df["body"].abs()
    df["body_pct"] = df["body_abs"].rolling(60, min_periods=20).rank(pct=True)
    df["d1_trend"] = np.where(
        df["ema21"] > df["ema55"], "bull",
        np.where(df["ema21"] < df["ema55"], "bear", "flat"))
    df["fwd1"] = df["close"].pct_change().shift(-1)
    return df


def compute_d1_signals(d1: pd.DataFrame, *,
                       body_pct_threshold=0.95, trend_filter=True,
                       min_atr_pct=0.008, rr=2.0, atr_mult_stop=1.5,
                       max_holding_d1=5) -> list[dict]:
    d1 = d1.copy()
    signals = []
    for i in range(len(d1)):
        row = d1.iloc[i]
        if i < 55:
            continue
        if trend_filter and row["d1_trend"] != "bull":
            continue
        if pd.isna(row["body_pct"]) or row["body_pct"] < body_pct_threshold:
            continue
        if row["body"] <= 0:
            continue
        if pd.isna(row["atr14"]) or row["atr14"] / row["close"] < min_atr_pct:
            continue
        entry_bar = i + 1
        if entry_bar >= len(d1):
            continue
        entry_price = d1.iloc[entry_bar]["open"]
        stop = entry_price - atr_mult_stop * row["atr14"]
        risk = entry_price - stop
        target = entry_price + risk * rr
        signals.append({
            "d1_bar": i,
            "entry_bar": entry_bar,
            "entry_ts": d1.iloc[entry_bar]["ts"],
            "entry_price": entry_price,
            "stop": stop,
            "target": target,
            "risk_pips": risk / PIP,
            "direction": "LONG",
            "d1_close": row["close"],
            "d1_body_pct": row["body_pct"],
            "d1_atr": row["atr14"],
        })
    return signals


def backtest_with_fixed_lots(d1: pd.DataFrame, signals: list[dict], *,
                             rr=2.0, atr_mult_stop=1.5, max_holding_d1=5,
                             lot_size=0.05, risk_ratio=3.0,
                             spread_pips=3.0, slippage_pips=1.0) -> pd.DataFrame:
    """Backtest with fixed 0.05 lots.
    Win ratio = 1:3 means profit is 3x the risk amount.
    """
    if not signals:
        return pd.DataFrame()

    cost_pips = 2 * slippage_pips + 2 * spread_pips  # round-trip
    cost_per_lot_per_pip = 10.0  # standard: $10/pip per lot for XAUUSD
    cost_per_lot = cost_pips * cost_per_lot_per_pip  # $ per lot round-trip

    trades = []
    for sig in signals:
        i = sig["entry_bar"]
        if i >= len(d1) - 1:
            continue
        entry_price = sig["entry_price"]
        stop = sig["stop"]
        target = sig["target"]
        risk_pips = sig["risk_pips"]
        max_j = min(i + max_holding_d1, len(d1) - 1)
        exited = False
        for j in range(i + 1, max_j + 1):
            jrow = d1.iloc[j]
            jhi = jrow["high"]
            jlo = jrow["low"]
            if jlo <= stop:
                exit_price = stop
                reason = "stop"
                exited = True
            elif jhi >= target:
                exit_price = target
                reason = "target"
                exited = True
            if exited:
                break
        if not exited:
            j = max_j
            exit_price = d1.iloc[j]["close"]
            reason = "end"

        gross_pips = (exit_price - entry_price) / PIP
        net_pips = gross_pips - cost_pips

        # With fixed 0.05 lots:
        # Each pip = $0.50 (0.05 lot * $10/pip/lot)
        gross_usd = gross_pips * cost_per_lot_per_pip * lot_size
        cost_usd = cost_per_lot * lot_size  # fixed round-trip cost
        net_usd = net_pips * cost_per_lot_per_pip * lot_size

        # With risk_ratio 1:3 for wins, losses are at -risk amount
        if reason == "target":
            # Win: profit = risk_amount * risk_ratio
            risk_amount = risk_pips * cost_per_lot_per_pip * lot_size - cost_usd
            profit_usd = risk_amount * risk_ratio
            loss_usd = -risk_amount - cost_usd
            net_usd = profit_usd
        elif reason == "stop":
            risk_amount = risk_pips * cost_per_lot_per_pip * lot_size
            net_usd = -risk_amount - cost_usd
        # For "end" (force close), use actual net_pips * lot value
        else:
            net_usd = net_pips * cost_per_lot_per_pip * lot_size

        entry_ts = d1.iloc[i]["ts"]
        exit_ts = d1.iloc[j]["ts"]

        trades.append({
            "entry_ts": entry_ts,
            "exit_ts": exit_ts,
            "entry_price": entry_price,
            "exit_price": exit_price,
            "risk_pips": risk_pips,
            "gross_pips": gross_pips,
            "net_pips": net_pips,
            "reason": reason,
            "lot_size": lot_size,
            "gross_usd": gross_usd,
            "cost_usd": cost_usd,
            "net_usd": net_usd,
            "holding_days": (exit_ts - entry_ts).days,
        })

    return pd.DataFrame(trades)


def main():
    print("=" * 70)
    print("V11 AUGUST 2026 ANALYSIS")
    print("Parameters: $1000 balance, 0.05 lots, 1:3 win ratio (risk:reward)")
    print("=" * 70)

    d1 = load("D1")
    print(f"Loaded D1: {len(d1)} bars ({d1['ts'].iloc[0]} to {d1['ts'].iloc[-1]})")

    d1 = build_d1_features(d1)

    # Best params from V11 research
    params = dict(
        body_pct_threshold=0.95, trend_filter=True, min_atr_pct=0.008,
        rr=3.0, atr_mult_stop=1.5, max_holding_d1=5)

    signals = compute_d1_signals(d1.copy(), **params)
    print(f"Total signals: {len(signals)}")

    # Run backtest with fixed lots, risk_reward 1:3
    trades_df = backtest_with_fixed_lots(
        d1, signals, lot_size=0.05, risk_ratio=3.0,
        **{k: params[k] for k in ["rr", "atr_mult_stop", "max_holding_d1"]})

    if trades_df.empty:
        print("No trades to analyze.")
        return

    # Filter for August 2026 trades
    trades_df["entry_month"] = trades_df["entry_ts"].dt.to_period("M")
    august_trades = trades_df[trades_df["entry_month"].astype(str) == "2026-08"]

    print(f"\n--- AUGUST 2026 TRADES ---")
    print(f"Trades entered in August 2026: {len(august_trades)}")

    if len(august_trades) > 0:
        for _, t in august_trades.iterrows():
            print(f"  Entry: {t['entry_ts'].strftime('%Y-%m-%d')} | "
                  f"Price: {t['entry_price']:.2f} | "
                  f"Risk: {t['risk_pips']:.1f} pips | "
                  f"Exit: {t['exit_ts'].strftime('%Y-%m-%d')} | "
                  f"Gross: {t['gross_pips']:.1f} pips | "
                  f"Net USD: ${t['net_usd']:.2f} | "
                  f"Reason: {t['reason']}")

        total_profit_loss = august_trades["net_usd"].sum()
        win_trades = august_trades[august_trades["net_usd"] > 0]
        loss_trades = august_trades[august_trades["net_usd"] <= 0]
        win_rate = len(win_trades) / len(august_trades) * 100

        print(f"\n--- AUGUST 2026 P&L SUMMARY ---")
        print(f"Starting balance: $1000.00")
        print(f"Total trades: {len(august_trades)}")
        print(f"Winning trades: {len(win_trades)} ({win_rate:.1f}%)")
        print(f"Losing trades: {len(loss_trades)} ({100-win_rate:.1f}%)")
        print(f"Total P&L: ${total_profit_loss:.2f}")
        print(f"Ending balance: ${1000 + total_profit_loss:.2f}")

        if len(win_trades) > 0:
            print(f"Avg win: ${win_trades['net_usd'].mean():.2f}")
        if len(loss_trades) > 0:
            print(f"Avg loss: ${loss_trades['net_usd'].mean():.2f}")

    else:
        print("No trades were entered during August 2026.")
        print("The V11 D1 momentum strategy triggers only ~82 times per decade.")

    # Also show all-time summary
    print(f"\n--- ALL-TIME SUMMARY ---")
    total_all = trades_df["net_usd"].sum()
    print(f"Total trades: {len(trades_df)}")
    print(f"Total P&L: ${total_all:.2f}")
    print(f"With $1000 starting balance: ending at ${1000 + total_all:.2f}")

    # Show monthly trade counts
    print(f"\n--- MONTHLY TRADE COUNTS (all time) ---")
    monthly = trades_df.groupby("entry_month").size()
    for period, count in monthly.items():
        print(f"  {period}: {count} trades")

    print(f"\n{'='*70}")
    print("ANALYSIS COMPLETE — read-only, no MT5 writes, no live trading")
    print("=" * 70)


if __name__ == "__main__":
    main()
