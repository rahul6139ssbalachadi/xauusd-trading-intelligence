"""V15 PROBE: session-break liquidity sweep — descriptive only.

USER HYPOTHESIS (V15): after the daily session break, take the first H1
candle's high/low as a range. On M5, wait for price to SWEEP one side
(liquidity grab), then enter back inside the range on a BODY close, with
9 EMA confirmation, stop beyond the sweep, target 1:1.5.

DESCRIPTIVE ONLY. This file does not backtest a strategy. It answers the
questions that decide whether a backtest is even worth writing:

  Q0  How many session-break ranges does the M5 data actually contain?
      HARD CONSTRAINT: M5 has 44,578 bars spanning 2025-10-23 -> 2026-10-01
      (~11 months). That caps sample size no matter how good the idea is.
  Q1  Which session-break hour? TradingView's "Session Breaks" is a
      user setting; this repo stores BROKER time. Sweep several candidate
      hours and report rather than assuming one.
  Q2  How often does price sweep the range on M5 at all?
  Q3  After a sweep, does price return INSIDE the range (the entry premise)?
  Q4  How much does it move after returning — is it big enough to clear cost?
      M5 round trip ~= 8 pips (2x 2 pips spread + 2x 2 pips slippage).
      Sweep targets are typically 20-60 pips. This is the make-or-break row.

Read-only vs db/trading.db. No MT5 writes. No live trading.

Usage:
    ./.venv/Scripts/python.exe research/v15_sweep_probe.py
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from market_data import config as cfg

DB = cfg.PROJECT_ROOT / cfg.load_settings()["paths"]["db"]

PIP = 0.10
POINT = 0.01
COST_PIPS = 8.0        # 2x spread (2.0) + 2x slippage (2.0), round trip
BROKER_OFFSET = 3      # UTC+3, per this repo's convention (see CLAUDE.md)

# Candidate session breaks, in BROKER time. Gold's London open is the most
# commonly traded break; 00:00 broker is XM's daily rollover.
CANDIDATE_BREAKS = (0, 6, 7, 8, 12, 13)


def load(symbol: str, tf: str) -> pd.DataFrame:
    con = sqlite3.connect(DB)
    df = pd.read_sql_query(
        "SELECT ts_broker_epoch, open, high, low, close, spread "
        "FROM market_data WHERE symbol=? AND timeframe=? AND source='mt5' "
        "ORDER BY ts_broker_epoch",
        con, params=(symbol, tf))
    con.close()
    if df.empty:
        return df
    df["ts_utc"] = pd.to_datetime(df["ts_broker_epoch"], unit="s", utc=True)
    # broker-local wall clock, as a naive timestamp (the epoch is broker time)
    df["bt"] = pd.to_datetime(
        df["ts_broker_epoch"], unit="s") + pd.Timedelta(hours=BROKER_OFFSET)
    df["spread_pips"] = df["spread"] / 10.0
    # Stable integer positional index: NaN sentinels elsewhere promote the
    # index to float, which breaks .iloc[i:i+n] slicing.
    df = df.reset_index(drop=True)
    return df


def first_candle_of_session(broker_day: pd.Series, hour: int) -> pd.Series:
    """True on the FIRST bar of each broker day at/after `hour`."""
    cand = broker_day.dt.hour == hour
    # first such bar per day
    first = cand & ~cand.shift(1).fillna(False)
    return first


def build_ranges(h1: pd.DataFrame, break_hour: int) -> pd.DataFrame:
    """First H1 candle at/after break_hour on each day -> the range."""
    h = h1.copy()
    is_first = first_candle_of_session(h["bt"], break_hour)
    r = h[is_first].copy()
    r = r.rename(columns={"high": "rng_high", "low": "rng_low",
                          "open": "rng_open"})
    return r[["bt", "ts_broker_epoch", "rng_high", "rng_low", "rng_open"]]


def find_sweeps(m5: pd.DataFrame, rng: pd.DataFrame,
                horizon_bars: int) -> pd.DataFrame:
    """On M5, sweep each day's range within `horizon_bars` after the range.

    A sweep = M5 low breaks rng_low (BUY setup) or M5 high breaks rng_high
    (SELL setup). One sweep per side per day, first occurrence only.
    """
    out = []
    for _, row in rng.iterrows():
        start = row["ts_broker_epoch"]
        end = start + horizon_bars * 300
        win = m5[(m5["ts_broker_epoch"] >= start) &
                 (m5["ts_broker_epoch"] <= end)]
        if win.empty:
            continue
        lo_breaks = win[win["low"] < row["rng_low"]]
        hi_breaks = win[win["high"] > row["rng_high"]]
        out.append({
            "day_epoch": start,
            "rng_high": row["rng_high"], "rng_low": row["rng_low"],
            "n_bars": len(win),
            "buy_sweep_i": int(lo_breaks.index[0]) if len(lo_breaks) else -1,
            "buy_sweep_low": float(lo_breaks["low"].iloc[0]) if len(lo_breaks) else np.nan,
            "sell_sweep_i": int(hi_breaks.index[0]) if len(hi_breaks) else -1,
            "sell_sweep_high": float(hi_breaks["high"].iloc[0]) if len(hi_breaks) else np.nan,
        })
    sw = pd.DataFrame(out)
    # NaN sentinels promote these columns to float; iloc needs true ints.
    for c in ("buy_sweep_i", "sell_sweep_i"):
        sw[c] = sw[c].astype(int)
    return sw


def study(symbol: str = "XAUUSD") -> None:
    h1 = load(symbol, "H1")
    m5 = load(symbol, "M5")
    print("=" * 78)
    print(f"V15 PROBE — session-break liquidity sweep  [{symbol}]")
    print("=" * 78)
    print(f"  H1: {len(h1):6d} bars  {h1['bt'].iloc[0].date()} -> {h1['bt'].iloc[-1].date()}")
    print(f"  M5: {len(m5):6d} bars  {m5['bt'].iloc[0].date()} -> {m5['bt'].iloc[-1].date()}")
    print("  M5 is the BINDING constraint -- see Q0 for the real depth")
    print(f"  M5 round-trip cost ~= {COST_PIPS:.0f} pips "
          f"(median spread {m5['spread_pips'].median():.1f} pips)")

    # Q0: how many days does M5 actually cover?
    days = m5["bt"].dt.date.nunique()
    print(f"\n[Q0] M5 covers {days} distinct broker days")

    results = {}
    for bh in CANDIDATE_BREAKS:
        rng = build_ranges(h1, bh)
        rng = rng[rng["ts_broker_epoch"].isin(
            m5["ts_broker_epoch"])]  # only days M5 can see
        if rng.empty:
            continue
        # the range candle must be resolvable on M5 too
        sw = find_sweeps(m5, rng, horizon_bars=18)   # 18*5m = 90m after
        if sw.empty:
            continue
        n_b = int((sw["buy_sweep_i"] >= 0).sum())
        n_s = int((sw["sell_sweep_i"] >= 0).sum())
        results[bh] = (len(sw), n_b, n_s, sw)
        print(f"\n[Q1] break at broker hour {bh:02d}:00 -> {len(sw)} ranges, "
              f"{n_b} low-sweeps (BUY), {n_s} high-sweeps (SELL)")

    if not results:
        print("\n  NO candidate break hour yields ranges inside the M5 window.")
        return

    # Use the busiest break hour for the detail study
    bh = max(results, key=lambda k: results[k][1] + results[k][2])
    n_rng, n_b, n_s, sw = results[bh]
    print(f"\n  busiest break hour = {bh:02d}:00 (used below)")
    print(f"\n[Q2] sweep frequency: {n_b + n_s} sweeps over {n_rng} ranges "
          f"= {(n_b + n_s) / max(n_rng, 1) * 100:.0f}% of days produce a sweep")

    # Q3/Q4: after a sweep, does price come back inside, and how far?
    print("\n[Q3/Q4] forward move after the sweep bar "
          "(body-close-back-inside required for a real entry)")
    print(f"  {'side':6s} {'n':>5s} {'back-in%':>9s} {'med_fwd':>14s} {'>cost%':>8s}")
    for side, i_col, rng_col, dirn in (("BUY", "buy_sweep_i", "rng_low", +1),
                                       ("SELL", "sell_sweep_i", "rng_high", -1)):
        rows = []
        # NOTE: use itertuples/columns, not iterrows -- iterrows unifies row
        # dtypes, so the int index columns come back as float and iloc
        # raises "cannot do positional indexing ... of type float64".
        for _, r in sw.iterrows():
            i = int(r[i_col])          # explicit int() coerces it back
            if i < 0:
                continue
            after = m5.iloc[i:i + 12]
            if after.empty:
                continue
            rng_level = r[rng_col]
            if dirn > 0:
                body_in = after["close"] > rng_level
                fav = (after["high"].max() - r["buy_sweep_low"]) / PIP
            else:
                body_in = after["close"] < rng_level
                fav = (r["sell_sweep_high"] - after["low"].min()) / PIP
            rows.append({"back_in": bool(body_in.iloc[0]), "fav": fav})
        if not rows:
            continue
        df = pd.DataFrame(rows)
        med = df["fav"].median()
        print(f"  {side:6s} {len(df):5d} {df['back_in'].mean() * 100:8.1f}% "
              f"med_fwd={med:8.1f} "
              f"gt_cost={(df['fav'] > COST_PIPS).mean() * 100:5.1f}%")

    print("\n" + "=" * 78)
    print("READ THIS BEFORE TRUSTING ANY V15 BACKTEST")
    print("=" * 78)
    print(f"  - M5 history is only ~{days} days ({m5['bt'].iloc[0].date()} "
          f"-> {m5['bt'].iloc[-1].date()}).")
    print("  - Any V15 result is a LOW-CONFIDENCE estimate, not a validation.")
    print("  - A sweep setup needs ~15-30 qualifying days to say anything.")
    print("  - If the counts above are small, the correct verdict is")
    print("    INCONCLUSIVE, not PASS or FAIL.")
    print("\nDescriptive only. Read-only vs db/trading.db.")


def main() -> None:
    study("XAUUSD")


if __name__ == "__main__":
    main()