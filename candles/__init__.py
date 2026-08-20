"""Candle-pattern statistical testing (CLAUDE.md §9).

Detects common candlestick patterns and computes, for each:
  - total occurrences
  - winning occurrences (in next N bars)
  - losing occurrences
  - win rate
  - average return
  - maximum adverse excursion (MAE)
  - maximum favorable excursion (MFE)
  - profit factor

Patterns with insufficient sample size (occurrences < min_samples) are
flagged and excluded from any decision logic — per spec §9: "Reject patterns
with insufficient sample size."

All statistics are computed on realized price data (no look-ahead). A pattern
is detected at bar `i` using only data through bar `i`; the forward return
is measured over the next `horizon` bars.

Run with the project venv python:
  ./.venv/Scripts/python.exe -c "from candles import analyze_patterns; ..."
"""
from __future__ import annotations

from dataclasses import dataclass, field
import math
from typing import Sequence

import numpy as np
import pandas as pd


@dataclass
class PatternStats:
    pattern: str
    occurrences: int
    wins: int
    losses: int
    win_rate: float
    avg_return_pips: float
    max_adverse_excursion_pips: float   # avg MAE in pips
    max_favorable_excursion_pips: float  # avg MFE in pips
    profit_factor: float
    sufficient: bool   # meets min_samples threshold
    notes: str = ""


# ---------------------------------------------------------------------------
# Pattern detection (vectorized, look-back only)
# ---------------------------------------------------------------------------
def _body_range(o, h, l, c):
    body = np.abs(c - o)
    rng = h - l
    rng_safe = np.where(rng == 0, np.nan, rng)
    return body, rng, rng_safe


def detect_doji(o, h, l, c, rng, rng_safe, threshold: float = 0.1) -> np.ndarray:
    """Doji: body is < threshold fraction of range."""
    body = np.abs(c - o)
    ratio = np.where(rng_safe == 0, np.nan, body / rng_safe)
    return ratio < threshold


def detect_hammer(o, h, l, c, h_arr, l_arr, rng) -> np.ndarray:
    """Hammer (bullish): small body near top, long lower shadow >= 2x body."""
    body = np.minimum(o, c)
    lower_shadow = body - l_arr
    upper_shadow = h_arr - np.maximum(o, c)
    body_size = np.abs(c - o)
    ratio = np.where(body_size == 0, np.nan, lower_shadow / body_size)
    return (ratio >= 2.0) & (upper_shadow < body_size) & (lower_shadow > 0)


def detect_shooting_star(o, h, l, c, h_arr, l_arr) -> np.ndarray:
    """Shooting star (bearish): small body near bottom, long upper shadow."""
    body_size = np.abs(c - o)
    upper_shadow = h_arr - np.maximum(o, c)
    lower_shadow = np.minimum(o, c) - l_arr
    ratio = np.where(body_size == 0, np.nan, upper_shadow / body_size)
    return (ratio >= 2.0) & (lower_shadow < body_size) & (upper_shadow > 0)


def detect_engulfing(o, h, l, c, direction: str = "bullish") -> np.ndarray:
    """Engulfing: current body fully contains previous body, opposite direction."""
    body = np.abs(c - o)
    prev_o = np.roll(o, 1)
    prev_c = np.roll(c, 1)
    prev_body = np.abs(prev_c - prev_o)
    if direction == "bullish":
        # prev was down (close < open), current is up and engulfs
        was_down = prev_c < prev_o
        is_up = c > o
        engulfs = (np.minimum(o, c) <= np.minimum(prev_o, prev_c)) & \
                  (np.maximum(o, c) >= np.maximum(prev_o, prev_c))
        return was_down & is_up & engulfs & (body > prev_body)
    else:
        was_up = prev_c > prev_o
        is_down = c < o
        engulfs = (np.minimum(o, c) <= np.minimum(prev_o, prev_c)) & \
                  (np.maximum(o, c) >= np.maximum(prev_o, prev_c))
        return was_up & is_down & engulfs & (body > prev_body)


def detect_inside_bar(o, h, l, c, h_arr, l_arr) -> np.ndarray:
    """Inside bar: current range is within previous range."""
    prev_h = np.roll(h_arr, 1)
    prev_l = np.roll(l_arr, 1)
    return (h_arr <= prev_h) & (l_arr >= prev_l)


def detect_pin_bar(o, h, l, c, h_arr, l_arr) -> np.ndarray:
    """Pin bar: long shadow >= 2x body in one direction, small body."""
    body_size = np.abs(c - o)
    rng = h_arr - l_arr
    lower_shadow = np.minimum(o, c) - l_arr
    upper_shadow = h_arr - np.maximum(o, c)
    ratio_lower = np.where(body_size == 0, np.nan, lower_shadow / body_size)
    ratio_upper = np.where(body_size == 0, np.nan, upper_shadow / body_size)
    return ((ratio_lower >= 2.0) & (lower_shadow > 0)) | \
           ((ratio_upper >= 2.0) & (upper_shadow > 0))


PATTERN_DETECTORS = {
    "doji": lambda o, h, l, c, h_arr=None, l_arr=None, rng=None, rng_safe=None, **kw: detect_doji(o, h, l, c, rng, rng_safe),
    "hammer": lambda o, h, l, c, h_arr=None, l_arr=None, rng=None, rng_safe=None, **kw: detect_hammer(o, h, l, c, h_arr or h, l_arr or l, rng),
    "shooting_star": lambda o, h, l, c, h_arr=None, l_arr=None, rng=None, rng_safe=None, **kw: detect_shooting_star(o, h, l, c, h_arr or h, l_arr or l),
    "engulfing_bullish": lambda o, h, l, c, h_arr=None, l_arr=None, rng=None, rng_safe=None, **kw: detect_engulfing(o, h, l, c, "bullish"),
    "engulfing_bearish": lambda o, h, l, c, h_arr=None, l_arr=None, rng=None, rng_safe=None, **kw: detect_engulfing(o, h, l, c, "bearish"),
    "inside_bar": lambda o, h, l, c, h_arr=None, l_arr=None, rng=None, rng_safe=None, **kw: detect_inside_bar(o, h, l, c, h_arr or h, l_arr or l),
    "pin_bar": lambda o, h, l, c, h_arr=None, l_arr=None, rng=None, rng_safe=None, **kw: detect_pin_bar(o, h, l, c, h_arr or h, l_arr or l),
}


# ---------------------------------------------------------------------------
# Forward-return analysis with MAE/MFE
# ---------------------------------------------------------------------------
def _measure_forward(df: pd.DataFrame, pattern_idx: list[int],
                     horizon: int = 5, point_value: float = 0.01,
                     pip_value: float = 0.10) -> dict:
    """For each pattern occurrence at bar i, measure returns over next `horizon` bars.

    Returns (avg_return_pips, avg_mae_pips, avg_mfe_pips, wins, losses, returns_list).
    Direction: bullish patterns -> long bias, bearish -> short bias (measured
    as price moving toward the pattern's implied direction).
    """
    # We measure the raw price return over the horizon (direction-agnostic for
    # now — the caller assigns direction). For bullish patterns we measure
    # upside move; bearish patterns measure downside move.
    close = df["close"].to_numpy()
    o = df["open"].to_numpy()
    h = df["high"].to_numpy()
    l = df["low"].to_numpy()
    n = len(df)

    returns = []
    maes = []  # adverse = against direction
    mfes = []  # favorable = with direction
    wins = 0
    losses = 0

    for direction, idx_list in pattern_idx.items():
        for i in idx_list:
            if i + horizon >= n or i < 0:
                continue
            entry_price = close[i]  # assume enter at close of pattern bar
            if np.isnan(entry_price):
                continue
            # measure over next `horizon` bars
            hi = h[i + 1: i + 1 + horizon]
            lo = l[i + 1: i + 1 + horizon]
            cl_end = close[i + horizon]
            if len(hi) < horizon:
                continue
            max_hi = np.nanmax(hi)
            min_lo = np.nanmin(lo)

            if direction == "long":
                # favorable = price goes up
                mfe = (max_hi - entry_price)
                mae = (entry_price - min_lo)
                ret = (cl_end - entry_price)
            else:
                # favorable = price goes down
                mfe = (entry_price - min_lo)
                mae = (max_hi - entry_price)
                ret = (entry_price - cl_end)

            ret_pips = ret / point_value * pip_value / 0.10  # points->pips
            mae_pips = mae / point_value * pip_value / 0.10
            mfe_pips = mfe / point_value * pip_value / 0.10

            returns.append(ret_pips)
            maes.append(mae_pips)
            mfes.append(mfe_pips)
            if ret_pips > 0:
                wins += 1
            else:
                losses += 1

    return {
        "returns": returns,
        "maes": maes,
        "mfes": mfes,
        "wins": wins,
        "losses": losses,
    }


# ---------------------------------------------------------------------------
# Main analysis
# ---------------------------------------------------------------------------
def analyze_patterns(df: pd.DataFrame, min_samples: int = 30,
                     horizon: int = 5) -> dict[str, PatternStats]:
    """Detect all candle patterns in df and compute their statistics.

    `df` must have columns open, high, low, close. Patterns are detected
    with look-back only (no future data at detection time).

    Returns a dict mapping pattern name -> PatternStats. Patterns with
    occurrences < min_samples are flagged (sufficient=False).
    """
    # Build the full context arrays once, pass to all detectors uniformly
    o = df["open"].to_numpy()
    h = df["high"].to_numpy(dtype=float)
    l = df["low"].to_numpy(dtype=float)
    c = df["close"].to_numpy()
    body, rng, rng_safe = _body_range(o, h, l, c)

    results: dict[str, PatternStats] = {}
    # direction mapping: bullish patterns -> long, bearish -> short
    directions = {
        "doji": "long",
        "hammer": "long",
        "shooting_star": "short",
        "engulfing_bullish": "long",
        "engulfing_bearish": "short",
        "inside_bar": "long",  # entry depends on trend; measure long for now
        "pin_bar": "long",  # pin bar can be either; simplified to long
    }

    for pname, detector in PATTERN_DETECTORS.items():
        try:
            flags = detector(o, h, l, c, h_arr=h, l_arr=l, rng=rng, rng_safe=rng_safe)
        except Exception as e:
            results[pname] = PatternStats(
                pattern=pname, occurrences=0, wins=0, losses=0,
                win_rate=0, avg_return_pips=0, max_adverse_excursion_pips=0,
                max_favorable_excursion_pips=0, profit_factor=0,
                sufficient=False, notes=f"detection error: {e}")
            continue
        flags = flags.astype(bool)
        occurrences = int(np.nansum(flags))
        if occurrences == 0:
            results[pname] = PatternStats(
                pattern=pname, occurrences=0, wins=0, losses=0,
                win_rate=0, avg_return_pips=0, max_adverse_excursion_pips=0,
                max_favorable_excursion_pips=0, profit_factor=0,
                sufficient=False, notes="no occurrences")
            continue
        idx_list = np.where(flags)[0]
        direction = directions.get(pname, "long")
        m = _measure_forward(df, {direction: idx_list}, horizon)

        returns = m["returns"]
        maes = m["maes"]
        mfes = m["mfes"]
        wins = m["wins"]
        losses = m["losses"]

        if returns:
            avg_ret = float(np.mean(returns))
            avg_mae = float(np.mean(maes))
            avg_mfe = float(np.mean(mfes))
            wr = wins / (wins + losses) if (wins + losses) > 0 else 0.0
            gross_win = sum(r for r in returns if r > 0)
            gross_loss = -sum(r for r in returns if r <= 0)
            pf = gross_win / gross_loss if gross_loss > 0 else float("inf")
        else:
            avg_ret = avg_mae = avg_mfe = 0.0
            wr = 0.0
            pf = 0.0

        sufficient = occurrences >= min_samples
        results[pname] = PatternStats(
            pattern=pname, occurrences=occurrences, wins=wins, losses=losses,
            win_rate=wr, avg_return_pips=avg_ret,
            max_adverse_excursion_pips=avg_mae,
            max_favorable_excursion_pips=avg_mfe,
            profit_factor=pf if math.isfinite(pf) else 0.0,
            sufficient=sufficient,
            notes=f"horizon={horizon}B, min_samples={min_samples}")

    return results


def render_pattern_report(stats: dict[str, PatternStats]) -> str:
    """Render pattern statistics as a human-readable table."""
    lines = []
    lines.append(f"{'Pattern':<20} {'Occ':>5} {'Win%':>6} {'AvgRet':>8} "
                 f"{'PF':>6} {'MAE':>8} {'MFE':>8} {'OK':>4}")
    lines.append("-" * 70)
    for pname, s in stats.items():
        ok = "YES" if s.sufficient else "no"
        lines.append(f"{pname:<20} {s.occurrences:>5} {s.win_rate*100:>5.0f}% "
                     f"{s.avg_return_pips:>7.1f} {s.profit_factor:>5.1f} "
                     f"{s.max_adverse_excursion_pips:>7.1f} "
                     f"{s.max_favorable_excursion_pips:>7.1f} {ok:>4}")
    lines.append("-" * 70)
    lines.append("Note: patterns marked 'no' have insufficient samples (min 30).")
    lines.append("All statistics computed on realized price data — no look-ahead.")
    return "\n".join(lines)
