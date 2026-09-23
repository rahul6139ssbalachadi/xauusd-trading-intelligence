"""Risk engine + position sizing (Phase 10).

Implements fixed-fractional position sizing and hard risk guards. This is
MEASUREMENT/PLANNING only — it never places or modifies trades. All values
are computed from the strategy's risk_pct and the per-trade stop distance.

Sizing model (standard fixed-fractional):
    risk_amount_usd  = equity * risk_pct
    stop_distance_usd= stop_distance_price * contract_multiplier * lot_unit
    lot_size         = risk_amount_usd / stop_distance_usd

For gold XAUUSD with a standard lot:
    contract_multiplier = 100 USD per point per standard lot
    (1.0 lot moves $100 per $1 of price; 1 pip = 0.10 USD per lot)
    -> 1 standard lot risk per $1 stop = $100 ; per 1-pip stop = $10

Guards (all apply BEFORE a trade is "allowed"):
    - max_positions: never exceed strategy.max_positions open at once
    - max_risk_per_trade_pct: cap risk_pct if strategy's is too high
    - max_lots: hard ceiling on lot size (broker/safety)
    - min_lots: broker minimum
These are deliberately conservative and fail safe (return 0 lots if any
precondition is violated).
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

# Gold (XAUUSD) convention: standard lot = 100 USD per price point.
CONTRACT_MULTIPLIER = 100.0
PIP = 0.10  # USD per pip per standard lot


@dataclass
class AccountState:
    """Read-only snapshot of account equity used for sizing.

    Never constructed from a live connection here. In backtests it is the
    running equity; in paper/live it would come from the broker read-only.
    """
    equity: float = 10_000.0
    balance: float = 10_000.0


@dataclass
class RiskConfig:
    """Hard risk limits (Phase 10 guard rails)."""
    max_positions: int = 1
    max_risk_per_trade_pct: float = 0.01     # never risk more than 1% / trade
    max_lots: float = 5.0
    min_lots: float = 0.01
    lot_step: float = 0.01


def compute_lot_size(
    equity: float,
    risk_pct: float,
    stop_distance_price: float,
    contract_multiplier: float = CONTRACT_MULTIPLIER,
    cfg: RiskConfig | None = None,
) -> float:
    """Fixed-fractional lot size for one trade.

    Returns a valid lot size (>= min_lots, <= max_lots, rounded to lot_step),
    or 0.0 if the inputs are invalid (negative equity, zero/negative stop,
    or effective risk exceeds the cap). Rounding is FLOOR to lot_step so we
    never exceed intended risk.
    """
    cfg = cfg or RiskConfig()
    if equity <= 0 or stop_distance_price <= 0 or risk_pct <= 0:
        return 0.0

    # cap the effective risk at the hard limit
    eff_risk_pct = min(risk_pct, cfg.max_risk_per_trade_pct)
    risk_amount = equity * eff_risk_pct
    stop_distance_usd = stop_distance_price * contract_multiplier
    if stop_distance_usd <= 0:
        return 0.0
    raw_lots = risk_amount / stop_distance_usd

    # floor to lot step, apply min/max
    import math
    lots = math.floor(raw_lots / cfg.lot_step) * cfg.lot_step
    if lots < cfg.min_lots:
        return 0.0
    if lots > cfg.max_lots:
        lots = cfg.max_lots
    return round(lots, 2)


def risk_per_trade_usd(
    lots: float,
    stop_distance_price: float,
    contract_multiplier: float = CONTRACT_MULTIPLIER,
) -> float:
    """Actual USD risk for a sized position at its stop distance."""
    return lots * stop_distance_price * contract_multiplier


def can_open(
    open_positions: int,
    cfg: RiskConfig | None = None,
) -> bool:
    """Guard: are we allowed to open another position right now?"""
    cfg = cfg or RiskConfig()
    return open_positions < cfg.max_positions


def apply_risk_to_backtest(trades, equity_start: float = 10_000.0,
                           cfg: RiskConfig | None = None,
                           contract_multiplier: float = CONTRACT_MULTIPLIER,
                           risk_pct: float = 0.0025):
    """Attach lot size + USD risk to an existing list of Trade objects.

    Mutates each Trade in place, adding `lots` and `risk_usd`. Net P&L is
    also converted to USD (net_pips * PIP * lots) so the equity curve is in
    dollars, enabling dollar-denominated drawdown / expectancy.

    `risk_pct` defaults to 0.25% but can be overridden (e.g. from a
    strategy's own risk_pct field) so wide-stop strategies like V11 get the
    risk fraction they actually declare.
    """
    cfg = cfg or RiskConfig()
    for t in trades:
        stop_dist = abs(t.entry_price - t.stop)
        t.lots = compute_lot_size(equity_start, risk_pct, stop_dist,
                                  contract_multiplier, cfg)
        t.risk_usd = risk_per_trade_usd(t.lots, stop_dist, contract_multiplier)
        # recompute net in USD
        t.net_usd = t.net_pips * PIP * t.lots if hasattr(t, "net_pips") else 0.0
    return trades
