"""Tests for Monte Carlo robustness simulation (Phase 11 robustness)."""
from __future__ import annotations

import sys
from pathlib import Path
from dataclasses import dataclass

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from montecarlo import run_monte_carlo, MCConfig


@dataclass
class FakeTrade:
    net_pips: float
    entry_price: float = 0.0
    stop: float = 0.0
    side: str = "BUY"


def test_empty_trades_returns_not_robust():
    stats = run_monte_carlo([])
    assert stats.n_iterations == 0
    assert stats.is_robust is False
    assert stats.ruin_prob == 1.0


def test_positive_edge_is_robust():
    """A clearly winning trade sequence should pass robustness."""
    # 100 trades, each +10 pips, consistent
    trades = [FakeTrade(net_pips=10.0) for _ in range(100)]
    stats = run_monte_carlo(trades, MCConfig(n_iterations=100, seed=42))
    assert stats.n_iterations == 100
    assert stats.is_robust is True
    assert stats.ruin_prob == 0.0
    assert stats.net_p5 > 0
    # all-winner edge -> PF should be very high (999 sentinel for inf)
    assert stats.profit_factor_mean > 0


def test_negative_edge_is_not_robust():
    """A clearly losing sequence should be flagged not-robust."""
    trades = [FakeTrade(net_pips=-10.0) for _ in range(100)]
    stats = run_monte_carlo(trades, MCConfig(n_iterations=100, seed=42))
    assert stats.is_robust is False
    assert stats.ruin_prob > 0.5


def test_shuffle_does_not_change_mean():
    """Shuffling changes the path but not the mean (order is exchangeable)."""
    trades = [FakeTrade(net_pips=10.0) for _ in range(50)] + \
             [FakeTrade(net_pips=-5.0) for _ in range(50)]
    # disable scatter + jitter to isolate shuffle effect
    stats_no_shuffle = run_monte_carlo(trades, MCConfig(n_iterations=100, seed=42,
                                                        shuffle=False,
                                                        scatter_pct=0.0,
                                                        jitter_pct=0.0))
    stats_shuffle = run_monte_carlo(trades, MCConfig(n_iterations=100, seed=42,
                                                     shuffle=True,
                                                     scatter_pct=0.0,
                                                     jitter_pct=0.0))
    # mean net should be the same (same trades, shuffled)
    assert abs(stats_no_shuffle.net_mean - stats_shuffle.net_mean) < 1.0
    # without jitter/scatter, no-shuffle has deterministic max_dd=0
    # (all trades same), shuffle also deterministic since all +10/-5
    assert stats_no_shuffle.max_dd_mean >= 0


def test_jitter_perturbs_results():
    """Jitter should widen the distribution compared to no jitter."""
    trades = [FakeTrade(net_pips=10.0) for _ in range(50)] + \
             [FakeTrade(net_pips=-5.0) for _ in range(50)]
    # disable scatter to isolate jitter effect
    stats_no_jitter = run_monte_carlo(trades, MCConfig(n_iterations=100, seed=42,
                                                       jitter_pct=0.0,
                                                       scatter_pct=0.0))
    stats_jitter = run_monte_carlo(trades, MCConfig(n_iterations=100, seed=42,
                                                    jitter_pct=0.1,
                                                    scatter_pct=0.0))
    # jitter should produce some variance
    assert stats_jitter.net_std > 0
    # means should be close (jitter is symmetric)
    assert abs(stats_no_jitter.net_mean - stats_jitter.net_mean) < 10.0


def test_scatter_drops_trades():
    """Scatter should reduce trade count -> affect net."""
    trades = [FakeTrade(net_pips=10.0) for _ in range(100)]
    # disable shuffle+jitter to isolate scatter
    stats = run_monte_carlo(trades, MCConfig(n_iterations=50, seed=42,
                                             scatter_pct=0.2,
                                             shuffle=False,
                                             jitter_pct=0.0))
    # with 20% scatter on 100 winning trades, mean should be ~80*10=800
    # but with variance
    assert stats.net_mean < 1000  # less than full 1000
    assert stats.net_mean > 0


def test_percentiles_ordering():
    """p5 <= median <= p95."""
    trades = [FakeTrade(net_pips=10.0) for _ in range(50)] + \
             [FakeTrade(net_pips=-5.0) for _ in range(50)]
    stats = run_monte_carlo(trades, MCConfig(n_iterations=200, seed=42))
    assert stats.net_p5 <= stats.net_median <= stats.net_p95
    assert stats.max_dd_p50 <= stats.max_dd_p95
