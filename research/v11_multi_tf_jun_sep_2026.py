"""Multi-timeframe V11 backtest: Jun-Sep 2026, $1000 balance, 2% risk/trade.

Tests all available timeframes (M1, M5, M15, H1, D1), tries multiple R:R ratios,
computes lot size from 2% risk, and recommends the best timeframe + config.

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
BALANCE = 1000.0
MAX_RISK_PCT = 0.02  # 2% of balance
MAX_LOSS_PER_TRADE = BALANCE * MAX_RISK_PCT  # $20
USD_PER_PIP_PER_LOT = 10.0
LOT_STEP = 0.01
MIN_LOTS = 0.01
MAX_LOTS = 10.0

# Per-TF parameters (adaptive)
TF_PARAMS = {
    "M1":  {"ema_fast": 8,  "ema_slow": 21, "rank_window": 20, "min_atr_pct": 0.0001, "body_pct_threshold": 0.90, "atr_mult_stop": 1.5},
    "M5":  {"ema_fast": 13, "ema_slow": 34, "rank_window": 30, "min_atr_pct": 0.002,  "body_pct_threshold": 0.90, "atr_mult_stop": 1.5},
    "M15": {"ema_fast": 21, "ema_slow": 55, "rank_window": 60, "min_atr_pct": 0.003,  "body_pct_threshold": 0.95, "atr_mult_stop": 1.0},
    "H1":  {"ema_fast": 21, "ema_slow": 55, "rank_window": 60, "min_atr_pct": 0.004,  "body_pct_threshold": 0.95, "atr_mult_stop": 0.8},
    "D1":  {"ema_fast": 21, "ema_slow": 55, "rank_window": 60, "min_atr_pct": 0.005,  "body_pct_threshold": 0.95, "atr_mult_stop": 0.5},
}

MONTHS = [
    ("2026-06", "2026-06-01", "2026-07-01"),
    ("2026-07", "2026-07-01", "2026-08-01"),
    ("2026-08", "2026-08-01", "2026-09-01"),
    ("2026-09", "2026-09-01", "2026-10-01"),
]

RR_RATIOS = [1.0, 1.5, 2.0, 2.5, 3.0, 4.0]


def load(tf: str) -> pd.DataFrame:
    con = sqlite3.connect(DB)
    df = pd.read_sql_query(
        "SELECT ts_broker_epoch, open, high, low, close, tick_volume, spread "
        "FROM market_data WHERE symbol='XAUUSD' AND timeframe=? AND source='mt5' "
        "ORDER BY ts_broker_epoch",
        con, params=(tf,))
    con.close()
    df["ts"] = pd.to_datetime(df["ts_broker_epoch"], unit="s", utc=True)
    df["ts_broker"] = df["ts"] + pd.Timedelta(hours=3)
    return df


def build_features(df: pd.DataFrame, p: dict) -> pd.DataFrame:
    df = df.copy()
    df["ema21"] = ema(df["close"], p["ema_fast"])
    df["ema55"] = ema(df["close"], p["ema_slow"])
    df["atr14"] = atr(df["high"], df["low"], df["close"], 14)
    df["body"] = (df["close"] - df["open"]) / df["open"]
    df["body_abs"] = df["body"].abs()
    df["body_pct"] = df["body_abs"].rolling(p["rank_window"], min_periods=max(20, p["rank_window"] // 3)).rank(pct=True)
    df["d1_trend"] = np.where(
        df["ema21"] > df["ema55"], "bull",
        np.where(df["ema21"] < df["ema55"], "bear", "flat"))
    return df


def compute_signals(df: pd.DataFrame, p: dict) -> list[dict]:
    signals = []
    for i in range(len(df)):
        row = df.iloc[i]
        if i < p["ema_slow"]:
            continue
        if row["d1_trend"] != "bull":
            continue
        if pd.isna(row["body_pct"]) or row["body_pct"] < p["body_pct_threshold"]:
            continue
        if row["body"] <= 0:
            continue
        if pd.isna(row["atr14"]) or row["atr14"] / row["close"] < p["min_atr_pct"]:
            continue

        entry_bar = i + 1
        if entry_bar >= len(df):
            continue

        entry_price = df.iloc[entry_bar]["open"]
        stop = entry_price - p["atr_mult_stop"] * row["atr14"]
        risk = entry_price - stop
        stop_pips = risk / 0.10

        if stop_pips <= 0:
            continue

        # Lot size from 2% risk
        lots = MAX_LOSS_PER_TRADE / (stop_pips * USD_PER_PIP_PER_LOT)
        lots = max(MIN_LOTS, min(MAX_LOTS, round(lots / LOT_STEP) * LOT_STEP))
        lots = round(lots, 2)

        risk_usd = stop_pips * USD_PER_PIP_PER_LOT * lots

        signals.append({
            "entry_bar": entry_bar,
            "entry_ts": df.iloc[entry_bar]["ts"],
            "entry_price": entry_price,
            "stop": stop,
            "risk": risk,
            "risk_pips": stop_pips,
            "risk_usd": risk_usd,
            "lots": lots,
            "atr": row["atr14"],
            "body_pct": row["body_pct"],
        })
    return signals


def backtest_risk_based(
    df: pd.DataFrame,
    signals: list[dict],
    *,
    rr: float,
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
        lots = sig["lots"]
        risk_usd = sig["risk_usd"]

        if risk_usd > MAX_LOSS_PER_TRADE:
            continue

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
            net_usd = net_pips * USD_PER_PIP_PER_LOT * lots

            trades.append(Trade(
                entry_bar=i, entry_price=entry_price, side="LONG",
                stop=stop, target=target,
                exit_bar=j, exit_price=exit_price,
                exit_reason=reason, points=gross * PIP / 0.01,
                cost_pips=cost_pips, net_pips=net_pips,
                duration_bars=j - i,
                lots=lots, risk_usd=risk_usd, net_usd=net_usd))
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
            net_usd = net_pips * USD_PER_PIP_PER_LOT * lots

            trades.append(Trade(
                entry_bar=i, entry_price=entry_price, side="LONG",
                stop=stop, target=target,
                exit_bar=j, exit_price=exit_price,
                exit_reason="end", points=gross * PIP / 0.01,
                cost_pips=cost_pips, net_pips=net_pips,
                duration_bars=j - i,
                lots=lots, risk_usd=risk_usd, net_usd=net_usd))

    metrics = compute_metrics(trades)
    total_usd = sum(t.net_usd for t in trades)
    metrics["total_usd"] = total_usd
    metrics["avg_lots"] = np.mean([t.lots for t in trades]) if trades else 0
    metrics["max_risk_usd"] = MAX_LOSS_PER_TRADE

    return trades, metrics


def run_tf(tf: str, df_all: pd.DataFrame) -> dict:
    """Run full backtest for one timeframe across all R:R ratios, all months."""
    p = TF_PARAMS.get(tf, TF_PARAMS["D1"])
    df_full = build_features(df_all, p)

    # Slice Jun-Sep 2026
    mask = (df_full["ts_broker"] >= "2026-06-01") & (df_full["ts_broker"] < "2026-10-01")
    df = df_full[mask].reset_index(drop=True)

    if len(df) < 50:
        return {"tf": tf, "bars": len(df), "results": [], "best": None}

    signals = compute_signals(df, p)

    results = []
    for rr in RR_RATIOS:
        trades, m = backtest_risk_based(df, signals, rr=rr)
        net_usd = m.get("total_usd", 0)
        pf = m.get("profit_factor", 0)
        nt = m.get("total_trades", 0)
        wr = m.get("win_rate", 0)
        max_dd = m.get("max_drawdown_pips", 0)
        sharpe = m.get("sharpe", 0)
        expectancy = m.get("expectancy_pips", 0)
        avg_lots = m.get("avg_lots", 0)
        max_risk = m.get("max_risk_usd", 0)

        results.append({
            "rr": rr, "trades": nt, "net_usd": net_usd, "pf": pf,
            "win_rate": wr, "max_dd_pips": max_dd, "sharpe": sharpe,
            "expectancy_pips": expectancy, "avg_lots": avg_lots,
            "max_risk_usd": max_risk,
        })

    # Best by net USD (min 3 trades)
    valid = [r for r in results if r["trades"] >= 3]
    best = max(valid, key=lambda r: r["net_usd"]) if valid else None

    return {"tf": tf, "bars": len(df), "signals": len(signals), "results": results, "best": best}


def main():
    print("=" * 75)
    print("MULTI-TIMEFRAME V11 BACKTEST — JUN-SEP 2026")
    print(f"Balance: ${BALANCE:,.0f}  Max Risk: {MAX_RISK_PCT*100:.0f}% (${MAX_LOSS_PER_TRADE:.0f}/trade)")
    print(f"Timeframes: M1, M5, M15, H1, D1")
    print(f"R:R ratios: {', '.join(str(r) for r in RR_RATIOS)}")
    print("=" * 75)

    all_results = {}
    for tf in ["M1", "M5", "M15", "H1", "D1"]:
        df_all = load(tf)
        if len(df_all) == 0:
            print(f"\n{tf}: SKIP (no data in DB)")
            continue
        all_results[tf] = run_tf(tf, df_all)

    # Summary table
    print(f"\n{'='*75}")
    print("SUMMARY TABLE (Jun-Sep 2026)")
    print(f"{'='*75}")
    print(f"{'TF':<6} {'Bars':>6} {'Sigs':>5} {'Best R:R':>8} {'Trades':>7} "
          f"{'Net $':>10} {'PF':>6} {'Win%':>6} {'MaxDD':>8} {'Sharpe':>7} {'Avg Lots':>9}")
    print("-" * 75)

    for tf in ["M1", "M5", "M15", "H1", "D1"]:
        r = all_results.get(tf)
        if r and r.get("best"):
            b = r["best"]
            print(f"{tf:<6} {r['bars']:>6} {r['signals']:>5} {b['rr']:>8.1f} "
                  f"{b['trades']:>7} {b['net_usd']:>10.2f} {b['pf']:>6.2f} "
                  f"{b['win_rate']*100:>5.0f}% {b['max_dd_pips']:>7.1f}p "
                  f"{b['sharpe']:>7.2f} {b['avg_lots']:>9.2f}")
        else:
            print(f"{tf:<6} {'--':>6} {'--':>5} {'N/A':>8} {'--':>7} "
                  f"{'--':>10} {'--':>6} {'--':>6} {'--':>8} {'--':>7} {'--':>9}")

    # Recommendation
    print(f"\n{'='*75}")
    print("RECOMMENDATION")
    print(f"{'='*75}")

    # Find best overall
    best_overall = None
    best_tf = None
    for tf in ["M1", "M5", "M15", "H1", "D1"]:
        r = all_results.get(tf)
        if r and r.get("best"):
            b = r["best"]
            if best_overall is None or b["net_usd"] > best_overall["net_usd"]:
                best_overall = b
                best_tf = tf

    if best_overall:
        print(f"\n  BEST TIMEFRAME: {best_tf}")
        print(f"  BEST R:R: {best_overall['rr']:.1f}")
        print(f"  Trades: {best_overall['trades']}")
        print(f"  Net P&L: ${best_overall['net_usd']:.2f}")
        print(f"  Profit Factor: {best_overall['pf']:.2f}")
        print(f"  Win Rate: {best_overall['win_rate']*100:.0f}%")
        print(f"  Max Drawdown: {best_overall['max_dd_pips']:.1f} pips")
        print(f"  Sharpe: {best_overall['sharpe']:.2f}")
        print(f"  Avg Lots: {best_overall['avg_lots']:.2f}")
        print(f"  Max Risk/Trade: ${best_overall['max_risk_usd']:.2f}")
    else:
        print("\n  No valid configuration found across all timeframes.")

    # Detailed per-TF breakdown
    print(f"\n{'='*75}")
    print("DETAILED RESULTS PER TIMEFRAME")
    print(f"{'='*75}")

    for tf in ["M1", "M5", "M15", "H1", "D1"]:
        r = all_results.get(tf)
        if not r or not r.get("results"):
            continue
        print(f"\n--- {tf} ---")
        print(f"  Bars: {r['bars']}  Signals: {r['signals']}")
        for res in r["results"]:
            print(f"  R:R={res['rr']:.1f}: trades={res['trades']:3d}  "
                  f"net=${res['net_usd']:8.2f}  PF={res['pf']:5.2f}  "
                  f"win%={res['win_rate']*100:4.0f}  maxDD={res['max_dd_pips']:6.1f}p  "
                  f"sharpe={res['sharpe']:5.2f}  avg_lots={res['avg_lots']:.2f}  "
                  f"max_risk=${res['max_risk_usd']:.2f}")

    print(f"\n{'='*75}")
    print("RESEARCH COMPLETE — read-only, no MT5 writes, no live trading.")
    print("=" * 75)


if __name__ == "__main__":
    main()
