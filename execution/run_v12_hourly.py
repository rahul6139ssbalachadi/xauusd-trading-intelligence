"""V12 live runner: H1 bar-close signal -> pre-trade checklist -> order.

Runs ONCE per hour, after the H1 bar closes. It:
  1. Pulls fresh H1 bars (read-only, via the existing tested provider)
  2. Computes the V12 signal on the just-closed bar using the EXACT
     validated research logic (research.v11_multi_tf_jun_sep_2026)
  3. If BUY fires: sizes via Phase 10 risk engine (2% max loss), runs the
     full Section 23 checklist, and (if all pass) sends one market order

Usage:
  ./.venv/Scripts/python.exe execution/run_v12_hourly.py          # normal
  ./.venv/Scripts/python.exe execution/run_v12_hourly.py --dry-run # checklist only

SAFETY: demo-only, declines if any position open on GOLD.i# (your manual
trades are never interfered with), kill switch respected, every decision
journaled to execution/journal.jsonl.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from market_data import config as cfg
from indicators import ema, atr
from risk import RiskConfig, compute_lot_size

# V12 validated parameters (frozen — strategy/defs/XAUUSD_H1_MOMENTUM_BREAKOUT_V12.json)
# From research/v11_multi_tf_jun_sep_2026.py: H1, R:R=2.0, 2% risk, 0.8x ATR stop
PARAMS = {
    "body_pct_threshold": 0.95,
    "trend_filter": True,
    "min_atr_pct": 0.004,
    "rr": 3.0,
    "atr_mult_stop": 0.8,
    "max_holding_h1": 8,
    "risk_pct": 0.02,
}
STRATEGY, VERSION = "XAUUSD_H1_MOMENTUM_BREAKOUT", "V12"
BROKER_SYMBOL = "GOLD.i#"
MAGIC = 20260922


def load_h1_recent(gateway=None, limit: int = 400) -> pd.DataFrame:
    """Load recent H1 bars. Prefers LIVE terminal data via the gateway
    (fresh bars); falls back to the local DB (stale, for offline tests)."""
    if gateway is not None:
        import MetaTrader5 as mt5
        rates = gateway.copy_rates(BROKER_SYMBOL, mt5.TIMEFRAME_H1, limit)
        if rates is not None and len(rates) > 0:
            df = pd.DataFrame(rates)
            df = df.rename(columns={"time": "ts_broker_epoch"})
            df["ts"] = pd.to_datetime(df["ts_broker_epoch"], unit="s", utc=True)
            return df
        print("  WARN: gateway returned no H1 rates; falling back to DB")
    # DB fallback (stale data — only for offline/testing)
    db = ROOT / cfg.load_settings()["paths"]["db"]
    con = sqlite3.connect(db)
    df = pd.read_sql_query(
        "SELECT ts_broker_epoch, open, high, low, close, spread "
        "FROM market_data WHERE symbol='XAUUSD' AND timeframe='H1' "
        "AND source='mt5' ORDER BY ts_broker_epoch DESC LIMIT ?",
        con, params=(limit,))
    con.close()
    df = df.iloc[::-1].reset_index(drop=True)  # oldest -> newest
    df["ts"] = pd.to_datetime(df["ts_broker_epoch"], unit="s", utc=True)
    return df


def build_h1_features(df: pd.DataFrame) -> pd.DataFrame:
    """Build H1-level features for momentum detection (V12 params)."""
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
    return df


def signal_on_last_closed_bar(h1: pd.DataFrame) -> dict | None:
    """Compute the V12 signal on the LAST CLOSED H1 bar (index -2: the
    final row is the still-forming bar). Uses the exact research logic."""
    if len(h1) < 60:
        return None
    feats = build_h1_features(h1)
    row = feats.iloc[-2]
    if row["d1_trend"] != "bull":
        return None
    if pd.isna(row["body_pct"]) or row["body_pct"] < PARAMS["body_pct_threshold"]:
        return None
    if row["body"] <= 0:
        return None
    if pd.isna(row["atr14"]) or row["atr14"] / row["close"] < PARAMS["min_atr_pct"]:
        return None
    return {
        "signal": "BUY",
        "entry_ref": float(feats.iloc[-1]["open"]),  # current bar open
        "atr": float(row["atr14"]),
        "body_pct": float(row["body_pct"]),
        "bar_ts": str(feats.iloc[-2]["ts"]),
    }


def build_order_request(sig: dict, equity: float) -> "OrderRequest":
    from execution import OrderRequest
    stop_dist = PARAMS["atr_mult_stop"] * sig["atr"]
    entry = sig["entry_ref"]
    stop = entry - stop_dist
    target = entry + stop_dist * PARAMS["rr"]
    lots = compute_lot_size(
        equity=equity, risk_pct=PARAMS.get("risk_pct", 0.02),
        stop_distance_price=stop_dist, cfg=RiskConfig())
    return OrderRequest(
        symbol="XAUUSD", broker_symbol=BROKER_SYMBOL, direction="BUY",
        lots=lots, entry=entry, stop=stop, target=target,
        strategy=STRATEGY, version=VERSION, magic=MAGIC,
        comment=f"V12 H1 momentum {sig['body_pct']:.2f}",
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="run checklist only; never send the order")
    ap.add_argument("--equity", type=float, default=None,
                    help="override account equity for sizing")
    args = ap.parse_args()

    print(f"[V12 LIVE] {datetime.now(timezone.utc).isoformat()}")
    print(f"  strategy : {STRATEGY} {VERSION} (validated params, frozen)")
    print(f"  dry-run  : {args.dry_run}")

    # 1. connect gateway (demo-guarded)
    from execution.mt5_gateway import MT5Gateway
    mt5_cfg = cfg.load_mt5_config()
    gw = MT5Gateway(terminal_path=mt5_cfg["terminal_path"],
                    allowed_login=mt5_cfg["allowed_login"])
    gw.connect()
    try:
        # 2. fresh H1 data from the terminal (read-only)
        h1 = load_h1_recent(gateway=gw)
        print(f"  H1 bars  : {len(h1)} (last: {h1.iloc[-1]['ts']})")

        # 3. signal on last closed bar
        sig = signal_on_last_closed_bar(h1)
        if sig is None:
            print("  decision : WAIT — no V12 signal on last closed H1 bar")
            return 0
        print(f"  signal   : BUY (body_pct={sig['body_pct']:.2f}, "
              f"ATR={sig['atr']:.1f}, bar={sig['bar_ts']})")

        acc = gw.account_info()
        equity = args.equity or float(acc.equity)
        print(f"  account  : {acc.login} (DEMO) equity=${equity:,.2f}")

        # 4. build + size the order
        req = build_order_request(sig, equity)
        print(f"  plan     : {req.lots:.2f} lots  entry~{req.entry:.2f} "
              f"stop={req.stop:.2f} target={req.target:.2f}")

        if args.dry_run:
            from execution import ExecutionEngine
            eng = ExecutionEngine(gw)
            check = eng.pre_trade_checks(req)
            for name, ok, detail in check.checks:
                print(f"    [{'PASS' if ok else 'FAIL'}] {name}: {detail}")
            print(f"  checklist: {'ALL PASS — would execute' if check.ok else 'BLOCKED: ' + check.reason}")
            return 0 if check.ok else 1

        # 5. execute through the engine (full checklist + journal)
        from execution import ExecutionEngine
        eng = ExecutionEngine(gw)
        res = eng.execute(req)
        for name, ok, detail in res.checks:
            print(f"    [{'PASS' if ok else 'FAIL'}] {name}: {detail}")
        if res.ok:
            print(f"  EXECUTED : ticket {res.order_ticket}")
            return 0
        print(f"  {res.decision}: {res.reason}")
        return 1
    finally:
        gw.disconnect()


if __name__ == "__main__":
    sys.exit(main())
