"""V11 H1 backtest: fixed 0.5 lots, R:R=3.0, max $100 loss/trade, each month of 2026.

Read-only: queries db/trading.db only.
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
from backtest import compute_metrics, Trade

DB = cfg.PROJECT_ROOT / cfg.load_settings()["paths"]["db"]

# Fixed parameters
LOTS = 0.5
RR = 3.0
MAX_LOSS_PER_TRADE = 100.0  # USD
USD_PER_PIP_PER_LOT = 10.0
BALANCE = 5000.0

# H1 parameters (same as multi-TF script)
EMA_FAST = 21
EMA_SLOW = 55
RANK_WINDOW = 60
MIN_ATR_PCT = 0.004
BODY_PCT_THRESHOLD = 0.95

MONTHS = [
    ("2026-01", "2026-01-01", "2026-02-01"),
    ("2026-02", "2026-02-01", "2026-03-01"),
    ("2026-03", "2026-03-01", "2026-04-01"),
    ("2026-04", "2026-04-01", "2026-05-01"),
    ("2026-05", "2026-05-01", "2026-06-01"),
    ("2026-06", "2026-06-01", "2026-07-01"),
    ("2026-07", "2026-07-01", "2026-08-01"),
    ("2026-08", "2026-08-01", "2026-09-01"),
]


def load() -> pd.DataFrame:
    con = sqlite3.connect(DB)
    df = pd.read_sql_query(
        "SELECT ts_broker_epoch, open, high, low, close, tick_volume, spread "
        "FROM market_data WHERE symbol='XAUUSD' AND timeframe='H1' AND source='mt5' "
        "ORDER BY ts_broker_epoch",
        con)
    con.close()
    df["ts"] = pd.to_datetime(df["ts_broker_epoch"], unit="s", utc=True)
    df["ts_broker"] = df["ts"] + pd.Timedelta(hours=3)
    return df


def build_features(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["ema21"] = ema(df["close"], EMA_FAST)
    df["ema55"] = ema(df["close"], EMA_SLOW)
    df["atr14"] = atr(df["high"], df["low"], df["close"], 14)
    df["body"] = (df["close"] - df["open"]) / df["open"]
    df["body_abs"] = df["body"].abs()
    df["body_pct"] = df["body_abs"].rolling(RANK_WINDOW, min_periods=max(20, RANK_WINDOW // 3)).rank(pct=True)
    df["d1_trend"] = np.where(
        df["ema21"] > df["ema55"], "bull",
        np.where(df["ema21"] < df["ema55"], "bear", "flat"))
    return df


def compute_signals(df: pd.DataFrame) -> list[dict]:
    signals = []
    for i in range(len(df)):
        row = df.iloc[i]
        if i < 55:
            continue
        if row["d1_trend"] != "bull":
            continue
        if pd.isna(row["body_pct"]) or row["body_pct"] < BODY_PCT_THRESHOLD:
            continue
        if row["body"] <= 0:
            continue
        if pd.isna(row["atr14"]) or row["atr14"] / row["close"] < MIN_ATR_PCT:
            continue

        entry_bar = i + 1
        if entry_bar >= len(df):
            continue

        entry_price = df.iloc[entry_bar]["open"]
        stop = entry_price - 1.5 * row["atr14"]
        risk = entry_price - stop
        stop_pips = risk / 0.10

        # Check if this trade would exceed max loss with fixed lots
        risk_usd = stop_pips * USD_PER_PIP_PER_LOT * LOTS
        if risk_usd > MAX_LOSS_PER_TRADE:
            continue  # Skip trade that would exceed max loss

        signals.append({
            "entry_bar": entry_bar,
            "entry_ts": df.iloc[entry_bar]["ts"],
            "entry_price": entry_price,
            "stop": stop,
            "risk": risk,
            "risk_pips": stop_pips,
            "risk_usd": risk_usd,
            "atr": row["atr14"],
            "body_pct": row["body_pct"],
        })
    return signals


def backtest_fixed_lots(
    df: pd.DataFrame,
    signals: list[dict],
    *,
    rr: float = RR,
    max_holding_bars: int = 8,
    spread_pips: float = 3.0,
    slippage_pips: float = 1.0,
) -> tuple[list[Trade], dict]:
    if not signals:
        return [], compute_metrics([])

    trades = []
    PIP = 0.10
    cost_pips = 2 * slippage_pips + 2 * spread_pips

    for sig in signals:
        i = sig["entry_bar"]
        if i >= len(df) - 1:
            continue

        entry_price = sig["entry_price"]
        stop = sig["stop"]
        risk = sig["risk"]
        target = entry_price + risk * rr
        stop_pips = risk / PIP

        # Fixed lots
        risk_usd = stop_pips * USD_PER_PIP_PER_LOT * LOTS
        if risk_usd > MAX_LOSS_PER_TRADE:
            continue  # Skip: would exceed max loss

        max_j = min(i + max_holding_bars, len(df) - 1)
        exited = False

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
                    exit_price, reason = stop, "stop"
                else:
                    exit_price, reason = target, "target"
            elif stop_hit:
                exit_price, reason = stop, "stop"
            elif target_hit:
                exit_price, reason = target, "target"
            else:
                continue

            gross = exit_price - entry_price
            net_pips = gross / PIP - cost_pips
            net_usd = net_pips * USD_PER_PIP_PER_LOT * LOTS

            trades.append(Trade(
                entry_bar=i, entry_price=entry_price, side="LONG",
                stop=stop, target=target,
                exit_bar=j, exit_price=exit_price,
                exit_reason=reason, points=gross * PIP / 0.01,
                cost_pips=cost_pips, net_pips=net_pips,
                duration_bars=j - i,
                lots=LOTS, risk_usd=risk_usd, net_usd=net_usd))
            exited = True
            break

        if not exited:
            j = max_j
            if j <= i:
                continue
            jrow = df.iloc[j]
            exit_price = jrow["close"]
            gross = exit_price - entry_price
            net_pips = gross / PIP - cost_pips
            net_usd = net_pips * USD_PER_PIP_PER_LOT * LOTS

            trades.append(Trade(
                entry_bar=i, entry_price=entry_price, side="LONG",
                stop=stop, target=target,
                exit_bar=j, exit_price=exit_price,
                exit_reason="end", points=gross * PIP / 0.01,
                cost_pips=cost_pips, net_pips=net_pips,
                duration_bars=j - i,
                lots=LOTS, risk_usd=risk_usd, net_usd=net_usd))

    metrics = compute_metrics(trades)
    total_usd = sum(t.net_usd for t in trades)
    metrics["total_usd"] = total_usd
    metrics["avg_lots"] = LOTS
    metrics["max_risk_usd"] = MAX_LOSS_PER_TRADE

    return trades, metrics


def run_month(df_all: pd.DataFrame, month_label: str, start: str, end: str) -> dict:
    print(f"\n{'='*70}")
    print(f"MONTH: {month_label}")
    print(f"{'='*70}")

    # Build features on FULL dataset first (warmup), then slice by month
    df_full = build_features(df_all)
    df = df_full[(df_full["ts_broker"] >= start) & (df_full["ts_broker"] < end)].reset_index(drop=True)
    print(f"  Bars: {len(df)}")

    if len(df) < 50:
        print(f"  SKIP: insufficient data")
        return {"month": month_label, "bars": len(df), "signals": 0, "trades": 0}

    signals = compute_signals(df)
    print(f"  Signals: {len(signals)}")

    if not signals:
        print(f"  SKIP: no signals")
        return {"month": month_label, "bars": len(df), "signals": 0, "trades": 0}

    trades, m = backtest_fixed_lots(df, signals)
    net_usd = m.get("total_usd", 0)
    pf = m.get("profit_factor", 0)
    nt = m.get("total_trades", 0)
    wr = m.get("win_rate", 0)
    max_dd = m.get("max_drawdown_pips", 0)
    sharpe = m.get("sharpe", 0)
    expectancy = m.get("expectancy_pips", 0)

    print(f"  Trades: {nt}")
    print(f"  Net P&L: ${net_usd:,.2f}")
    print(f"  Profit Factor: {pf:.2f}")
    print(f"  Win Rate: {wr*100:.0f}%")
    print(f"  Max Drawdown: {max_dd:.1f} pips")
    print(f"  Sharpe: {sharpe:.2f}")
    print(f"  Expectancy: {expectancy:.1f} pips/trade")
    print(f"  Avg Risk/Trade: ${trades[0].risk_usd:.2f}" if trades else "  No trades")

    # Cumulative equity
    eq = 0.0
    for t in trades:
        eq += t.net_usd
    print(f"  Cumulative Equity: ${BALANCE + eq:,.2f}")

    return {
        "month": month_label,
        "bars": len(df),
        "signals": len(signals),
        "trades": nt,
        "net_usd": net_usd,
        "pf": pf,
        "win_rate": wr,
        "max_dd_pips": max_dd,
        "sharpe": sharpe,
        "expectancy_pips": expectancy,
        "avg_risk_usd": trades[0].risk_usd if trades else 0,
    }


def main():
    print("=" * 70)
    print("V11 H1 BACKTEST — FIXED 0.5 LOTS, R:R=3.0, MAX $100 LOSS/TRADE")
    print(f"Balance: ${BALANCE:,.0f}  Lots: {LOTS}  R:R={RR}  Max Loss: ${MAX_LOSS_PER_TRADE}")
    print("=" * 70)

    df_all = load()
    print(f"Full H1 history: {len(df_all)} bars ({df_all['ts'].iloc[0]} to {df_all['ts'].iloc[-1]})")

    results = []
    for label, start, end in MONTHS:
        r = run_month(df_all, label, start, end)
        results.append(r)

    # Summary table
    print(f"\n{'='*70}")
    print("MONTHLY SUMMARY TABLE")
    print(f"{'='*70}")
    print(f"{'Month':<8} {'Bars':>6} {'Sigs':>5} {'Trades':>7} {'Net $':>10} {'PF':>6} {'Win%':>6} {'MaxDD':>8} {'Sharpe':>7} {'Equity':>10}")
    print("-" * 80)

    total_net = 0.0
    total_trades = 0
    for r in results:
        if r.get("trades", 0) > 0:
            eq = BALANCE + r["net_usd"]
            print(f"{r['month']:<8} {r['bars']:>6} {r.get('signals',0):>5} {r['trades']:>7} "
                  f"{r['net_usd']:>10.2f} {r['pf']:>6.2f} {r['win_rate']*100:>5.0f}% "
                  f"{r['max_dd_pips']:>7.1f}p {r['sharpe']:>7.2f} {eq:>10.2f}")
            total_net += r["net_usd"]
            total_trades += r["trades"]
        else:
            print(f"{r['month']:<8} {r.get('bars',0):>6} {r.get('signals',0):>5} {'0':>7} "
                  f"{'--':>10} {'--':>6} {'--':>6} {'--':>8} {'--':>7} {'--':>10}")

    print("-" * 80)
    print(f"{'TOTAL':<8} {'':>6} {'':>5} {total_trades:>7} {total_net:>10.2f} {'':>6} {'':>6} {'':>8} {'':>7} {BALANCE + total_net:>10.2f}")

    print(f"\n{'='*70}")
    print("RESEARCH COMPLETE — read-only, no MT5 writes, no live trading.")
    print("=" * 70)


if __name__ == "__main__":
    main()
