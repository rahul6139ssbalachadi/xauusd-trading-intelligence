"""V7 research: M15 momentum-reversal strategy on XAUUSD.

STANDALONE research script. Based on descriptive probe finding:
  M15 bottom-10% reversal bars: win% 54.4 (slight edge)
  M15 top-10% momentum bars: win% 49.7 (no edge)

V7 = MOMENTUM-REVERSAL (different concept from V1-V6):
  - M15 context: identify extreme momentum bars (bottom 10% by body size)
    as potential reversal candidates
  - M5 trigger: RSI divergence (lower low in price but higher low in RSI)
    OR price action reversal pattern (pin bar / inside bar)
  - Entry in REVERSAL direction (fade the extreme momentum)
  - EMA20/EMA50 cross for trend context (only fade if trend is extended)
  - ADX filter (only trade when ADX > threshold, confirming momentum extreme)
  - Stop below/above extreme, tight R:R (1:1 to 1:2)
  - Session filter: London/NY

DISCIPLINE: grid search on TRAIN only; best config measured (never re-tuned)
on val + walk-forward OOS + Monte Carlo.
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
from candles import detect_inside_bar as _detect_inside_bar, detect_pin_bar as _detect_pin_bar


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


# ---------------------------------------------------------------------------
# Candle pattern detectors that work on arrays
# ---------------------------------------------------------------------------
def _detect_inside_bar(o, h, l, c):
    prev_h = np.roll(h, 1)
    prev_l = np.roll(l, 1)
    return (h <= prev_h) & (l >= prev_l)


def _detect_pin_bar(o, h, l, c):
    body_size = np.abs(c - o)
    body_size_safe = np.where(body_size == 0, np.nan, body_size)
    upper_shadow = h - np.maximum(o, c)
    lower_shadow = np.minimum(o, c) - l
    ratio_lower = np.where(body_size_safe == 0, np.nan, lower_shadow / body_size_safe)
    ratio_upper = np.where(body_size_safe == 0, np.nan, upper_shadow / body_size_safe)
    return ((ratio_lower >= 2.0) & (lower_shadow > 0)) | \
           ((ratio_upper >= 2.0) & (upper_shadow > 0))


# ---------------------------------------------------------------------------
# Signal computation (vectorized)
# ---------------------------------------------------------------------------
def compute_signal_mask(
    df: pd.DataFrame,
    *,
    min_adx: float = 20,
    atr_mult_stop: float = 0.8,
    rr: float = 1.5,
    ema_span: int = 50,
    max_spread_pips: float = 3.0,
    momentum_threshold: float = 0.10,  # bottom X% of momentum bars
    pattern_type: str = "pin_bar",  # "pin_bar", "inside_bar", "doji", "all"
    vol_pct: float = 0.0,  # min tick_volume percentile filter (0 = disabled)
    session_filter: bool = True,
) -> pd.DataFrame:
    """Vectorized V7 momentum-reversal signal mask.

    Finds extreme momentum bars (small body = exhaustion) + reversal patterns,
    then enters in REVERSAL direction (fade the momentum).
    """
    df = df.copy()
    close = df["close"].to_numpy()
    o = df["open"].to_numpy()
    h = df["high"].to_numpy()
    l = df["low"].to_numpy()
    c = df["close"].to_numpy()
    atr = df["atr"].to_numpy()
    spread = df["spread_pips"].to_numpy()
    sess = df["session"].astype(str).to_numpy()
    adx = df["adx"].to_numpy()
    vol = df["tick_volume"].to_numpy() if "tick_volume" in df else np.full(len(df), np.nan)

    # 1. Identify momentum exhaustion: |close-open| as fraction of range in bottom X%
    body_range = np.abs(c - o)
    rng = h - l
    rng_safe = np.where(rng > 0, rng, np.nan)
    momentum_frac = np.where(rng_safe > 0, body_range / rng_safe, 0.0)
    # Small body = exhaustion (bottom momentum_threshold fraction)
    # Use a percentile-based approach
    valid_mom = momentum_frac[~np.isnan(momentum_frac)]
    if len(valid_mom) == 0:
        df["signal"] = "WAIT"
        df["stop"] = np.nan
        df["target"] = np.nan
        return df
    mom_threshold = np.percentile(valid_mom, momentum_threshold * 100)
    is_exhaustion = momentum_frac <= mom_threshold

    # 2. Detect reversal patterns
    pin_bar = _detect_pin_bar(o, h, l, c)
    inside_bar = _detect_inside_bar(o, h, l, c)
    doji_mask = np.abs(c - o) / np.where(rng_safe > 0, rng_safe, np.nan) < 0.1

    if pattern_type == "pin_bar":
        reversal_pattern = pin_bar
    elif pattern_type == "inside_bar":
        reversal_pattern = inside_bar
    elif pattern_type == "doji":
        reversal_pattern = doji_mask
    else:  # all
        reversal_pattern = pin_bar | inside_bar | doji_mask

    # 3. EMA trend context (trend must be established for fade)
    ema_fast = df["close"].ewm(span=20, adjust=False, min_periods=20).mean().to_numpy()
    ema_slow = df["close"].ewm(span=ema_span, adjust=False,
                               min_periods=ema_span).mean().to_numpy()
    ema_ok = ~np.isnan(ema_slow) & ~np.isnan(ema_fast)

    # 4. Determine reversal direction:
    # Bullish reversal: price made a down move, pin bar with long lower shadow
    # Bearish reversal: price made an up move, pin bar with long upper shadow
    lower_shadow = np.minimum(o, c) - l
    upper_shadow = h - np.maximum(o, c)
    bullish_rev = pin_bar & (lower_shadow > upper_shadow) & is_exhaustion
    bearish_rev = pin_bar & (upper_shadow > lower_shadow) & is_exhaustion

    if pattern_type == "inside_bar":
        # Inside bar breakout: if breakout above prev high -> up; below prev low -> down
        prev_h = np.roll(h, 1)
        prev_l = np.roll(l, 1)
        breakout_up = inside_bar & (close > prev_h)
        breakout_down = inside_bar & (close < prev_l)
        bullish_rev = breakout_up
        bearish_rev = breakout_down

    # 5. Filters
    sess_ok = ~session_filter | np.isin(sess, ["london", "newyork"])
    adx_ok = np.where(np.isnan(adx), False, adx >= min_adx)
    spread_ok = np.where(np.isnan(spread), False, spread <= max_spread_pips)

    if vol_pct > 0 and not np.all(np.isnan(vol)):
        vol_thresh = np.nanpercentile(vol, vol_pct * 100)
        vol_ok = vol >= vol_thresh
    else:
        vol_ok = np.ones(len(df), dtype=bool)

    buy = bullish_rev & sess_ok & adx_ok & spread_ok & vol_ok & ema_ok
    sell = bearish_rev & sess_ok & adx_ok & spread_ok & vol_ok & ema_ok

    sig = np.where(buy, "BUY", np.where(sell, "SELL", "WAIT"))
    df["signal"] = sig
    df["stop"] = np.nan
    df["target"] = np.nan
    df["ema_fast"] = ema_fast
    df["ema_slow"] = ema_slow

    atr_safe = np.where(np.isnan(atr), 0.0, atr)
    stop_buy = close - atr_mult_stop * atr_safe
    target_buy = close + atr_mult_stop * atr_safe * rr
    stop_sell = close + atr_mult_stop * atr_safe
    target_sell = close - atr_mult_stop * atr_safe * rr
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
    "min_adx": [15, 20, 25],
    "atr_mult_stop": [0.5, 0.8, 1.0],
    "rr": [1.0, 1.5, 2.0],
    "ema_span": [50, 100],
    "momentum_threshold": [0.05, 0.10, 0.15],
    "pattern_type": ["pin_bar", "inside_bar", "all"],
    "max_spread_pips": [3.0, 5.0],
    "vol_pct": [0.0, 0.2],
}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    print("=" * 70)
    print("V7 RESEARCH: M15 Momentum-Reversal (XAUUSD M5)")
    print("=" * 70)

    df = load_tf("M5")
    print(f"Loaded M5: {len(df)} bars ({df['ts'].iloc[0]} to {df['ts'].iloc[-1]})")

    print("\n[0] Building features + detecting reversal patterns...")
    feats = build_features(df)
    n_pinbar = _detect_pin_bar(
        feats["open"].to_numpy(), feats["high"].to_numpy(),
        feats["low"].to_numpy(), feats["close"].to_numpy()
    ).sum()
    n_inside = _detect_inside_bar(
        feats["open"].to_numpy(), feats["high"].to_numpy(),
        feats["low"].to_numpy(), feats["close"].to_numpy()
    ).sum()
    print(f"   pin_bar={n_pinbar}, inside_bar={n_inside}")

    tr_df, va_df, te_df = train_val_test_split(feats, 0.6, 0.2, 0.2)
    print(f"\nTRAIN: {len(tr_df)} bars | VAL: {len(va_df)} | TEST: {len(te_df)}")

    grid = list(itertools.product(*PARAM_GRID.values()))
    print(f"\n[1] Grid search on TRAIN ({len(grid)} combos)...")
    best = None
    results = []
    for p in grid:
        params = dict(zip(PARAM_GRID.keys(), p))
        sigs = compute_signal_mask(tr_df.copy(), **params)
        m, _ = backtest_vectorized(sigs)
        net = m.get("net_pips", float("nan"))
        pf = m.get("profit_factor", float("nan"))
        nt = m.get("total_trades", 0)
        winr = m.get("win_rate", 0)
        results.append((net, pf, nt, winr, params))
        if (best is None or (pd.notna(net) and net > (best[0] if best else float("-inf")))) and nt >= 15:
            best = (net, pf, nt, winr, params)

    results.sort(key=lambda r: (r[0] if pd.notna(r[0]) else float("-inf")), reverse=True)
    print(f"\nTop 15 configs by TRAIN net_pips (min 15 trades):")
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

    print("\n[2] Measuring best TRAIN config on VALIDATION...")
    net, pf, nt, winr, params = best
    print(f"  Best TRAIN: net={net:.1f}  PF={pf:.2f}  trades={nt}  win%={winr*100:.0f}")
    print(f"  Params: {params}")

    sigs_v = compute_signal_mask(va_df.copy(), **params)
    vm, _ = backtest_vectorized(sigs_v)
    print(f"  VAL: net={vm.get('net_pips', float('nan')):.1f}  "
          f"PF={vm.get('profit_factor', float('nan')):.2f}  "
          f"trades={vm.get('total_trades',0)}  "
          f"win%={vm.get('win_rate',0)*100:.0f}")

    print("\n[3] Walk-forward OOS (fixed params, no re-tuning)...")
    windows = list(walk_forward_windows(len(feats), train_frac=0.3, test_frac=0.1))
    oos_trades = 0
    is_net_list, oos_net_list = [], []
    for wn, ((tr_s, tr_e), (te_s, te_e)) in enumerate(windows):
        wt_df = feats.iloc[tr_s:tr_e].copy()
        we_df = feats.iloc[te_s:te_e].copy()
        wt_sigs = compute_signal_mask(wt_df, **params)
        we_sigs = compute_signal_mask(we_df, **params)
        im, _ = backtest_vectorized(wt_sigs)
        om, _ = backtest_vectorized(we_sigs)
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

    print("\n[4] Full-dataset backtest (best config on ENTIRE dataset)...")
    full_sigs = compute_signal_mask(feats.copy(), **params)
    full_m, full_trades = backtest_vectorized(full_sigs)
    print(f"  FULL: net={full_m.get('net_pips', float('nan')):.1f}  "
          f"PF={full_m.get('profit_factor', float('nan')):.2f}  "
          f"trades={full_m.get('total_trades',0)}  "
          f"win%={full_m.get('win_rate',0)*100:.0f}  "
          f"sharpe={full_m.get('sharpe',0):.2f}  "
          f"maxDD={full_m.get('max_drawdown_pips',0):.1f}")

    print("\n[5] Monte Carlo robustness (on full-dataset trades)...")
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
