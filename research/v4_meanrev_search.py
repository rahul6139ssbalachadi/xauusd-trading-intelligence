"""V4 research: mean-reversion bounce hypothesis on XAUUSD.

STANDALONE research script — does NOT modify the tested Phase 7-12 engine.
Reuses `build_features` (P5+P6) and `compute_metrics` (P8) but implements a
NEW entry rule the generic evaluate() does not support:

V3 = mean-reversion (structurally different from V1/V2 trend-following):
  - M15 context: ADX >= floor, vol regime in {normal, low}
  - M5 trigger: RSI <= oversold (BUY) or RSI >= overbought (SELL)
  - AND a CHoCH reversal event at/before this bar (break-then-reverse retest)
  - AND price within ~2 points of recent swing support/resistance

DISCIPLINE (CLAUDE.md §9-13):
  - Parameter search runs ONLY on the TRAIN window.
  - Best config measured (never re-tuned) on val + walk-forward OOS.

Vectorized for speed: bias alignment done once, then numpy masks per combo.
  ./.venv/Scripts/python.exe research/v4_meanrev_search.py
"""
from __future__ import annotations

import itertools
import sqlite3
import sys
from dataclasses import replace
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from market_data import config as cfg
from strategy import build_features
from backtest import compute_metrics, _bar_exit, Trade
from validation import train_val_test_split, walk_forward_windows


# ---------------------------------------------------------------------------
# Data loading (same shape the test suite uses)
# ---------------------------------------------------------------------------
def load_xauusd() -> tuple[pd.DataFrame, pd.DataFrame]:
    db = cfg.PROJECT_ROOT / cfg.load_settings()["paths"]["db"]
    con = sqlite3.connect(db)
    m5 = pd.read_sql_query(
        "SELECT ts_broker_epoch, open, high, low, close, spread "
        "FROM market_data WHERE symbol='XAUUSD' AND timeframe='M5' "
        "AND source='mt5' ORDER BY ts_broker_epoch", con)
    m15 = pd.read_sql_query(
        "SELECT ts_broker_epoch, open, high, low, close, spread "
        "FROM market_data WHERE symbol='XAUUSD' AND timeframe='M15' "
        "AND source='mt5' ORDER BY ts_broker_epoch", con)
    con.close()
    for d in (m5, m15):
        d["ts"] = pd.to_datetime(d["ts_broker_epoch"], unit="s", utc=True)
        d["spread_pips"] = d["spread"] * 0.01 / 0.10
    return m15, m5


# ---------------------------------------------------------------------------
# Precompute bias-aligned context once (as-of merge, no look-ahead)
# ---------------------------------------------------------------------------
def precompute_context(trig_df: pd.DataFrame, bias_df: pd.DataFrame,
                       bias_warmup: int = 200) -> pd.DataFrame:
    """Return trig_df + per-bar aligned M15 context columns.

    Uses merge_asof (sorted by ts, direction='backward') so each M5 bar
    sees only M15 data at or before it. A leading warmup buffer on the
    bias frame prevents indicator cold-start inside the window.
    """
    bcols = ["ts", "adx", "vol_regime"]
    b = bias_df[bcols].sort_values("ts").reset_index(drop=True)
    t = trig_df[["ts"]].sort_values("ts").reset_index(drop=True)
    merged = pd.merge_asof(t, b, on="ts", direction="backward")
    merged = merged.set_index(trig_df.index[:len(merged)])
    trig_df = trig_df.copy()
    trig_df["ctx_adx"] = merged["adx"]
    trig_df["ctx_vol"] = merged["vol_regime"]
    return trig_df


# ---------------------------------------------------------------------------
# Vectorized signal computation (numpy)
# ---------------------------------------------------------------------------
def compute_signal_mask(trig_df: pd.DataFrame, *,
                       rsi_oversold: int, rsi_overbought: int,
                       bias_min_adx: float,
                       vol_allowed: set[str],
                       sessions: list[str],
                       window: int = 15) -> pd.DataFrame:
    """Return trig_df with added 'signal' column (+ stop/target for active).

    Fully vectorized: boolean masks, no Python per-bar loop.
    """
    df = trig_df.copy()
    n = len(df)

    # session mask
    sess_ok = df["session"].isin(sessions).to_numpy()

    # bias context masks
    ctx_adx = df["ctx_adx"].to_numpy()
    adx_ok = np.where(np.isnan(ctx_adx), False, ctx_adx >= bias_min_adx)
    ctx_vol = df["ctx_vol"].to_numpy()
    vol_ok = np.isin(list(ctx_vol), list(vol_allowed))

    rsi = df["rsi"].to_numpy()
    choch = df["last_choch_dir"].astype(object).where(df["last_choch_dir"].notna(), "").to_numpy()
    atr = df["atr"].to_numpy()
    close = df["close"].to_numpy()

    # rolling min/max distance in ATR units (volatility-normalized "near support")
    roll_lo = df["low"].rolling(window, min_periods=window).min().to_numpy()
    roll_hi = df["high"].rolling(window, min_periods=window).max().to_numpy()
    atr_arr = np.where(np.isnan(atr), 1.0, atr)  # avoid div-by-zero
    near_low = np.where(
        np.isnan(roll_lo), False, (close - roll_lo) <= 1.0 * atr_arr)
    near_high = np.where(
        np.isnan(roll_hi), False, (roll_hi - close) <= 1.0 * atr_arr)

    valid = np.isfinite(rsi) & np.isfinite(atr) & np.isfinite(close)

    buy = (sess_ok & adx_ok & vol_ok & valid &
           (rsi <= rsi_oversold) & (choch == "up") & near_low)
    sell = (sess_ok & adx_ok & vol_ok & valid &
            (rsi >= rsi_overbought) & (choch == "down") & near_high)

    sig = np.where(buy, "BUY",
            np.where(sell, "SELL", "WAIT"))
    df["signal"] = sig
    df["stop"] = np.nan
    df["target"] = np.nan
    return df


# ---------------------------------------------------------------------------
# Vectorized backtest: given signal array, simulate trades with next-bar open
# ---------------------------------------------------------------------------
def backtest_vectorized(feats: pd.DataFrame, atr_mult: float,
                        rr: float, slippage_pips: float = 0.5) -> dict:
    """Fast backtest over precomputed signal frame.

    Uses the same intrabar SL/TP logic as the tested backtest engine.
    Entry at next-bar open; _bar_exit for intrabar resolution.
    """
    n = len(feats)
    POINT = 0.01
    PIP = 0.10
    cost_pts_const = 2 * slippage_pips * (PIP / POINT)  # per-trade slippage

    sig = feats["signal"].to_numpy()
    o = feats["open"].to_numpy()
    h = feats["high"].to_numpy()
    l = feats["low"].to_numpy()
    c = feats["close"].to_numpy()
    spr = feats["spread"].to_numpy()  # points (0.01 = 1 pip)
    # recompute stop/target around actual entry (next-bar open)
    # entry at i+1; stop/target from bar i's signal recalc at entry price
    entry = np.roll(o, -1)  # entry[i] = open[i+1]
    entry[-1] = np.nan
    atr_arr = feats["atr"].to_numpy()

    trades: list[Trade] = []
    equity = [0.0]
    in_pos = False
    pos_entry_idx = -1
    pos_side = ""
    pos_stop = 0.0
    pos_target = 0.0

    i = 0
    while i < n - 1:
        if not in_pos:
            s = sig[i]
            if s in ("BUY", "SELL") and not np.isnan(entry[i]):
                e = entry[i]
                atr = atr_arr[i]
                if not np.isnan(atr):
                    if s == "BUY":
                        pos_stop = e - atr_mult * atr
                        pos_target = e + atr_mult * atr * rr
                    else:
                        pos_stop = e + atr_mult * atr
                        pos_target = e - atr_mult * atr * rr
                    pos_entry_idx = i + 1
                    pos_side = s
                    in_pos = True
            i += 1
            continue
        # we are in a position; check exit at bars i+1, i+2, ...
        # the position opened at entry bar (i+1 from signal bar i)
        # check each subsequent bar for SL/TP
        j = pos_entry_idx + 1  # first bar to check (entry bar itself not checked, same-bar)
        # actually entry bar = i+1; check from i+1 onwards
        j = i + 1
        exited = False
        while j < n:
            reason, price = _bar_exit(pos_side, entry[i], pos_stop, pos_target,
                                      float(h[j]), float(l[j]))
            if reason:
                gross = (price - entry[i]) if pos_side == "BUY" else (entry[i] - price)
                sp = float(spr[j]) if not np.isnan(spr[j]) else 0.0
                cost_pts = 2 * sp + cost_pts_const
                net_pips = gross * POINT / PIP - cost_pts * POINT / PIP
                dur = j - pos_entry_idx
                trades.append(Trade(
                    entry_bar=pos_entry_idx, entry_price=entry[i],
                    side=pos_side, stop=pos_stop, target=pos_target,
                    exit_bar=j, exit_price=float(price),
                    exit_reason=reason, points=gross,
                    cost_pips=cost_pts * POINT / PIP, net_pips=net_pips,
                    duration_bars=dur))
                equity.append(equity[-1] + net_pips)
                in_pos = False
                pos = None
                i = j
                exited = True
                break
            j += 1
        if not exited:
            # force close at last bar
            last = n - 1
            gross = (c[last] - entry[i]) if pos_side == "BUY" else (entry[i] - c[last])
            net_pips = gross * POINT / PIP
            trades.append(Trade(
                entry_bar=pos_entry_idx, entry_price=entry[i],
                side=pos_side, stop=pos_stop, target=pos_target,
                exit_bar=last, exit_price=float(c[last]),
                exit_reason="end", points=gross,
                cost_pips=0.0, net_pips=net_pips,
                duration_bars=last - pos_entry_idx))
            equity.append(equity[-1] + net_pips)
            in_pos = False
            i = last

    return compute_metrics(trades)


# ---------------------------------------------------------------------------
# Parameter search grid — TRAIN ONLY
# ---------------------------------------------------------------------------
PARAM_GRID = {
    "rsi_oversold": [20, 25, 30, 35],
    "rsi_overbought": [65, 70, 75, 80],
    "bias_min_adx": [12, 15, 18],
    "atr_mult": [0.8, 1.0, 1.2],
    "rr": [1.3, 1.5, 1.8],
}


def main():
    print("=" * 70)
    print("V4 RESEARCH: Mean-Reversion Bounce Hypothesis (XAUUSD)")
    print("=" * 70)
    m15_raw, m5_raw = load_xauusd()
    print(f"Loaded M15: {len(m15_raw)} bars, M5: {len(m5_raw)} bars")

    full_bias = build_features(m15_raw)
    full_trig = build_features(m5_raw)

    n = len(full_trig)
    tr_df, va_df, te_df = train_val_test_split(full_trig, 0.6, 0.2, 0.2)
    print(f"TRAIN: {len(tr_df)} bars | VAL: {len(va_df)} | TEST: {len(te_df)}")

    # ---- Precompute bias-aligned context for TRAIN ----
    print("\n[0] Precomputing bias-aligned context for TRAIN window...")
    trig_train = tr_df.copy()
    # the bias frame must cover trig_train's time range + warmup; use full_bias
    trig_train = precompute_context(trig_train, full_bias)

    # ---- Step 1: parameter search on TRAIN only (vectorized) ----
    print("\n[1] Parameter search on TRAIN window (vectorized)...")
    grid = list(itertools.product(
        PARAM_GRID["rsi_oversold"], PARAM_GRID["rsi_overbought"],
        PARAM_GRID["bias_min_adx"],
        PARAM_GRID["atr_mult"], PARAM_GRID["rr"],
    ))
    best = None
    results = []
    for p in grid:
        params = {"rsi_oversold": p[0], "rsi_overbought": p[1],
                  "bias_min_adx": p[2], "atr_mult": p[3], "rr": p[4]}
        sigs = compute_signal_mask(trig_train,
                                   rsi_oversold=params["rsi_oversold"],
                                   rsi_overbought=params["rsi_overbought"],
                                   bias_min_adx=params["bias_min_adx"],
                                   vol_allowed={"normal", "low"},
                                   sessions=["london", "newyork"])
        # apply atr_mult/rr to stop/target properly
        sigs = apply_stop_target(sigs, params["atr_mult"], params["rr"])
        m = backtest_vectorized(sigs, params["atr_mult"], params["rr"])
        net = m.get("net_pips", float("nan"))
        pf = m.get("profit_factor", float("nan"))
        nt = m.get("total_trades", 0)
        results.append((net, pf, nt, params))
        if (best is None or (pd.notna(net) and net > (best[0] if best else float("-inf")))) \
           and nt >= 30:
            best = (net, pf, nt, params)

    results.sort(key=lambda r: (r[0] if pd.notna(r[0]) else float("-inf")), reverse=True)
    print(f"\nTop 5 configs by TRAIN net_pips (min 30 trades) of {len(results)} grid:")
    shown = 0
    for net, pf, nt, params in results:
        if nt >= 30 and shown < 5:
            print(f"  net={net:9.1f}  PF={pf:5.2f}  trades={nt:4d}  {params}")
            shown += 1
    if shown == 0:
        print("  (no config produced >=30 trades in TRAIN)")

    # ---- Step 2: measure best on VALIDATION (fixed params) ----
    print("\n[2] Measuring best TRAIN config on VALIDATION...")
    if best is not None:
        net, pf, nt, params = best
        print(f"  Best TRAIN: net={net:.1f}  PF={pf:.2f}  trades={nt}  {params}")
        trig_val = precompute_context(va_df.copy(), full_bias)
        sigs_v = apply_stop_target(
            compute_signal_mask(trig_val,
                                rsi_oversold=params["rsi_oversold"],
                                rsi_overbought=params["rsi_overbought"],
                                bias_min_adx=params["bias_min_adx"],
                                vol_allowed={"normal", "low"},
                                sessions=["london", "newyork"]),
            params["atr_mult"], params["rr"])
        vm = backtest_vectorized(sigs_v, params["atr_mult"], params["rr"])
        print(f"  VAL: net={vm.get('net_pips', float('nan')):.1f}  "
              f"PF={vm.get('profit_factor', float('nan')):.2f}  "
              f"trades={vm.get('total_trades', 0)}  "
              f"win%={vm.get('win_rate', 0)*100:.0f}")

        # ---- Step 3: walk-forward OOS ----
        print("\n[3] Walk-forward OOS (fixed params, no re-tuning)...")
        windows = list(walk_forward_windows(n, train_frac=0.5, test_frac=0.25))
        oos_trades = 0
        is_net_list, oos_net_list = [], []
        for (tr_s, tr_e), (te_s, te_e) in windows:
            wt_df = full_trig.iloc[tr_s:tr_e]
            we_df = full_trig.iloc[te_s:te_e]
            wt_c = precompute_context(wt_df.copy(), full_bias)
            we_c = precompute_context(we_df.copy(), full_bias)
            wt_s = apply_stop_target(compute_signal_mask(wt_c,
                rsi_oversold=params["rsi_oversold"],
                rsi_overbought=params["rsi_overbought"],
                bias_min_adx=params["bias_min_adx"],
                vol_allowed={"normal", "low"},
                sessions=["london", "newyork"]), params["atr_mult"], params["rr"])
            we_s = apply_stop_target(compute_signal_mask(we_c,
                rsi_oversold=params["rsi_oversold"],
                rsi_overbought=params["rsi_overbought"],
                bias_min_adx=params["bias_min_adx"],
                vol_allowed={"normal", "low"},
                sessions=["london", "newyork"]), params["atr_mult"], params["rr"])
            im = backtest_vectorized(wt_s, params["atr_mult"], params["rr"])
            om = backtest_vectorized(we_s, params["atr_mult"], params["rr"])
            is_net_list.append(im.get("net_pips", 0))
            oos_net_list.append(om.get("net_pips", 0))
            oos_trades += om.get("total_trades", 0)
            print(f"  [{tr_s}:{tr_e}]->[{te_s}:{te_e}] "
                  f"IS net={im.get('net_pips',0):.1f}(t{im.get('total_trades',0)}) "
                  f"OOS net={om.get('net_pips',0):.1f}(t{om.get('total_trades',0)})")
        is_mean = sum(is_net_list)/len(is_net_list) if is_net_list else 0
        oos_mean = sum(oos_net_list)/len(oos_net_list) if oos_net_list else 0
        deg = 1.0 - (oos_mean/is_mean) if is_mean else float("nan")
        print(f"\n  WF: windows={len(windows)} IS_mean={is_mean:.1f} "
              f"OOS_mean={oos_mean:.1f} OOS_deg={deg:.2f} OOS_trades={oos_trades}")
    else:
        print("  No config met min 30-trade threshold on TRAIN.")

    # ---- Step 4: V1 contrast ----
    print("\n[4] Contrast: V1 trend-following on same TRAIN window...")
    from strategy import Strategy
    from backtest import run_backtest
    v1 = Strategy.load(Path(__file__).resolve().parents[1] /
                      "strategy/defs/XAUUSD_STRUCTURE_BREAK_V1.json")
    v1res = run_backtest(v1, full_bias.iloc[:len(tr_df)], tr_df)
    print(f"  V1 TRAIN: net={v1res.metrics.get('net_pips',0):.1f} "
          f"PF={v1res.metrics.get('profit_factor',0):.2f} "
          f"trades={v1res.metrics.get('total_trades',0)} "
          f"win%={v1res.metrics.get('win_rate',0)*100:.0f}")

    print("\n" + "=" * 70)
    print("RESEARCH COMPLETE — no MT5 writes, no live trading, no fabrication.")
    print("=" * 70)


def apply_stop_target(sigs: pd.DataFrame, atr_mult: float, rr: float) -> pd.DataFrame:
    """Fill stop/target columns from ATR multiple + risk:reward."""
    df = sigs.copy()
    close = df["close"].to_numpy()
    atr = df["atr"].to_numpy()
    buy = df["signal"] == "BUY"
    sell = df["signal"] == "SELL"
    df.loc[buy, "stop"] = close[buy] - atr_mult * atr[buy]
    df.loc[buy, "target"] = close[buy] + atr_mult * atr[buy] * rr
    df.loc[sell, "stop"] = close[sell] + atr_mult * atr[sell]
    df.loc[sell, "target"] = close[sell] - atr_mult * atr[sell] * rr
    return df


if __name__ == "__main__":
    main()
