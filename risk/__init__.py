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
    # How much the 0.01 lot floor may push ACTUAL risk above the configured
    # risk before the trade is refused. 0.0 = strict: any rounding up is
    # refused. Raise it only with a written reason.
    lot_floor_tolerance: float = 0.0


# Reasons a sizing request can be refused. Stable strings: they are journaled
# and grepped, so treat them as an interface.
REASON_OK = ""
REASON_INVALID_INPUT = "INVALID_INPUT"
REASON_LOT_FLOOR_EXCEEDS_RISK = "LOT_FLOOR_EXCEEDS_RISK"
REASON_RISK_CAP_EXCEEDED = "RISK_CAP_EXCEEDED"


@dataclass
class SizingDecision:
    """The result of sizing ONE trade, with the arithmetic exposed.

    Exists because "0 lots" alone is not an answer an operator can act on.
    The interesting case is REASON_LOT_FLOOR_EXCEEDS_RISK: the trade is
    economically correct but the broker's 0.01 minimum would force real risk
    above the configured risk, so it is refused rather than resized.

    Every field is a plain number so this can be journaled as JSON directly.
    """
    lots: float                  # 0.0 when declined
    declined: bool
    reason: str
    raw_lots: float              # unrounded, pre-floor
    floor_lots: float            # what the broker minimum would force
    intended_risk_usd: float
    actual_risk_usd: float       # 0.0 when declined
    intended_risk_pct: float
    actual_risk_pct: float       # 0.0 when declined
    stop_distance_price: float
    equity: float

    @property
    def floor_risk_pct(self) -> float:
        """What the risk WOULD be if the 0.01 floor were accepted.

        This is the number that justifies the refusal, so it is reported
        even when the trade is declined.
        """
        if self.equity <= 0 or self.stop_distance_price <= 0:
            return 0.0
        return (self.floor_lots * self.stop_distance_price
                * CONTRACT_MULTIPLIER) / self.equity

    def to_dict(self) -> dict:
        d = dict(self.__dict__)
        d["floor_risk_pct"] = self.floor_risk_pct
        return d


def size_position(
    equity: float,
    risk_pct: float,
    stop_distance_price: float,
    contract_multiplier: float = CONTRACT_MULTIPLIER,
    cfg: RiskConfig | None = None,
) -> SizingDecision:
    """Size one trade and say WHY if it cannot be sized.

    The difference from compute_lot_size(): this never silently returns 0.
    It distinguishes "inputs were nonsense" from "the broker's 0.01 lot
    minimum would make me risk more than you configured", and reports the
    computed lot and the actual risk percentage in both cases.

    Refusal rule for the lot floor: if the correct position is SMALLER than
    the broker minimum, the only way to trade is to round UP, which risks
    more than intended. That is refused, not resized — per the standing rule
    that a stop or a size is never widened to make a trade fit.
    """
    cfg = cfg or RiskConfig()
    stop = float(stop_distance_price)

    def refuse(reason: str) -> SizingDecision:
        return SizingDecision(
            lots=0.0, declined=True, reason=reason, raw_lots=0.0,
            floor_lots=cfg.min_lots, intended_risk_usd=0.0,
            actual_risk_usd=0.0, intended_risk_pct=0.0, actual_risk_pct=0.0,
            stop_distance_price=stop, equity=float(equity),
        )

    if equity <= 0 or stop <= 0 or risk_pct <= 0:
        return refuse(REASON_INVALID_INPUT)

    eff_risk_pct = min(risk_pct, cfg.max_risk_per_trade_pct)
    intended_usd = equity * eff_risk_pct
    stop_usd = stop * contract_multiplier
    if stop_usd <= 0:
        return refuse(REASON_INVALID_INPUT)

    raw_lots = intended_usd / stop_usd
    import math
    floored = math.floor(raw_lots / cfg.lot_step) * cfg.lot_step

    # THE LOT FLOOR: correct size is below the broker minimum, so trading at
    # the minimum would over-risk. Compare against the configured risk, not
    # the capped one, so the message names the number the operator set.
    if floored < cfg.min_lots:
        floor_risk_usd = cfg.min_lots * stop_usd
        floor_risk_pct = floor_risk_usd / equity
        configured = min(risk_pct, cfg.max_risk_per_trade_pct)
        if floor_risk_pct > configured * (1.0 + cfg.lot_floor_tolerance):
            return SizingDecision(
                lots=0.0, declined=True,
                reason=REASON_LOT_FLOOR_EXCEEDS_RISK,
                raw_lots=round(raw_lots, 6), floor_lots=cfg.min_lots,
                intended_risk_usd=round(intended_usd, 2),
                actual_risk_usd=0.0,
                intended_risk_pct=round(configured, 6),
                actual_risk_pct=0.0,
                stop_distance_price=stop, equity=float(equity),
            )
        # Tolerance accepted it: size at the floor, risk stated honestly.
        lots = cfg.min_lots
    else:
        lots = floored

    if lots > cfg.max_lots:
        lots = cfg.max_lots

    lots = round(lots, 2)
    actual_usd = risk_per_trade_usd(lots, stop, contract_multiplier)
    return SizingDecision(
        lots=lots, declined=False, reason=REASON_OK,
        raw_lots=round(raw_lots, 6), floor_lots=cfg.min_lots,
        intended_risk_usd=round(intended_usd, 2),
        actual_risk_usd=round(actual_usd, 2),
        intended_risk_pct=round(min(risk_pct, cfg.max_risk_per_trade_pct), 6),
        actual_risk_pct=round(actual_usd / equity, 6),
        stop_distance_price=stop, equity=float(equity),
    )


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

    Thin wrapper over size_position() so there is exactly ONE sizing
    implementation. Callers that need the reason (and the lot-floor
    arithmetic) should use size_position() directly.
    """
    return size_position(equity, risk_pct, stop_distance_price,
                         contract_multiplier, cfg).lots


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
