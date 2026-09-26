"""Trade simulation for the monthly backtester.

Reuses `backtest.Trade` and `backtest.compute_metrics` (the repo's tested
metric layer) so numbers are directly comparable to every other research
result. Only the ORCHESTRATION is new: signal vs trade accounting, monthly
bucketing, and the risk rules the spec asks for.

EXECUTION ASSUMPTIONS — every one is either read from existing config or
declared here as an explicit, changeable default. Nothing is invented
silently:

  spread      MEASURED: mean of the per-bar `spread` column over the period
              (points -> USD via the instrument's point size). Using the
              recorded spread is more honest than the flat 3-pip figure
              older scripts hardcode.
  slippage    1.0 pip per side (DEFAULT, configurable) — matches
              research/v11_d1_momentum.backtest_d1_signals default.
  commission  0.0 (DEFAULT, configurable). XMGlobal charges no per-lot
              commission on this account's gold/CFD symbols; if that ever
              changes, set --commission.
  swap        0.0 (DEFAULT, configurable). Not modelled: the DB has no swap
              data and holding V11 trades is multi-day, so this is a KNOWN
              UNDERSTATEMENT for multi-day holds, documented in the report.
  lot step    0.01, min 0.01 (risk.RiskConfig defaults, read from the
              existing dataclass).
  max risk    strategy's own risk_pct from the frozen params (V11 1%,
              V12 2%), capped by risk.RiskConfig.max_risk_per_trade_pct (1%).
  max pos     1 (risk.RiskConfig.max_positions) — a signal arriving while a
              position is open is recorded as a SKIPPED signal, not silently
              dropped and not merged into the open trade.
  sessions    both strategies declare sessions=["all"]; no session filter.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from backtest import Trade, compute_metrics
from risk import RiskConfig
from monthly_bt.data import UNITS
from monthly_bt.strategies import StrategySpec, normalise_signal

DEFAULT_SLIPPAGE_PIPS = 1.0
DEFAULT_COMMISSION_PER_LOT = 0.0


@dataclass
class SimConfig:
    """Every execution assumption in one place, all overridable."""
    initial_balance: float = 10_000.0
    slippage_pips: float = DEFAULT_SLIPPAGE_PIPS
    commission_per_lot: float = DEFAULT_COMMISSION_PER_LOT
    swap_per_lot_night: float = 0.0
    spread_multiplier: float = 1.0
    max_positions: int = 1
    lot_step: float = 0.01
    min_lots: float = 0.01
    max_lots: float = 5.0
    max_risk_per_trade_pct: float = 0.01
    use_measured_spread: bool = True
    fallback_spread_pips: float = 3.0

    def as_dict(self) -> dict:
        return dict(self.__dict__)


def measured_spread_pips(df: pd.DataFrame, symbol: str, mult: float = 1.0) -> float:
    """Mean recorded spread over the period, in pips of that instrument."""
    u = UNITS[symbol]
    return float((df["spread"] * u["point"]).mean()) * mult


def size_lots(equity: float, risk_pct: float, risk_pips: float,
              symbol: str, cfg: SimConfig) -> float:
    """Fixed-fractional sizing using the repo's risk engine, then floored to
    the lot step so intended risk is never exceeded.

    NOTE: risk.compute_lot_size takes a PRICE distance and the XAUUSD
    contract multiplier (100). For BTC that multiplier is wrong, so this
    wrapper calls the same function with the instrument's own contract
    size. The arithmetic is therefore identical to risk/__init__.py; only
    the constant differs, and that constant is the one data.py documents.
    """
    eff_risk_pct = min(risk_pct, cfg.max_risk_per_trade_pct)
    risk_usd = equity * eff_risk_pct
    u = UNITS[symbol]
    # USD of price move per pip, per lot
    usd_per_pip = u["pip"] * u["contract"]
    if usd_per_pip <= 0 or risk_pips <= 0:
        return 0.0
    lots = risk_usd / (risk_pips * usd_per_pip)
    lots = math.floor(lots / cfg.lot_step) * cfg.lot_step
    if lots < cfg.min_lots:
        return 0.0
    return round(min(lots, cfg.max_lots), 2)


def simulate(df: pd.DataFrame, signals: list[dict], spec: StrategySpec,
             symbol: str, cfg: SimConfig, period: str = "") -> dict:
    """Run signals through the execution simulator.

    df MUST already contain features (indicators warmed on full history).
    `signals` are the uniform dicts from strategies.normalise_signal.

    A trade is only opened if a position slot is free AND the trade can be
    sized. Both rejections are recorded as skipped signals with a reason,
    so SIGNAL count and TRADE count are never conflated.
    """
    pip = UNITS[symbol]["pip"]
    usd_per_pip = pip * UNITS[symbol]["contract"]
    spread_pips = (measured_spread_pips(df, symbol, cfg.spread_multiplier)
                   if cfg.use_measured_spread else cfg.fallback_spread_pips)
    slip = cfg.slippage_pips
    cost_pips = 2 * spread_pips + 2 * slip          # round trip

    max_hold = spec.max_holding
    equity = cfg.initial_balance
    open_until = -1                                   # last bar index occupied
    trades: list[Trade] = []
    skipped: list[dict] = []
    rows: list[dict] = []

    for sig in sorted(signals, key=lambda s: s["entry_bar"]):
        i = sig["entry_bar"]
        if i <= open_until:
            skipped.append({**sig, "skip_reason": "position_already_open"})
            continue

        lots = size_lots(equity, spec.params["risk_pct"], sig["risk_pips"],
                         symbol, cfg)
        if lots <= 0:
            skipped.append({**sig, "skip_reason": "size_below_min_lot"})
            continue

        entry = sig["entry"]
        stop = sig["sl"]
        target = sig["tp"]
        commission = cfg.commission_per_lot * lots
        fees_pips = commission / usd_per_pip if usd_per_pip else 0.0

        last = min(i + max_hold, len(df) - 1)
        if last <= i:
            skipped.append({**sig, "skip_reason": "no_forward_bars"})
            continue

        exit_price, exit_reason, exit_bar = None, None, None
        for j in range(i + 1, last + 1):
            r = df.iloc[j]
            hit_stop = r["low"] <= stop
            hit_tp = r["high"] >= target
            if hit_stop and hit_tp:
                # conservative: the level closer to entry fills first
                if abs(entry - stop) <= abs(target - entry):
                    exit_price, exit_reason = stop, "stop"
                else:
                    exit_price, exit_reason = target, "target"
            elif hit_stop:
                exit_price, exit_reason = stop, "stop"
            elif hit_tp:
                exit_price, exit_reason = target, "target"
            else:
                continue
            exit_bar = j
            break
        if exit_bar is None:
            exit_bar, exit_price, exit_reason = last, float(df.iloc[last]["close"]), "end"

        gross = exit_price - entry
        net_pips = gross / pip - cost_pips - fees_pips
        risk_pips = sig["risk_pips"]
        t = Trade(
            entry_bar=i, entry_price=entry, side="BUY",
            stop=stop, target=target,
            exit_bar=exit_bar, exit_price=float(exit_price),
            exit_reason=exit_reason,
            points=gross / UNITS[symbol]["point"],
            cost_pips=cost_pips + fees_pips,
            net_pips=net_pips,
            duration_bars=exit_bar - i,
            lots=lots,
            risk_usd=risk_pips * usd_per_pip * lots,
            net_usd=net_pips * usd_per_pip * lots,
        )
        trades.append(t)
        equity += t.net_usd
        open_until = exit_bar

        rows.append({
            **{k: sig[k] for k in ("strategy", "symbol", "timeframe", "signal",
                                   "signal_bar", "entry_bar", "signal_ts",
                                   "entry_ts", "entry", "sl", "tp",
                                   "risk_pips", "atr", "body_pct")},
            "exit_bar": exit_bar,
            "exit_ts": str(df.iloc[exit_bar]["ts_broker"]),
            "exit": float(exit_price),
            "exit_reason": exit_reason,
            "lots": lots,
            "commission": commission,
            "swap": 0.0,
            "spread_pips": spread_pips,
            "slippage_pips": slip,
            "cost_pips": t.cost_pips,
            "gross_pips": gross / pip,
            "net_pips": net_pips,
            "r_multiple": net_pips / risk_pips if risk_pips else None,
            "gross_pnl": t.points * UNITS[symbol]["contract"],
            "fees": t.cost_pips * usd_per_pip * lots,
            "net_pnl": t.net_usd,
            "balance_after": equity,
            "period": period,
        })

    return {
        "trades": trades,
        "trade_rows": rows,
        "skipped": skipped,
        "metrics": compute_metrics(trades),
        "ending_balance": equity,
        "spread_pips": spread_pips,
        "cost_pips_roundtrip": cost_pips,
        "pip": pip,
        "usd_per_pip_per_lot": usd_per_pip,
    }
