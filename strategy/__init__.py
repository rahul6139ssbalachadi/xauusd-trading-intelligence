"""Strategy engine (Phase 7).

Structured, version-controlled strategy representation plus a deterministic
evaluator that turns a strategy definition + computed features into
BUY / SELL / WAIT decisions with explicit reasons.

Design (consistent with the rest of the repo):
  - pure functions, no trading side effects
  - features are passed in (computed by build_features from P5 indicators
    + P6 market structure); the evaluator only interprets them
  - every decision carries a list of human-readable reasons so the system
    can explain WHY, per CLAUDE.md sections 3 and 10
  - no P&L is computed here (Phase 8 backtest does that)
  - a strategy is a dataclass that serializes to JSON for version control;
    strategies are NEVER overwritten, only superseded by a new version

The candidate rule (XAUUSD structure-break V1) is defined in defs/ as JSON
and loaded at runtime. This module holds the generic engine; specific
strategies are data, not code.

CLAUDE.md reminder (section 8): structure features are FEATURES, not
predictors. This engine is a hypothesis generator/testbed, not a claim of
edge. Statistical validation happens in the Phase 8 backtest.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Literal

import pandas as pd

from indicators import adx, atr, ema, rsi
from market_structure import (
    classify_swings,
    session_of,
    structure_events,
    swing_highs,
    swing_lows,
    volatility_regime,
)

Signal = Literal["BUY", "SELL", "WAIT"]


# --------------------------------------------------------------------------
# Structured strategy representation
# --------------------------------------------------------------------------
@dataclass
class Strategy:
    """A version-controlled strategy definition.

    Rules are encoded as plain data so they can be diffed, stored, and
    never overwritten (each saved version is a new file).
    """

    name: str
    version: str
    market: str
    bias_timeframe: str          # e.g. "M15" — directional bias
    trigger_timeframe: str       # e.g. "M5"  — entry trigger
    entry_rules: dict            # structured rule tree
    stop: dict                   # stop-loss definition (e.g. ATR multiple)
    target: dict                 # take-profit definition (risk/reward)
    risk_pct: float              # risk per trade, fraction of equity
    max_positions: int
    sessions: list[str]          # allowed sessions (broker time)
    notes: str = ""

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2)

    def save(self, directory: Path) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{self.name}_{self.version}.json"
        path.write_text(self.to_json())
        return path

    @classmethod
    def load(cls, path: Path) -> "Strategy":
        data = json.loads(Path(path).read_text())
        # Backwards-compat: provide defaults for missing required fields
        defaults = {
            "sessions": ["all"],
            "notes": "",
            "max_positions": 1,
        }
        for k, v in defaults.items():
            if k not in data:
                data[k] = v
        # Filter to only known fields
        valid = {f.name for f in fields(cls)}
        filtered = {k: v for k, v in data.items() if k in valid}
        return cls(**filtered)


# --------------------------------------------------------------------------
# Feature builder (P5 indicators + P6 structure, per timeframe)
# --------------------------------------------------------------------------
def build_features(
    df: pd.DataFrame,
    bias_tf: str = "M15",
    trigger_tf: str = "M5",
    swing_left: int = 5,
    swing_right: int = 5,
    broker_offset_h: int = 3,
) -> pd.DataFrame:
    """Attach indicators + structure columns to an OHLCV frame.

    `df` must have columns open/high/low/close and a tz-aware `ts`
    (or `timestamp`) column. Returns a copy with added feature columns:
      ema_fast, ema_slow, rsi, atr, adx, swing_h, swing_l, swing_label,
      session, vol_regime, last_bos_dir, last_choch_dir
    """
    out = df.copy()
    close = out["close"]
    high = out["high"]
    low = out["low"]

    out["ema_fast"] = ema(close, 20)
    out["ema_slow"] = ema(close, 50)
    out["rsi"] = rsi(close, 14)
    out["atr"] = atr(high, low, close, 14)
    ad, _, _ = adx(high, low, close, 14)
    out["adx"] = ad

    out["swing_h"] = swing_highs(high, swing_left, swing_right)
    out["swing_l"] = swing_lows(low, swing_left, swing_right)
    out["swing_label"] = classify_swings(high, low, swing_left, swing_right)

    ts = out["ts"] if "ts" in out.columns else out["timestamp"]
    out["session"] = session_of(ts, broker_offset_h)
    out["vol_regime"] = volatility_regime(high, low, close)

    # most recent structure event direction up to each bar
    ev = structure_events(high, low, swing_left, swing_right)
    out["last_bos_dir"] = pd.NA
    out["last_choch_dir"] = pd.NA
    for _, row in ev.iterrows():
        i = int(row["bar"])
        if i >= len(out):
            continue
        if row["kind"] == "BOS":
            out.loc[out.index[i]:, "last_bos_dir"] = row["direction"]
        else:
            out.loc[out.index[i]:, "last_choch_dir"] = row["direction"]

    return out


# --------------------------------------------------------------------------
# Rule evaluator -> BUY / SELL / WAIT with reasons
# --------------------------------------------------------------------------
def _bias_series(df: pd.DataFrame) -> pd.Series:
    """Per-bar directional bias from EMA fast vs slow.

    Returns a Series ('up'/'down'/'flat') aligned to df's index. Bias at bar
    i uses only data available through bar i (no look-ahead). Warm-up bars
    before both EMAs are defined are 'flat'.
    """
    ef = df["ema_fast"]
    es = df["ema_slow"]
    out = pd.Series(index=df.index, dtype="object")
    up = (ef > es)
    down = (ef < es)
    out[up] = "up"
    out[down] = "down"
    out[(~up & ~down) | ef.isna() | es.isna()] = "flat"
    return out


def evaluate(
    strat: Strategy,
    bias_df: pd.DataFrame,
    trigger_df: pd.DataFrame,
) -> pd.DataFrame:
    """Produce a decision per trigger-bar.

    Returns a DataFrame aligned to `trigger_df` index with columns:
      signal (BUY/SELL/WAIT), reasons (list[str]), bias (up/down/flat),
      stop (float or NaN), target (float or NaN)
    """
    r = strat.entry_rules
    bias_trend = r.get("bias_trend", "up")           # "up"/"down"
    bias_min_adx = float(r.get("bias_min_adx", 20))
    trigger_struct = r.get("trigger_structure", "BOS")  # "BOS"/"CHoCH"
    trigger_struct_dir = r.get("trigger_structure_dir", "up")  # "up"/"down"
    vol_allow = set(r.get("vol_regimes_allowed", ["normal", "high"]))
    rsi_max = float(r.get("rsi_max", 70))
    rsi_min = float(r.get("rsi_min", 30))
    max_spread = float(r.get("max_spread_pips", 3.0))
    require_structure = bool(r.get("require_structure", True))

    # PER-BAR bias (rolling, no look-ahead) — not just the last bar.
    # Align bias to the trigger frame BY TIMESTAMP (as-of merge): each M5
    # trigger bar uses the latest M15 bias bar at or before its time. This is
    # correct multi-timeframe alignment and avoids the positional-alignment
    # bug (different-length frames must not be matched bar-for-bar).
    bias_series = _bias_series(bias_df).to_frame("bias").reset_index(drop=True)
    bias_series["ts"] = bias_df["ts"].reset_index(drop=True)
    trig_idx = trigger_df.reset_index(drop=True).copy()
    # merge_asof requires sorted keys
    _b = bias_series.sort_values("ts")
    _t = trig_idx.sort_values("ts")
    merged = pd.merge_asof(
        _t, _b[["ts", "bias"]], on="ts", direction="backward",
    )
    merged_bias = merged["bias"].tolist()

    rows = []
    for i in range(len(trig_idx)):
        row = trig_idx.iloc[i]
        reasons: list[str] = []

        # 1. session filter
        if strat.sessions and row.get("session") not in strat.sessions:
            rows.append(_mk("WAIT", ["session not allowed"], "n/a", row))
            continue

        # 2. bias filter (per-bar, timestamp-aligned)
        # "both" mode accepts either direction (signal follows per-bar bias)
        bias = merged_bias[i]
        if bias_trend != "both" and bias != bias_trend:
            rows.append(_mk("WAIT", [f"bias {bias} != required {bias_trend}"], bias, row))
            continue
        reasons.append(f"bias {bias} confirmed")

        # 3. ADX trend strength on bias tf
        if pd.notna(row.get("adx")) and row["adx"] < bias_min_adx:
            rows.append(_mk("WAIT", [f"adx {row['adx']:.1f} < {bias_min_adx}"], bias, row))
            continue
        reasons.append(f"adx {row['adx']:.1f} >= {bias_min_adx}")

        # 4. volatility regime filter
        if row.get("vol_regime") not in vol_allow:
            rows.append(_mk("WAIT", [f"vol {row.get('vol_regime')} excluded"], bias, row))
            continue

        # 5. RSI extremes guard
        if pd.notna(row.get("rsi")):
            if bias_trend == "up" and row["rsi"] > rsi_max:
                rows.append(_mk("WAIT", [f"rsi {row['rsi']:.1f} overbought"], bias, row))
                continue
            if bias_trend == "down" and row["rsi"] < rsi_min:
                rows.append(_mk("WAIT", [f"rsi {row['rsi']:.1f} oversold"], bias, row))
                continue

        # 6. spread guard
        spread_pips = float(row.get("spread_pips", 0.0) or 0.0)
        if spread_pips > max_spread:
            rows.append(_mk("WAIT", [f"spread {spread_pips:.1f} > {max_spread}"], bias, row))
            continue

        # 7. structure trigger (skipped if the strategy opts out)
        if require_structure:
            # in "both" mode the required structure direction follows the
            # per-bar bias (up bias -> need bullish break, down -> bearish)
            struct_dir = trigger_struct_dir if bias_trend != "both" else bias
            triggered = _structure_trigger(
                trig_idx, i, trigger_struct, struct_dir
            )
            if not triggered:
                rows.append(_mk("WAIT", ["no structure trigger"], bias, row))
                continue
            reasons.append(f"{trigger_struct} {struct_dir} confirmed")

        # 8. emit signal + ATR-based stop/target
        if bias_trend == "both":
            signal = "BUY" if bias == "up" else "SELL"
        else:
            signal = "BUY" if bias_trend == "up" else "SELL"
        stop, target = _stops_targets(strat, row, signal)
        reasons.append(f"ATR stop {strat.stop.get('atr_multiple')}x, "
                       f"R:R {strat.target.get('risk_reward')}")
        rows.append(_mk(signal, reasons, bias, row, stop, target))

    return pd.DataFrame(rows, index=trigger_df.index)


def _mk(signal, reasons, bias, row, stop=float("nan"), target=float("nan")):
    return {
        "signal": signal,
        "reasons": reasons,
        "bias": bias,
        "stop": stop,
        "target": target,
        "atr": row.get("atr", float("nan")),
        "close": row.get("close", float("nan")),
    }


def _structure_trigger(df, i, kind, direction) -> bool:
    """True if, at/before bar i, the last structure event of `kind` has
    `direction` matching, and it is recent (within `max_bars` lookback)."""
    max_bars = 5
    lo = max(0, i - max_bars)
    for j in range(i, lo - 1, -1):
        rr = df.iloc[j]
        bos = rr.get("last_bos_dir")
        ch = rr.get("last_choch_dir")
        if kind == "BOS" and pd.notna(bos) and bos == direction:
            return True
        if kind == "CHoCH" and pd.notna(ch) and ch == direction:
            return True
    return False


def _stops_targets(strat: Strategy, row, signal: str):
    atr = float(row.get("atr", float("nan")))
    price = float(row.get("close", float("nan")))
    if pd.isna(atr) or pd.isna(price):
        return float("nan"), float("nan")
    mult = float(strat.stop.get("atr_multiple", 1.5))
    rr = float(strat.target.get("risk_reward", 2.0))
    if signal == "BUY":
        stop = price - mult * atr
        target = price + mult * atr * rr
    else:
        stop = price + mult * atr
        target = price - mult * atr * rr
    return stop, target
