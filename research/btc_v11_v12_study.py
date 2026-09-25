"""BTC V11/V12 quantitative backtest + robustness study (2026).

READ-ONLY. Loads BTCUSD bars from db/trading.db. Places no orders, opens no
positions, and never touches the execution engine or approved.json.

Strategy logic is REUSED, not rewritten: `build_d1_features` and
`compute_d1_signals` are imported from research.v11_d1_momentum so the signal
definition is provably identical to the gold version. Only the COST and
UNIT model is adapted, because gold's PIP=0.10 is meaningless at BTC's
$84,000 price level.

Unit model for BTC (XMGlobal-MT5 BTCUSD#):
  digits=2, point=0.01, contract_size=1.0
  1 "pip" here = 1.00 USD of price movement (so $100 move = 100 pips)
  mean observed spread = 2,121 points = $21.21  <-- DOMINANT cost term
  round-trip cost = 2*spread + 2*slippage, in the same unit

The V11 signal is D1; V12 is the H1 sibling. Both are long-only momentum
breakouts with an EMA trend filter, an ATR stop and a fixed R:R target.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backtest import Trade, compute_metrics
from indicators import ema, atr
from montecarlo import MCConfig, run_monte_carlo

DB = ROOT / "db" / "trading.db"
SYMBOL = "BTCUSD"
OUT = ROOT / "research" / "btc"

# BTC unit model — see module docstring.
PIP = 1.0          # 1 pip = $1.00 of price
POINT = 0.01

V11_PARAMS = dict(body_pct_threshold=0.95, trend_filter=True,
                  min_atr_pct=0.005, rr=2.0, atr_mult_stop=1.5,
                  max_holding_d1=8)
V12_PARAMS = dict(body_pct_threshold=0.95, trend_filter=True,
                  min_atr_pct=0.004, rr=3.0, atr_mult_stop=0.8,
                  max_holding_h1=8)


# ---------------------------------------------------------------- data
def load(tf: str) -> pd.DataFrame:
    con = sqlite3.connect(DB)
    df = pd.read_sql_query(
        "SELECT ts_broker_epoch, open, high, low, close, tick_volume, spread "
        "FROM market_data WHERE symbol=? AND timeframe=? AND source='mt5' "
        "ORDER BY ts_broker_epoch", con, params=(SYMBOL, tf))
    con.close()
    df["ts"] = pd.to_datetime(df["ts_broker_epoch"], unit="s", utc=True)
    return df


def data_quality(df: pd.DataFrame, label: str) -> dict:
    dts = df["ts_broker_epoch"].diff().dropna()
    mode = dts.mode().iloc[0] if len(dts) else 0
    bad = int(((df["high"] < df[["open", "close", "low"]].max(axis=1)) |
               (df["low"] > df[["open", "close", "high"]].min(axis=1))).sum())
    return {"timeframe": label, "bars": len(df),
            "start": str(df["ts"].iloc[0].date()),
            "end": str(df["ts"].iloc[-1].date()),
            "duplicates": int(df["ts_broker_epoch"].duplicated().sum()),
            "gaps": int((dts != mode).sum()) if mode else 0,
            "ohlc_violations": bad,
            "min_close": float(df["close"].min()),
            "max_close": float(df["close"].max()),
            "mean_spread_usd": float((df["spread"] * POINT).mean())}


# ------------------------------------------------------------- features
def features(df: pd.DataFrame, min_atr_pct: float) -> pd.DataFrame:
    """V11/V12 feature build. Identical maths to the gold implementation —
    only the min_atr_pct threshold differs per timeframe (BTC needs a
    larger floor because ATR14 on BTC is ~1-3% of price)."""
    df = df.copy()
    df["ema21"] = ema(df["close"], 21)
    df["ema55"] = ema(df["close"], 55)
    df["atr14"] = atr(df["high"], df["low"], df["close"], 14)
    df["body"] = (df["close"] - df["open"]) / df["open"]
    df["body_abs"] = df["body"].abs()
    df["body_pct"] = df["body_abs"].rolling(60, min_periods=20).rank(pct=True)
    df["trend"] = np.where(df["ema21"] > df["ema55"], "bull",
                            np.where(df["ema21"] < df["ema55"], "bear", "flat"))
    return df


def signals(f: pd.DataFrame, p: dict) -> list[dict]:
    """V11/V12 signal definition, verbatim in structure: top-momentum bar in
    an uptrend, entry at the NEXT bar's open, ATR stop, fixed R:R target."""
    out = []
    for i in range(1, len(f) - 1):
        row = f.iloc[i]
        if p["trend_filter"] and row["trend"] != "bull":
            continue
        if pd.isna(row["body_pct"]) or row["body_pct"] < p["body_pct_threshold"]:
            continue
        if row["body"] <= 0:
            continue
        if pd.isna(row["atr14"]) or row["atr14"] / row["close"] < p["min_atr_pct"]:
            continue
        j = i + 1                      # NO look-ahead: next bar's open
        entry = float(f.iloc[j]["open"])
        stop = entry - p["atr_mult_stop"] * float(row["atr14"])
        risk = entry - stop
        if risk <= 0:
            continue
        out.append({"entry_bar": j, "entry_price": entry, "stop": stop,
                    "target": entry + risk * p["rr"], "direction": "LONG",
                    "d1_body_pct": float(row["body_pct"]),
                    "entry_ts": str(f.iloc[j]["ts"])})
    return out


# ------------------------------------------------------------- backtest
def backtest(f: pd.DataFrame, sigs: list[dict], p: dict,
             spread_pips: float, slippage_pips: float) -> list[Trade]:
    """Long-only, intrabar stop/target, force-close at max holding.

    When both stop and target are touched in the same bar, the CLOSER level
    is assumed first (conservative)."""
    cost = 2 * spread_pips + 2 * slippage_pips
    # V11 holds max_holding_d1 bars, V12 max_holding_h1. Accept either.
    max_hold = p.get("max_holding_d1") or p.get("max_holding_h1")
    trades: list[Trade] = []
    for s in sigs:
        i = s["entry_bar"]
        if i >= len(f) - 1:
            continue
        entry, stop, target = s["entry_price"], s["stop"], s["target"]
        last = min(i + max_hold, len(f) - 1)
        done = False
        for j in range(i + 1, last + 1):
            r = f.iloc[j]
            if r["low"] <= stop and r["high"] >= target:
                # Conservative: assume the worse (closer) level fills first.
                d_stop, d_tgt = abs(entry - stop), abs(target - entry)
                if d_stop <= d_tgt:
                    px, why = stop, "stop"
                else:
                    px, why = target, "target"
            elif r["low"] <= stop:
                px, why = stop, "stop"
            elif r["high"] >= target:
                px, why = target, "target"
            else:
                continue
            gross = px - entry
            trades.append(Trade(
                entry_bar=i, entry_price=entry, side="LONG", stop=stop,
                target=target, exit_bar=j, exit_price=px, exit_reason=why,
                points=gross / POINT, cost_pips=cost,
                net_pips=gross / PIP - cost, duration_bars=j - i))
            done = True
            break
        if not done and last > i:
            r = f.iloc[last]
            gross = float(r["close"]) - entry
            trades.append(Trade(
                entry_bar=i, entry_price=entry, side="LONG", stop=stop,
                target=target, exit_bar=last, exit_price=float(r["close"]),
                exit_reason="end", points=gross / POINT, cost_pips=cost,
                net_pips=gross / PIP - cost, duration_bars=last - i))
    return trades


def _realised_spread_pips(f: pd.DataFrame) -> float:
    """Per-bar recorded spread in BTC pips ($). Using the REAL observed
    spread is the honest choice; a flat assumption would understate cost."""
    return float((f["spread"] * POINT).mean())


# ----------------------------------------------------------------- study
def evaluate(name: str, f: pd.DataFrame, p: dict, slip: float,
             spread_mult: float, label: str) -> dict:
    sigs = signals(f, p)
    spread = _realised_spread_pips(f) * spread_mult
    trades = backtest(f, sigs, p, spread, slip)
    m = compute_metrics(trades)
    mc = (run_monte_carlo(trades, MCConfig(n_iterations=2000, seed=42))
          if trades else None)
    return {"name": name, "cost_case": label,
            "spread_usd": spread, "slippage_usd": slip,
            "cost_pips_roundtrip": 2 * spread + 2 * slip,
            "n_signals": len(sigs), "metrics": m,
            "mc": {"net_p5": mc.net_p5, "profit_factor_p5": mc.profit_factor_p5,
                   "ruin_prob": mc.ruin_prob, "is_robust": mc.is_robust}
            if mc else None,
            "trades": trades}


def walk_forward(f: pd.DataFrame, p: dict, split_frac: float = 0.70,
                 spread_mult: float = 1.0, slip: float = 5.0) -> dict:
    """Chronological holdout: train on the first `split_frac`, measure the
    untouched remainder. Parameters are NEVER re-fit on the test side.

    A sub-window with no signals yields compute_metrics() == {"total_trades":0},
    so every metric comes back None. That is a REAL finding (the edge did not
    persist), not a crash condition — so normalise None to 0.0 here and let
    the report show a genuine zero rather than blowing up.
    """
    cut = int(len(f) * split_frac)
    is_f, oos_f = f.iloc[:cut], f.iloc[cut:]
    is_res = evaluate("IS", is_f, p, slip, spread_mult, "wf")
    oos_res = evaluate("OOS", oos_f, p, slip, spread_mult, "wf")

    def g(res, key, default=0.0):
        v = res["metrics"].get(key)
        if v is None or v != v:          # None or NaN
            return default
        if v in (float("inf"), float("-inf")):
            return default              # no losses in-window: not "infinite edge"
        return v

    is_n, oos_n = g(is_res, "net_pips"), g(oos_res, "net_pips")
    return {"is_net_pips": is_n, "oos_net_pips": oos_n,
            "is_trades": is_res["metrics"].get("total_trades", 0),
            "oos_trades": oos_res["metrics"].get("total_trades", 0),
            "is_pf": g(is_res, "profit_factor"),
            "oos_pf": g(oos_res, "profit_factor"),
            "is_win": g(is_res, "win_rate"),
            "oos_win": g(oos_res, "win_rate"),
            "degradation": (1 - oos_n / is_n) if is_n else float("nan")}


def cost_stress(f: pd.DataFrame, p: dict) -> list[dict]:
    """Base vs stress: widen spread and add slippage. The point is to find
    whether any edge survives the real cost floor."""
    rows = []
    for label, mult, slip in [("BASE (real spread)", 1.0, 5.0),
                              ("STRESS 1.5x spread", 1.5, 10.0),
                              ("STRESS 2x spread", 2.0, 20.0)]:
        r = evaluate(label, f, p, slip, mult, label)
        rows.append({"cost_case": r["cost_case"],
                     "spread_usd": r["spread_usd"],
                     "cost_pips_roundtrip": r["cost_pips_roundtrip"],
                     "net_pips": r["metrics"].get("net_pips"),
                     "pf": r["metrics"].get("profit_factor"),
                     "trades": r["metrics"].get("total_trades", 0),
                     "win": r["metrics"].get("win_rate")})
    return rows


def regime_split(f: pd.DataFrame, trades: list[Trade]) -> dict:
    """Split realised P&L by the ATR regime prevailing at entry."""
    if not trades:
        return {}
    buckets = {"high_vol": [], "low_vol": []}
    med = f["atr14"].median()
    for t in trades:
        a = float(f.iloc[t.entry_bar]["atr14"])
        c = float(f.iloc[t.entry_bar]["close"])
        key = "high_vol" if a / c > med / f["close"].median() else "low_vol"
        buckets[key].append(t.net_pips)
    return {k: {"n": len(v), "net": sum(v),
                "avg": sum(v) / len(v) if v else 0.0}
            for k, v in buckets.items() if v}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(OUT))
    args = ap.parse_args()
    outdir = Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)

    d1, h1 = load("D1"), load("H1")
    print("=" * 72)
    print("BTC V11 vs V12 — QUANTITATIVE BACKTEST & ROBUSTNESS STUDY")
    print("=" * 72)
    print(f"generated {datetime.now(timezone.utc).isoformat(timespec='seconds')}")
    print(f"git commit: {_git()}")

    print("\n[1] DATA QUALITY")
    qual = {"D1": data_quality(d1, "D1"), "H1": data_quality(h1, "H1")}
    for k, q in qual.items():
        print(f"  {k}: {q['bars']} bars {q['start']}->{q['end']} "
              f"dup={q['duplicates']} gaps={q['gaps']} "
              f"ohlc_bad={q['ohlc_violations']} "
              f"close {q['min_close']:.0f}..{q['max_close']:.0f} "
              f"mean_spread=${q['mean_spread_usd']:.2f}")

    results = {}
    for name, raw, p, hold_key in (("V11", d1, V11_PARAMS, "max_holding_d1"),
                                   ("V12", h1, V12_PARAMS, "max_holding_h1")):
        print(f"\n{'=' * 72}\n[{name}] {p[hold_key]}-bar hold, "
              f"R:R {p['rr']}, {p['atr_mult_stop']}x ATR stop\n{'=' * 72}")
        f = features(raw, p["min_atr_pct"])
        base = evaluate(name, f, p, 5.0, 1.0, "BASE")
        m = base["metrics"]
        print(f"  signals: {base['n_signals']}   trades: {m.get('total_trades')}")
        print(f"  cost: spread ${base['spread_usd']:.2f} + slippage $5 -> "
              f"${base['cost_pips_roundtrip']:.2f} round-trip "
              f"({100 * base['cost_pips_roundtrip'] / f['close'].median():.3f}% of price)")
        # compute_metrics returns win_rate as a FRACTION and the drawdown
        # key is max_drawdown_pips (not max_drawdown).
        print(f"  net {m.get('net_pips'):.1f} pips | PF {m.get('profit_factor'):.3f} | "
              f"win {100 * m.get('win_rate', 0):.1f}% | "
              f"maxDD {m.get('max_drawdown_pips'):.1f} | "
              f"sharpe {m.get('sharpe'):.3f}")

        print("\n  COST STRESS")
        stress = cost_stress(f, p)
        for r in stress:
            print(f"    {r['cost_case']:<20} cost ${r['cost_pips_roundtrip']:>8.2f} "
                  f"trades {r['trades']:>3} net {r['net_pips']:.1f} "
                  f"PF {r['pf'] if r['pf'] == r['pf'] else float('nan'):.3f}")

        print("\n  WALK-FORWARD (70/30 chronological, params never re-fit)")
        wf = walk_forward(f, p)
        print(f"    IS : {wf['is_trades']} trades net {wf['is_net_pips']:.1f} "
              f"PF {wf['is_pf']:.3f} win {100 * wf['is_win']:.1f}%")
        print(f"    OOS: {wf['oos_trades']} trades net {wf['oos_net_pips']:.1f} "
              f"PF {wf['oos_pf']:.3f} win {100 * wf['oos_win']:.1f}%")
        print(f"    degradation: {wf['degradation']}")

        print("\n  MONTE CARLO (2000 iterations, seed 42)")
        if base["mc"]:
            mc = base["mc"]
            print(f"    net_p5 {mc['net_p5']} | PF_p5 {mc['profit_factor_p5']} "
                  f"| ruin {100 * mc['ruin_prob']:.1f}% | "
                  f"robust={mc['is_robust']}")

        reg = regime_split(f, base["trades"])
        if reg:
            print("\n  REGIME SPLIT")
            for k, v in reg.items():
                print(f"    {k:<10} n={v['n']:>3} net={v['net']:.1f} "
                      f"avg={v['avg']:.1f}")

        results[name] = {"quality": qual, "params": p, "base": {
            k: v for k, v in base.items() if k != "trades"},
            "cost_stress": stress, "walk_forward": wf, "regime": reg}

    _verdict(results)
    raw = {"generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "commit": _git(), "results": results}
    (outdir / "btc_v11_v12_results.json").write_text(
        json.dumps(raw, indent=2, default=str), encoding="utf-8")
    print(f"\nraw results -> {outdir / 'btc_v11_v12_results.json'}")
    return 0


def _git() -> str:
    import subprocess
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                              cwd=ROOT, capture_output=True, text=True,
                              timeout=15).stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def _verdict(results: dict) -> None:
    print(f"\n{'=' * 72}\nVERDICT\n{'=' * 72}")
    for name, r in results.items():
        b = r["base"]["metrics"]
        wf, mc = r["walk_forward"], r["base"]["mc"]
        flags = []
        if (b.get("profit_factor") or 0) < 1.0:
            flags.append("PF<1")
        if wf["oos_net_pips"] <= 0:
            flags.append("OOS net<=0")
        if wf["oos_trades"] < 5:
            flags.append("too few OOS trades")
        if mc and not mc["is_robust"]:
            flags.append("MC not robust")
        verdict = "NO ROBUST EDGE" if flags else "candidate passed all flags"
        print(f"  {name}: {verdict}")
        for f in flags:
            print(f"     - {f}")


if __name__ == "__main__":
    sys.exit(main())
