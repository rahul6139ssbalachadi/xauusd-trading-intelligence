"""Self-improvement loop (CLAUDE.md §25).

The spec requires a controlled pipeline:

    CURRENT STRATEGY -> PROPOSED IMPROVEMENT -> BACKTEST -> OUT-OF-SAMPLE
    -> WALK-FORWARD -> PAPER TRADE -> ROBUSTNESS CHECK -> USER APPROVAL
    -> NEW VERSION

Every stage already exists in this repo (`backtest`, `validation`,
`montecarlo`, `research`). What was MISSING is the gate that runs them in
order and refuses weak candidates — plus the hard rule that the system may
NOT modify its own production strategy without explicit human approval.

## The safety property this module is built around

Self-improvement means the system evaluating its own work. That is only safe
if the evaluation cannot also *apply* itself. So this package is split:

- `propose()`  — runs the gates and WRITES a candidate. Read-only w.r.t. the
  running system. Cannot place orders, cannot touch `approved.json`.
- `promote()`  — the ONLY function that can add to `approved.json`, and it
  requires an explicit `approved_by` human. There is no code path from
  `propose()` to `promote()`.

A rejected candidate is a normal, expected outcome and is recorded, not
discarded. Per §15, failed experiments contain information.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

ROOT = Path(__file__).resolve().parents[1]
PROPOSALS_DIR = ROOT / "execution" / "proposals"


# --------------------------------------------------------------------------
# Gate thresholds — deliberately conservative
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class Gates:
    """Acceptance thresholds. Tuned to reject, not to admit.

    Defaults follow the levels V11 had to clear: positive out-of-sample net,
    profit factor above 1, no out-of-sample collapse, Monte Carlo robust, and
    enough trades that the result is not pure noise.
    """

    min_trades: int = 30
    min_oos_trades: int = 10
    min_profit_factor: float = 1.20
    min_oos_net_pips: float = 0.0
    max_oos_degradation: float = 0.50     # OOS must retain >=50% of IS net
    max_ruin_prob: float = 0.05
    require_mc_robust: bool = True

    def __post_init__(self) -> None:
        if self.min_trades < 1:
            raise ValueError("min_trades must be >= 1")
        if not 0.0 <= self.max_ruin_prob <= 1.0:
            raise ValueError("max_ruin_prob must be a probability")
        if self.max_oos_degradation > 1.0:
            raise ValueError("max_oos_degradation > 1.0 forbids any OOS gain")


# --------------------------------------------------------------------------
# Verdicts
# --------------------------------------------------------------------------
@dataclass
class GateResult:
    name: str
    passed: bool
    detail: str


@dataclass
class Verdict:
    """Outcome of running every gate. ACCEPT is a PROPOSAL, not a promotion."""

    accepted: bool
    results: list[GateResult] = field(default_factory=list)

    @property
    def failures(self) -> list[GateResult]:
        return [r for r in self.results if not r.passed]

    def reason(self) -> str:
        if self.accepted:
            return "all gates passed — awaiting human approval"
        return "; ".join(f"{r.name}: {r.detail}" for r in self.failures)

    def to_dict(self) -> dict[str, Any]:
        return {
            "accepted": self.accepted,
            "reason": self.reason(),
            "gates": [asdict(r) for r in self.results],
        }


@dataclass
class Proposal:
    """A candidate improvement. Status starts at PENDING and only ever
    changes through an explicit `promote()` call naming a human."""

    strategy: str
    base_version: str
    candidate_version: str
    created_at: str
    verdict: Verdict
    evidence: dict[str, Any] = field(default_factory=dict)
    notes: str = ""
    status: str = "PENDING"          # PENDING | APPROVED | REJECTED
    approved_by: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "strategy": self.strategy,
            "base_version": self.base_version,
            "candidate_version": self.candidate_version,
            "created_at": self.created_at,
            "status": self.status,
            "approved_by": self.approved_by,
            "notes": self.notes,
            "evidence": self.evidence,
            "verdict": self.verdict.to_dict(),
        }


# --------------------------------------------------------------------------
# The gate runner
# --------------------------------------------------------------------------
def _num(v: Any) -> float | None:
    """Coerce to a usable float, treating None/NaN/non-numeric as missing."""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f else f  # NaN check


def evaluate_gates(
    backtest_metrics: dict[str, Any],
    walk_forward_summary: dict[str, Any],
    mc_stats: Any = None,
    gates: Gates | None = None,
) -> Verdict:
    """Run every acceptance gate and return a single verdict.

    Each input is the output of an EXISTING stage:
      backtest_metrics      -> backtest.compute_metrics(...)
      walk_forward_summary  -> validation.summarize_walk_forward(...)
      mc_stats              -> montecarlo.run_monte_carlo(...)
    """
    gates = gates or Gates()
    results: list[GateResult] = []

    # Gate 1: enough trades to mean anything.
    trades = _num(backtest_metrics.get("total_trades")) or 0
    results.append(GateResult(
        "sample_size", trades >= gates.min_trades,
        f"{int(trades)} trades (min {gates.min_trades})"))

    # Gate 2: in-sample profit factor. 0.0 is the engine's honest value for
    # "no wins at all" and must fail, not pass as "> 0".
    pf = _num(backtest_metrics.get("profit_factor"))
    results.append(GateResult(
        "profit_factor", pf is not None and pf >= gates.min_profit_factor,
        f"PF {pf if pf is not None else 'n/a'} (min {gates.min_profit_factor})"))

    # Gate 3: out-of-sample trades. Zero OOS trades means the edge is
    # period-dependent — this is exactly how V1-V10 failed.
    oos_trades = _num(walk_forward_summary.get("oos_total_trades")) or 0
    results.append(GateResult(
        "oos_sample", oos_trades >= gates.min_oos_trades,
        f"{int(oos_trades)} OOS trades (min {gates.min_oos_trades})"))

    # Gate 4: out-of-sample profitability.
    oos_net = _num(walk_forward_summary.get("oos_mean_net_pips"))
    results.append(GateResult(
        "oos_net", oos_net is not None and oos_net > gates.min_oos_net_pips,
        f"OOS net {oos_net if oos_net is not None else 'n/a'} pips "
        f"(min {gates.min_oos_net_pips})"))

    # Gate 5: no out-of-sample collapse.
    deg = _num(walk_forward_summary.get("oos_degradation"))
    if deg is None or deg != deg:
        # Undefined degradation happens when IS net is 0/NaN. Treat the
        # unknown conservatively rather than waving the candidate through.
        results.append(GateResult(
            "oos_degradation", False,
            "degradation undefined (in-sample net was 0/NaN)"))
    else:
        results.append(GateResult(
            "oos_degradation", deg <= gates.max_oos_degradation,
            f"degradation {deg:.2f} (max {gates.max_oos_degradation})"))

    # Gate 6/7: Monte Carlo robustness.
    if gates.require_mc_robust:
        if mc_stats is None:
            results.append(GateResult(
                "mc_robust", False, "no Monte Carlo results supplied"))
        else:
            p5 = _num(getattr(mc_stats, "net_p5", None))
            ruin = _num(getattr(mc_stats, "ruin_prob", None))
            robust = bool(getattr(mc_stats, "is_robust", False))
            results.append(GateResult(
                "mc_robust", robust,
                f"robust={robust}, net_p5={p5}, ruin_prob={ruin}"))
            if ruin is not None:
                results.append(GateResult(
                    "ruin_prob", ruin <= gates.max_ruin_prob,
                    f"ruin {ruin:.1%} (max {gates.max_ruin_prob:.1%})"))

    return Verdict(accepted=all(r.passed for r in results), results=results)


# --------------------------------------------------------------------------
# propose() — read-only w.r.t. the running system
# --------------------------------------------------------------------------
def propose(
    strategy: str,
    base_version: str,
    candidate_version: str,
    backtest_metrics: dict[str, Any],
    walk_forward_summary: dict[str, Any],
    mc_stats: Any = None,
    gates: Gates | None = None,
    notes: str = "",
    evidence: dict[str, Any] | None = None,
    out_dir: Path | None = None,
) -> Proposal:
    """Evaluate a candidate and persist it as a PENDING proposal.

    This function deliberately CANNOT affect live trading: it writes a file
    and nothing else. It does not call order_send, does not modify
    approved.json, and does not write to the live journal.
    """
    verdict = evaluate_gates(backtest_metrics, walk_forward_summary,
                             mc_stats, gates)
    if mc_stats is not None:
        evidence = dict(evidence or {})
        evidence["monte_carlo"] = {
            k: v for k, v in vars(mc_stats).items()
            if isinstance(v, (int, float, str, bool))
        }

    prop = Proposal(
        strategy=strategy,
        base_version=base_version,
        candidate_version=candidate_version,
        created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        verdict=verdict,
        evidence=dict(evidence or {}),
        notes=notes,
    )
    _persist(prop, out_dir)
    return prop


def _persist(prop: Proposal, out_dir: Path | None = None) -> Path:
    d = Path(out_dir) if out_dir else PROPOSALS_DIR
    d.mkdir(parents=True, exist_ok=True)
    stamp = prop.created_at.replace(":", "").replace("-", "")
    name = f"{prop.strategy}_{prop.candidate_version}_{stamp}.json"
    path = d / name
    path.write_text(json.dumps(prop.to_dict(), indent=2), encoding="utf-8")
    return path


# --------------------------------------------------------------------------
# promote() — the ONLY path to production, and it demands a human name
# --------------------------------------------------------------------------
class PromotionRefused(RuntimeError):
    """Raised when a promotion is attempted without explicit approval."""


def promote(
    proposal: Proposal,
    approved_by: str,
    approved_path: Path | None = None,
) -> Path:
    """Record a HUMAN approval. Requires an explicit `approved_by`.

    This is intentionally a separate, loudly-named call that the system can
    never reach on its own: `propose()` does not call it, and nothing in the
    research/backtest path invokes it. The empty/whitespace guard below is
    the last line of defense against a vacuous "approval".
    """
    if not isinstance(approved_by, str) or not approved_by.strip():
        raise PromotionRefused(
            "promotion requires an explicit human approver name; "
            "self-approval is not permitted")
    if not proposal.verdict.accepted:
        raise PromotionRefused(
            f"candidate {proposal.candidate_version} failed its gates: "
            f"{proposal.verdict.reason()}")

    target = Path(approved_path) if approved_path else (
        ROOT / "execution" / "approved.json")
    data: dict[str, Any] = {"approved": []}
    if target.exists():
        data = json.loads(target.read_text(encoding="utf-8"))
        data.setdefault("approved", [])

    # Version immutability (§10/§15): a version is never silently replaced.
    existing = {a.get("version") for a in data["approved"]}
    if proposal.candidate_version in existing:
        raise PromotionRefused(
            f"version {proposal.candidate_version} already approved; "
            "create a new version instead of overwriting")

    data["approved"].append({
        "name": proposal.strategy,
        "version": proposal.candidate_version,
        "supersedes": proposal.base_version,
        "approved_on": datetime.now(timezone.utc).date().isoformat(),
        "approved_by": approved_by.strip(),
        "evidence": proposal.evidence,
    })
    target.write_text(json.dumps(data, indent=2), encoding="utf-8")

    proposal.status = "APPROVED"
    proposal.approved_by = approved_by.strip()
    return target
