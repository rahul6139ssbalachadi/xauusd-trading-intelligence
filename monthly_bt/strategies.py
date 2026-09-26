"""Strategy adapters — REUSE, never re-implementation.

Both adapters import the *existing* production/research code and call it.
No signal formula is restated here.

V11  research.v11_d1_momentum.build_d1_features  (D1)
     research.v11_d1_momentum.compute_d1_signals  (keyword-only args)
     params frozen at execution/run_v11_daily.py:PARAMS, which mirror
     strategy/defs/XAUUSD_D1_MOMENTUM_BREAKOUT_V11.json research_results.

V12  execution.run_v12_hourly.build_h1_features    (H1)
     execution.run_v12_hourly.PARAMS               (frozen)
     execution.run_v12_hourly.signal_on_last_closed_bar evaluates the gate
     set on a SINGLE bar; for a historical scan we evaluate the same gate
     set on EVERY bar. `verify_v12_gate_parity` (and
     tests/test_monthly_bt.py::TestV12Parity) prove the two agree bar for
     bar, so the historical scan is the same logic, not a lookalike.

LOOK-AHEAD
  Both strategies enter at the OPEN of the bar AFTER the signal bar
  (`entry_bar = i + 1`). The signal bar's own close is the last information
  used, and it is only known once that bar has closed. Indicators are
  computed on the full history BEFORE slicing, so every EMA/ATR/rank value
  at bar i uses only bars <= i. `fwd1/fwd3/fwd5` columns in V11's feature
  builder are forward-looking by construction but are NEVER read by
  compute_d1_signals (verified by the parity test); they exist only for
  the drift-context printout in research/v11_d1_momentum.main().
"""
from __future__ import annotations

import inspect
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
import sys
if str(ROOT) not in sys.path:            # noqa: E402
    sys.path.insert(0, str(ROOT))

from execution.run_v11_daily import PARAMS as V11_LIVE_PARAMS
from execution.run_v12_hourly import PARAMS as V12_LIVE_PARAMS
from execution.run_v12_hourly import build_h1_features
import research.v11_d1_momentum as v11_research
from monthly_bt.data import UNITS


@dataclass(frozen=True)
class StrategySpec:
    key: str                 # "V11" / "V12"
    version: str             # "V11" / "V12"
    name: str                # strategy def name
    timeframe: str           # D1 / H1
    params: dict
    max_holding: int
    source: str              # where the logic + params came from


V11 = StrategySpec(
    key="V11", version="V11", name="XAUUSD_D1_MOMENTUM_BREAKOUT",
    timeframe="D1",
    params=dict(V11_LIVE_PARAMS),      # body_pct .95, min_atr .005, rr 2.0,
    max_holding=V11_LIVE_PARAMS["max_holding_d1"],
    source="execution/run_v11_daily.py:PARAMS + "
           "research/v11_d1_momentum.py:compute_d1_signals",
)

V12 = StrategySpec(
    key="V12", version="V12", name="XAUUSD_H1_MOMENTUM_BREAKOUT",
    timeframe="H1",
    params=dict(V12_LIVE_PARAMS),      # body_pct .95, min_atr .004, rr 3.0,
    max_holding=V12_LIVE_PARAMS["max_holding_h1"],
    source="execution/run_v12_hourly.py:PARAMS + "
           "execution/run_v12_hourly.py:signal_on_last_closed_bar gate set",
)

STRATEGIES = {"V11": V11, "V12": V12}


def get(key: str) -> StrategySpec:
    if key.upper() not in STRATEGIES:
        raise ValueError(f"unknown strategy {key!r}; have {sorted(STRATEGIES)}")
    return STRATEGIES[key.upper()]


# ---------------------------------------------------------------- V11
_SIGNAL_KWARGS_V11 = {
    "body_pct_threshold", "trend_filter", "min_atr_pct",
    "rr", "atr_mult_stop", "max_holding_d1",
}


def v11_signals(df: pd.DataFrame) -> list[dict]:
    """V11 signals via the EXISTING research function, unchanged.

    Only the params dict is filtered to the function's keyword-only
    signature (risk_pct is a live-sizing field, not a signal field).
    """
    kwargs = {k: V11.params[k] for k in _SIGNAL_KWARGS_V11}
    assert set(kwargs) <= set(inspect.signature(
        v11_research.compute_d1_signals).parameters), "V11 kwargs drifted"
    return v11_research.compute_d1_signals(df, **kwargs)


def v11_features(df: pd.DataFrame) -> pd.DataFrame:
    """Features from the existing research builder, unchanged."""
    return v11_research.build_d1_features(df)


# ---------------------------------------------------------------- V12
def v12_features(df: pd.DataFrame) -> pd.DataFrame:
    """H1 features from the existing live module, unchanged."""
    return build_h1_features(df)


def _v12_gate(row: pd.Series) -> bool:
    """The exact gate set of execution.run_v12_hourly.signal_on_last_closed_bar,
    factored out so the same four conditions can be applied bar by bar.
    Each condition is copied verbatim from that function's body."""
    if row["d1_trend"] != "bull":
        return False
    if pd.isna(row["body_pct"]) or row["body_pct"] < V12.params["body_pct_threshold"]:
        return False
    if row["body"] <= 0:
        return False
    if pd.isna(row["atr14"]) or row["atr14"] / row["close"] < V12.params["min_atr_pct"]:
        return False
    return True


def v12_signals(df: pd.DataFrame) -> list[dict]:
    """V12 signals over full history, same gate set, same entry rule.

    Entry is at bar i+1's open (V11's convention and the only convention
    the live runner implies: it reads the NEXT bar's open as entry_ref).
    """
    return v12_signals_from_features(v12_features(df))


def v12_signals_from_features(feats: pd.DataFrame) -> list[dict]:
    """Signal scan over an ALREADY-built feature frame.

    Split from v12_signals deliberately: recomputing features inside a
    month-sized window would cold-start the EMA55 / ATR14 / 60-bar rank and
    silently change the strategy. Callers that sliced a period must pass
    the features they built on the full history.
    """
    p = V12.params
    out: list[dict] = []
    for i in range(len(feats) - 1):
        if i < 55:                        # same warmup as the live gate
            continue
        row = feats.iloc[i]
        if not _v12_gate(row):
            continue
        j = i + 1
        entry = float(feats.iloc[j]["open"])
        stop = entry - p["atr_mult_stop"] * float(row["atr14"])
        risk = entry - stop
        if risk <= 0:
            continue
        out.append({
            "signal_bar": i,
            "entry_bar": j,
            "entry_ts": feats.iloc[j]["ts"],
            "signal_ts": feats.iloc[i]["ts"],
            "entry_price": entry,
            "stop": stop,
            "target": entry + risk * p["rr"],
            "risk": risk,
            "direction": "LONG",
            "body_pct": float(row["body_pct"]),
            "atr": float(row["atr14"]),
        })
    return out


def v12_gate_series(df: pd.DataFrame) -> pd.Series:
    """Boolean gate mask, used by verify_v12_gate_parity and tests."""
    feats = v12_features(df)
    return feats.apply(lambda r: _v12_gate(r), axis=1)


def verify_v12_gate_parity(df: pd.DataFrame, n: int = 300) -> dict:
    """Prove the historical scan reproduces the LIVE gate exactly.

    For n different bar positions, take the frame up to that bar, so the
    live function's `iloc[-2]` is the same bar, and compare its verdict to
    our bar-wise gate. Any disagreement is a logic drift bug.
    """
    from execution.run_v12_hourly import signal_on_last_closed_bar
    feats = v12_features(df)
    disagreements = 0
    checked = 0
    # positions to probe: last bar of each of n equal chunks
    step = max(1, len(df) // n)
    for end in range(60, len(df), step):
        window = df.iloc[:end].reset_index(drop=True)
        live = signal_on_last_closed_bar(window)
        ours = _v12_gate(feats.iloc[end - 1])
        checked += 1
        if (live is not None) != ours:
            disagreements += 1
    return {"checked": checked, "disagreements": disagreements,
            "parity": disagreements == 0}


# ------------------------------------------------------------- shared
def normalise_signal(sig: dict, spec: StrategySpec, symbol: str) -> dict:
    """Uniform shape for the engine/visual layers, whatever the source dict
    looked like. Values are copied, never recomputed."""
    pip = UNITS[symbol]["pip"]
    entry = float(sig["entry_price"])
    stop = float(sig["stop"])
    target = float(sig["target"])
    sig_bar = int(sig.get("signal_bar", sig.get("d1_bar", sig["entry_bar"] - 1)))
    return {
        "strategy": spec.key,
        "strategy_name": spec.name,
        "symbol": symbol,
        "timeframe": spec.timeframe,
        "signal": "BUY",                 # both V11 and V12 are LONG-only by design
        "signal_bar": sig_bar,
        "entry_bar": int(sig["entry_bar"]),
        "signal_ts": sig.get("signal_ts", sig.get("entry_ts")),
        "entry_ts": sig["entry_ts"],
        "entry": entry,
        "sl": stop,
        "tp": target,
        "risk_price": entry - stop,
        "risk_pips": (entry - stop) / pip,
        "atr": sig.get("atr", sig.get("d1_atr")),
        "body_pct": sig.get("body_pct", sig.get("d1_body_pct")),
    }


def generate(feats: pd.DataFrame, spec: StrategySpec, symbol: str) -> list[dict]:
    """Signals for one strategy, uniform shape.

    `feats` must ALREADY have features built on the FULL history (see the
    monthly pattern: build on full history, then slice). V11's research
    function takes the frame directly; V12 scans the pre-built columns so
    a sliced window never re-cold-starts an EMA.
    """
    raw = (v11_signals(feats) if spec.key == "V11"
           else v12_signals_from_features(feats))
    return [normalise_signal(s, spec, symbol) for s in raw]
