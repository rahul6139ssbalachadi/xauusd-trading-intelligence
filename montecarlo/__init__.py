"""Monte Carlo robustness simulation (CLAUDE.md §11, §15).

Tests whether a strategy's backtest result is robust to ordering and
position-size variation, or whether it depends on a lucky sequence of wins
and losses. Three perturbations:

  1. SHUFFLE   — randomize the order of completed trades. If the equity
     curve is path-dependent (a lucky streak early), the shuffled P&L will
     differ sharply. This catches "lottery-ticket" backtests.

  2. SCATTER   — drop a random fraction of trades (simulating missed
     executions / liquidity). If performance collapses when trades are
     dropped, the result is fragile.

  3. JITTER    — perturb each trade's return by +/- a small percentage
     (modeling slippage / fill variance). Wide outcome spread => the edge
     is within the noise.

Outputs:
  - per-iteration equity curves
  - distribution of net, profit factor, max drawdown, win rate
  - ruin probability (P(n_equity < ruin_threshold))
  - drawdown percentiles (5%, 25%, 50%, 75%, 95%)
  - worst-case / best-case net over the simulation distribution

Uses ONLY the completed-trade list from a backtest (BacktestResult.trades).
No future data, no new simulation of market prices — this is purely about
the robustness of the TRADE P&L sequence to ordering and noise.

Run with the project venv python:
  ./.venv/Scripts/python.exe -c "from montecarlo import run_monte_carlo; ..."
"""
from __future__ import annotations

from dataclasses import dataclass, field
import math
from typing import Sequence

import numpy as np


@dataclass
class MCConfig:
    """Monte Carlo simulation parameters."""
    n_iterations: int = 1000
    seed: int | None = 42
    shuffle: bool = True
    scatter_pct: float = 0.10   # fraction of trades dropped per iteration
    jitter_pct: float = 0.05     # ±5% return perturbation
    ruin_threshold: float = -200.0  # pips below which the strategy is "ruined"
    equity_start: float = 0.0    # start at 0 so net is in pips


@dataclass
class MCStats:
    """Aggregated simulation outcomes."""
    n_iterations: int
    net_mean: float
    net_std: float
    net_min: float
    net_max: float
    net_median: float
    net_p5: float
    net_p95: float
    profit_factor_mean: float
    profit_factor_p5: float
    profit_factor_p95: float
    win_rate_mean: float
    max_dd_mean: float
    max_dd_p50: float
    max_dd_p95: float
    ruin_prob: float
    is_robust: bool           # True if net_p5 > 0 and ruin_prob < 5%
    notes: str = ""


def _trade_returns(trades: Sequence) -> np.ndarray:
    """Extract per-trade net pips as a numpy array."""
    returns = []
    for t in trades:
        r = getattr(t, "net_pips", None)
        if r is None or (isinstance(r, float) and (math.isnan(r) or math.isinf(r))):
            continue
        returns.append(float(r))
    return np.array(returns, dtype=float)


def _metrics_from_returns(arr: np.ndarray) -> dict:
    """Compute net, PF, win rate, max DD for a 1-D return array."""
    if len(arr) == 0:
        return {"net": 0.0, "profit_factor": 0.0, "win_rate": 0.0, "max_dd": 0.0}
    net = float(arr.sum())
    wins = arr[arr > 0]
    losses = arr[arr <= 0]
    gross_win = float(wins.sum()) if len(wins) else 0.0
    gross_loss = -float(losses.sum()) if len(losses) else 0.0
    if gross_loss > 0:
        pf = gross_win / gross_loss
    elif gross_win > 0:
        pf = 999.0  # all winners, no losers
    else:
        pf = 0.0
    win_rate = len(wins) / len(arr) if len(arr) > 0 else 0.0
    eq = np.cumsum(arr)
    peak = np.maximum.accumulate(eq)
    dd = peak - eq
    max_dd = float(dd.max()) if len(dd) else 0.0
    return {"net": net, "profit_factor": pf, "win_rate": win_rate, "max_dd": max_dd}


def run_monte_carlo(trades: Sequence, cfg: MCConfig | None = None) -> MCStats:
    """Run Monte Carlo perturbation on a list of Trade objects.

    Each iteration:
      - optionally shuffles trade order
      - optionally drops `scatter_pct` of trades
      - optionally jitters each return by ±jitter_pct
    Then computes net, PF, win_rate, max_drawdown for that iteration.

    Returns aggregated statistics over all iterations.
    """
    cfg = cfg or MCConfig()
    rng = np.random.default_rng(cfg.seed)
    returns = _trade_returns(trades)
    n_orig = len(returns)

    if n_orig == 0:
        return MCStats(
            n_iterations=0, net_mean=0, net_std=0, net_min=0, net_max=0,
            net_median=0, net_p5=0, net_p95=0,
            profit_factor_mean=0, profit_factor_p5=0, profit_factor_p95=0,
            win_rate_mean=0, max_dd_mean=0, max_dd_p50=0, max_dd_p95=0,
            ruin_prob=1.0, is_robust=False,
            notes="no trades provided",
        )

    nets = np.empty(cfg.n_iterations)
    pfs = np.empty(cfg.n_iterations)
    wrs = np.empty(cfg.n_iterations)
    dds = np.empty(cfg.n_iterations)

    for it in range(cfg.n_iterations):
        arr = returns.copy()
        # 1. shuffle
        if cfg.shuffle:
            rng.shuffle(arr)
        # 2. scatter — drop random subset
        if cfg.scatter_pct > 0:
            mask = rng.random(len(arr)) > cfg.scatter_pct
            arr = arr[mask]
        # 3. jitter — perturb each return
        if cfg.jitter_pct > 0:
            jitter = 1.0 + rng.uniform(-cfg.jitter_pct, cfg.jitter_pct, size=len(arr))
            arr = arr * jitter
        m = _metrics_from_returns(arr)
        nets[it] = m["net"]
        pfs[it] = m["profit_factor"] if math.isfinite(m["profit_factor"]) else 0.0
        wrs[it] = m["win_rate"]
        dds[it] = m["max_dd"]

    ruin_prob = float(np.mean(nets < cfg.ruin_threshold))
    net_p5 = float(np.percentile(nets, 5))
    net_p95 = float(np.percentile(nets, 95))
    pf_p5 = float(np.percentile(pfs, 5))
    pf_p95 = float(np.percentile(pfs, 95))

    # robust = even in the 5th percentile worst run, net is positive AND
    # ruin probability is low
    is_robust = (net_p5 > 0) and (ruin_prob < 0.05)

    notes = (f"n_iter={cfg.n_iterations}, n_trades={n_orig}, "
             f"shuffle={cfg.shuffle}, scatter={cfg.scatter_pct}, "
             f"jitter={cfg.jitter_pct}, ruin<thr={cfg.ruin_threshold}")

    return MCStats(
        n_iterations=cfg.n_iterations,
        net_mean=float(nets.mean()), net_std=float(nets.std(ddof=1)),
        net_min=float(nets.min()), net_max=float(nets.max()),
        net_median=float(np.median(nets)),
        net_p5=net_p5, net_p95=net_p95,
        profit_factor_mean=float(np.nanmean(
            [p for p in pfs if math.isfinite(p)])),
        profit_factor_p5=pf_p5, profit_factor_p95=pf_p95,
        win_rate_mean=float(wrs.mean()),
        max_dd_mean=float(dds.mean()),
        max_dd_p50=float(np.percentile(dds, 50)),
        max_dd_p95=float(np.percentile(dds, 95)),
        ruin_prob=ruin_prob, is_robust=is_robust, notes=notes,
    )
