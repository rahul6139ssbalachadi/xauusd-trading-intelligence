"""Exercise the §25 self-improvement gate on the REAL V11 result.

Read-only: loads stored D1 bars from db/trading.db, runs the validated V11
signal logic, and feeds the result through `self_improvement.propose()`.

The point is to prove the gate reproduces the historical verdict — V11 was
accepted when it cleared these bars. Nothing here places an order; propose()
has no way to reach the execution path or approved.json.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import research.v11_d1_momentum as v11
from montecarlo import MCConfig, run_monte_carlo
from self_improvement import Gates, propose

# The validated V11 config (strategy/defs/XAUUSD_D1_MOMENTUM_BREAKOUT_V11.json).
SIGNAL_PARAMS = dict(body_pct_threshold=0.95, trend_filter=True,
                      min_atr_pct=0.005, rr=2.0, atr_mult_stop=1.5,
                      max_holding_d1=8)
BT_PARAMS = {k: SIGNAL_PARAMS[k] for k in
             ("rr", "atr_mult_stop", "max_holding_d1")}


def _oos_summary(d1, sigs, split_frac: float = 0.70) -> tuple[dict, int]:
    """Chronological holdout: the honest OOS proxy for a D1-only strategy."""
    split = int(len(d1) * split_frac)
    is_sigs = [s for s in sigs if s["entry_bar"] < split]
    oos_sigs = [s for s in sigs if s["entry_bar"] >= split]

    _, is_m = v11.backtest_d1_signals(d1, is_sigs, **BT_PARAMS)
    _, oos_m = v11.backtest_d1_signals(d1, oos_sigs, **BT_PARAMS)

    is_net = float(is_m.get("net_pips", 0.0) or 0.0)
    oos_net = float(oos_m.get("net_pips", 0.0) or 0.0)
    degr = 1.0 - (oos_net / is_net) if is_net else float("nan")
    return {
        "n_windows": 1,
        "is_mean_net_pips": is_net,
        "oos_mean_net_pips": oos_net,
        "is_total_trades": is_m.get("total_trades", 0),
        "oos_total_trades": oos_m.get("total_trades", 0),
        "oos_degradation": degr,
    }, len(oos_sigs)


def run_candidate(out_dir: Path, gates: Gates | None = None):
    d1 = v11.build_d1_features(v11.load("D1"))
    sigs = v11.compute_d1_signals(d1, **SIGNAL_PARAMS)
    trades, metrics = v11.backtest_d1_signals(d1, sigs, **BT_PARAMS)
    wf, _ = _oos_summary(d1, sigs)
    mc = run_monte_carlo(trades, MCConfig(n_iterations=400)) if trades else None

    return propose("XAUUSD_D1_MOMENTUM_BREAKOUT", "V11", "V13", metrics, wf,
                   mc, gates=gates, out_dir=out_dir,
                   evidence={"bars": len(d1), "signals": len(sigs),
                             "holdout_frac": 0.70},
                   notes="V11 re-validated through the §25 gate")


def main() -> int:
    d1 = v11.load("D1")
    print(f"D1 bars: {len(d1)}  ({d1['ts'].iloc[0]} -> {d1['ts'].iloc[-1]})")

    with tempfile.TemporaryDirectory(dir=ROOT / "db") as tmp:
        p = run_candidate(Path(tmp))
        print(f"\nCandidate : {p.candidate_version} (supersedes {p.base_version})")
        print(f"Verdict   : {'ACCEPTED' if p.verdict.accepted else 'REJECTED'}")
        for g in p.verdict.results:
            print(f"  [{'PASS' if g.passed else 'FAIL'}] {g.name}: {g.detail}")
        if p.verdict.failures:
            print(f"\nReason    : {p.verdict.reason()}")
        print(f"Status    : {p.status}  "
              f"(promotion requires an explicit human approver)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
