"""V11 live runner: D1 bar-close signal -> pre-trade checklist -> order.

Runs ONCE per day, after the D1 bar closes (broker time ~00:00, i.e. after
midnight UTC+3). It:
  1. Pulls fresh D1 bars (read-only, via the existing tested provider)
  2. Computes the V11 signal on the just-closed bar using the EXACT
     validated research logic (research.v11_d1_momentum)
  3. If BUY fires: sizes via Phase 10 risk engine, runs the full Section 23
     checklist, and (if all pass) sends one market order with SL/TP

Usage:
  ./.venv/Scripts/python.exe execution/run_v11_daily.py          # normal
  ./.venv/Scripts/python.exe execution/run_v11_daily.py --dry-run # checklist only, no order

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

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from market_data import config as cfg
from indicators import ema, atr
from risk import RiskConfig, compute_lot_size
from execution.signal_payload import Signal, journal_signal, write_signal_csv
import research.v11_d1_momentum as v11

# V11 validated parameters (frozen — strategy/defs/XAUUSD_D1_MOMENTUM_BREAKOUT_V11.json
# research_results.train.params, the config that passed all validation gates)
PARAMS = {
    "body_pct_threshold": 0.95,
    "trend_filter": True,
    "min_atr_pct": 0.005,
    "rr": 2.0,
    "atr_mult_stop": 1.5,
    "max_holding_d1": 8,
    "risk_pct": 0.01,
}
STRATEGY, VERSION = "XAUUSD_D1_MOMENTUM_BREAKOUT", "V11"
BROKER_SYMBOL = "GOLD.i#"
MAGIC = 20260922

# Where the MT5 chart indicator picks up markers. The EA reads
# <terminal data>\MQL5\Files\signals\*.csv; MQL5/Files is the repo's
# mql5/Files mirror that gets copied into the terminal. See
# docs/MT5_ARCHITECTURE.md.
CHART_DIR = ROOT / "mql5" / "signals"


def _chart_csv() -> Path:
    return CHART_DIR / f"{VERSION}_XAUUSD_live.csv"


def load_d1_recent(gateway=None, limit: int = 400) -> pd.DataFrame:
    """Load recent D1 bars. Prefers LIVE terminal data via the gateway
    (fresh bars); falls back to the local DB (stale, for offline tests)."""
    if gateway is not None:
        import MetaTrader5 as mt5
        rates = gateway.copy_rates(BROKER_SYMBOL, mt5.TIMEFRAME_D1, limit)
        if rates is not None and len(rates) > 0:
            df = pd.DataFrame(rates)
            df = df.rename(columns={"time": "ts_broker_epoch"})
            df["ts"] = pd.to_datetime(df["ts_broker_epoch"], unit="s", utc=True)
            return df
        print("  WARN: gateway returned no D1 rates; falling back to DB")
    # DB fallback (stale data — only for offline/testing)
    db = ROOT / cfg.load_settings()["paths"]["db"]
    con = sqlite3.connect(db)
    df = pd.read_sql_query(
        "SELECT ts_broker_epoch, open, high, low, close, spread "
        "FROM market_data WHERE symbol='XAUUSD' AND timeframe='D1' "
        "AND source='mt5' ORDER BY ts_broker_epoch DESC LIMIT ?",
        con, params=(limit,))
    con.close()
    df = df.iloc[::-1].reset_index(drop=True)  # oldest -> newest
    df["ts"] = pd.to_datetime(df["ts_broker_epoch"], unit="s", utc=True)
    return df


def signal_on_last_closed_bar(d1: pd.DataFrame) -> dict | None:
    """Compute the V11 signal on the LAST CLOSED D1 bar (index -2: the
    final row is the still-forming bar). Uses the exact research logic."""
    if len(d1) < 60:
        return None
    feats = v11.build_d1_features(d1)
    # compute_d1_signals needs fwd columns; but for live use we evaluate
    # the last CLOSED bar only — replicate the gate conditions directly:
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
        equity=equity, risk_pct=PARAMS.get("risk_pct", 0.01),
        stop_distance_price=stop_dist, cfg=RiskConfig())
    return OrderRequest(
        symbol="XAUUSD", broker_symbol=BROKER_SYMBOL, direction="BUY",
        lots=lots, entry=entry, stop=stop, target=target,
        strategy=STRATEGY, version=VERSION, magic=MAGIC,
        comment=f"V11 D1 momentum {sig['body_pct']:.2f}",
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="run checklist only; never send the order")
    ap.add_argument("--equity", type=float, default=None,
                    help="override account equity for sizing")
    args = ap.parse_args()

    print(f"[V11 LIVE] {datetime.now(timezone.utc).isoformat()}")
    print(f"  strategy : {STRATEGY} {VERSION} (validated params, frozen)")
    print(f"  dry-run  : {args.dry_run}")

    # 1. connect gateway (demo-guarded)
    from execution.mt5_gateway import MT5Gateway, MT5GatewayError
    mt5_cfg = cfg.load_mt5_config()
    gw = MT5Gateway(terminal_path=mt5_cfg["terminal_path"],
                    allowed_login=mt5_cfg["allowed_login"])
    try:
        gw.connect()
    except MT5GatewayError as e:
        # A refused connection is a legitimate, expected outcome — the
        # wrong account is logged in, the terminal is closed, the broker
        # is down. It must be recorded and reported, never crash the
        # runner: a cron job that dies with a traceback tells the operator
        # nothing, and a silent death looks identical to "no signal".
        print(f"  BLOCKED  : {e}")
        from execution.signal_payload import journal_signal, Signal
        journal_signal(Signal(
            strategy=STRATEGY, strategy_number=VERSION, symbol="XAUUSD",
            broker_symbol=BROKER_SYMBOL, timeframe="D1", direction="BLOCKED",
            confidence=0.0, reason=f"execution refused: {e}", magic=MAGIC,
        ))
        return 1
    try:
        # 2. fresh D1 data from the terminal (read-only)
        d1 = load_d1_recent(gateway=gw)
        print(f"  D1 bars  : {len(d1)} (last: {d1.iloc[-1]['ts']})")

        # 3. signal on last closed bar
        sig = signal_on_last_closed_bar(d1)
        if sig is None:
            print("  decision : WAIT — no V11 signal on last closed D1 bar")
            # Record the refusal too. A WAIT with a reason is forward
            # evidence; an unrecorded WAIT is indistinguishable from a
            # crashed runner.
            journal_signal(Signal(
                strategy=STRATEGY, strategy_number=VERSION, symbol="XAUUSD",
                broker_symbol=BROKER_SYMBOL, timeframe="D1", direction="WAIT",
                confidence=0.0,
                reason=("no V11 momentum entry on the last closed D1 bar "
                        f"({d1.iloc[-1]['ts']}); WAIT is a valid decision"),
                bar_timestamp=str(d1.iloc[-1]["ts"]), magic=MAGIC,
            ))
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

        # 4b. emit the canonical signal payload (name, number, symbol,
        #     direction, entry, SL, TP, timestamps, confidence, reason)
        #     BEFORE the order, so the decision is recorded even if the
        #     send fails. Confidence is stated, not invented: these are the
        #     frozen validated gates, all of which passed, so the call is
        #     a rules decision rather than a probabilistic one.
        payload = Signal(
            strategy=STRATEGY, strategy_number=VERSION, symbol="XAUUSD",
            broker_symbol=BROKER_SYMBOL, timeframe="D1", direction="BUY",
            entry=req.entry, stop=req.stop, target=req.target,
            lots=req.lots, risk_pct=PARAMS.get("risk_pct", 0.01),
            risk_usd=abs(req.entry - req.stop) * 100.0 * req.lots,
            confidence=1.0, magic=MAGIC,
            reason=(f"D1 momentum bar body_pct={sig['body_pct']:.2f} "
                    f"(threshold {PARAMS.get('body_pct_threshold')}), "
                    f"EMA21>EMA55 up-bias, ATR={sig['atr']:.1f}"),
            reasons=[f"body_pct={sig['body_pct']:.2f}", f"ATR={sig['atr']:.1f}"],
            invalidation="close below the 1.5x ATR stop, or EMA21<EMA55",
            bar_timestamp=sig["bar_ts"],
            simulated=not (not args.dry_run),
        )
        journal_signal(payload)
        write_signal_csv([payload], _chart_csv(), append=True)
        print(f"  payload  : conf={payload.confidence:.2f} r:R={payload.risk_reward:.2f}")

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
