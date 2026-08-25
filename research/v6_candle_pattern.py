"""V6 research: candle-pattern continuation strategy on XAUUSD.

STANDALONE research script. Reuses build_features (P5+P6), compute_metrics (P8),
walk-forward (P9), and Monte Carlo (robustness).

V6 = CANDLE-PATTERN CONTINUATION:
  The structure-break logic (V1-V5) produced zero edge. But candle-pattern
  stats showed engulfing_bearish PF=1.13 and engulfing_bullish PF=1.07 on
  2,200+ occurrences. V6 tests candle patterns as ENTRY signals with:
  - M15 EMA trend alignment (entry in direction of EMA cross)
  - M15 ADX strength filter
  - M5 engulfing/doji pattern detection on the trigger bar
  - Stop below/above pattern range, R:R target
  - Session filter (London/NY)

DIFFERENT from V1-V5: uses CANDLE PATTERNS as the entry trigger, not
structure events (BOS/CHoCH). This is a fundamentally different signal.

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
from candles import detect_engulfing, detect_doji


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------
def load_xauusd_m5() -> pd.DataFrame:
    """Load M5 XAUUSD data with spread in pips and tz-aware ts."""
    db = cfg.PROJECT_ROOT / cfg.load_settings()["paths"]["db"]
    con = sqlite3.connect(db)
    df = pd.read_sql_query(
        "SELECT ts_broker_epoch, open, high, low, close, spread, tick_volume "
        "FROM market_data WHERE symbol='XAUUSD' AND timeframe='M5' "
        "AND source='mt5' ORDER BY ts_broker_epoch", con
    )
    con.close()
    df["ts"] = pd.to_datetime(df["ts_broker_epoch"], unit="s", utc=True)
    df["spread_pips"] = df["spread"] * 0.01 / 0.10  # points -> pips
    df["tick_volume"] = df["tick_volume"].astype(float)
    return df


def load_xauusd_m15() -> pd.DataFrame:
    """Load M15 XAUUSD data with spread in pips and tz-aware ts."""
    db = cfg.PROJECT_ROOT / cfg.load_settings()["paths"]["db"]
    con = sqlite3.connect(db)
    df = pd.read_sql_query(
        "SELECT ts_broker_epoch, open, high, low, close, spread, tick_volume "
        "FROM market_data WHERE symbol='XAUUSD' AND timeframe='M15' "
        "AND source='mt5' ORDER BY ts_broker_epoch", con
    )
    con.close()
    df["ts"] = pd.to_datetime(df["ts_broker_epoch"], unit="s", utc=True)
    df["spread_pips"] = df["spread"] * 0.01 / 0.10
    df["tick_volume"] = df["tick_volume"].astype(float)
    return df


# ---------------------------------------------------------------------------
# Vectorized candle pattern detection (on M5 trigger frame)
# ---------------------------------------------------------------------------
def detect_patterns_on_frame(df: pd.DataFrame) -> pd.DataFrame:
    """Attach boolean pattern flags to the M5 frame."""
    df = df.copy()
    o = df["open"].to_numpy()
    h = df["high"].to_numpy()
    l = df["low"].to_numpy()
    c = df["close"].to_numpy()
    body = np.abs(c - o)
    rng = h - l
    rng_safe = np.where(rng == 0, np.nan, rng)

    df["engulfing_bull"] = detect_engulfing(o, h, l, c, "bullish")
    df["engulfing_bear"] = detect_engulfing(o, h, l, c, "bearish")
    df["doji"] = detect_doji(o, h, l, c, rng_safe, rng_safe)
    # Hammer / shooting star need h_arr, l_arr
    df["hammer"] = _detect_hammer(o, h, l, c)
    df["shooting_star"] = _detect_shooting_star(o, h, l, c)
    return df


def _detect_hammer(o, h, l, c):
    body = np.minimum(o, c)
    lower_shadow = body - l
    upper_shadow = h - np.maximum(o, c)
    body_size = np.abs(c - o)
    ratio = np.where(body_size == 0, np.nan, lower_shadow / body_size)
    return (ratio >= 2.0) & (upper_shadow < body_size) & (lower_shadow > 0)


def _detect_shooting_star(o, h, l, c):
    body_size = np.abs(c - o)
    upper_shadow = h - np.maximum(o, c)
    lower_shadow = np.minimum(o, c) - l
    ratio = np.where(body_size == 0, np.nan, upper_shadow / body_size)
    return (ratio >= 2.0) & (lower_shadow < body_size) & (upper_shadow > 0)


def precompute_context(trig_df: pd.DataFrame, bias_df: pd.DataFrame) -> pd.DataFrame:
    """Align M15 bias metrics to each M5 bar by timestamp (as-of, no look-ahead)."""
    bcols = ["ts", "adx", "vol_regime", "ema_fast", "ema_slow"]
    b = bias_df[bcols].sort_values("ts").reset_index(drop=True)
    trig_idx = trig_df[["ts"]].reset_index(drop=True)
    trig_idx["_orig_idx"] = trig_idx.index
    merged = pd.merge_asof(
        trig_idx.sort_values("ts"), b, on="ts", direction="backward"
    )
    merged = merged.sort_values("_orig_idx").set_index("_orig_idx")
    trig_df = trig_df.copy()
    for col in bcols:
        if col != "ts":
            trig_df[col] = merged[col].reindex(trig_df.index)
    return trig_df


# ---------------------------------------------------------------------------
# Signal computation (vectorized)
# ---------------------------------------------------------------------------
def compute_signal_mask(
    df: pd.DataFrame,
    *,
    min_adx: float = 20,
    max_spread_pips: float = 3.0,
    atr_mult_stop: float = 1.0,
    rr: float = 2.0,
    sessions: list[str] = None,
    pattern: str = "engulfing",  # "engulfing", "doji", "all"
    ema_span: int = 20,
    use_ema_align: bool = True,
) -> pd.DataFrame:
    """Vectorized V6 candle-pattern signal mask.

    BUY  : bullish engulfing (or doji in uptrend) + ADX>=min + price>EMA20
    SELL : bearish engulfing (or doji in downtrend) + ADX>=min + price<EMA20
    """
    df = df.copy()
    if sessions is None:
        sessions = ["london", "newyork"]

    close = df["close"].to_numpy()
    open_ = df["open"].to_numpy()
    atr = df["atr"].to_numpy()
    spread = df["spread_pips"].to_numpy()
    sess = df["session"].astype(str).to_numpy()
    adx = df["adx"].to_numpy()

    # Pattern selection
    if pattern == "engulfing":
        buy_pattern = df["engulfing_bull"].to_numpy()
        sell_pattern = df["engulfing_bear"].to_numpy()
    elif pattern == "doji":
        buy_pattern = df["doji"].to_numpy()
        sell_pattern = df["doji"].to_numpy()
    else:  # all
        buy_pattern = (df["engulfing_bull"] | df["doji"]).to_numpy()
        sell_pattern = (df["engulfing_bear"] | df["doji"]).to_numpy()

    # EMA alignment (trend direction filter)
    ema = df["close"].ewm(span=ema_span, adjust=False,
                         min_periods=ema_span).mean().to_numpy()
    ema_ok_buy = np.where(np.isnan(ema), False, close > ema)
    ema_ok_sell = np.where(np.isnan(ema), False, close < ema)

    # Filters
    sess_ok = np.isin(sess, sessions)
    adx_ok = np.where(np.isnan(adx), False, adx >= min_adx)
    spread_ok = np.where(np.isnan(spread), False, spread <= max_spread_pips)
    atr_safe = np.where(np.isnan(atr), 0.0, atr)

    buy = buy_pattern & sess_ok & adx_ok & spread_ok
    sell = sell_pattern & sess_ok & adx_ok & spread_ok
    if use_ema_align:
        buy = buy & ema_ok_buy
        sell = sell & ema_ok_sell

    sig = np.where(buy, "BUY", np.where(sell, "SELL", "WAIT"))
    df["signal"] = sig
    df["stop"] = np.nan
    df["target"] = np.nan

    # ATR-based stop / R:R target from entry (bar i close)
    stop_buy = close - atr_mult_stop * atr_safe
    target_buy = close + atr_mult_stop * atr_safe * rr
    stop_sell = close + atr_mult_stop * atr_safe
    target_sell = close - atr_mult_stop * atr_safe * rr
    df["stop"] = np.where(buy, stop_buy, np.where(sell, stop_sell, np.nan))
    df["target"] = np.where(buy, target_buy, np.where(sell, target_sell, np.nan))
    return df


# ---------------------------------------------------------------------------
# Vectorized backtest (entry next bar open, intrabar SL/TP)
# ---------------------------------------------------------------------------
def backtest_vectorized(feats: pd.DataFrame, atr_mult: float = 1.0,
                        rr: float = 2.0, slippage_pips: float = 0.5) -> tuple[dict, list]:
    """Fast vectorized backtest. Returns (metrics, trade_list)."""
    POINT = 0.01
    PIP = 0.10
    cost_pts_const = 2 * slippage_pips * (PIP / POINT)

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
    equity = [0.0]
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
        c_slice = c[j_start:]
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
            sp = spr[j_start + first_exit_idx]

            if stop_hit[first_exit_idx] and target_hit[first_exit_idx]:
                d_stop = abs(e - stop)
                d_target = abs(target - e)
                if d_stop <= d_target:
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
        cost_pts = 2 * sp + cost_pts_const
        net_pips = gross * POINT / PIP - cost_pts * POINT / PIP

        trades.append(Trade(
            entry_bar=i + 1, entry_price=e, side=s,
            stop=stop, target=target,
            exit_bar=exit_j, exit_price=exit_price,
            exit_reason=reason, points=gross,
            cost_pips=cost_pts * POINT / PIP, net_pips=net_pips,
            duration_bars=exit_j - (i + 1)))
        equity.append(equity[-1] + net_pips)
        last_exit_bar = exit_j

    return compute_metrics(trades), trades


# ---------------------------------------------------------------------------
# Parameter search grid — TRAIN ONLY
# ---------------------------------------------------------------------------
PARAM_GRID = {
    "pattern": ["engulfing"],
    "min_adx": [15, 20, 25],
    "atr_mult_stop": [0.5, 1.0, 1.5],
    "rr": [1.5, 2.0, 2.5],
    "ema_span": [20, 50],
    "use_ema_align": [True, False],
    "max_spread_pips": [3.0, 5.0],
    "sessions": [["london", "newyork"]],
}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    print("=" * 70)
    print("V6 RESEARCH: Candle-Pattern Continuation (XAUUSD M5+M15)")
    print("=" * 70)

    m5_raw = load_xauusd_m5()
    m15_raw = load_xauusd_m15()
    print(f"Loaded M15: {len(m15_raw)} bars  ({m15_raw['ts'].iloc[0]} to {m15_raw['ts'].iloc[-1]})")
    print(f"Loaded M5:  {len(m5_raw)} bars  ({m5_raw['ts'].iloc[0]} to {m5_raw['ts'].iloc[-1]})")

    print("\n[0] Building M15 features (EMA, RSI, ATR, ADX, structure)...")
    feats_m15 = build_features(m15_raw)
    print("[0b] Building M5 features + pattern detection + M15 context alignment...")
    feats_m5 = build_features(m5_raw)
    feats_m5 = detect_patterns_on_frame(feats_m5)
    feats_m5 = precompute_context(feats_m5, feats_m15)

    n_bull = feats_m5["engulfing_bull"].sum()
    n_bear = feats_m5["engulfing_bear"].sum()
    n_doji = feats_m5["doji"].sum()
    print(f"   Patterns: engulfing_bull={n_bull}, engulfing_bear={n_bear}, doji={n_doji}")

    tr_df, va_df, te_df = train_val_test_split(feats_m5, 0.6, 0.2, 0.2)
    print(f"\nTRAIN: {len(tr_df)} bars | VAL: {len(va_df)} | TEST: {len(te_df)}")

    # ---- Step 1: grid search on TRAIN only ----
    grid = list(itertools.product(*PARAM_GRID.values()))
    print(f"\n[1] Grid search on TRAIN ({len(grid)} combos)...")
    best = None
    results = []
    for p in grid:
        params = dict(zip(PARAM_GRID.keys(), p))
        sigs = compute_signal_mask(tr_df.copy(), **params)
        m, _ = backtest_vectorized(sigs, params["atr_mult_stop"], params["rr"])
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

    if shown == 0 and not pf_sorted:
        print("  (no config produced >=15 trades in TRAIN)")
        print("\n" + "=" * 70)
        print("RESEARCH COMPLETE — no MT5 writes, no live trading, no fabrication.")
        return

    # ---- Step 2: measure best on VALIDATION ----
    print("\n[2] Measuring best TRAIN config on VALIDATION...")
    net, pf, nt, winr, params = best
    print(f"  Best TRAIN: net={net:.1f}  PF={pf:.2f}  trades={nt}  win%={winr*100:.0f}")
    print(f"  Params: {params}")

    sigs_v = compute_signal_mask(va_df.copy(), **params)
    vm, _ = backtest_vectorized(sigs_v, params["atr_mult_stop"], params["rr"])
    print(f"  VAL: net={vm.get('net_pips', float('nan')):.1f}  "
          f"PF={vm.get('profit_factor', float('nan')):.2f}  "
          f"trades={vm.get('total_trades',0)}  "
          f"win%={vm.get('win_rate',0)*100:.0f}")

    # ---- Step 3: walk-forward OOS ----
    print("\n[3] Walk-forward OOS (fixed params, no re-tuning)...")
    windows = list(walk_forward_windows(len(feats_m5), train_frac=0.3, test_frac=0.1))
    oos_trades = 0
    is_net_list, oos_net_list = [], []
    for wn, ((tr_s, tr_e), (te_s, te_e)) in enumerate(windows):
        wt_df = feats_m5.iloc[tr_s:tr_e].copy()
        we_df = feats_m5.iloc[te_s:te_e].copy()
        wt_sigs = compute_signal_mask(wt_df, **params)
        we_sigs = compute_signal_mask(we_df, **params)
        im, _ = backtest_vectorized(wt_sigs, params["atr_mult_stop"], params["rr"])
        om, _ = backtest_vectorized(we_sigs, params["atr_mult_stop"], params["rr"])
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

    # ---- Step 4: full-dataset backtest ----
    print("\n[4] Full-dataset backtest (best config on ENTIRE dataset)...")
    full_sigs = compute_signal_mask(feats_m5.copy(), **params)
    full_m, full_trades = backtest_vectorized(full_sigs, params["atr_mult_stop"], params["rr"])
    print(f"  FULL: net={full_m.get('net_pips', float('nan')):.1f}  "
          f"PF={full_m.get('profit_factor', float('nan')):.2f}  "
          f"trades={full_m.get('total_trades',0)}  "
          f"win%={full_m.get('win_rate',0)*100:.0f}  "
          f"sharpe={full_m.get('sharpe',0):.2f}  "
          f"maxDD={full_m.get('max_drawdown_pips',0):.1f}")

    # ---- Step 5: Monte Carlo robustness ----
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
