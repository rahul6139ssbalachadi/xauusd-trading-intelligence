"""V11 M15 Adaptation — same D1 logic applied to M15 timeframe.

Applies the EXACT same V11 D1 momentum strategy logic but on M15:
- EMA21 > EMA55 trend filter (bullish bias)
- Top 5% momentum bars (body percentile vs rolling window)
- 1.5x ATR14 stop
- 3R target (1:3 risk-reward)
- Fixed 0.05 lots, $1000 starting balance

Parameter adaptation for M15:
  The D1 strategy uses min_atr_pct=0.008 (ATR14/close >= 0.8%).
  On M15, ATR14/close averages 0.18% — the same 0.8% would eliminate
  ALL signals. To apply the same LOGIC to M15, we scale the ATR percentile
  filter: the D1 filter selects ATR14/close in the top ~60% of its range.
  On M15, we apply the same percentile filter (ATR14/close >= 25th pct).

Read-only: queries db/trading.db only. No MT5 writes.
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


def build_features(df: pd.DataFrame) -> pd.DataFrame:
    """Build V11-style features: EMA21/55 trend, body momentum percentile, ATR14."""
    df = df.copy()
    df["ema21"] = ema(df["close"], 21)
    df["ema55"] = ema(df["close"], 55)
    df["atr14"] = atr(df["high"], df["low"], df["close"], 14)

    # Body return
    df["body"] = (df["close"] - df["open"]) / df["open"]
    df["body_abs"] = df["body"].abs()

    # Momentum percentile: same 60-bar (or scaled) rolling rank as D1
    # On M15, 60 bars = 15 hours. D1's 60 bars = 60 days.
    # For M15 we use 240 bars = 4 days, equivalent-ish in trading activity
    df["body_pct"] = df["body_abs"].rolling(240, min_periods=120).rank(pct=True)

    # Trend: EMA21 > EMA55 = bullish (same as D1 V11)
    df["trend"] = np.where(
        df["ema21"] > df["ema55"], "bull",
        np.where(df["ema21"] < df["ema55"], "bear", "flat"))

    # ATR percentile (for M15-appropriate volatility filter)
    df["atr_pct"] = df["atr14"].rolling(240, min_periods=120).rank(pct=True)
    df["atr_pct_val"] = df["atr14"] / df["close"]

    return df


def compute_signals(df: pd.DataFrame, *,
                    body_pct_threshold: float = 0.95,
                    trend_filter: bool = True,
                    min_atr_pct_threshold: float = 0.50,  # 50th percentile (median vol) — scaled from D1's absolute 0.8%
                    rr: float = 3.0,
                    atr_mult_stop: float = 1.5,
                    max_holding: int = 5) -> list[dict]:
    """Compute V11 momentum signals on M15.

    Same logic as D1 V11:
    1. Trend filter: EMA21 > EMA55 (bullish)
    2. Strong momentum: body percentile > 0.95 (top 5%)
    3. Direction from body sign (bullish bar only, long bias)
    4. Volatility filter: ATR percentile >= threshold (scaled for M15)
    5. Entry at next bar open
    6. Stop = 1.5x ATR, Target = 3x risk
    7. Max holding = 5 bars
    """
    df = df.copy()
    warmup = 240  # need enough bars for body_pct + atr_pct rolling

    # Vectorized signal detection
    mask = (
        (df["trend"] == "bull") &
        (df["body"] > 0) &
        (df["body_pct"] >= body_pct_threshold) &
        (df["atr_pct"] >= min_atr_pct_threshold)
    )
    mask = mask.fillna(False)

    # Warmup exclusion
    mask.iloc[:warmup] = False

    signal_indices = df.index[mask].tolist()

    signals = []
    for idx in signal_indices:
        i = idx if isinstance(idx, int) else df.index.get_loc(idx)
        row = df.iloc[i]

        entry_bar = i + 1
        if entry_bar >= len(df):
            continue

        entry_price = df.iloc[entry_bar]["open"]
        stop = entry_price - atr_mult_stop * row["atr14"]
        risk = entry_price - stop
        target = entry_price + risk * rr
        risk_pips = risk / PIP

        signals.append({
            "entry_bar": entry_bar,
            "entry_ts": df.iloc[entry_bar]["ts"],
            "entry_price": entry_price,
            "stop": stop,
            "target": target,
            "risk_pips": risk_pips,
            "direction": "LONG",
            "atr14": row["atr14"],
            "body_pct": row["body_pct"],
        })

    return signals


def backtest_fixed_lots(df: pd.DataFrame, signals: list[dict], *,
                        rr=3.0, atr_mult_stop=1.5, max_holding=5,
                        lot_size=0.05, risk_ratio=3.0,
                        spread_pips=3.0, slippage_pips=1.0) -> pd.DataFrame:
    """Backtest with fixed 0.05 lots and 1:3 risk-reward.

    On M15, costs are: spread ~3 pips + slippage ~1 pip = ~4 pips
    per side, so ~8 pips round-trip.
    """
    if not signals:
        return pd.DataFrame()

    cost_pips = 2 * slippage_pips + 2 * spread_pips  # round-trip = 8.0 pips
    cost_per_lot_per_pip = 10.0  # $10/pip per lot for XAUUSD
    cost_per_lot = cost_pips * cost_per_lot_per_pip  # $80 per lot round-trip

    trades = []
    for sig in signals:
        i = sig["entry_bar"]
        if i >= len(df) - 1:
            continue

        entry_price = sig["entry_price"]
        stop = sig["stop"]
        target = sig["target"]
        risk_pips = sig["risk_pips"]

        max_j = min(i + max_holding, len(df) - 1)
        exited = False
        exit_price = None
        reason = None
        j = i

        for j in range(i + 1, max_j + 1):
            jrow = df.iloc[j]
            jhi = jrow["high"]
            jlo = jrow["low"]

            stop_hit = jlo <= stop
            target_hit = jhi >= target

            if stop_hit and target_hit:
                d_stop = abs(entry_price - stop)
                d_target = abs(target - entry_price)
                if d_stop <= d_target:
                    exit_price = stop
                    reason = "stop"
                else:
                    exit_price = target
                    reason = "target"
                exited = True
                break
            elif stop_hit:
                exit_price = stop
                reason = "stop"
                exited = True
                break
            elif target_hit:
                exit_price = target
                reason = "target"
                exited = True
                break

        if not exited:
            j = max_j
            exit_price = df.iloc[j]["close"]
            reason = "end"

        gross_pips = (exit_price - entry_price) / PIP
        net_pips = gross_pips - cost_pips

        # Fixed 0.05 lots, 1:3 RR for wins
        cost_usd = cost_per_lot * lot_size  # $4.00 round-trip per trade

        if reason == "target":
            # Win: profit = net_risk * 3 (1:3 risk-reward)
            risk_amount = risk_pips * cost_per_lot_per_pip * lot_size  # gross risk
            risk_amount_net = risk_amount - cost_usd  # net risk after cost
            profit_usd = risk_amount_net * risk_ratio  # 3x the net risk
            net_usd = profit_usd
        elif reason == "stop":
            # Loss: lose the risk amount + cost
            risk_amount = risk_pips * cost_per_lot_per_pip * lot_size
            net_usd = -risk_amount - cost_usd
        else:
            # Force close: use actual net_pips * lot value
            net_usd = net_pips * cost_per_lot_per_pip * lot_size

        entry_ts = df.iloc[i]["ts"]
        exit_ts = df.iloc[j]["ts"]

        trades.append({
            "entry_ts": entry_ts,
            "exit_ts": exit_ts,
            "entry_month": str(entry_ts.to_period("M")),
            "entry_price": entry_price,
            "exit_price": exit_price,
            "risk_pips": risk_pips,
            "gross_pips": gross_pips,
            "net_pips": net_pips,
            "reason": reason,
            "lot_size": lot_size,
            "net_usd": net_usd,
            "holding_bars": j - i,
        })

    return pd.DataFrame(trades)


def main():
    print("=" * 70)
    print("V11 M15 ADAPTATION — Same logic as D1 V11, applied to M15")
    print("Parameters: $1000 balance, 0.05 lots, 1:3 risk-reward")
    print("=" * 70)

    # Load M15 data
    m15 = load("M15")
    print(f"Loaded M15: {len(m15)} bars ({m15['ts'].iloc[0]} to {m15['ts'].iloc[-1]})")

    m15 = build_features(m15)

    # V11 core parameters (same logic, ATR filter scaled for M15 volatility)
    # D1 uses min_atr_pct=0.008 (absolute). On M15, ATR14/close averages 0.18%,
    # so we use the 50th percentile of ATR14/close as the volatility gate.
    params = dict(
        body_pct_threshold=0.95,      # top 5% momentum bars — SAME as D1
        trend_filter=True,             # EMA21>EMA55 bullish — SAME as D1
        min_atr_pct_threshold=0.50,    # ATR in top 50% of recent range (M15-scaled)
        rr=3.0,                        # 1:3 risk-reward — SAME as D1 V11
        atr_mult_stop=1.5,             # 1.5x ATR stop — SAME as D1 V11
        max_holding=5)                 # 5 M15 bars (5 x 15min = 75 min)

    signals = compute_signals(m15.copy(), **params)
    print(f"Total signals: {len(signals)}")

    # M15 costs: spread ~3 pips + slippage ~1 pip each way = 8 pips round-trip
    trades_df = backtest_fixed_lots(
        m15, signals, lot_size=0.05, risk_ratio=3.0,
        spread_pips=3.0, slippage_pips=1.0,
        **{k: params[k] for k in ["rr", "atr_mult_stop", "max_holding"]})

    if trades_df.empty:
        print("No trades to analyze.")
        return

    # Filter for August 2026 trades
    august_trades = trades_df[trades_df["entry_month"] == "2026-08"]

    print(f"\n--- AUGUST 2026 TRADES (M15) ---")
    print(f"Trades entered in August 2026: {len(august_trades)}")

    if len(august_trades) > 0:
        for _, t in august_trades.iterrows():
            print(f"  Entry: {t['entry_ts'].strftime('%Y-%m-%d %H:%M')} | "
                  f"Price: {t['entry_price']:.2f} | "
                  f"Risk: {t['risk_pips']:.1f} pips | "
                  f"Gross: {t['gross_pips']:.1f} pips | "
                  f"Net USD: ${t['net_usd']:.2f} | "
                  f"Reason: {t['reason']} | "
                  f"Holding: {t['holding_bars']} bars")

        total_profit_loss = august_trades["net_usd"].sum()
        win_trades = august_trades[august_trades["net_usd"] > 0]
        loss_trades = august_trades[august_trades["net_usd"] <= 0]
        win_rate = len(win_trades) / len(august_trades) * 100

        print(f"\n--- AUGUST 2026 P&L SUMMARY (M15) ---")
        print(f"Starting balance: $1000.00")
        print(f"Total trades: {len(august_trades)}")
        print(f"Winning trades: {len(win_trades)} ({win_rate:.1f}%)")
        print(f"Losing trades: {len(loss_trades)} ({100-win_rate:.1f}%)")
        print(f"Total P&L: ${total_profit_loss:.2f}")
        print(f"Ending balance: ${1000 + total_profit_loss:.2f}")

        if len(win_trades) > 0:
            print(f"  Avg win: ${win_trades['net_usd'].mean():.2f}")
            print(f"  Avg win gross pips: {win_trades['gross_pips'].mean():.1f}")
        if len(loss_trades) > 0:
            print(f"  Avg loss: ${loss_trades['net_usd'].mean():.2f}")

    else:
        print("No V11 signals fired during August 2026 on M15.")

    # Show all-time summary for M15
    print(f"\n--- ALL-TIME SUMMARY (M15, {len(trades_df)} trades) ---")
    total_all = trades_df["net_usd"].sum()
    all_win_rate = (trades_df["net_usd"] > 0).mean() * 100
    print(f"Total trades: {len(trades_df)}")
    print(f"Win rate: {all_win_rate:.1f}%")
    print(f"Total P&L: ${total_all:.2f}")
    print(f"With $1000 starting balance: ending at ${1000 + total_all:.2f}")

    # Show key stats
    print(f"\n--- KEY STATS ---")
    print(f"Round-trip cost per trade (0.05 lots): ${0.05 * 10.0 * 8.0:.2f} (8 pips)")
    avg_risk = trades_df["risk_pips"].mean() if len(trades_df) > 0 else 0
    print(f"Avg risk per trade: {avg_risk:.1f} pips (${avg_risk * 0.05 * 10.0:.2f})")
    print(f"Avg gross per trade: {trades_df['gross_pips'].mean():.1f} pips")
    cum_equity = trades_df["net_usd"].cumsum()
    print(f"Max drawdown: ${cum_equity.max() - cum_equity.min():.2f}")

    print(f"\n{'='*70}")
    print("ANALYSIS COMPLETE — read-only, no MT5 writes, no live trading")
    print("All numbers COMPUTED from db/trading.db (XAUUSD M15 data)")
    print("=" * 70)


if __name__ == "__main__":
    main()
