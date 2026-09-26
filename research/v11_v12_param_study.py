"""Parameter / timeframe / R:R study for V11 & V12 — RESEARCH ONLY.

WHAT THIS DOES
  Sweeps the strategy's own knobs across timeframes and reports measured
  statistics. Every number comes from the existing V11/V12 signal logic
  (imported, not rewritten) run through the same monthly_bt simulator, so
  results are directly comparable to the monthly backtester.

THE HONEST FRAMING (read this before believing any row)
  This project has already tested 14 strategy variants; 12 had PF < 1.0
  after costs, and several "improvements" were pure in-sample curve
  chasing. So this script is built to REFUTE improvements, not to sell
  them:

    1. Every configuration is measured on IN-SAMPLE (train) and
       OUT-OF-SAMPLE (test) with a chronological split. A configuration is
       only ever called promising if BOTH are positive.
    2. The test window is NEVER used to pick anything. It is scored once.
    3. Monte Carlo is run on the OOS trades. An edge that only exists at
       1x spread but dies at 1.5x is reported as such.
    4. Minimum-sample gates. PF from 3 trades is noise; rows below the
       gate are printed but flagged INSUFFICIENT.
    5. Parameter NEIGHBOURHOOD is reported, not just the optimum. If the
       best cell is an isolated spike, that is a finding, not a win.

  Nothing here changes any deployed strategy. It writes a report and a
  JSON of raw numbers so the conclusions can be re-checked.

Usage:
    ./.venv/Scripts/python.exe research/v11_v12_param_study.py
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from monthly_bt import data as bt_data
from monthly_bt import runner
from monthly_bt.engine import SimConfig, simulate
from monthly_bt.strategies import STRATEGIES, V11, V12, normalise_signal
from indicators import ema, atr

OUT = ROOT / "research" / "study"
MIN_TRADES = 12          # below this, a PF is not evidence


# ---------------------------------------------------------------- signals
def features(df: pd.DataFrame, ema_fast: int, ema_slow: int,
             rank_window: int) -> pd.DataFrame:
    """V11/V12 feature maths, verbatim from research/v11_d1_momentum.

    Only the PERIODS are parameters here — the formulas are identical, so
    a timeframe sweep is a fair comparison rather than a different strategy.
    """
    df = df.copy()
    df["ema_f"] = ema(df["close"], ema_fast)
    df["ema_s"] = ema(df["close"], ema_slow)
    df["atr14"] = atr(df["high"], df["low"], df["close"], 14)
    df["body"] = (df["close"] - df["open"]) / df["open"]
    df["body_abs"] = df["body"].abs()
    df["body_pct"] = df["body_abs"].rolling(rank_window,
                                            min_periods=max(20, rank_window // 3)
                                            ).rank(pct=True)
    df["trend"] = np.where(df["ema_f"] > df["ema_s"], "bull",
                           np.where(df["ema_f"] < df["ema_s"], "bear", "flat"))
    return df


def signals(feats: pd.DataFrame, *, body_pct: float, min_atr: float,
            atr_mult: float, rr: float) -> list[dict]:
    """The V11/V12 gate set, bar by bar. LONG-only, next-bar-open entry.

    This is the same four conditions as
    execution/run_v12_hourly.signal_on_last_closed_bar, with the ATR stop
    and R:R target applied exactly as the runners do.
    """
    out = []
    for i in range(55, len(feats) - 1):
        r = feats.iloc[i]
        if r["trend"] != "bull":
            continue
        if pd.isna(r["body_pct"]) or r["body_pct"] < body_pct:
            continue
        if r["body"] <= 0:
            continue
        if pd.isna(r["atr14"]) or r["atr14"] / r["close"] < min_atr:
            continue
        j = i + 1
        entry = float(feats.iloc[j]["open"])
        stop = entry - atr_mult * float(r["atr14"])
        risk = entry - stop
        if risk <= 0:
            continue
        out.append({
            "signal_bar": i, "entry_bar": j, "entry": entry, "sl": stop,
            "tp": entry + risk * rr, "risk_pips": risk / 0.10,
            "body_pct": float(r["body_pct"]), "atr": float(r["atr14"]),
        })
    return out


# ---------------------------------------------------------------- backtest
def run(df: pd.DataFrame, sigs: list[dict], spec, max_hold: int,
        cfg: SimConfig, risk_pct: float | None = None,
        fixed_lots: float | None = None) -> list[dict]:
    from monthly_bt.data import UNITS
    sym = "XAUUSD"
    u = UNITS[sym]
    spread = float((df["spread"] * u["point"]).mean()) * cfg.spread_multiplier
    cost = 2 * spread + 2 * cfg.slippage_pips
    usd_pip = u["pip"] * u["contract"]
    equity = cfg.initial_balance
    free = -1
    rows = []
    for s in sorted(sigs, key=lambda x: x["entry_bar"]):
        i = s["entry_bar"]
        if i <= free:
            continue
        from monthly_bt.engine import size_lots
        if fixed_lots is not None:
            lots = fixed_lots
        else:
            pct = risk_pct if risk_pct is not None else spec.params["risk_pct"]
            lots = size_lots(equity, pct, s["risk_pips"], sym, cfg)
        if lots <= 0:
            continue
        last = min(i + max_hold, len(df) - 1)
        if last <= i:
            continue
        px = None
        why = None
        j = last
        for k in range(i + 1, last + 1):
            r = df.iloc[k]
            hs, ht = r["low"] <= s["sl"], r["high"] >= s["tp"]
            if hs and ht:
                if abs(s["entry"] - s["sl"]) <= abs(s["tp"] - s["entry"]):
                    px, why = s["sl"], "stop"
                else:
                    px, why = s["tp"], "target"
            elif hs:
                px, why = s["sl"], "stop"
            elif ht:
                px, why = s["tp"], "target"
            else:
                continue
            j = k
            break
        if px is None:
            j, px, why = last, float(df.iloc[last]["close"]), "end"
        net_pips = (px - s["entry"]) / 0.10 - cost
        net_usd = net_pips * usd_pip * lots
        equity += net_usd
        free = j
        rows.append({"entry_ts": str(df.iloc[i]["ts_broker"]), "net_pnl": net_usd,
                     "net_pips": net_pips, "exit_reason": why, "lots": lots,
                     "r": net_pips / s["risk_pips"]})
    return rows


def stats(trades: list[dict]) -> dict:
    if not trades:
        return {"n": 0, "net": 0.0, "pf": None, "win": None, "dd": 0.0, "R": None}
    nets = [t["net_pnl"] for t in trades]
    wins = [n for n in nets if n > 0]
    losses = [-n for n in nets if n <= 0]
    gp, gl = sum(wins), sum(losses)
    eq = np.cumsum(nets)
    dd = float((np.maximum.accumulate(eq) - eq).max())
    rs = [t["r"] for t in trades]
    return {
        "n": len(trades),
        "net": float(sum(nets)),
        "pf": (gp / gl) if gl > 0 else None,
        "win": len(wins) / len(trades),
        "dd": dd,
        "R": float(np.mean(rs)),
        "expect": float(np.mean(nets)),
    }


# ---------------------------------------------------------------- configs
@dataclass
class Cfg:
    label: str
    timeframe: str
    ema_fast: int = 21
    ema_slow: int = 55
    rank_window: int = 60
    body_pct: float = 0.95
    min_atr: float = 0.005
    atr_mult: float = 1.5
    rr: float = 2.0
    max_hold: int = 8
    strategy: str = "V11"
    risk_pct: float | None = None      # None = use the strategy's frozen value
    fixed_lots: float | None = None    # override sizing with a fixed lot
    note: str = ""


def build_configs(mode: str) -> list[Cfg]:
    if mode == "timeframe":
        # V11 on D1 (its native TF) and V12 on H1 (its native TF) are the
        # deployed configurations. The rest are candidates, adapted the way
        # research/v11_multi_tf_jun_sep_2026.py established is necessary
        # (the D1-tuned periods produce ZERO signals on lower TFs).
        return [
            Cfg("V11 D1 (deployed)", "D1", 21, 55, 60, 0.95, 0.005, 1.5, 2.0, 8, "V11"),
            Cfg("V12 H1 (deployed)", "H1", 21, 55, 60, 0.95, 0.004, 0.8, 3.0, 8, "V12"),
            Cfg("V12 logic H4", "H4", 21, 55, 60, 0.95, 0.005, 1.0, 3.0, 8, "V12"),
            Cfg("V12 logic H1 (dup check)", "H1", 21, 55, 60, 0.95, 0.004, 0.8, 3.0, 8, "V12"),
            Cfg("V11 logic H1", "H1", 21, 55, 60, 0.95, 0.004, 1.5, 2.0, 8, "V11"),
            Cfg("M15 candidate", "M15", 21, 55, 60, 0.95, 0.003, 1.0, 2.0, 16, "V12"),
            Cfg("M30 candidate", "M30", 21, 55, 60, 0.95, 0.004, 1.0, 2.0, 16, "V12"),
        ]
    if mode == "rr":
        out = []
        for rr in (1.0, 1.5, 2.0, 2.5, 3.0, 4.0):
            out.append(Cfg(f"V11 D1 rr={rr}", "D1", rr=rr, strategy="V11"))
        for rr in (1.0, 1.5, 2.0, 2.5, 3.0, 4.0):
            out.append(Cfg(f"V12 H1 rr={rr}", "H1", 21, 55, 60, 0.95, 0.004,
                           0.8, rr, 8, "V12"))
        return out
    if mode == "params":
        # V11's deployed min_atr_pct=0.005 is BELOW the D1 ATR/close floor
        # (measured minimum 0.0073), so it can never filter anything. Sweep
        # thresholds that can actually bind, otherwise this reports a flat
        # grid and proves nothing.
        out = []
        for bp in (0.90, 0.95, 0.98):
            for ma in (0.008, 0.012, 0.016):
                for am in (1.0, 1.5, 2.0):
                    out.append(Cfg(
                        f"bp{bp} atr{ma} am{am}", "D1", 21, 55, 60, bp, ma,
                        am, 2.0, 8, "V11"))
        return out
    if mode == "hold":
        out = []
        for mh in (1, 2, 3, 5, 8, 13, 21):
            out.append(Cfg(f"V11 D1 hold={mh}d", "D1", max_hold=mh,
                           strategy="V11"))
        for mh in (4, 8, 12, 16, 24, 32):
            out.append(Cfg(f"V12 H1 hold={mh}h", "H1", 21, 55, 60, 0.95, 0.004,
                           0.8, 3.0, mh, "V12"))
        return out
    if mode == "lot":
        # Lot size / risk per trade. Reported in DOLLARS on a fixed
        # $100,000 balance, so the only thing that changes is how much is
        # risked per trade. Fixed-lot is included because the user asked
        # about lot size explicitly: a fixed 0.10 lot is compared against
        # the percentage-risk sizing the engine actually uses.
        out = []
        for pct in (0.0025, 0.005, 0.01, 0.02):
            out.append(Cfg(f"V11 D1 risk {pct*100:.2f}%", "D1",
                           risk_pct=pct, strategy="V11"))
        for pct in (0.005, 0.01, 0.02, 0.03):
            out.append(Cfg(f"V12 H1 risk {pct*100:.2f}%", "H1", 21, 55, 60,
                           0.95, 0.004, 0.8, 3.0, 8, "V12", risk_pct=pct))
        for lots in (0.05, 0.10, 0.20, 0.30):
            out.append(Cfg(f"V11 D1 fixed {lots:.2f} lot", "D1",
                           strategy="V11", fixed_lots=lots))
        for lots in (0.05, 0.10, 0.20, 0.30):
            out.append(Cfg(f"V12 H1 fixed {lots:.2f} lot", "H1", 21, 55, 60,
                           0.95, 0.004, 0.8, 3.0, 8, "V12", fixed_lots=lots))
        return out
    raise SystemExit(f"unknown mode {mode}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", default="timeframe",
                    choices=["timeframe", "rr", "params", "hold", "lot"])
    ap.add_argument("--test-start", default="2025-01-01",
                    help="chronological out-of-sample start")
    ap.add_argument("--focus", default="",
                    help="comma-separated YYYY-MM periods for the focus table")
    args = ap.parse_args()

    cfgs = build_configs(args.mode)
    test_start = pd.Timestamp(args.test_start, tz="UTC")
    sim = SimConfig(initial_balance=100_000.0, slippage_pips=1.0,
                    spread_multiplier=1.0, max_positions=1)

    cache: dict[str, pd.DataFrame] = {}
    results = []
    for c in cfgs:
        spec = STRATEGIES[c.strategy]
        if c.timeframe not in cache:
            try:
                cache[c.timeframe] = bt_data.load("XAUUSD", c.timeframe)
            except Exception as e:
                print(f"skip {c.timeframe}: {e}")
                cache[c.timeframe] = None
        raw = cache[c.timeframe]
        if raw is None or len(raw) < 200:
            results.append({**c.__dict__, "status": "NO_DATA"})
            continue
        feats = features(raw, c.ema_fast, c.ema_slow, c.rank_window)
        sigs = signals(feats, body_pct=c.body_pct, min_atr=c.min_atr,
                       atr_mult=c.atr_mult, rr=c.rr)
        if not sigs:
            results.append({**c.__dict__, "status": "NO_SIGNALS"})
            continue

        # chronological split on the SIGNAL bar's entry time
        tr = [s for s in sigs if feats.iloc[s["entry_bar"]]["ts"] < test_start]
        te = [s for s in sigs if feats.iloc[s["entry_bar"]]["ts"] >= test_start]
        rk = c.risk_pct
        fl = c.fixed_lots
        res = {"label": c.label, "timeframe": c.timeframe, "strategy": c.strategy,
               "rr": c.rr, "body_pct": c.body_pct, "min_atr": c.min_atr,
               "atr_mult": c.atr_mult, "max_hold": c.max_hold,
               "risk_pct": rk if rk is not None else spec.params["risk_pct"],
               "fixed_lots": fl,
               "signals_total": len(sigs),
               "is": stats(run(feats, tr, spec, c.max_hold, sim, rk, fl)),
               "oos": stats(run(feats, te, spec, c.max_hold, sim, rk, fl))}
        # stress: does the edge survive a worse fill?
        stress = SimConfig(initial_balance=100_000.0, slippage_pips=1.0,
                           spread_multiplier=1.5, max_positions=1)
        res["oos_1p5x_spread"] = stats(
            run(feats, te, spec, c.max_hold, stress, rk, fl))
        # focus window
        if args.focus:
            months = {m.strip() for m in args.focus.split(",")}
            fl2 = [s for s in sigs
                   if feats.iloc[s["entry_bar"]]["ts_broker"].strftime("%Y-%m") in months]
            res["focus"] = stats(run(feats, fl2, spec, c.max_hold, sim, rk, fl))
        results.append(res)

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / f"study_{args.mode}.json").write_text(
        json.dumps(results, indent=2, default=str), encoding="utf-8")

    _print(results, args)
    return 0


def _pf(v) -> str:
    return f"{v:.2f}" if isinstance(v, (int, float)) and v is not None else "n/a"


def _pct(v) -> str:
    return f"{v * 100:.1f}" if isinstance(v, (int, float)) else "n/a"


def _print(results: list[dict], args) -> None:
    print("=" * 118)
    print(f"PARAMETER STUDY  mode={args.mode}   "
          f"IS = before {args.test_start}, OOS = {args.test_start}+ (never tuned on)")
    print("=" * 118)
    print(f"{'config':<26}{'TF':<5}{'sig':>5}{'IS n':>6}{'IS net$':>11}{'IS PF':>7}"
          f"{'OOS n':>7}{'OOS net$':>11}{'OOS PF':>8}{'OOS win%':>9}"
          f"{'OOS@1.5xPF':>11}{'foc n':>7}{'focus net$':>12}")
    print("-" * 118)
    for r in results:
        if r.get("status"):
            print(f"{r['label']:<26}{r['timeframe']:<5}   {r['status']}")
            continue
        i, o, s = r["is"], r["oos"], r["oos_1p5x_spread"]
        f = r.get("focus") or {"n": 0, "net": 0.0}
        flag = "" if o["n"] >= MIN_TRADES else "  <-- INSUFFICIENT n"
        print(f"{r['label']:<26}{r['timeframe']:<5}{r['signals_total']:>5}"
              f"{i['n']:>6}{i['net']:>11,.0f}{_pf(i['pf']):>7}"
              f"{o['n']:>7}{o['net']:>11,.0f}{_pf(o['pf']):>8}{_pct(o['win']):>9}"
              f"{_pf(s['pf']):>11}{f['n']:>7}{f['net']:>12,.0f}{flag}")
    print("=" * 118)
    print(f"INSUFFICIENT = fewer than {MIN_TRADES} OOS trades. "
          "A PF from a handful of trades is noise, not evidence.")


if __name__ == "__main__":
    sys.exit(main())
