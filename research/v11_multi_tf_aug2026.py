"""Multi-timeframe V11 backtest: M1/M5/M15/H1/H4/D1 for August 2026.

$5,000 balance, $50 max loss per trade. Lot size computed from stop distance.
Tests R:R ratios 1.0-3.0 per timeframe and picks the best.

Read-only: queries db/trading.db only.
"""
from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from market_data import config as cfg
from indicators import ema, atr
from backtest import compute_metrics, Trade

DB = cfg.PROJECT_ROOT / cfg.load_settings()["paths"]["db"]

# August 2026 window (broker time UTC+3)
AUG_START = "2026-08-01"
AUG_END = "2026-08-31"

# Risk parameters
BALANCE = 5000.0
MAX_LOSS_PER_TRADE = 50.0  # USD
LOT_STEP = 0.01
MIN_LOTS = 0.01
MAX_LOTS = 10.0

# XAUUSD: 1 lot = 100 oz, 1 pip = $0.10 price = $10 per lot
USD_PER_PIP_PER_LOT = 10.0

TIMEFRAMES = ["M1", "M5", "M15", "H1", "H4", "D1"]
RR_RATIOS = [1.0, 1.5, 2.0, 2.5, 3.0]


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


def filter_august(df: pd.DataFrame) -> pd.DataFrame:
    """Filter to August 2026 (broker time UTC+3)."""
    df = df.copy()
    df["ts_broker"] = df["ts"] + pd.Timedelta(hours=3)
    mask = (df["ts_broker"] >= AUG_START) & (df["ts_broker"] < "2026-09-01")
    return df[mask].reset_index(drop=True)


def build_features(df: pd.DataFrame, *, ema_fast: int = 21, ema_slow: int = 55, rank_window: int = 60) -> pd.DataFrame:
    """Build V11 features: EMA, ATR14, body_pct (rolling rank)."""
    df = df.copy()
    df["ema21"] = ema(df["close"], ema_fast)
    df["ema55"] = ema(df["close"], ema_slow)
    df["atr14"] = atr(df["high"], df["low"], df["close"], 14)

    df["body"] = (df["close"] - df["open"]) / df["open"]
    df["body_abs"] = df["body"].abs()
    df["body_pct"] = df["body_abs"].rolling(rank_window, min_periods=max(20, rank_window // 3)).rank(pct=True)

    df["d1_trend"] = np.where(
        df["ema21"] > df["ema55"], "bull",
        np.where(df["ema21"] < df["ema55"], "bear", "flat"))
    return df


def compute_signals(
    df: pd.DataFrame,
    *,
    body_pct_threshold: float = 0.95,
    trend_filter: bool = True,
    min_atr_pct: float = 0.005,
    **kwargs,
) -> list[dict]:
    """Compute V11 momentum signals."""
    signals = []
    for i in range(len(df)):
        row = df.iloc[i]
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
        if entry_bar >= len(df):
            continue

        entry_price = df.iloc[entry_bar]["open"]
        stop = entry_price - 1.5 * row["atr14"]
        risk = entry_price - stop

        signals.append({
            "entry_bar": entry_bar,
            "entry_ts": df.iloc[entry_bar]["ts"],
            "entry_price": entry_price,
            "stop": stop,
            "risk": risk,
            "risk_pips": risk / 0.10,
            "atr": row["atr14"],
            "body_pct": row["body_pct"],
        })
    return signals


def backtest_with_fixed_risk(
    df: pd.DataFrame,
    signals: list[dict],
    *,
    rr: float,
    max_holding_bars: int = 8,
    spread_pips: float = 3.0,
    slippage_pips: float = 1.0,
) -> tuple[list[Trade], dict]:
    """Backtest with $50 max loss per trade. Lot size = $50 / (stop_pips * $10)."""
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
        stop_pips = risk / PIP

        # Lot size: $50 max loss / (stop_pips * $10 per lot per pip)
        if stop_pips <= 0:
            continue
        lots = MAX_LOSS_PER_TRADE / (stop_pips * USD_PER_PIP_PER_LOT)
        lots = max(MIN_LOTS, min(MAX_LOTS, round(lots / LOT_STEP) * LOT_STEP))
        lots = round(lots, 2)

        # Actual risk with rounded lots
        actual_risk_usd = stop_pips * USD_PER_PIP_PER_LOT * lots

        target = entry_price + risk * rr
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
                lots=lots, risk_usd=actual_risk_usd, net_usd=net_usd))
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
                lots=lots, risk_usd=actual_risk_usd, net_usd=net_usd))

    # Compute metrics including USD P&L
    metrics = compute_metrics(trades)
    total_usd = sum(t.net_usd for t in trades)
    metrics["total_usd"] = total_usd
    metrics["avg_lots"] = np.mean([t.lots for t in trades]) if trades else 0
    metrics["max_risk_usd"] = max((t.risk_usd for t in trades), default=0)

    return trades, metrics


# Per-timeframe parameter adaptation
TF_PARAMS = {
    "M1":  {"ema_fast": 8,  "ema_slow": 21, "rank_window": 20, "min_atr_pct": 0.001, "body_pct_threshold": 0.90},
    "M5":  {"ema_fast": 13, "ema_slow": 34, "rank_window": 30, "min_atr_pct": 0.002, "body_pct_threshold": 0.90},
    "M15": {"ema_fast": 21, "ema_slow": 55, "rank_window": 60, "min_atr_pct": 0.003, "body_pct_threshold": 0.95},
    "H1":  {"ema_fast": 21, "ema_slow": 55, "rank_window": 60, "min_atr_pct": 0.004, "body_pct_threshold": 0.95},
    "H4":  {"ema_fast": 21, "ema_slow": 55, "rank_window": 60, "min_atr_pct": 0.005, "body_pct_threshold": 0.95},
    "D1":  {"ema_fast": 21, "ema_slow": 55, "rank_window": 60, "min_atr_pct": 0.005, "body_pct_threshold": 0.95},
}


def run_tf(tf: str) -> dict:
    """Run full backtest for one timeframe across all R:R ratios."""
    print(f"\n{'='*70}")
    print(f"TIMEFRAME: {tf}")
    print(f"{'='*70}")

    df = load(tf)
    if len(df) == 0:
        print(f"  SKIP: no data in DB for {tf}")
        return {"tf": tf, "bars": 0, "signals": 0, "results": []}

    print(f"  Loaded: {len(df)} bars ({df['ts'].iloc[0]} to {df['ts'].iloc[-1]})")

    df = filter_august(df)
    print(f"  August 2026: {len(df)} bars")

    if len(df) < 100:
        print(f"  SKIP: insufficient data ({len(df)} bars)")
        return {"tf": tf, "bars": len(df), "results": []}

    p = TF_PARAMS.get(tf, TF_PARAMS["D1"])
    feat_params = {k: p[k] for k in ("ema_fast", "ema_slow", "rank_window") if k in p}
    sig_params = {k: p[k] for k in ("body_pct_threshold", "min_atr_pct") if k in p}
    df = build_features(df, **feat_params)
    signals = compute_signals(df, **sig_params)
    print(f"  Signals: {len(signals)}")

    if not signals:
        print(f"  SKIP: no signals generated")
        return {"tf": tf, "bars": len(df), "signals": 0, "results": []}

    results = []
    for rr in RR_RATIOS:
        trades, m = backtest_with_fixed_risk(df, signals, rr=rr)
        net_usd = m.get("total_usd", 0)
        pf = m.get("profit_factor", 0)
        nt = m.get("total_trades", 0)
        wr = m.get("win_rate", 0)
        max_dd = m.get("max_drawdown_pips", 0)
        avg_lots = m.get("avg_lots", 0)
        max_risk = m.get("max_risk_usd", 0)

        results.append({
            "rr": rr,
            "trades": nt,
            "net_usd": net_usd,
            "pf": pf,
            "win_rate": wr,
            "max_dd_pips": max_dd,
            "avg_lots": avg_lots,
            "max_risk_usd": max_risk,
        })

        print(f"  R:R={rr:.1f}: trades={nt:3d}  net=${net_usd:8.2f}  "
              f"PF={pf:5.2f}  win%={wr*100:4.0f}  maxDD={max_dd:6.1f}p  "
              f"avg_lots={avg_lots:.2f}  max_risk=${max_risk:.2f}")

    # Pick best by net USD (with min 3 trades)
    valid = [r for r in results if r["trades"] >= 3]
    if valid:
        best = max(valid, key=lambda r: r["net_usd"])
        print(f"\n  BEST: R:R={best['rr']:.1f}  net=${best['net_usd']:.2f}  "
              f"PF={best['pf']:.2f}  trades={best['trades']}")
    else:
        best = None
        print(f"\n  No valid config (min 3 trades)")

    return {"tf": tf, "bars": len(df), "signals": len(signals), "results": results, "best": best}


def main():
    print("=" * 70)
    print("MULTI-TIMEFRAME V11 BACKTEST — AUGUST 2026")
    print(f"Balance: ${BALANCE:,.0f}  Max loss/trade: ${MAX_LOSS_PER_TRADE:.0f}")
    print(f"Timeframes: {', '.join(TIMEFRAMES)}")
    print(f"R:R ratios: {', '.join(str(r) for r in RR_RATIOS)}")
    print("=" * 70)

    all_results = {}
    for tf in TIMEFRAMES:
        all_results[tf] = run_tf(tf)

    # Summary table
    print(f"\n{'='*70}")
    print("SUMMARY TABLE")
    print(f"{'='*70}")
    print(f"{'TF':<6} {'Bars':>6} {'Sigs':>5} {'Best R:R':>8} {'Trades':>7} "
          f"{'Net $':>10} {'PF':>6} {'Win%':>6} {'MaxDD':>8} {'Avg Lots':>9}")
    print("-" * 70)

    for tf in TIMEFRAMES:
        r = all_results[tf]
        if r.get("best"):
            b = r["best"]
            print(f"{tf:<6} {r['bars']:>6} {r['signals']:>5} {b['rr']:>8.1f} "
                  f"{b['trades']:>7} {b['net_usd']:>10.2f} {b['pf']:>6.2f} "
                  f"{b['win_rate']*100:>5.0f}% {b['max_dd_pips']:>7.1f}p "
                  f"{b['avg_lots']:>9.2f}")
        else:
            print(f"{tf:<6} {r['bars']:>6} {r.get('signals',0):>5} {'N/A':>8} "
                  f"{'N/A':>7} {'N/A':>10} {'N/A':>6} {'N/A':>6} {'N/A':>8} {'N/A':>9}")

    # Detailed per-TF breakdown
    print(f"\n{'='*70}")
    print("DETAILED RESULTS PER TIMEFRAME")
    print(f"{'='*70}")

    for tf in TIMEFRAMES:
        r = all_results[tf]
        if not r.get("results"):
            continue
        print(f"\n--- {tf} ---")
        print(f"  Bars: {r['bars']}  Signals: {r['signals']}")
        for res in r["results"]:
            print(f"  R:R={res['rr']:.1f}: trades={res['trades']:3d}  "
                  f"net=${res['net_usd']:8.2f}  PF={res['pf']:5.2f}  "
                  f"win%={res['win_rate']*100:4.0f}  maxDD={res['max_dd_pips']:6.1f}p  "
                  f"avg_lots={res['avg_lots']:.2f}  max_risk=${res['max_risk_usd']:.2f}")

    print(f"\n{'='*70}")
    print("RESEARCH COMPLETE — read-only, no MT5 writes, no live trading.")
    print("=" * 70)


if __name__ == "__main__":
    main()
