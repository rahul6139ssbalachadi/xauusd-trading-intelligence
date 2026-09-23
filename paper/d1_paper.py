"""D1 paper-trading harness for V11 (D1 Momentum Breakout).

This is a SIMULATION-ONLY journal harness. It does NOT place, modify, or close
any trade and never connects to MT5 in trade mode. It reuses the V11 research
logic (compute_d1_signals + backtest_d1_signals) to generate buy-side D1
signals, then records every bar into the same append-only JournalStore used
by the generic paper_run, so the audit trail is uniform regardless of whether
a strategy lives in the generic engine or in a standalone research script.

The journal captures, per bar:
  - the decision (BUY / WAIT)
  - the per-bar bias (EMA21>EMA55 => bullish D1 trend)
  - the human-readable reasons WHY the signal fired (or didn't)
  - planned entry, stop, target, lots, risk_usd (reusing Phase 10 sizing)

Per CLAUDE.md section 22 and the user's standing safety rule: the live demo
MT5 account is never touched; live_trading_enabled stays False.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from paper import JournalEntry, JournalStore
from risk import RiskConfig, apply_risk_to_backtest
import research.v11_d1_momentum as v11


def _load_d1() -> pd.DataFrame:
    """Load the full D1 XAUUSD frame with the feature columns V11 needs."""
    d1 = v11.load("D1")
    return v11.build_d1_features(d1)


def _reasons_for_signal(bar_row: pd.Series, params: dict) -> list[str]:
    """Human-readable reasons explaining why a signal fired on this bar."""
    reasons: list[str] = []
    if bar_row["d1_trend"] == "bull":
        reasons.append("D1 EMA21 > EMA55 (bullish trend)")
    else:
        reasons.append(f"D1 trend {bar_row['d1_trend']} (requires bull)")
    if pd.notna(bar_row.get("body_pct")):
        if bar_row["body_pct"] >= params["body_pct_threshold"]:
            reasons.append(
                f"body_pct {bar_row['body_pct']:.2f} >= {params['body_pct_threshold']}"
            )
        else:
            reasons.append(
                f"body_pct {bar_row['body_pct']:.2f} < {params['body_pct_threshold']}"
            )
    else:
        reasons.append("body_pct NaN (insufficient history)")
    if pd.notna(bar_row.get("atr14")) and pd.notna(bar_row.get("close")):
        atr_pct = bar_row["atr14"] / bar_row["close"]
        if atr_pct >= params["min_atr_pct"]:
            reasons.append(f"ATR/price={atr_pct:.4f} >= {params['min_atr_pct']}")
        else:
            reasons.append(f"ATR/price={atr_pct:.4f} < {params['min_atr_pct']}")
    if bar_row["body"] > 0:
        reasons.append("bullish bar (close>open)")
    else:
        reasons.append("not a bullish bar")
    return reasons


def paper_run_d1(
    strat_params: dict,
    journal: JournalStore,
    equity: float = 10_000.0,
    cfg: RiskConfig | None = None,
    risk_pct: float = 0.01,
    max_entries: int | None = None,
) -> list:
    """Run V11 signal generation over the full D1 dataset and journal decisions.

    Mirrors the generic paper_run semantics but operates on V11's own
    signal/backtest path. For every D1 bar we record the decision state:

      - Signal bars that produce a trade: journal the BUY decision with planned
        entry/stop/target/lots/risk. V11 enters at bar i+1 (next D1 open)
        after the signal fires on bar i, so the signal bar i is logged as BUY
        and the trade mapped by entry_bar == i+1 supplies the sizing.
      - All other bars: journal a WAIT with bias + reasons.

    `risk_pct` defaults to 1% per trade per user instruction. R:R 1:2 means
    the reward target is 2% of equity per winning trade.

    Returns the list of sized trades (matching run_backtest output shape).
    """
    cfg = cfg or RiskConfig()
    d1 = _load_d1()

    # Generate signals with the provided params
    signals = v11.compute_d1_signals(d1, **strat_params)

    # Run the backtest to get sized trades (reuses Phase 10 risk sizing)
    bt_params = {k: strat_params[k] for k in ("rr", "atr_mult_stop", "max_holding_d1")}
    trades, metrics = v11.backtest_d1_signals(d1, signals, **bt_params)
    sized = apply_risk_to_backtest(trades, equity_start=equity, cfg=cfg,
                                    risk_pct=risk_pct)

    # Map TRADE ENTRY BAR -> trade (entry_bar = signal_bar + 1 in V11)
    trade_by_entry = {t.entry_bar: t for t in sized}
    # Map SIGNAL BAR -> trade (signal bar fires on i, entry at i+1)
    trade_by_signal = {t.entry_bar - 1: t for t in sized}

    count = 0
    for i in range(len(d1)):
        row = d1.iloc[i]

        if i in trade_by_signal:
            t = trade_by_signal[i]
            reasons = _reasons_for_signal(row, strat_params)
            reasons.append(
                f"signal fired -> entry at next D1 bar open ${t.entry_price:.2f}"
            )
            reasons.append(f"stop=${t.stop:.2f}")
            reasons.append(f"target=${t.target:.2f}")
            reasons.append(f"lots={t.lots:.2f}")
            reasons.append(f"risk=${t.risk_usd:.2f}")

            entry = JournalEntry(
                ts=str(row["ts"]),
                strategy="XAUUSD_D1_MOMENTUM_BREAKOUT",
                version="V11",
                decision="BUY",
                bias=row["d1_trend"],
                reasons=reasons,
                entry_plan=t.entry_price,
                stop=t.stop,
                target=t.target,
                lots=t.lots,
                risk_usd=t.risk_usd,
                equity=equity,
            )
        else:
            # WAIT — determine the primary reason
            reasons = _reasons_for_signal(row, strat_params)
            if i < 55:
                reasons.append("warmup (need EMA55)")
            elif row["d1_trend"] != "bull":
                reasons.append("D1 trend not bullish - skip")
            elif pd.isna(row.get("body_pct")):
                reasons.append("body_pct NaN - insufficient history")
            elif row["body_pct"] < strat_params["body_pct_threshold"]:
                reasons.append(
                    f"body_pct {row['body_pct']:.2f} below threshold "
                    f"{strat_params['body_pct_threshold']} - no momentum signal"
                )
            elif row["body"] <= 0:
                reasons.append("bar not bullish - no signal")
            else:
                reasons.append("no entry conditions met")

            entry = JournalEntry(
                ts=str(row["ts"]),
                strategy="XAUUSD_D1_MOMENTUM_BREAKOUT",
                version="V11",
                decision="WAIT",
                bias=row["d1_trend"],
                reasons=reasons,
                equity=equity,
            )

        journal.append(entry)
        count += 1
        if max_entries and count >= max_entries:
            break

    return sized
