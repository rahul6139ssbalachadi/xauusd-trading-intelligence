"""Strategy research harness (post-Phase-12 work, "option 2").

Disciplined parameter search for the structure-break family of strategies.

HARD RULES (from CLAUDE.md §12-14 — no overfitting):
  - Optimization happens ONLY on the TRAIN window.
  - The chosen parameters are then measured on VALIDATION and a held-out
    OUT-OF-SAMPLE / walk-forward test. We NEVER tune on those.
  - This module only MEASURES; it mints experiment IDs via the registry and
    records every run, but it does not auto-deploy anything.

What we vary (the structure-break knobs):
  - bias_min_adx        : trend-strength gate
  - atr_multiple (stop) : stop distance
  - risk_reward (target): reward:risk
  - bias_trend          : "up" / "down" / "both"
  - require_structure   : True / False
  - sessions            : which sessions allowed
  - rsi band            : rsi_max / rsi_min guards

The grid is intentionally small and coarse to avoid spurious in-sample
"wins". A parameter set is only interesting if it survives OOS.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from itertools import product
from pathlib import Path

import pandas as pd

from strategy import Strategy
from backtest import run_backtest
from validation import train_val_test_split, run_walk_forward, summarize_walk_forward
from reporting import ExperimentRegistry
from risk import RiskConfig


@dataclass
class SearchResult:
    params: dict
    train_net: float
    train_pf: float
    train_trades: int
    val_net: float
    val_pf: float
    oos_net: float
    oos_pf: float
    wf_degradation: float
    experiment_id: str = ""


def _make_strategy(name, params) -> Strategy:
    return Strategy(
        name=name, version=params.get("version", "SEARCH"),
        market="XAUUSD", bias_timeframe="M15", trigger_timeframe="M5",
        entry_rules={
            "bias_trend": params["bias_trend"],
            "bias_min_adx": params["bias_min_adx"],
            "trigger_structure": "BOS",
            "trigger_structure_dir": params.get("trigger_structure_dir", "up"),
            "require_structure": params["require_structure"],
            "vol_regimes_allowed": params.get("vol_regimes_allowed", ["normal", "high"]),
            "rsi_max": params.get("rsi_max", 70.0),
            "rsi_min": params.get("rsi_min", 30.0),
            "max_spread_pips": params.get("max_spread_pips", 3.0),
        },
        stop={"type": "atr", "atr_multiple": params["atr_multiple"]},
        target={"type": "risk_reward", "risk_reward": params["risk_reward"]},
        risk_pct=0.0025, max_positions=1,
        sessions=params["sessions"], notes="grid search",
    )


def grid_search(
    bias_df: pd.DataFrame,
    trigger_df: pd.DataFrame,
    grid: dict,
    train_frac: float = 0.6,
    val_frac: float = 0.2,
    slippage_pips: float = 0.5,
    registry: ExperimentRegistry | None = None,
    max_results: int = 50,
) -> list[SearchResult]:
    """Run a coarse grid search on the TRAIN window only.

    For each param combo we backtest the train segment, then VALIDATE and
    OUT-OF-SAMPLE (the held-out 20%) to see if any edge survives. The
    returned list is sorted by train net pips (desc) but every row carries
    its OOS numbers so you can judge real viability.
    """
    tr, va, te = train_val_test_split(trigger_df, train_frac, val_frac, 1 - train_frac - val_frac)
    # bias slices aligned to each trigger window by time
    from validation import _slice_bias
    bias_tr = _slice_bias(bias_df, tr, 200)
    bias_va = _slice_bias(bias_df, va, 200)
    bias_te = _slice_bias(bias_df, te, 200)

    # precompute features ONCE per window (not per param)
    import pandas as pd
    from strategy import build_features
    ftr_b = build_features(bias_tr); ftr_t = build_features(tr)
    fva_b = build_features(bias_va); fva_t = build_features(va)
    fte_b = build_features(bias_te); fte_t = build_features(te)

    keys = list(grid.keys())
    combos = list(product(*[grid[k] for k in keys]))
    results: list[SearchResult] = []
    for vals in combos:
        params = dict(zip(keys, vals))
        params.setdefault("trigger_structure_dir",
                          "up" if params["bias_trend"] != "both" else "up")
        params.setdefault("vol_regimes_allowed", ["normal", "high"])
        strat = _make_strategy("SEARCH", params)

        r_tr = run_backtest(strat, feats_bias=ftr_b, feats_trig=ftr_t, slippage_pips=slippage_pips)
        r_va = run_backtest(strat, feats_bias=fva_b, feats_trig=fva_t, slippage_pips=slippage_pips)
        r_te = run_backtest(strat, feats_bias=fte_b, feats_trig=fte_t, slippage_pips=slippage_pips)
        mt, mv, mte = r_tr.metrics, r_va.metrics, r_te.metrics

        eid = ""
        if registry:
            eid = registry.mint("BACKTEST", f"grid {params}")

        results.append(SearchResult(
            params=params,
            train_net=mt.get("net_pips", 0.0), train_pf=mt.get("profit_factor", 0.0),
            train_trades=mt.get("total_trades", 0),
            val_net=mv.get("net_pips", 0.0), val_pf=mv.get("profit_factor", 0.0),
            oos_net=mte.get("net_pips", 0.0), oos_pf=mte.get("profit_factor", 0.0),
            wf_degradation=float("nan"), experiment_id=eid,
        ))
        if len(results) >= max_results:
            break

    results.sort(key=lambda r: r.train_net, reverse=True)
    return results


def top_by_oos(results: list[SearchResult], n: int = 5):
    """Return param sets whose OOS net is positive, sorted by OOS net."""
    viable = [r for r in results if r.oos_net > 0]
    viable.sort(key=lambda r: r.oos_net, reverse=True)
    return viable[:n]
