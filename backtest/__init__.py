"""Backtest engine (Phase 8).

Event-driven, no look-ahead simulation of a strategy's decisions against
historical OHLCV. Produces the full metric set required by CLAUDE.md
section 11, with realistic costs (spread, commission, slippage) and
intrabar stop/target resolution.

Core anti-bias guarantees:
  - A position opened on signal bar `i` enters at bar `i+1` OPEN (never at
    the signal bar's own close, never using future information).
  - Stop/target are checked per-bar against that bar's HIGH/LOW (intrabar
    touch). Whichever is hit first ends the trade; if both are touched in
    the same bar, the nearer one (by distance from entry) wins — a
    conservative, defensible convention.
  - Only one position at a time (max_positions=1) until Phase 10 risk
    engine adds sizing; this keeps the backtest a clean per-trade test.

This is a HYPOTHESIS TEST, not a profit claim. Per CLAUDE.md it must be
followed by train/validation/out-of-sample (Phase 9) and walk-forward
(Phase 13) before any strategy is trusted.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from strategy import Strategy, build_features, evaluate

# gold pip conventions (verified earlier): point=0.01, pip=$0.10
POINT = 0.01
PIP = 0.10


@dataclass
class Trade:
    entry_bar: int
    entry_price: float
    side: str                 # "BUY"/"SELL"
    stop: float
    target: float
    exit_bar: int
    exit_price: float
    exit_reason: str          # "stop"/"target"/"end"
    points: float             # gross move in price points (signed by side)
    cost_pips: float          # total round-trip cost in pips
    net_pips: float           # points->pips, minus cost
    duration_bars: int


@dataclass
class BacktestResult:
    trades: list[Trade] = field(default_factory=list)
    equity_curve: pd.Series | None = None
    metrics: dict = field(default_factory=dict)


def _bar_exit(side: str, entry: float, stop: float, target: float,
              high: float, low: float) -> tuple[str, float]:
    """Determine intrabar exit. Returns (reason, exit_price).

    Conservative rule: if the bar touches both stop and target, the level
    closer to entry (in price) is assumed hit first.
    """
    if side == "BUY":
        hit_stop = low <= stop
        hit_target = high >= target
    else:  # SELL: stop above, target below
        hit_stop = high >= stop
        hit_target = low <= target
    if hit_stop and hit_target:
        d_stop = abs(entry - stop)
        d_target = abs(target - entry)
        if d_stop <= d_target:
            return "stop", stop
        return "target", target
    if hit_stop:
        return "stop", stop
    if hit_target:
        return "target", target
    return "", float("nan")


def run_backtest(
    strat: Strategy,
    bias_df: pd.DataFrame | None = None,
    trigger_df: pd.DataFrame | None = None,
    feats_bias: pd.DataFrame | None = None,
    feats_trig: pd.DataFrame | None = None,
    commission_per_lot: float = 0.0,   # account currency per standard lot RT
    slippage_pips: float = 0.5,
    point_value: float = POINT,
) -> BacktestResult:
    """Simulate the strategy on historical bars.

    `bias_df` / `trigger_df` are raw OHLCV frames (with `spread` in points
    if available). Features and decisions are computed inside so the
    caller never accidentally feeds future data.

    For repeated runs over the SAME data (e.g. a parameter grid), pass
    precomputed `feats_bias` / `feats_trig` to skip indicator rebuilds.
    """
    if feats_bias is None:
        feats_bias = build_features(bias_df)
    if feats_trig is None:
        feats_trig = build_features(trigger_df)
    dec = evaluate(strat, feats_bias, feats_trig)

    # align by position; entry happens at NEXT bar open
    n = len(feats_trig)
    trades: list[Trade] = []
    equity = [0.0]
    in_pos = False
    pos = None

    for i in range(n - 1):
        # close any open position at bar i+1 if stop/target touched
        if in_pos:
            row = feats_trig.iloc[i + 1]
            reason, price = _bar_exit(
                pos.side, pos.entry_price, pos.stop, pos.target,
                float(row["high"]), float(row["low"]),
            )
            if reason:
                pos.exit_bar = int(i + 1)
                pos.exit_price = price
                pos.exit_reason = reason
                pos.duration_bars = pos.exit_bar - pos.entry_bar
                # gross points (signed)
                if pos.side == "BUY":
                    gross = (price - pos.entry_price)
                else:
                    gross = (pos.entry_price - price)
                # cost: spread (entry+exit) + slippage RT + commission
                sp = float(row.get("spread", 0.0) or 0.0)
                cost_pts = _cost_points(sp, slippage_pips, commission_per_lot, point_value)
                pos.points = gross
                pos.cost_pips = cost_pts * point_value / PIP
                pos.net_pips = gross * point_value / PIP - pos.cost_pips
                trades.append(pos)
                equity.append(equity[-1] + pos.net_pips)
                in_pos = False
                pos = None

        # open a new position if flat and signal says so (enter next bar)
        sig = dec["signal"].iloc[i + 1] if False else dec["signal"].iloc[i]
        if not in_pos and sig in ("BUY", "SELL"):
            row = feats_trig.iloc[i]
            # enter at NEXT bar open
            nxt = feats_trig.iloc[i + 1]
            entry = float(nxt["open"])
            stop = float(dec["stop"].iloc[i])
            target = float(dec["target"].iloc[i])
            # recompute stop/target relative to actual entry so geometry holds
            atr = float(row.get("atr", float("nan")))
            if not math.isnan(atr):
                mult = float(strat.stop.get("atr_multiple", 1.5))
                if sig == "BUY":
                    stop = entry - mult * atr
                    target = entry + mult * atr * float(strat.target.get("risk_reward", 2.0))
                else:
                    stop = entry + mult * atr
                    target = entry - mult * atr * float(strat.target.get("risk_reward", 2.0))
            pos = Trade(
                entry_bar=int(i + 1), entry_price=entry, side=sig,
                stop=stop, target=target, exit_bar=int(i + 1),
                exit_price=float("nan"), exit_reason="", points=float("nan"),
                cost_pips=0.0, net_pips=float("nan"), duration_bars=0,
            )
            in_pos = True

    # force-close any position still open at the last bar (mark-to-market)
    if in_pos and pos is not None:
        last = feats_trig.iloc[-1]
        pos.exit_bar = int(n - 1)
        pos.exit_price = float(last["close"])
        pos.exit_reason = "end"
        pos.duration_bars = pos.exit_bar - pos.entry_bar
        gross = (pos.exit_price - pos.entry_price) if pos.side == "BUY" else (pos.entry_price - pos.exit_price)
        pos.points = gross
        sp = float(last.get("spread", 0.0) or 0.0)
        cost_pts = _cost_points(sp, slippage_pips, commission_per_lot, point_value)
        pos.cost_pips = cost_pts * point_value / PIP
        pos.net_pips = gross * point_value / PIP - pos.cost_pips
        trades.append(pos)
        equity.append(equity[-1] + pos.net_pips)

    return BacktestResult(
        trades=trades,
        equity_curve=pd.Series(equity[1:], index=range(len(trades))),
        metrics=compute_metrics(trades),
    )


def _cost_points(spread_points: float, slippage_pips: float,
                 commission_per_lot: float, point_value: float) -> float:
    """Round-trip trading cost expressed in price points.

    spread paid on entry and exit + round-trip slippage + commission
    (converted to point-equivalent via point_value).
    """
    cost = spread_points + spread_points
    cost += slippage_pips * 2 * (PIP / point_value)
    cost += (commission_per_lot / point_value) if commission_per_lot else 0.0
    return cost


def compute_metrics(trades: list[Trade]) -> dict:
    """All metrics required by CLAUDE.md section 11 (where computable)."""
    if not trades:
        return {"total_trades": 0}
    wins = [t for t in trades if t.net_pips > 0]
    losses = [t for t in trades if t.net_pips <= 0]
    win_rate = len(wins) / len(trades)
    gross_win = sum(t.net_pips for t in wins)
    gross_loss = -sum(t.net_pips for t in losses)
    pf = (gross_win / gross_loss) if gross_loss > 0 else float("inf")
    expectancy = sum(t.net_pips for t in trades) / len(trades)

    # equity curve for drawdown / sharpe
    eq = np.cumsum([t.net_pips for t in trades])
    peak = np.maximum.accumulate(eq)
    dd = peak - eq
    max_dd = dd.max()

    per_trade = np.array([t.net_pips for t in trades])
    # simple per-trade sharpe/sortino (trades as "returns")
    sd = per_trade.std(ddof=1) if len(per_trade) > 1 else 0.0
    sharpe = (per_trade.mean() / sd) if sd > 0 else 0.0
    downside = per_trade[per_trade < 0]
    dsd = downside.std(ddof=1) if len(downside) > 1 else 0.0
    sortino = (per_trade.mean() / dsd) if dsd > 0 else 0.0

    # streaks
    cur_w = cur_l = best_w = best_l = 0
    for t in trades:
        if t.net_pips > 0:
            cur_w += 1; cur_l = 0; best_w = max(best_w, cur_w)
        else:
            cur_l += 1; cur_w = 0; best_l = max(best_l, cur_l)

    avg_win = (gross_win / len(wins)) if wins else 0.0
    avg_loss = (gross_loss / len(losses)) if losses else 0.0

    return {
        "total_trades": len(trades),
        "win_rate": win_rate,
        "lose_rate": 1 - win_rate,
        "net_pips": float(eq[-1]),
        "gross_win_pips": gross_win,
        "gross_loss_pips": gross_loss,
        "profit_factor": pf,
        "expectancy_pips": expectancy,
        "max_drawdown_pips": float(max_dd),
        "avg_win_pips": avg_win,
        "avg_loss_pips": avg_loss,
        "sharpe": float(sharpe),
        "sortino": float(sortino),
        "longest_win_streak": best_w,
        "longest_loss_streak": best_l,
        "avg_duration_bars": float(np.mean([t.duration_bars for t in trades])),
    }
