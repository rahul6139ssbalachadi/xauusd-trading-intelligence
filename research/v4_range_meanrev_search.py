"""V4 research: range mean-reversion hypothesis on XAUUSD.

STANDALONE research script. Reuses build_features (P5+P6) and compute_metrics (P8)
but implements a NEW entry rule that the generic evaluate() does not support.

V4 = RANGE MEAN-REVERSION (structurally different from V1/V2/V3):
  - M15 context: ADX < 25 (range-bound, NOT trending)
  - M5 trigger: RSI NEUTRAL (35-65), indecision candle (body/range < 0.4),
    price near 20-bar EMA anchor (within 0.5xATR)
  - Direction follows most recent CHoCH reversal (mean-revert the last break)
  - Tight ATR stop (0.6-1.0x), R:R 1.0-1.5

DISCIPLINE (CLAUDE.md §9-13):
  - Parameter search on TRAIN only.
  - Best config measured (never re-tuned) on val + walk-forward OOS.

Vectorized for speed.
  ./.venv/Scripts/python.exe research/v4_range_meanrev_search.py
"""
from __future__ import annotations

import itertools
import sqlite3
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from market_data import config as cfg
from strategy import build_features
from backtest import compute_metrics, _bar_exit, Trade
from validation import train_val_test_split, walk_forward_windows


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


def precompute_context(trig_df: pd.DataFrame, bias_df: pd.DataFrame) -> pd.DataFrame:
    """Align M15 bias metrics to each M5 bar by timestamp (as-of, no look-ahead)."""
    bcols = ["ts", "adx", "vol_regime"]
    b = bias_df[bcols].sort_values("ts").reset_index(drop=True)
    t = trig_df[["ts"]].copy()
    t_idx = t.reset_index()
    merged = pd.merge_asof(t_idx.sort_values("ts"), b, on="ts", direction="backward")
    merged = merged.sort_values("index").set_index("index")
    trig_df = trig_df.copy()
    trig_df["ctx_adx"] = merged["adx"].reindex(trig_df.index)
    trig_df["ctx_vol"] = merged["vol_regime"].reindex(trig_df.index)
    return trig_df


def compute_signal_mask(trig_df: pd.DataFrame, *,
                        rsi_min: int, rsi_max: int,
                        bias_max_adx: float,
                        vol_allowed: set[str],
                        sessions: list[str],
                        indecision_max: float,
                        near_anchor_atr: float,
                        anchor_ema: int,
                        window: int = 15) -> pd.DataFrame:
    """Vectorized V4 range mean-reversion signal mask."""
    df = trig_df.copy()
    n = len(df)

    # session filter
    sess_ok = df["session"].isin(sessions).to_numpy()

    # bias context: ADX must be BELOW threshold (range condition)
    ctx_adx = df["ctx_adx"].to_numpy()
    adx_ok = np.where(np.isnan(ctx_adx), False, ctx_adx < bias_max_adx)

    # vol regime
    ctx_vol = df["ctx_vol"].astype(object).fillna("").to_numpy()
    vol_ok = np.isin(list(ctx_vol), list(vol_allowed))

    rsi = df["rsi"].to_numpy()
    choch = df["last_choch_dir"].astype(object).fillna("").to_numpy()
    close = df["close"].to_numpy()
    o = df["open"].to_numpy()
    h = df["high"].to_numpy()
    l = df["low"].to_numpy()
    atr = df["atr"].to_numpy()

    # EMA anchor
    ema_anchor = df["close"].ewm(span=anchor_ema, adjust=False,
                                min_periods=anchor_ema).mean().to_numpy()
    atr_safe = np.where(np.isnan(atr), 1.0, atr)
    dist_ema = np.abs(close - ema_anchor) / atr_safe
    near_anchor = dist_ema <= near_anchor_atr

    # indecision: body/range ratio
    body = np.abs(close - o)
    rng = h - l
    rng_safe = np.where(np.isnan(rng) | (rng == 0), 1.0, rng)
    body_ratio = body / rng_safe
    indecision = body_ratio < indecision_max

    # RSI neutral (NOT at extremes)
    rsi_neutral = (rsi >= rsi_min) & (rsi <= rsi_max)

    valid = np.isfinite(rsi) & np.isfinite(atr) & np.isfinite(close) & np.isfinite(ema_anchor)

    # BUY: CHoCH down recently (price broke down then reversed up) ->
    #      we buy the retest of the broken support (now resistance flip)
    buy = (sess_ok & adx_ok & vol_ok & valid & rsi_neutral & indecision & near_anchor &
           (choch == "down"))
    # SELL: CHoCH up recently (price broke up then reversed down) ->
    #       we sell the retest of the broken resistance (now support flip)
    sell = (sess_ok & adx_ok & vol_ok & valid & rsi_neutral & indecision & near_anchor &
            (choch == "up"))

    sig = np.where(buy, "BUY",
            np.where(sell, "SELL", "WAIT"))
    df["signal"] = sig
    df["stop"] = np.nan
    df["target"] = np.nan
    return df


def apply_stop_target(sigs: pd.DataFrame, atr_mult: float, rr: float) -> pd.DataFrame:
    """Fill stop/target for active signals from ATR multiple + R:R."""
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


def backtest_vectorized(feats: pd.DataFrame, atr_mult: float,
                        rr: float, slippage_pips: float = 0.5) -> dict:
    """Fast backtest over precomputed signal frame (same logic as Phase 8)."""
    n = len(feats)
    POINT = 0.01
    PIP = 0.10
    cost_pts_const = 2 * slippage_pips * (PIP / POINT)

    sig = feats["signal"].to_numpy()
    o = feats["open"].to_numpy()
    h = feats["high"].to_numpy()
    l = feats["low"].to_numpy()
    c = feats["close"].to_numpy()
    spr = feats["spread"].to_numpy()
    atr = feats["atr"].to_numpy()
    entry = np.roll(o, -1)
    entry[-1] = np.nan

    trades = []
    equity = [0.0]
    i = 0
    while i < n - 1:
        s = sig[i]
        if s not in ("BUY", "SELL"):
            i += 1
            continue
        if np.isnan(entry[i]):
            i += 1
            continue
        e = entry[i]
        a = atr[i]
        if np.isnan(a):
            i += 1
            continue
        if s == "BUY":
            stop = e - atr_mult * a
            target = e + atr_mult * a * rr
        else:
            stop = e + atr_mult * a
            target = e - atr_mult * a * rr
        # check exit bars from i+1 onwards
        j = i + 1
        exited = False
        while j < n:
            reason, price = _bar_exit(s, e, stop, target, float(h[j]), float(l[j]))
            if reason:
                gross = (price - e) if s == "BUY" else (e - price)
                sp = float(spr[j]) if not np.isnan(spr[j]) else 0.0
                cost_pts = 2 * sp + cost_pts_const
                net_pips = gross * POINT / PIP - cost_pts * POINT / PIP
                trades.append(Trade(
                    entry_bar=i + 1, entry_price=e, side=s,
                    stop=stop, target=target,
                    exit_bar=j, exit_price=float(price),
                    exit_reason=reason, points=gross,
                    cost_pips=cost_pts * POINT / PIP, net_pips=net_pips,
                    duration_bars=j - (i + 1)))
                equity.append(equity[-1] + net_pips)
                in_pos = False
                i = j
                exited = True
                break
            j += 1
        if not exited:
            gross = (c[-1] - e) if s == "BUY" else (e - c[-1])
            trades.append(Trade(
                entry_bar=i + 1, entry_price=e, side=s,
                stop=stop, target=target,
                exit_bar=n - 1, exit_price=float(c[-1]),
                exit_reason="end", points=gross,
                cost_pips=0.0, net_pips=gross * POINT / PIP,
                duration_bars=(n - 1) - (i + 1)))
            equity.append(equity[-1] + gross * POINT / PIP)
            i = n - 1
    return compute_metrics(trades)


# ---------------------------------------------------------------------------
# Parameter search grid — TRAIN ONLY
# ---------------------------------------------------------------------------
PARAM_GRID = {
    "rsi_min": [30, 35, 40],
    "rsi_max": [65, 70, 75],
    "bias_max_adx": [20, 25, 30],
    "indecision_max": [0.3, 0.4, 0.5],
    "near_anchor_atr": [0.3, 0.5, 0.8],
    "atr_mult": [0.6, 0.8, 1.0],
    "rr": [1.0, 1.2, 1.5],
}


def main():
    print("=" * 70)
    print("V4 RESEARCH: Range Mean-Reversion Hypothesis (XAUUSD)")
    print("=" * 70)
    m15_raw, m5_raw = load_xauusd()
    print(f"Loaded M15: {len(m15_raw)} bars, M5: {len(m5_raw)} bars")

    full_bias = build_features(m15_raw)
    full_trig = build_features(m5_raw)
    n = len(full_trig)
    tr_df, va_df, te_df = train_val_test_split(full_trig, 0.6, 0.2, 0.2)
    print(f"TRAIN: {len(tr_df)} bars | VAL: {len(va_df)} | TEST: {len(te_df)}")

    # precompute context for TRAIN
    print("\n[0] Precomputing bias-aligned context for TRAIN...")
    trig_train = precompute_context(tr_df.copy(), full_bias)

    # ---- Step 1: parameter search on TRAIN only ----
    print(f"\n[1] Parameter search on TRAIN window ({len(list(itertools.product(*PARAM_GRID.values())))} combos)...")
    grid = list(itertools.product(
        PARAM_GRID["rsi_min"], PARAM_GRID["rsi_max"],
        PARAM_GRID["bias_max_adx"], PARAM_GRID["indecision_max"],
        PARAM_GRID["near_anchor_atr"], PARAM_GRID["atr_mult"], PARAM_GRID["rr"],
    ))
    best = None
    results = []
    for p in grid:
        params = {"rsi_min": p[0], "rsi_max": p[1], "bias_max_adx": p[2],
                  "indecision_max": p[3], "near_anchor_atr": p[4],
                  "atr_mult": p[5], "rr": p[6]}
        sigs = compute_signal_mask(trig_train,
                                   rsi_min=params["rsi_min"],
                                   rsi_max=params["rsi_max"],
                                   bias_max_adx=params["bias_max_adx"],
                                   vol_allowed={"normal", "low"},
                                   sessions=["london", "newyork"],
                                   indecision_max=params["indecision_max"],
                                   near_anchor_atr=params["near_anchor_atr"],
                                   anchor_ema=20)
        sigs = apply_stop_target(sigs, params["atr_mult"], params["rr"])
        m = backtest_vectorized(sigs, params["atr_mult"], params["rr"])
        net = m.get("net_pips", float("nan"))
        pf = m.get("profit_factor", float("nan"))
        nt = m.get("total_trades", 0)
        winr = m.get("win_rate", 0)
        results.append((net, pf, nt, winr, params))
        if (best is None or (pd.notna(net) and net > (best[0] if best else float("-inf")))) \
           and nt >= 30:
            best = (net, pf, nt, winr, params)

    results.sort(key=lambda r: (r[0] if pd.notna(r[0]) else float("-inf")), reverse=True)
    print(f"\nTop 10 configs by TRAIN net_pips (min 30 trades) of {len(results)}:")
    shown = 0
    for net, pf, nt, winr, params in results:
        if nt >= 30 and shown < 10:
            print(f"  net={net:9.1f}  PF={pf:5.2f}  trades={nt:4d}  "
                  f"win%={winr*100:4.0f}  {params}")
            shown += 1
    # also show top by PF for context
    pf_sorted = [r for r in results if r[2] >= 30]
    pf_sorted.sort(key=lambda r: r[1] if pd.notna(r[1]) else -1, reverse=True)
    print(f"\nTop 5 by PROFIT FACTOR (min 30 trades):")
    for net, pf, nt, winr, params in pf_sorted[:5]:
        print(f"  PF={pf:5.2f}  net={net:9.1f}  trades={nt:4d}  "
              f"win%={winr*100:4.0f}  {params}")
    if not pf_sorted:
        print("  (none met 30-trade threshold)")
    if shown == 0 and not pf_sorted:
        print("  (no config produced >=30 trades in TRAIN)")

    # ---- Step 2: measure best on VALIDATION ----
    print("\n[2] Measuring best TRAIN config on VALIDATION...")
    if best is not None:
        net, pf, nt, winr, params = best
        print(f"  Best TRAIN: net={net:.1f}  PF={pf:.2f}  trades={nt}  win%={winr*100:.0f}")
        print(f"  Params: {params}")
        trig_val = precompute_context(va_df.copy(), full_bias)
        sigs_v = apply_stop_target(compute_signal_mask(trig_val,
            rsi_min=params["rsi_min"], rsi_max=params["rsi_max"],
            bias_max_adx=params["bias_max_adx"],
            vol_allowed={"normal", "low"},
            sessions=["london", "newyork"],
            indecision_max=params["indecision_max"],
            near_anchor_atr=params["near_anchor_atr"],
            anchor_ema=20), params["atr_mult"], params["rr"])
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
                rsi_min=params["rsi_min"], rsi_max=params["rsi_max"],
                bias_max_adx=params["bias_max_adx"],
                vol_allowed={"normal", "low"},
                sessions=["london", "newyork"],
                indecision_max=params["indecision_max"],
                near_anchor_atr=params["near_anchor_atr"],
                anchor_ema=20), params["atr_mult"], params["rr"])
            we_s = apply_stop_target(compute_signal_mask(we_c,
                rsi_min=params["rsi_min"], rsi_max=params["rsi_max"],
                bias_max_adx=params["bias_max_adx"],
                vol_allowed={"normal", "low"},
                sessions=["london", "newyork"],
                indecision_max=params["indecision_max"],
                near_anchor_atr=params["near_anchor_atr"],
                anchor_ema=20), params["atr_mult"], params["rr"])
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
    v1res = run_backtest(v1, full_bias, tr_df)
    print(f"  V1 TRAIN: net={v1res.metrics.get('net_pips',0):.1f} "
          f"PF={v1res.metrics.get('profit_factor',0):.2f} "
          f"trades={v1res.metrics.get('total_trades',0)} "
          f"win%={v1res.metrics.get('win_rate',0)*100:.0f}")

    print("\n" + "=" * 70)
    print("RESEARCH COMPLETE — no MT5 writes, no live trading, no fabrication.")
    print("All numbers are COMPUTED from db/trading.db.")
    print("=" * 70)


if __name__ == "__main__":
    main()
