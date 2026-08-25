"""V8 research: volatility-expansion breakout strategy on XAUUSD.

STANDALONE research script. A fundamentally different hypothesis from V1-V7:
  V1-V5: structure-break / BOS / CHoCH (trend-following and mean-reversion)
  V6: candle-pattern continuation (engulfing/pin bar)
  V7: momentum-reversal (fade extreme momentum with pin bars)
  V8: VOLATILITY-EXPANSION breakout — trade WITH the expansion
  when ATR spikes above its recent range (volatility contraction -> explosion).

V8 logic:
  - M15: compute ATR(14) and its 50-bar rolling percentile rank
  - When ATR percentile is in bottom 20% (volatility compression),
    watch for an expansion breakout (ATR jumps above 70th percentile)
  - M5: wait for a directional breakout bar (close > upper BB or close < lower BB
    on M5, confirming the expansion direction)
  - Entry: market on next M5 bar open, in direction of breakout
  - M15 ADX confirms trend strength (>=15, not too high to avoid chop)
  - Stop: below the compression-period low (buy) or above (sell)
  - Target: 2R (ATR-based)
  - Session: London/NY overlap (highest volume)

This trades WITH volatility expansion, not against it. The descriptive analysis
showed 00:00 broker time (21:00 UTC) has highest mean absolute move.

DISCIPLINE: grid search on TRAIN only; best config measured on VAL + OOS + MC.
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
from backtest import compute_metrics, Trade
from validation import train_val_test_split, walk_forward_windows
from indicators import bollinger


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------
def load_tf(tf: str) -> pd.DataFrame:
    db = cfg.PROJECT_ROOT / cfg.load_settings()["paths"]["db"]
    con = sqlite3.connect(db)
    df = pd.read_sql_query(
        "SELECT ts_broker_epoch, open, high, low, close, spread, tick_volume "
        "FROM market_data WHERE symbol='XAUUSD' AND timeframe=? "
        "AND source='mt5' ORDER BY ts_broker_epoch", con, params=(tf,))
    con.close()
    df["ts"] = pd.to_datetime(df["ts_broker_epoch"], unit="s", utc=True)
    df["spread_pips"] = df["spread"] * 0.01 / 0.10
    df["tick_volume"] = df["tick_volume"].astype(float)
    return df


def _rolling_percentile(arr: np.ndarray, window: int, pct: float) -> np.ndarray:
    """Compute rolling percentile of arr using a uniform weighting (simple)."""
    n = len(arr)
    result = np.full(n, np.nan)
    half = window // 2
    for i in range(window, n):
        window_data = arr[i - window:i]
        valid = window_data[~np.isnan(window_data)]
        if len(valid) > 0:
            result[i] = np.percentile(valid, pct * 100)
    return result


def _rolling_min(arr: np.ndarray, window: int) -> np.ndarray:
    """Rolling minimum."""
    s = pd.Series(arr)
    return s.rolling(window, min_periods=window).min().to_numpy()


def _rolling_max(arr: np.ndarray, window: int) -> np.ndarray:
    """Rolling maximum."""
    s = pd.Series(arr)
    return s.rolling(window, min_periods=window).max().to_numpy()


# ---------------------------------------------------------------------------
# Signal computation — V8: volatility-expansion breakout
# ---------------------------------------------------------------------------
def compute_signal_mask(
    m5_df: pd.DataFrame,
    m15_framed: pd.DataFrame,
    *,
    atr_mult_stop: float = 1.0,
    rr: float = 2.0,
    vol_compress_pct: float = 20.0,  # ATR percentile below this = compression
    vol_expand_pct: float = 70.0,   # ATR percentile above this = expansion
    adx_min: float = 15,
    atr_lookback: int = 14,
    bb_std: float = 2.0,
    max_spread_pips: float = 3.0,
    session_filter: bool = True,
    slippage_pips: float = 0.5,
    lookback: int = 10,
) -> pd.DataFrame:
    """V8 signal: volatility compression -> expansion breakout entry.

    m5_df: M5 dataframe with features (build_features applied)
    m15_framed: M15 dataframe aligned to M5 timestamps (as-of merge)
                  with columns: atr_14, adx, bb_upper, bb_lower, session
    """
    df = m5_df.copy()
    close = df["close"].to_numpy()
    o = df["open"].to_numpy()
    h = df["high"].to_numpy()
    l = df["low"].to_numpy()
    c = df["close"].to_numpy()
    spread = df["spread_pips"].to_numpy()
    sess = df["session"].astype(str).to_numpy()
    atr = df["atr"].to_numpy()

    # Align M15 context to M5 by timestamp (as-of, no look-ahead)
    m15 = m15_framed[["ts", "atr_14", "adx", "bb_upper", "bb_lower"]].sort_values("ts").reset_index(drop=True)
    trig_idx = df[["ts"]].reset_index(drop=True)
    trig_idx["_orig_idx"] = trig_idx.index
    merged = pd.merge_asof(trig_idx.sort_values("ts"), m15, on="ts", direction="backward")
    merged = merged.sort_values("_orig_idx").set_index("_orig_idx")

    m15_atr = merged["atr_14"].reindex(df.index).to_numpy()
    m15_adx = merged["adx"].reindex(df.index).to_numpy()
    m15_bb_hi = merged["bb_upper"].reindex(df.index).to_numpy()
    m15_bb_lo = merged["bb_lower"].reindex(df.index).to_numpy()

    # 1. Volatility regime on M15: ATR percentile rank
    m15_atr_valid = m15_atr[~np.isnan(m15_atr)]
    if len(m15_atr_valid) < 30:
        df["signal"] = "WAIT"
        df["stop"] = np.nan
        df["target"] = np.nan
        return df
    vol_p20 = np.percentile(m15_atr_valid, vol_compress_pct)
    vol_p70 = np.percentile(m15_atr_valid, vol_expand_pct)

    # 2. Volatility compression (M15 ATR in bottom 20%)
    is_compressed = m15_atr <= vol_p20

    # 3. Volatility expansion (M15 ATR jumped above 70th percentile)
    is_expanding = m15_atr >= vol_p70

    # 4. M5 BB breakout confirmation
    m5_bb_hi_s, m5_bb_lo_s, m5_bb_mid = bollinger(df["close"], period=20, num_std=bb_std)
    m5_bb_hi = m5_bb_hi_s.to_numpy()
    m5_bb_lo = m5_bb_lo_s.to_numpy()

    # 5. Compression-then-expansion: look back for compression, then breakout
    was_compressed = pd.Series(is_compressed).rolling(lookback, min_periods=lookback).max().to_numpy()
    was_compressed = ~np.isnan(was_compressed) & (was_compressed > 0)

    # 6. Directional breakout: M5 close breaks M5 BB
    breakout_up = is_expanding & was_compressed & (c > m5_bb_hi) & ~np.isnan(m5_bb_hi)
    breakout_down = is_expanding & was_compressed & (c < m5_bb_lo) & ~np.isnan(m5_bb_lo)

    # 7. Filters
    sess_ok = ~session_filter | np.isin(sess, ["london", "newyork"])
    adx_ok = ~np.isnan(m15_adx) & (m15_adx >= adx_min)
    spread_ok = ~np.isnan(spread) & (spread <= max_spread_pips)

    buy = breakout_up & sess_ok & adx_ok & spread_ok
    sell = breakout_down & sess_ok & adx_ok & spread_ok

    sig = np.where(buy, "BUY", np.where(sell, "SELL", "WAIT"))
    df["signal"] = sig
    df["stop"] = np.nan
    df["target"] = np.nan

    atol = np.where(np.isnan(atr), 0.0, atr)
    # Stop: below recent swing low (buy) / above recent swing high (sell)
    roll_lo = _rolling_min(l, 20)
    roll_hi = _rolling_max(h, 20)
    stop_buy = np.where(np.isnan(roll_lo), close - atr_mult_stop * atol, roll_lo - 0.5 * atol)
    stop_sell = np.where(np.isnan(roll_hi), close + atr_mult_stop * atol, roll_hi + 0.5 * atol)
    target_buy = close + atol * rr
    target_sell = close - atol * rr

    df["stop"] = np.where(buy, stop_buy, np.where(sell, stop_sell, np.nan))
    df["target"] = np.where(buy, target_buy, np.where(sell, target_sell, np.nan))
    return df


# ---------------------------------------------------------------------------
# Vectorized backtest
# ---------------------------------------------------------------------------
def backtest_vectorized(feats: pd.DataFrame, slippage_pips: float = 0.5) -> tuple[dict, list]:
    POINT = 0.01
    PIP = 0.10
    cost_constant = 2 * slippage_pips * (PIP / POINT)

    sig = feats["signal"].to_numpy()
    o = feats["open"].to_numpy()
    h = feats["high"].to_numpy()
    l = feats["low"].to_numpy()
    c = feats["close"].to_numpy()
    spr = feats["spread"].to_numpy()
    stops = feats["stop"].to_numpy()
    targets = feats["target"].to_numpy()

    n = len(feats)
    entry = np.roll(o, -1)
    entry[-1] = np.nan

    signal_mask = (sig == "BUY") | (sig == "SELL")
    signal_mask[-1] = False
    entry_bars = np.where(signal_mask)[0]

    trades = []
    last_exit_bar = -1

    for i in entry_bars:
        if i < last_exit_bar:
            continue
        if np.isnan(entry[i]) or np.isnan(stops[i]) or np.isnan(targets[i]):
            continue
        s = sig[i]
        e = entry[i]
        stop = stops[i]
        target = targets[i]
        j_start = i + 1
        if j_start >= n:
            continue

        h_slice = h[j_start:]
        l_slice = l[j_start:]
        spr_slice = spr[j_start:]

        if s == "BUY":
            stop_hit = l_slice <= stop
            target_hit = h_slice >= target
        else:
            stop_hit = h_slice >= stop
            target_hit = l_slice <= target

        any_exit = stop_hit | target_hit
        if not any_exit.any():
            exit_j = n - 1
            exit_price = c[-1]
            reason = "end"
            sp = spr[-1] if not np.isnan(spr[-1]) else 0.0
        else:
            first_exit_idx = np.where(any_exit)[0][0]
            exit_j = j_start + first_exit_idx
            sp = spr_slice[first_exit_idx]
            if stop_hit[first_exit_idx] and target_hit[first_exit_idx]:
                if abs(e - stop) <= abs(target - e):
                    reason = "stop"
                    exit_price = stop
                else:
                    reason = "target"
                    exit_price = target
            elif stop_hit[first_exit_idx]:
                reason = "stop"
                exit_price = stop
            else:
                reason = "target"
                exit_price = target

        gross = (exit_price - e) if s == "BUY" else (e - exit_price)
        cost_pts = 2 * sp + cost_constant
        net_pips = gross / PIP - cost_pts / PIP

        trades.append(Trade(
            entry_bar=i + 1, entry_price=e, side=s,
            stop=stop, target=target,
            exit_bar=exit_j, exit_price=exit_price,
            exit_reason=reason, points=gross,
            cost_pips=cost_pts / PIP, net_pips=net_pips,
            duration_bars=exit_j - (i + 1)))
        last_exit_bar = exit_j

    return compute_metrics(trades), trades


# ---------------------------------------------------------------------------
# Parameter grid — TRAIN ONLY
# ---------------------------------------------------------------------------
PARAM_GRID = {
    "atr_mult_stop": [1.0, 1.5, 2.0],
    "rr": [1.5, 2.0, 2.5, 3.0],
    "vol_compress_pct": [10.0, 20.0, 30.0],
    "vol_expand_pct": [60.0, 70.0, 80.0],
    "adx_min": [15, 20, 25],
    "bb_std": [2.0, 2.5],
    "max_spread_pips": [3.0, 5.0],
    "lookback": [5, 10, 15],
}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    print("=" * 70)
    print("V8 RESEARCH: Volatility-Expansion Breakout (XAUUSD M5+M15)")
    print("=" * 70)

    m5_raw = load_tf("M5")
    m15_raw = load_tf("M15")
    print(f"Loaded M15: {len(m15_raw)} bars  ({m15_raw['ts'].iloc[0]} to {m15_raw['ts'].iloc[-1]})")
    print(f"Loaded M5:  {len(m5_raw)} bars  ({m5_raw['ts'].iloc[0]} to {m5_raw['ts'].iloc[-1]})")

    print("\n[0] Building features...")
    feats_m15 = build_features(m15_raw)
    feats_m5 = build_features(m5_raw)

    # Build M15 framed data with BB for alignment
    m15_bb_hi_s, m15_bb_lo_s, _ = bollinger(feats_m15["close"], period=20, num_std=2.0)
    m15_framed = feats_m15[["ts", "atr", "adx"]].copy()
    m15_framed["atr_14"] = feats_m15["atr"].to_numpy()
    m15_framed["bb_upper"] = m15_bb_hi_s.to_numpy()
    m15_framed["bb_lower"] = m15_bb_lo_s.to_numpy()

    print("[1] Detecting volatility-regime breakout signals...")
    # Quick pre-count with default params
    default = dict(atr_mult_stop=1.5, rr=2.0, adx_min=15, lookback=10)
    pre = compute_signal_mask(feats_m5.copy(), m15_framed,
                              vol_compress_pct=20.0, vol_expand_pct=70.0,
                              bb_std=2.0, max_spread_pips=3.0, **default)
    n_buy = (pre["signal"] == "BUY").sum()
    n_sell = (pre["signal"] == "SELL").sum()
    print(f"   Default params: BUY signals={n_buy}, SELL signals={n_sell}")

    tr_df, va_df, te_df = train_val_test_split(feats_m5, 0.6, 0.2, 0.2)
    print(f"\nTRAIN: {len(tr_df)} bars | VAL: {len(va_df)} | TEST: {len(te_df)}")

    # Build M15 framed for the full dataset (for as-of alignment)
    # For TRAIN/VAL/TEST splits, we need to recompute the M15 context per split
    def get_m15_for_split(df_split):
        """Get M15 context aligned to this split's timestamps."""
        m15_sub = m15_framed.sort_values("ts").reset_index(drop=True)
        idx = df_split[["ts"]].reset_index(drop=True)
        idx["_orig_idx"] = idx.index
        merged = pd.merge_asof(idx.sort_values("ts"), m15_sub,
                               on="ts", direction="backward")
        merged = merged.sort_values("_orig_idx").set_index("_orig_idx")
        result = df_split.copy()
        for col in ["atr_14", "adx", "bb_upper", "bb_lower"]:
            result[f"_m15_{col}"] = merged[col].reindex(df_split.index)
        return result

    # We need M15 context columns on the TRAIN df for compute_signal_mask
    # Actually compute_signal_mask takes m5_df and m15_framed separately.
    # Let's pre-align M15 context to each M5 bar once for the full dataset,
    # then split by timestamp.

    # Align M15 context to ALL M5 bars once
    m15_align = m15_framed.sort_values("ts").reset_index(drop=True)
    m5_ts = feats_m5[["ts"]].reset_index(drop=True)
    m5_ts["_orig_idx"] = m5_ts.index
    merged_all = pd.merge_asof(m5_ts.sort_values("ts"), m15_align,
                               on="ts", direction="backward")
    merged_all = merged_all.sort_values("_orig_idx").set_index("_orig_idx")

    # Attach M15 columns to feats_m5
    feats_m5_aligned = feats_m5.copy()
    for col in ["atr_14", "adx", "bb_upper", "bb_lower"]:
        feats_m5_aligned[f"_m15_{col}"] = merged_all[col].reindex(feats_m5.index)

    # Split by timestamp (train_val_test_split works on index, which is positional)
    # We already split feats_m5 (unaligned) above. Let's split the aligned version too.
    split_idx = int(len(feats_m5) * 0.6)
    val_idx = int(len(feats_m5) * 0.8)
    tr_aligned = feats_m5_aligned.iloc[:split_idx]
    va_aligned = feats_m5_aligned.iloc[split_idx:val_idx]
    te_aligned = feats_m5_aligned.iloc[val_idx:]

    print(f"\n[2] Grid search on TRAIN ({len(list(itertools.product(*PARAM_GRID.values())))} combos)...")
    best = None
    results = []
    # Pre-extract M15 context for TRAIN split
    m15_tr = pd.DataFrame({
        "ts": tr_aligned["ts"],
        "atr_14": tr_aligned["_m15_atr_14"].to_numpy(),
        "adx": tr_aligned["_m15_adx"].to_numpy(),
        "bb_upper": tr_aligned["_m15_bb_upper"].to_numpy(),
        "bb_lower": tr_aligned["_m15_bb_lower"].to_numpy(),
    })

    for p in itertools.product(*PARAM_GRID.values()):
        params = dict(zip(PARAM_GRID.keys(), p))
        sigs = compute_signal_mask(tr_aligned.copy(), m15_tr, **params)
        m, _ = backtest_vectorized(sigs, params["rr"])
        net = m.get("net_pips", float("nan"))
        pf = m.get("profit_factor", float("nan"))
        nt = m.get("total_trades", 0)
        winr = m.get("win_rate", 0)
        results.append((net, pf, nt, winr, params))
        if (best is None or (pd.notna(net) and net > (best[0] if best else float("-inf")))) and nt >= 15:
            best = (net, pf, nt, winr, params)

    results.sort(key=lambda r: (r[0] if pd.notna(r[0]) else float("-inf")), reverse=True)
    print(f"\nTop 15 by TRAIN net_pips (min 15 trades):")
    shown = 0
    for net, pf, nt, winr, params in results:
        if nt >= 15 and shown < 15:
            print(f"  net={net:9.1f}  PF={pf:5.2f}  trades={nt:4d}  win%={winr*100:4.0f}  {params}")
            shown += 1

    pf_sorted = [r for r in results if r[2] >= 15]
    pf_sorted.sort(key=lambda r: r[1] if pd.notna(r[1]) else -1, reverse=True)
    print(f"\nTop 5 by PROFIT FACTOR (min 15 trades):")
    for net, pf, nt, winr, params in pf_sorted[:5]:
        print(f"  PF={pf:5.2f}  net={net:9.1f}  trades={nt:4d}  win%={winr*100:4.0f}  {params}")
    if not pf_sorted:
        print("  (none met 15-trade threshold)")

    if (not best or best[2] < 15) and not pf_sorted:
        print("\n  (no config produced >=15 trades in TRAIN)")
        print("\n" + "=" * 70)
        print("RESEARCH COMPLETE — no MT5 writes, no live trading, no fabrication.")
        return

    print("\n[3] Measuring best TRAIN config on VALIDATION...")
    net, pf, nt, winr, params = best
    print(f"  Best TRAIN: net={net:.1f}  PF={pf:.2f}  trades={nt}  win%={winr*100:.0f}")
    print(f"  Params: {params}")

    m15_va = pd.DataFrame({
        "ts": va_aligned["ts"],
        "atr_14": va_aligned["_m15_atr_14"].to_numpy(),
        "adx": va_aligned["_m15_adx"].to_numpy(),
        "bb_upper": va_aligned["_m15_bb_upper"].to_numpy(),
        "bb_lower": va_aligned["_m15_bb_lower"].to_numpy(),
    })
    sigs_v = compute_signal_mask(va_aligned.copy(), m15_va, **params)
    vm, _ = backtest_vectorized(sigs_v, params["rr"])
    print(f"  VAL: net={vm.get('net_pips', float('nan')):.1f}  "
          f"PF={vm.get('profit_factor', float('nan')):.2f}  "
          f"trades={vm.get('total_trades',0)}  "
          f"win%={vm.get('win_rate',0)*100:.0f}")

    print("\n[4] Walk-forward OOS (fixed params, no re-tuning)...")
    windows = list(walk_forward_windows(len(feats_m5_aligned), train_frac=0.3, test_frac=0.1))
    oos_trades = 0
    is_net_list, oos_net_list = [], []
    for wn, ((tr_s, tr_e), (te_s, te_e)) in enumerate(windows):
        # Build M15 context for each window
        def build_m15_ctx(df_sub):
            return pd.DataFrame({
                "ts": df_sub["ts"],
                "atr_14": df_sub["_m15_atr_14"].to_numpy(),
                "adx": df_sub["_m15_adx"].to_numpy(),
                "bb_upper": df_sub["_m15_bb_upper"].to_numpy(),
                "bb_lower": df_sub["_m15_bb_lower"].to_numpy(),
            })
        wt_df = feats_m5_aligned.iloc[tr_s:tr_e].copy()
        we_df = feats_m5_aligned.iloc[te_s:te_e].copy()
        wt_sigs = compute_signal_mask(wt_df, build_m15_ctx(wt_df), **params)
        we_sigs = compute_signal_mask(we_df, build_m15_ctx(we_df), **params)
        im, _ = backtest_vectorized(wt_sigs, params["rr"])
        om, _ = backtest_vectorized(we_sigs, params["rr"])
        is_net_list.append(im.get("net_pips", 0))
        oos_net_list.append(om.get("net_pips", 0))
        oos_trades += om.get("total_trades", 0)
        print(f"  W{wn+1}: IS  net={im.get('net_pips',0):7.1f} (t{im.get('total_trades',0)}) "
              f"| OOS net={om.get('net_pips',0):7.1f} (t{om.get('total_trades',0)})")

    is_mean = sum(is_net_list) / len(is_net_list) if is_net_list else 0
    oos_mean = sum(oos_net_list) / len(oos_net_list) if oos_net_list else 0
    deg = 1.0 - (oos_mean / is_mean) if is_mean else float("nan")
    print(f"\n  WF: windows={len(windows)} IS_mean={is_mean:.1f} "
          f"OOS_mean={oos_mean:.1f} deg={deg:.2f} OOS_trades={oos_trades}")

    print("\n[5] Full-dataset backtest (best config on ENTIRE dataset)...")
    m15_all = pd.DataFrame({
        "ts": feats_m5_aligned["ts"],
        "atr_14": feats_m5_aligned["_m15_atr_14"].to_numpy(),
        "adx": feats_m5_aligned["_m15_adx"].to_numpy(),
        "bb_upper": feats_m5_aligned["_m15_bb_upper"].to_numpy(),
        "bb_lower": feats_m5_aligned["_m15_bb_lower"].to_numpy(),
    })
    full_sigs = compute_signal_mask(feats_m5_aligned.copy(), m15_all, **params)
    full_m, full_trades = backtest_vectorized(full_sigs, params["rr"])
    print(f"  FULL: net={full_m.get('net_pips', float('nan')):.1f}  "
          f"PF={full_m.get('profit_factor', float('nan')):.2f}  "
          f"trades={full_m.get('total_trades',0)}  "
          f"win%={full_m.get('win_rate',0)*100:.0f}  "
          f"sharpe={full_m.get('sharpe',0):.2f}  "
          f"maxDD={full_m.get('max_drawdown_pips',0):.1f}")

    print("\n[6] Monte Carlo robustness (on full-dataset trades)...")
    if full_trades:
        from montecarlo import run_monte_carlo, MCConfig
        mc = run_monte_carlo(full_trades, MCConfig(n_iterations=500))
        print(f"  net_mean={mc.net_mean:.1f}  net_p5={mc.net_p5:.1f}  "
              f"PF_mean={mc.profit_factor_mean:.2f}  PF_p5={mc.profit_factor_p5:.2f}  "
              f"ruin_prob={mc.ruin_prob:.2%}  robust={mc.is_robust}")
    else:
        print("  (no trades for MC)")

    print("\n" + "=" * 70)
    print("RESEARCH COMPLETE — no MT5 writes, no live trading, no fabrication.")
    print("All numbers are COMPUTED from db/trading.db.")
    print("=" * 70)


if __name__ == "__main__":
    main()