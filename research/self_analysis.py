"""Self-analysis: run this session's candidates through the §25 gate.

CLAUDE.md §25 requires proposals to be evaluated by a gate that REFUSES
weak candidates. `self_improvement.evaluate_gates` is that gate. This
script feeds the candidates that came out of the May-Aug study through it
and records the verdicts, so the loop is closed with the repo's own
criteria rather than my opinion.

SAFETY: read-only. It calls the gate and PRINTS. It does not call
promote(), does not touch approved.json, and cannot place an order.
Promoting anything remains a manual, named-human action.

Usage:
    ./.venv/Scripts/python.exe research/self_analysis.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from monthly_bt import data as bt_data
from monthly_bt.engine import SimConfig
from monthly_bt.strategies import STRATEGIES
from research.v11_v12_param_study import features, run, signals

OUT = ROOT / "research" / "study"
SPLIT = pd.Timestamp("2025-01-01", tz="UTC")

# The candidates the May-Aug study surfaced, in the order they deserve
# scrutiny. Each is measured on the same chronological split.
CANDIDATES = [
    # label,                tf,  strategy, bp,   min_atr, am,  rr,  hold, risk
    ("V11 D1 deployed (baseline)", "D1", "V11", 0.95, 0.005, 1.5, 2.0, 8, 0.01),
    ("V11 D1 hold=2d",             "D1", "V11", 0.95, 0.005, 1.5, 2.0, 2, 0.01),
    ("V11 D1 hold=3d rr=1.5",      "D1", "V11", 0.95, 0.005, 1.5, 1.5, 3, 0.01),
    ("V11 D1 hold=13d",            "D1", "V11", 0.95, 0.005, 1.5, 2.0, 13, 0.01),
    ("V12 H1 deployed (baseline)", "H1", "V12", 0.95, 0.004, 0.8, 3.0, 8, 0.02),
    ("V12 H1 hold=16h",            "H1", "V12", 0.95, 0.004, 0.8, 3.0, 16, 0.02),
    ("V12 H1 hold=16h rr=2.5",     "H1", "V12", 0.95, 0.004, 0.8, 2.5, 16, 0.02),
]


class _T:
    """Adapter so a study trade row can feed backtest/montecarlo layers."""
    def __init__(self, t):
        self.net_pips = t["net_pips"]
        self.net_usd = t["net_pnl"]


def evaluate(label, tf, strat, bp, ma, am, rr, hold, risk_pct, sim):
    from montecarlo import MCConfig, run_monte_carlo

    spec = STRATEGIES[strat]
    feats = features(bt_data.load("XAUUSD", tf), 21, 55, 60)
    sigs = signals(feats, body_pct=bp, min_atr=ma, atr_mult=am, rr=rr)
    is_sigs = [s for s in sigs if feats.iloc[s["entry_bar"]]["ts"] < SPLIT]
    oos = [s for s in sigs if feats.iloc[s["entry_bar"]]["ts"] >= SPLIT]

    is_tr = run(feats, is_sigs, spec, hold, sim, risk_pct)
    oos_tr = run(feats, oos, spec, hold, sim, risk_pct)

    is_net = sum(t["net_pips"] for t in is_tr)
    oos_net = sum(t["net_pips"] for t in oos_tr)
    is_pf = pips_pf(is_tr)
    oos_mc = (run_monte_carlo([_T(t) for t in oos_tr],
                              MCConfig(n_iterations=2000, seed=42))
              if len(oos_tr) >= 5 else None)
    return {
        "label": label, "timeframe": tf, "strategy": strat,
        "is_trades": len(is_tr), "oos_trades": len(oos_tr),
        "is_net_pips": is_net, "oos_net_pips": oos_net,
        "is_pf": is_pf, "oos_pf": pips_pf(oos_tr),
        "oos_degradation": (1 - oos_net / is_net) if is_net else float("nan"),
        "mc_ruin_prob": oos_mc.ruin_prob if oos_mc else None,
        "mc_robust": oos_mc.is_robust if oos_mc else None,
        "_mc": oos_mc,          # excluded from the JSON dump
        "params": {"body_pct": bp, "min_atr": ma, "atr_mult": am,
                   "rr": rr, "max_hold": hold, "risk_pct": risk_pct},
    }


def pips_pf(trades: list[dict]) -> float | None:
    """Profit factor over net pips. Undefined (not inf) with no losers, so
    a candidate with no losses cannot sail the PF gate for the wrong
    reason."""
    w = [t["net_pips"] for t in trades if t["net_pips"] > 0]
    l = -sum(t["net_pips"] for t in trades if t["net_pips"] <= 0)
    return (sum(w) / l) if l > 0 else None


def gate_inputs(r: dict) -> tuple[dict, dict, object]:
    """Shape a candidate into the three arguments evaluate_gates expects.

    The gate was written for backtest.compute_metrics() +
    validation.summarize_walk_forward() + montecarlo output, so those are
    the exact key names used here rather than inventing new ones.
    """
    backtest_metrics = {"total_trades": r["is_trades"],
                        "profit_factor": r["is_pf"]}
    wf_summary = {"oos_total_trades": r["oos_trades"],
                  "oos_mean_net_pips": r["oos_net_pips"],
                  "oos_degradation": r["oos_degradation"]}
    return backtest_metrics, wf_summary, r.get("_mc")


def main() -> int:
    from self_improvement import Gates, evaluate_gates

    sim = SimConfig(initial_balance=100_000.0, slippage_pips=1.0,
                    spread_multiplier=1.0, max_positions=1)
    gates = Gates()

    print("=" * 116)
    print("SELF-ANALYSIS — this session's candidates through the §25 acceptance gate")
    print("=" * 116)
    print(f"split: in-sample < {SPLIT.date()}, out-of-sample >= {SPLIT.date()}")
    print(f"gates: min_trades={gates.min_trades} min_oos={gates.min_oos_trades} "
          f"min_PF={gates.min_profit_factor} max_degradation={gates.max_oos_degradation} "
          f"max_ruin={gates.max_ruin_prob} mc_robust={gates.require_mc_robust}")
    print("=" * 116)

    rows = []
    for c in CANDIDATES:
        r = evaluate(*c, sim)
        bm, wf, mc = gate_inputs(r)
        verdict = evaluate_gates(bm, wf, mc, gates)
        r["accepted"] = verdict.accepted
        r["gate_failures"] = [f"{g.name}: {g.detail}"
                              for g in verdict.results if not g.passed]
        r["gate_reason"] = verdict.reason
        rows.append(r)
        mark = "ACCEPT" if verdict.accepted else "REJECT"
        print(f"\n{r['label']}  [{r['timeframe']} / {r['strategy']}]  -> {mark}")
        print(f"   IS  {r['is_trades']:>3} trades  net {r['is_net_pips']:>9,.0f} pips"
              f"  PF {_pf(r['is_pf'])}")
        print(f"   OOS {r['oos_trades']:>3} trades  net {r['oos_net_pips']:>9,.0f} pips"
              f"  PF {_pf(r['oos_pf'])}  degradation {_d(r['oos_degradation'])}"
              f"  MC_robust={r['mc_robust']}")
        for g in verdict.results:
            if not g.passed:
                print(f"     FAIL {g.name}: {g.detail}")

    print()
    print("=" * 116)
    acc = [r for r in rows if r["accepted"]]
    print(f"RESULT: {len(acc)}/{len(rows)} candidates passed the gate.")
    if not acc:
        print("        Nothing to promote. The best candidates must be paper-")
        print("        traded first; no change to a live strategy is warranted.")
    print("=" * 116)
    print("No strategy was modified. approved.json untouched. No order placed.")
    print("Promotion remains a manual, named-human action (self_improvement.promote).")

    OUT.mkdir(parents=True, exist_ok=True)
    serial = [{k: v for k, v in r.items() if k != "_mc"} for r in rows]
    (OUT / "self_analysis.json").write_text(
        json.dumps(serial, indent=2, default=str), encoding="utf-8")
    print(f"\nwritten: {OUT / 'self_analysis.json'}")
    return 0


def _pf(v):
    return f"{v:.2f}" if isinstance(v, (int, float)) and v is not None else "n/a"


def _d(v):
    return f"{v:.2f}" if isinstance(v, (int, float)) and v == v else "n/a"


if __name__ == "__main__":
    sys.exit(main())
