"""Validation harness (Phase 9).

Implements the train / validation / out-of-sample discipline required by
CLAUDE.md sections 12-13:

  - train_val_test_split: one-shot 60/20/20 (configurable) split BY TIME
    (no shuffling — time-series data must keep order), no overlap.
  - walk_forward_windows: rolling (train, test) index windows for
    walk-forward analysis.
  - run_walk_forward: runs the (FIXED, un-optimized) strategy on each
    window's out-of-sample test segment and collects per-window metrics.
  - summarize_walk_forward: aggregate OOS performance + an in-sample vs
    out-of-sample degradation check.

CRITICAL ANTI-OVERFIT GUARANTEES (per CLAUDE.md):
  - The strategy is NEVER modified using test/validation data. This module
    only MEASURES a strategy you pass in. Optimization belongs in Phase 10+
    and must happen ONLY on the training window.
  - Bias (higher timeframe) gets a leading WARM-UP buffer so its EMAs/ADX
    are not artificially reset at each window start (which would leak the
    window's own data into the indicator or starve it of history).
  - No future information flows into any window.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterator

import pandas as pd

from backtest import run_backtest


# --------------------------------------------------------------------------
# One-shot train / validation / test split (by time, no shuffle)
# --------------------------------------------------------------------------
def train_val_test_split(
    df: pd.DataFrame,
    train: float = 0.6,
    val: float = 0.2,
    test: float = 0.2,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Split a time-ordered frame into train/val/test by fraction of rows.

    No shuffling. The three segments are contiguous and non-overlapping:
    earliest `train` -> train, next `val` -> val, last `test` -> test.

    Raises ValueError if fractions don't sum to ~1.
    """
    total = train + val + test
    if abs(total - 1.0) > 1e-6:
        raise ValueError(f"train+val+test must sum to 1.0, got {total}")
    n = len(df)
    i_tr = int(round(n * train))
    i_va = i_tr + int(round(n * val))
    return df.iloc[:i_tr], df.iloc[i_tr:i_va], df.iloc[i_va:]


# --------------------------------------------------------------------------
# Walk-forward window generator
# --------------------------------------------------------------------------
def walk_forward_windows(
    n: int,
    train_frac: float,
    test_frac: float,
    step: int | None = None,
) -> Iterator[tuple[tuple[int, int], tuple[int, int]]]:
    """Yield rolling (train_range, test_range) as (start, stop) iloc pairs.

    train_range covers `train_frac` of n rows; test_range covers `test_frac`,
    placed immediately after train. After each yield we roll forward by
    `step` (default = test length, giving non-overlapping test windows).
    Stops when there isn't room for a full train+test window.
    """
    if not (0 < train_frac < 1 and 0 < test_frac < 1):
        raise ValueError("train_frac and test_frac must be in (0,1)")
    if train_frac + test_frac > 1.0:
        raise ValueError("train_frac + test_frac must be <= 1.0")
    train_size = int(round(n * train_frac))
    test_size = int(round(n * test_frac))
    step = step or test_size
    start = 0
    while start + train_size + test_size <= n:
        train = (start, start + train_size)
        test = (start + train_size, start + train_size + test_size)
        yield train, test
        start += step


# --------------------------------------------------------------------------
# Run walk-forward on a (bias, trigger) pair
# --------------------------------------------------------------------------
@dataclass
class WindowResult:
    train_range: tuple[int, int]
    test_range: tuple[int, int]
    train_metrics: dict = field(default_factory=dict)
    test_metrics: dict = field(default_factory=dict)
    n_train_trades: int = 0
    n_test_trades: int = 0


def _slice_bias(bias_df: pd.DataFrame, trig_window: pd.DataFrame,
                warmup_bars: int) -> pd.DataFrame:
    """Bias rows overlapping the trigger window's time range, plus a leading
    warm-up buffer so indicators aren't cold-started inside the window."""
    t0 = trig_window["ts"].iloc[0]
    t1 = trig_window["ts"].iloc[-1]
    mask = (bias_df["ts"] >= t0) & (bias_df["ts"] <= t1)
    idx = bias_df.index[mask]
    if len(idx) == 0:
        return bias_df.iloc[0:0]
    first = idx[0]
    lead = max(0, first - warmup_bars)
    return bias_df.iloc[lead: idx[-1] + 1]


def run_walk_forward(
    strat,
    bias_df: pd.DataFrame,
    trigger_df: pd.DataFrame,
    train_frac: float = 0.5,
    test_frac: float = 0.25,
    step: int | None = None,
    bias_warmup_bars: int = 200,
    **bt_kwargs,
) -> list[WindowResult]:
    """Run the FIXED strategy across rolling walk-forward windows.

    For each window we backtest the training segment (in-sample) and the
    test segment (out-of-sample). The strategy is passed in unchanged — this
    function never tunes it.
    """
    n = len(trigger_df)
    results: list[WindowResult] = []
    for (tr_s, tr_e), (te_s, te_e) in walk_forward_windows(n, train_frac, test_frac, step):
        trig_train = trigger_df.iloc[tr_s:tr_e]
        trig_test = trigger_df.iloc[te_s:te_e]
        bias_train = _slice_bias(bias_df, trig_train, bias_warmup_bars)
        bias_test = _slice_bias(bias_df, trig_test, bias_warmup_bars)

        res_tr = run_backtest(strat, bias_train, trig_train, **bt_kwargs)
        res_te = run_backtest(strat, bias_test, trig_test, **bt_kwargs)
        results.append(WindowResult(
            train_range=(tr_s, tr_e),
            test_range=(te_s, te_e),
            train_metrics=res_tr.metrics,
            test_metrics=res_te.metrics,
            n_train_trades=res_tr.metrics.get("total_trades", 0),
            n_test_trades=res_te.metrics.get("total_trades", 0),
        ))
    return results


# --------------------------------------------------------------------------
# Aggregation + degradation report
# --------------------------------------------------------------------------
def _mean(values):
    vals = [v for v in values if v is not None and v == v]  # drop NaN/None
    return sum(vals) / len(vals) if vals else float("nan")


def summarize_walk_forward(results: list[WindowResult]) -> dict:
    """Aggregate out-of-sample metrics and flag IS/OOS degradation.

    'degradation' = 1 - (mean OOS net_pips / mean IS net_pips). A large
    positive value means the strategy earns far less (or loses) out of
    sample — a classic overfit / in-sample-dependence warning. NaN if
    either mean is zero/undefined.
    """
    is_net = [r.train_metrics.get("net_pips") for r in results]
    oos_net = [r.test_metrics.get("net_pips") for r in results]
    is_pf = [r.train_metrics.get("profit_factor") for r in results]
    oos_pf = [r.test_metrics.get("profit_factor") for r in results]
    is_wr = [r.train_metrics.get("win_rate") for r in results]
    oos_wr = [r.test_metrics.get("win_rate") for r in results]
    is_trades = [r.n_train_trades for r in results]
    oos_trades = [r.n_test_trades for r in results]

    is_mean = _mean(is_net)
    oos_mean = _mean(oos_net)
    if is_mean and is_mean == is_mean:  # not nan
        degradation = 1.0 - (oos_mean / is_mean) if is_mean != 0 else float("nan")
    else:
        degradation = float("nan")

    return {
        "n_windows": len(results),
        "is_mean_net_pips": is_mean,
        "oos_mean_net_pips": oos_mean,
        "is_mean_profit_factor": _mean(is_pf),
        "oos_mean_profit_factor": _mean(oos_pf),
        "is_mean_win_rate": _mean(is_wr),
        "oos_mean_win_rate": _mean(oos_wr),
        "is_total_trades": sum(is_trades),
        "oos_total_trades": sum(oos_trades),
        "oos_degradation": degradation,
    }
