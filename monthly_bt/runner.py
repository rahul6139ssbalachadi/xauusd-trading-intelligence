"""Period slicing + month-by-month orchestration.

THE MONTHLY PATTERN (reused from research/v11_m1_h1_monthly_2026.py, whose
lesson is recorded in the project skill): features are built on the FULL
history and only THEN sliced by month. Slicing first produces zero signals
because the 60-bar momentum rank and the EMA55/ATR14 warm-ups are cold
inside a single month.

A "month" here is broker-server time, matching how the strategy sees the
market. Signals are attributed to the month of their ENTRY bar (the moment
a trade could actually be taken), and the trade is allowed to run past the
month end — its exit may land in the next month. That is deliberate: the
holding period is part of the strategy, and truncating it would fabricate
a different strategy.
"""
from __future__ import annotations

import calendar
import subprocess
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from monthly_bt.data import BROKER_UTC_OFFSET_H, load, provenance
from monthly_bt.engine import SimConfig, simulate
from monthly_bt.strategies import STRATEGIES, StrategySpec, generate, \
    v11_features, v12_features


def build_features(df: pd.DataFrame, spec: StrategySpec) -> pd.DataFrame:
    """Full-history feature build using the existing per-strategy builder."""
    return v11_features(df) if spec.key == "V11" else v12_features(df)


def month_range(start: str, end: str) -> list[str]:
    """['2026-01','2026-02',...] inclusive of both ends."""
    y0, m0 = int(start[:4]), int(start[5:7])
    y1, m1 = int(end[:4]), int(end[5:7])
    out, y, m = [], y0, m0
    while (y, m) <= (y1, m1):
        out.append(f"{y:04d}-{m:02d}")
        m += 1
        if m == 13:
            y, m = y + 1, 1
    return out


def month_bounds(month: str) -> tuple[pd.Timestamp, pd.Timestamp]:
    y, m = int(month[:4]), int(month[5:7])
    start = pd.Timestamp(f"{y:04d}-{m:02d}-01")
    end = start + pd.offsets.MonthBegin(1)
    return start, end


def _aware(ts: pd.Timestamp) -> pd.Timestamp:
    """Market data timestamps are tz-aware (UTC). Period bounds are written
    as plain dates, so they must be localised before comparison — otherwise
    every slice raises tz-naive vs tz-aware."""
    return ts.tz_localize("UTC") if ts.tzinfo is None else ts


def slice_period(feats: pd.DataFrame, start: str, end: str,
                 warmup: int = 200) -> pd.DataFrame:
    """Slice a feature frame to [start, end) on BROKER time, keeping
    `warmup` leading bars PRECEDING the window.

    Those leading bars are real bars from the full-history feature build, so
    an indicator evaluated inside the window still sees its warm-up. They
    are returned too (not masked away) because generate() scans the whole
    window; run_period() then attributes a signal to a period by the broker
    time of its ENTRY bar, so a warm-up bar can never leak a signal into the
    period it precedes.
    """
    s, e = _aware(pd.Timestamp(start)), _aware(pd.Timestamp(end))
    inside = feats.index[(feats["ts_broker"] >= s) & (feats["ts_broker"] < e)]
    if not len(inside):
        # No bar actually falls inside the period. Returning the whole frame
        # here would silently backtest a decade for a 1990 request, so an
        # empty window is the honest answer.
        return feats.iloc[0:0], 0
    lo = max(0, int(inside[0]) - warmup)
    hi = int(inside[-1]) + 1
    # the offset of the window's first bar within the FULL frame. Callers
    # must add it back to every bar index so a signal's bar number is
    # meaningful in the full-history frame (the visual layer and the trade
    # log both index the full frame).
    return feats.iloc[lo:hi].reset_index(drop=True), lo


def run_period(feats_full: pd.DataFrame, spec: StrategySpec, symbol: str,
               start: str, end: str, cfg: SimConfig) -> dict:
    """One independent backtest period (a month, a year, or a custom range)."""
    window, offset = slice_period(feats_full, start, end)
    if window.empty:
        return {"period": period_label(start, end), "error": "no bars in range",
                "signals": 0, "trades": 0}
    signals = generate(window, spec, symbol)
    # attribute by entry bar inside the window
    in_window = window["ts_broker"]
    lo = _aware(pd.Timestamp(start))
    hi = _aware(pd.Timestamp(end))
    # Re-base everything onto the FULL frame (the log, the chart and the CSV
    # all index the full history), but hand the simulator WINDOW-relative
    # indices because it slices the window it was given.
    #
    # The copy is essential: a `keep` built from the original list would
    # alias these dicts, and the offset would then be applied twice.
    signals = [dict(s) for s in signals]
    for s in signals:
        s["signal_bar"] += offset
        s["entry_bar"] += offset
    keep = [s for s in signals
            if lo <= in_window.iloc[s["entry_bar"] - offset] < hi]
    sim_sigs = [{**s, "signal_bar": s["signal_bar"] - offset,
                 "entry_bar": s["entry_bar"] - offset} for s in keep]
    res = simulate(window, sim_sigs, spec, symbol, cfg,
                   period=period_label(start, end))
    for r in res["trade_rows"]:
        for k in ("signal_bar", "entry_bar", "exit_bar"):
            r[k] += offset
    for s in res.get("skipped", []):
        s["entry_bar"] += offset
    res["period"] = period_label(start, end)
    res["start"], res["end"] = start, end
    res["signals"] = keep             # ATTRIBUTED to this period by entry time
    res["signals_in_window"] = signals  # every signal the window could see
    res["window_bars"] = len(window)
    res["window_offset"] = offset
    return res


def period_label(start: str, end: str) -> str:
    """Label a period. A whole calendar month is labelled 'YYYY-MM'
    (start is the 1st and end is the 1st of the next month); anything else
    keeps an explicit range so a custom window is never mislabelled."""
    s, e = pd.Timestamp(start), pd.Timestamp(end)
    if s.day == 1 and s + pd.offsets.MonthBegin(1) == e:
        return s.strftime("%Y-%m")
    if s.year == e.year and s.month == e.month:
        return s.strftime("%Y-%m")
    return f"{start}..{end}"


def month_ends_for(feats: pd.DataFrame) -> list[str]:
    """Every YYYY-MM that has at least one bar, oldest first."""
    return sorted({f"{d.year:04d}-{d.month:02d}" for d in feats["ts_broker"]})


def run_monthly(feats_full: pd.DataFrame, spec: StrategySpec, symbol: str,
                months: list[str] | None, cfg: SimConfig,
                year: str | None = None) -> list[dict]:
    """Each month as an INDEPENDENT backtest period."""
    if months is None:
        if year:
            months = [m for m in month_ends_for(feats_full) if m[:4] == year]
        else:
            months = month_ends_for(feats_full)
    out = []
    for m in months:
        s, e = month_bounds(m)
        out.append(run_period(feats_full, spec, symbol,
                              s.strftime("%Y-%m-%d"), e.strftime("%Y-%m-%d"), cfg))
    return out


def git_commit() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], cwd=str(ROOT_DIR),
            stderr=subprocess.DEVNULL, text=True).strip()
    except Exception:
        return "unknown"


ROOT_DIR = __import__("pathlib").Path(__file__).resolve().parents[1]


def run_manifest(symbol: str, timeframe: str, spec: StrategySpec,
                 cfg: SimConfig, guard_status: dict, period: str) -> dict:
    """Reproducibility record (spec 14) stored with every run."""
    return {
        "strategy": spec.key,
        "strategy_name": spec.name,
        "strategy_version": spec.version,
        "logic_source": spec.source,
        "params": dict(spec.params),
        "symbol": symbol,
        "timeframe": timeframe,
        "data": provenance(symbol, timeframe),
        "initial_balance": cfg.initial_balance,
        "sim_config": cfg.as_dict(),
        "risk_pct_used": spec.params["risk_pct"],
        "max_positions": cfg.max_positions,
        "git_commit": git_commit(),
        "run_ts_utc": datetime.now(timezone.utc).isoformat(),
        "broker_utc_offset_h": BROKER_UTC_OFFSET_H,
        "period": period,
        "guard": guard_status,
    }
