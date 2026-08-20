"""Live signal engine for RK Trade.

Computes real-time BUY/SELL/WAIT signals for each user's registered
strategies using the live feed data. This module reuses the tested
Phase 7-8 strategy engine (evaluate + build_features) and risk engine
(Phase 10) to produce structured decisions with confidence and reasons.

SAFETY: This engine is ANALYSIS-ONLY. It produces signals with reasons,
confidence, stop, target, position size, and risk — but does NOT execute.
Users review the output and manually execute.

Per CLAUDE.md §17, the decision output includes:
  - Signal: BUY / SELL / WAIT
  - Confidence: percentage
  - Market regime, entry zone, SL, TP, risk %
  - Evidence list + invalidation conditions
  - Reasons NOT to trade
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import math

import numpy as np
import pandas as pd

from rk_trade.live_feed import LiveFeed
from rk_trade.strategies import StrategyRegistry
from rk_trade.users import UserManager
from strategy import Strategy, build_features, evaluate
from risk import RiskConfig, compute_lot_size, risk_per_trade_usd, AccountState
from market_data.config import PROJECT_ROOT


@dataclass
class SignalDecision:
    """A single strategy's real-time decision output."""
    timestamp: str
    user: str
    strategy_name: str
    strategy_version: str
    symbol: str
    signal: str        # BUY / SELL / WAIT
    confidence: float  # 0-100
    bias: str          # up / down / flat
    reasons: list[str] = field(default_factory=list)
    stop: float = float("nan")
    target: float = float("nan")
    lots: float = 0.0
    risk_usd: float = 0.0
    risk_pct: float = 0.0
    regime: str = ""
    spread_pips: float = 0.0
    wait_reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp,
            "user": self.user,
            "strategy": self.strategy_name,
            "version": self.strategy_version,
            "symbol": self.symbol,
            "signal": self.signal,
            "confidence": self.confidence,
            "bias": self.bias,
            "reasons": self.reasons,
            "stop": self.stop if not (isinstance(self.stop, float) and math.isnan(self.stop)) else None,
            "target": self.target if not (isinstance(self.target, float) and math.isnan(self.target)) else None,
            "lots": self.lots,
            "risk_usd": self.risk_usd,
            "risk_pct": self.risk_pct,
            "regime": self.regime,
            "spread_pips": self.spread_pips,
            "wait_reasons": self.wait_reasons,
        }


class SignalEngine:
    """Compute live signals for all of a user's active strategies."""

    def __init__(self, user_mgr: UserManager, strat_reg: StrategyRegistry,
                 risk_cfg: RiskConfig | None = None,
                 account_equity: float = 10_000.0):
        self.um = user_mgr
        self.sr = strat_reg
        self.risk_cfg = risk_cfg or RiskConfig()
        self.account = AccountState(equity=account_equity, balance=account_equity)
        self._bias_cache: dict[str, pd.DataFrame] = {}
        self._trig_cache: dict[str, pd.DataFrame] = {}

    def _prepare_data(self, symbol: str, tf: str = "M5",
                      lookback: int = 500) -> tuple[pd.DataFrame, pd.DataFrame]:
        """Fetch M15 (bias) + M5 (trigger) data, build features."""
        if not hasattr(self, "_feed"):
            self._feed = LiveFeed(symbol)

        with self._feed as feed:
            m15_raw = feed.get_latest_bars("M15", lookback)
            m5_raw = feed.get_latest_bars(tf, lookback)

        # convert spread points -> pips (gold: 1 pip = 0.10, 1 point = 0.01)
        for df in (m15_raw, m5_raw):
            if "spread" in df.columns:
                df["spread_pips"] = df["spread"] * 0.01 / 0.10
            df["ts"] = df["timestamp"]

        m15_feats = build_features(m15_raw)
        m5_feats = build_features(m5_raw)
        return m15_feats, m5_feats

    def compute_signal(self, username: str, strategy_alias: str,
                       symbol: str = "XAUUSD") -> SignalDecision:
        """Compute a real-time signal for one user + strategy."""
        from pathlib import Path
        # load the user's strategy file
        reg = self.sr._load_registry(username)
        if strategy_alias not in reg:
            raise KeyError(f"Strategy '{strategy_alias}' not registered for user '{username}'")
        strat_path = self.sr.um.strategy_dir(username) / reg[strategy_alias]["file"]
        strat = Strategy.load(Path(strat_path))

        # fetch live data
        m15_feats, m5_feats = self._prepare_data(symbol, "M5", lookback=500)
        if len(m15_feats) < 50 or len(m5_feats) < 50:
            return SignalDecision(
                timestamp=datetime.now(timezone.utc).isoformat(),
                user=username, strategy_name=strat.name,
                strategy_version=strat.version, symbol=symbol,
                signal="WAIT", confidence=0.0, bias="flat",
                wait_reasons=["insufficient data for indicators"],
            )

        # evaluate using the strategy engine (reuse Phase 7 logic)
        dec = evaluate(strat, m15_feats, m5_feats)
        latest = dec.iloc[-1]
        latest_m5 = m5_feats.iloc[-1]

        signal = latest["signal"]
        bias = latest["bias"]
        reasons = latest.get("reasons", []) if isinstance(latest.get("reasons"), list) else []

        # confidence: based on number of confirming factors + ADX strength
        confidence = _compute_confidence(strat, latest_m5, reasons)

        # regime
        regime = latest_m5.get("vol_regime", "unknown")

        # risk calc
        risk_pct = strat.risk_pct
        stop = float(latest.get("stop", float("nan")))
        target = float(latest.get("target", float("nan")))
        lots = 0.0
        risk_usd = 0.0
        if signal in ("BUY", "SELL") and not math.isnan(stop):
            stop_dist = abs(float(latest_m5.get("close", 0)) - stop)
            lots = compute_lot_size(self.account.equity, risk_pct, stop_dist,
                                    cfg=self.risk_cfg)
            risk_usd = lots * stop_dist * 100.0  # gold: 100 USD/pt per lot

        spread_pips = float(latest_m5.get("spread_pips", 0.0) or 0.0)

        return SignalDecision(
            timestamp=datetime.now(timezone.utc).isoformat(),
            user=username,
            strategy_name=strat.name,
            strategy_version=strat.version,
            symbol=symbol,
            signal=signal,
            confidence=confidence,
            bias=bias,
            reasons=reasons,
            stop=stop if not math.isnan(stop) else float("nan"),
            target=target if not math.isnan(target) else float("nan"),
            lots=lots,
            risk_usd=risk_usd,
            risk_pct=risk_pct * 100,
            regime=regime,
            spread_pips=spread_pips,
        )

    def compute_all(self, username: str, symbol: str = "XAUUSD") -> list[SignalDecision]:
        """Compute signals for all of a user's active strategies."""
        active = self.sr.get_active(username)
        if not active:
            return []
        decisions = []
        for alias, _path in active:
            try:
                d = self.compute_signal(username, alias, symbol)
                decisions.append(d)
            except Exception as e:
                decisions.append(SignalDecision(
                    timestamp=datetime.now(timezone.utc).isoformat(),
                    user=username, strategy_name=alias, strategy_version="ERR",
                    symbol=symbol, signal="WAIT", confidence=0.0, bias="flat",
                    wait_reasons=[f"engine error: {e}"],
                ))
        return decisions


def _compute_confidence(strat: Strategy, latest_m5: pd.Series,
                         reasons: list[str]) -> float:
    """Estimate confidence from confirming factors + ADX strength.

    Simple heuristic: 10 points per reason + ADX bonus (0-30 pts).
    Capped at 95. This is NOT a profitability predictor — it's a
    transparency aid per CLAUDE.md §32 (every agent response must be
    structured with confidence).
    """
    points = len(reasons) * 10
    adx = float(latest_m5.get("adx", 0.0) or 0.0)
    points += min(adx / 2, 30)  # ADX up to 60 -> 30 pts
    # penalty for WAIT signals
    if not reasons or len(reasons) < 3:
        points -= 20
    return min(max(points, 0.0), 95.0)
