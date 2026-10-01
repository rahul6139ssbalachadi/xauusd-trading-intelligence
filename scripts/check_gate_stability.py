"""Why did the §25 gate verdict on V12 flip from REJECT to ACCEPT?

A prior session recorded: V12 REJECTED — IS PF 0.98 (gate needs >= 1.20) and
OOS degradation 69.67 (needs <= 0.50). After the 2026-10-01 ingest the same
gate reports IS PF 1.31 / OOS PF 1.33 / degradation -2.38 -> ACCEPT.

H1 gained 2,541 bars and its start moved 2016-09-01 -> 2016-05-06. If a
4-month head extension flips a gate verdict, the metric is sample-sensitive
and the ACCEPT is not robust — which is itself the finding worth reporting.

Runs the gate on BOTH spans so the difference is attributable to the data.
Read-only.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd

from monthly_bt import data as bt_data
from monthly_bt.engine import SimConfig
from monthly_bt.strategies import STRATEGIES
from research.v11_v12_param_study import features, run, signals
from research.self_analysis import _T, gate_inputs, pips_pf
from self_improvement import Gates, evaluate_gates

OLD_FIRST = pd.Timestamp("2016-09-01", tz="UTC")
OLD_LAST = pd.Timestamp("2026-08-28", tz="UTC")
SPLIT = pd.Timestamp("2025-01-01", tz="UTC")

# V12 H1 deployed baseline params
V12 = dict(bp=0.95, ma=0.004, am=0.8, rr=3.0, hold=8, risk_pct=0.02)


def evaluate_span(label, h1_df, gates, sim):
    spec = STRATEGIES["V12"]
    feats = features(h1_df, 21, 55, 60)
    sigs = signals(feats, body_pct=V12["bp"], min_atr=V12["ma"],
                   atr_mult=V12["am"], rr=V12["rr"])
    is_sigs = [s for s in sigs if feats.iloc[s["entry_bar"]]["ts"] < SPLIT]
    oos_sigs = [s for s in sigs if feats.iloc[s["entry_bar"]]["ts"] >= SPLIT]

    is_tr = run(feats, is_sigs, spec, V12["hold"], sim, V12["risk_pct"])
    oos_tr = run(feats, oos_sigs, spec, V12["hold"], sim, V12["risk_pct"])

    is_net = sum(t["net_pips"] for t in is_tr)
    oos_net = sum(t["net_pips"] for t in oos_tr)

    from montecarlo import MCConfig, run_monte_carlo
    mc = (run_monte_carlo([_T(t) for t in oos_tr],
                          MCConfig(n_iterations=2000, seed=42))
          if len(oos_tr) >= 5 else None)

    r = {"is_trades": len(is_tr), "oos_trades": len(oos_tr),
         "is_net_pips": is_net, "oos_net_pips": oos_net,
         "is_pf": pips_pf(is_tr), "oos_pf": pips_pf(oos_tr),
         "oos_degradation": (1 - oos_net / is_net) if is_net else float("nan"),
         "mc_robust": mc.is_robust if mc else None, "_mc": mc}

    bm, wf, m = gate_inputs(r)
    verdict = evaluate_gates(bm, wf, m, gates)

    print(f"{label}")
    print(f"   bars        : {len(h1_df):>6d}")
    print(f"   IS  {len(is_tr):>3} trades  net {is_net:>9,.0f} pips  "
          f"PF {r['is_pf'] if r['is_pf'] else float('nan'):.2f}")
    print(f"   OOS {len(oos_tr):>3} trades  net {oos_net:>9,.0f} pips  "
          f"PF {r['oos_pf'] if r['oos_pf'] else float('nan'):.2f}  "
          f"deg {r['oos_degradation']:.2f}  MC={r['mc_robust']}")
    print(f"   -> {'ACCEPT' if verdict.accepted else 'REJECT'}")
    for f in verdict.results:
        if not f.passed:
            print(f"        FAIL {f.name}: {f.detail}")
    print()
    return verdict.accepted, r


def main() -> int:
    sim = SimConfig(initial_balance=100_000.0, slippage_pips=1.0,
                    spread_multiplier=1.0, max_positions=1)
    gates = Gates()

    full = bt_data.load("XAUUSD", "H1")
    ts = pd.to_datetime(full["ts_broker_epoch"], unit="s", utc=True)
    old = full[(ts >= OLD_FIRST) & (ts <= OLD_LAST)].reset_index(drop=True)

    print("=" * 78)
    print("V12 §25 GATE — OLD SPAN vs NEW SPAN (same strategy, same params)")
    print("=" * 78)
    print(f"H1 full: {len(full):>6d} bars  {ts.min()} .. {ts.max()}")
    print(f"H1 old : {len(old):>6d} bars  (previous span, head 4 months shorter)")
    print()

    a_ok, a = evaluate_span("NEW SPAN (current DB)", full, gates, sim)
    b_ok, b = evaluate_span("OLD SPAN (previous DB)", old, gates, sim)

    print("=" * 78)
    if a_ok != b_ok:
        print("FINDING: the gate VERDICT FLIPS on the same strategy purely by")
        print("         extending the history. V12's in-sample metrics are")
        print("         sample-sensitive, so this ACCEPT is NOT a robust result.")
        print("         Recommendation: treat V12's live approval as UNSTABLE and")
        print("         re-test on a FIXED calendar window before real money.")
        print(f"         IS PF: {b['is_pf']:.2f} (old) -> {a['is_pf']:.2f} (new)")
        print(f"         IS net: {b['is_net_pips']:,.0f} -> {a['is_net_pips']:,.0f} pips")
        print(f"         degradation: {b['oos_degradation']:.2f} -> {a['oos_degradation']:.2f}")
    else:
        print(f"FINDING: verdict stable across both spans ({'ACCEPT' if a_ok else 'REJECT'}).")
        print(f"         IS PF {b['is_pf']:.2f} -> {a['is_pf']:.2f}, "
              f"degradation {b['oos_degradation']:.2f} -> {a['oos_degradation']:.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
