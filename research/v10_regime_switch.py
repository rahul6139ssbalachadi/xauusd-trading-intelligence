"""V10 research: Regime-Switching Multi-Strategy Engine on XAUUSD.

A fundamentally different approach from V1-V9: instead of testing ONE
signal type, V10 classifies the market regime FIRST, then routes to the
appropriate sub-strategy. This is the "meta-strategy" approach.

V10 architecture:
  1. REGIME CLASSIFICATION (M15):
     - Volatility: ATR percentile (low/normal/high)
     - Trend: EMA34 vs EMA89 slope + ADX
     - Range: price oscillating between bounds
     - Abnormal: ATR in top 5% (news-driven spike)

  2. STRATEGY ROUTING:
     - TREND_UP:   trend-following (buy dips in EMA direction)
     - TREND_DOWN: trend-following (sell rallies in EMA direction)
     - RANGE:      mean-reversion (fade extremes within range bounds)
     - UNSURE:     WAIT (no trade)
     - ABNORMAL:   WAIT (news volatility, too risky)

  3. ENTRY (M5, in direction of regime):
     - Trend: pullback to EMA, continuation candle, ATR expansion
     - Range: bounce off support/resistance, RSI extremes, tight stop
     - Breakout: range-bound, then break direction confirmed

  4. SHARED RISK ENGINE: same stop/target/sizing logic across all sub-strategies

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
from indicators import ema, atr, rsi
from backtest import compute_metrics, Trade
from validation import train_val_test_split, walk_forward_windows

POINT = 0.01
PIP = 0.10


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------
def load_m5() -> pd.DataFrame:
    db = cfg.PROJECT_ROOT / cfg.load_settings()["paths"]["db"]
    con = sqlite3.connect(db)
    df = pd.read_sql_query(
        "SELECT ts_broker_epoch, open, high, low, close, spread, tick_volume "
        "FROM market_data WHERE symbol='XAUUSD' AND timeframe='M5' "
        "AND source='mt5' ORDER BY ts_broker_epoch", con)
    con.close()
    df["ts"] = pd.to_datetime(df["ts_broker_epoch"], unit="s", utc=True)
    df["spread_pips"] = df["spread"] * 0.01 / 0.10
    df["tick_volume"] = df["tick_volume"].astype(float)
    return df


def load_m15() -> pd.DataFrame:
    db = cfg.PROJECT_ROOT / cfg.load_settings()["paths"]["db"]
    con = sqlite3.connect(db)
    df = pd.read_sql_query(
        "SELECT ts_broker_epoch, open, high, low, close, spread, tick_volume "
        "FROM market_data WHERE symbol='XAUUSD' AND timeframe='M15' "
        "AND source='mt5' ORDER BY ts_broker_epoch", con)
    con.close()
    df["ts"] = pd.to_datetime(df["ts_broker_epoch"], unit="s", utc=True)
    return df


def resample_to(m15_df: pd.DataFrame, rule: str) -> pd.DataFrame:
    out = m15_df.set_index("ts").resample(rule).apply(
        {"open": "first", "high": "max", "low": "min", "close": "last",
         "tick_volume": "sum", "spread": "mean"}).dropna().reset_index()
    return out


# ---------------------------------------------------------------------------
# Regime classification
# ---------------------------------------------------------------------------
def classify_regime(df: pd.DataFrame,
                    vol_high_pct: float = 90.0,
                    vol_low_pct: float = 10.0,
                    adx_strong: float = 25.0,
                    adx_weak: float = 20.0) -> pd.DataFrame:
    """Classify market regime per bar. Returns df with 'regime' column.

    Regimes:
      - ABNORMAL_VOL: ATR in top (100 - vol_high_pct)% — news spike, skip all
      - TREND_UP:   EMA34 > EMA89 AND ADX >= adx_strong
      - TREND_DOWN: EMA34 < EMA89 AND ADX >= adx_strong
      - RANGE:      ADX < adx_weak (no clear trend)
      - UNSURE:     ADX between adx_weak and adx_strong (ambiguous)
    """
    df = df.copy()
    close = df["close"]
    high = df["high"]
    low = df["low"]

    df["ema34"] = ema(close, 34)
    df["ema89"] = ema(close, 89)
    df["atr14"] = atr(high, low, close, 14)
    df["atr_pct"] = df["atr14"].rank(pct=True) * 100  # 0-100 percentile

    # ADX
    from market_structure import volatility_regime as _vr
    # Compute ADX manually (already in indicators)
    a, pdi, mdi = _adx_manual(high, low, close, 14)
    df["adx"] = a

    conditions = [
        df["atr_pct"] >= vol_high_pct,
        (df["ema34"] > df["ema89"]) & (df["adx"] >= adx_strong) & (df["atr_pct"] < vol_high_pct),
        (df["ema34"] < df["ema89"]) & (df["adx"] >= adx_strong) & (df["atr_pct"] < vol_high_pct),
        (df["adx"] < adx_weak) & (df["atr_pct"] < vol_high_pct),
    ]
    choices = ["ABNORMAL_VOL", "TREND_UP", "TREND_DOWN", "RANGE"]
    df["regime"] = np.select(conditions, choices, default="UNSURE")
    return df


def _adx_manual(high, low, close, period=14):
    """Compute ADX (+DI, -DI) manually."""
    prev_high = high.shift(1)
    prev_low = low.shift(1)
    prev_close = close.shift(1)

    up_move = high - prev_high
    down_move = prev_low - low

    plus_dm = ((up_move > down_move) & (up_move > 0)).astype(float) * up_move
    minus_dm = ((down_move > up_move) & (down_move > 0)).astype(float) * down_move

    tr = pd.concat([
        (high - low),
        (high - prev_close).abs(),
        (low - prev_close).abs()
    ], axis=1).max(axis=1)

    atr_ = tr.ewm(alpha=1/period, adjust=False, min_periods=period).mean()
    plus_dm_sm = plus_dm.ewm(alpha=1/period, adjust=False, min_periods=period).mean()
    minus_dm_sm = minus_dm.ewm(alpha=1/period, adjust=False, min_periods=period).mean()

    plus_di = 100 * plus_dm_sm / atr_.replace(0.0, pd.NA)
    minus_di = 100 * minus_dm_sm / atr_.replace(0.0, pd.NA)
    dx = (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0.0, pd.NA) * 100
    adx_ = dx.ewm(alpha=1/period, adjust=False, min_periods=period).mean()
    return adx_, plus_di, minus_di


# ---------------------------------------------------------------------------
# Sub-strategy signal generators (vectorized)
# ---------------------------------------------------------------------------
def trend_signal(df: pd.DataFrame, *, ema_fast: int = 13, ema_slow: int = 34,
                 ema_pull: int = 21, atr_mult_stop: float = 1.5,
                 rr: float = 2.0, max_spread_pips: float = 3.0,
                 pullback_threshold: float = 0.8,
                 sessions: list[str] = None) -> pd.DataFrame:
    """Trend-following: pullback to EMA + continuation candle in trend direction."""
    if sessions is None:
        sessions = ["london", "newyork"]
    df = df.copy()
    df["ema_f"] = ema(df["close"], ema_fast)
    df["ema_p"] = ema(df["close"], ema_pull)
    df["ema_s"] = ema(df["close"], ema_slow)
    df["atr_v"] = atr(df["high"], df["low"], df["close"], 14)
    df["session"] = _session_col(df["ts"])

    close = df["close"].to_numpy()
    op = df["open"].to_numpy()
    atr_v = df["atr_v"].to_numpy()
    atr_safe = np.where(np.isnan(atr_v), 0.0, atr_v)

    ema_f_v = df["ema_f"].to_numpy()
    ema_s_v = df["ema_s"].to_numpy()
    ema_p_v = df["ema_p"].to_numpy()

    sess_ok = np.isin(df["session"].to_numpy(), sessions)
    spread_ok = df["spread_pips"].to_numpy() <= max_spread_pips

    # Pullback: price near EMA(pull) within pullback_threshold * ATR
    pull_dist = np.abs(close - ema_p_v) / np.where(atr_safe > 0, atr_safe, 1)
    pullback = pull_dist < pullback_threshold

    # Trend direction
    uptrend = ema_f_v > ema_s_v
    downtrend = ema_f_v < ema_s_v
    cont_buy = close > op
    cont_sell = close < op

    buy = (uptrend & pullback & cont_buy & sess_ok & spread_ok)
    sell = (downtrend & pullback & cont_sell & sess_ok & spread_ok)

    df["signal"] = np.where(buy, "BUY", np.where(sell, "SELL", "WAIT"))
    df["stop"] = np.nan
    df["target"] = np.nan
    stop_buy = close - atr_mult_stop * atr_safe
    target_buy = close + atr_mult_stop * atr_safe * rr
    stop_sell = close + atr_mult_stop * atr_safe
    target_sell = close - atr_mult_stop * atr_safe * rr
    df["stop"] = np.where(buy, stop_buy, np.where(sell, stop_sell, np.nan))
    df["target"] = np.where(buy, target_buy, np.where(sell, target_sell, np.nan))
    return df


def range_signal(df: pd.DataFrame, *, rsi_overbought: float = 70.0,
                 rsi_oversold: float = 30.0, ema_span: int = 21,
                 atr_mult_stop: float = 0.5, rr: float = 1.5,
                 max_spread_pips: float = 5.0,
                 range_window: int = 50,
                 sessions: list[str] = None) -> pd.DataFrame:
    """Mean-reversion: RSI extremes + bounce off range bounds."""
    if sessions is None:
        sessions = ["london", "newyork", "asia"]
    df = df.copy()
    df["ema_v"] = ema(df["close"], ema_span)
    df["rsi_v"] = rsi(df["close"], 14)
    df["atr_v"] = atr(df["high"], df["low"], df["close"], 14)
    df["session"] = _session_col(df["ts"])

    close = df["close"].to_numpy()
    op = df["open"].to_numpy()
    high = df["high"].to_numpy()
    low = df["low"].to_numpy()
    rsi_v = df["rsi_v"].to_numpy()
    ema_v = df["ema_v"].to_numpy()
    atr_v = df["atr_v"].to_numpy()
    atr_safe = np.where(np.isnan(atr_v), 0.0, atr_v)

    sess_ok = np.isin(df["session"].to_numpy(), sessions)
    spread_ok = df["spread_pips"].to_numpy() <= max_spread_pips

    # Range bounds: recent high/low
    hh = high  # we'll use rolling max/min
    ll = low
    upper = pd.Series(high).rolling(range_window, min_periods=range_window).max().to_numpy()
    lower = pd.Series(low).rolling(range_window, min_periods=range_window).min().to_numpy()

    # Buy: RSI oversold + price bounces off lower bound + close > open (reversal candle)
    buy_rsi = rsi_v < rsi_oversold
    buy_bounce = close > lower + 0.3 * (upper - lower)  # some distance above floor
    buy_candle = close > op
    # Sell: RSI overbought + price below upper + close < open
    sell_rsi = rsi_v > rsi_overbought
    sell_bounce = close < upper - 0.3 * (upper - lower)
    sell_candle = close < op

    buy = buy_rsi & buy_bounce & buy_candle & sess_ok & spread_ok
    sell = sell_rsi & sell_bounce & sell_candle & sess_ok & spread_ok

    df["signal"] = np.where(buy, "BUY", np.where(sell, "SELL", "WAIT"))
    df["stop"] = np.nan
    df["target"] = np.nan
    stop_buy = close - atr_mult_stop * atr_safe
    target_buy = close + atr_mult_stop * atr_safe * rr
    stop_sell = close + atr_mult_stop * atr_safe
    target_sell = close - atr_mult_stop * atr_safe * rr
    df["stop"] = np.where(buy, stop_buy, np.where(sell, stop_sell, np.nan))
    df["target"] = np.where(buy, target_buy, np.where(sell, target_sell, np.nan))
    return df


def breakout_signal(df: pd.DataFrame, *, ema_span: int = 21,
                    atr_mult_stop: float = 1.0, rr: float = 2.0,
                    max_spread_pips: float = 3.0,
                    range_window: int = 50,
                    sessions: list[str] = None) -> pd.DataFrame:
    """Breakout: price breaks out of recent range with volume confirmation."""
    if sessions is None:
        sessions = ["london", "newyork"]
    df = df.copy()
    df["ema_v"] = ema(df["close"], ema_span)
    df["atr_v"] = atr(df["high"], df["low"], df["close"], 14)
    df["session"] = _session_col(df["ts"])

    close = df["close"].to_numpy()
    op = df["open"].to_numpy()
    high = df["high"].to_numpy()
    low = df["low"].to_numpy()
    ema_v = df["ema_v"].to_numpy()
    atr_v = df["atr_v"].to_numpy()
    vol = df["tick_volume"].to_numpy()
    atr_safe = np.where(np.isnan(atr_v), 0.0, atr_v)

    sess_ok = np.isin(df["session"].to_numpy(), sessions)
    spread_ok = df["spread_pips"].to_numpy() <= max_spread_pips

    upper = pd.Series(high).rolling(range_window, min_periods=range_window).max().to_numpy()
    lower = pd.Series(low).rolling(range_window, min_periods=range_window).min().to_numpy()
    vol_ma = pd.Series(vol).rolling(range_window, min_periods=range_window).mean().to_numpy()
    vol_ok = vol >= vol_ma * 1.0  # above average volume

    # Breakout up: close above upper bound, gap or strong close
    break_up = close > upper
    break_down = close < lower
    cont_buy = close > op
    cont_sell = close < op

    buy = break_up & cont_buy & sess_ok & spread_ok & vol_ok
    sell = break_down & cont_sell & sess_ok & spread_ok & vol_ok

    df["signal"] = np.where(buy, "BUY", np.where(sell, "SELL", "WAIT"))
    df["stop"] = np.nan
    df["target"] = np.nan
    stop_buy = close - atr_mult_stop * atr_safe
    target_buy = close + atr_mult_stop * atr_safe * rr
    stop_sell = close + atr_mult_stop * atr_safe
    target_sell = close - atr_mult_stop * atr_safe * rr
    df["stop"] = np.where(buy, stop_buy, np.where(sell, stop_sell, np.nan))
    df["target"] = np.where(buy, target_buy, np.where(sell, target_sell, np.nan))
    return df


def _session_col(ts: pd.Series) -> np.ndarray:
    """Session label in BROKER time."""
    utc_hour = ts.dt.hour
    broker_hour = (utc_hour + 3) % 24
    sess = np.select([
        (broker_hour >= 0) & (broker_hour < 7),
        (broker_hour >= 7) & (broker_hour < 13),
        (broker_hour >= 13) & (broker_hour < 21),
    ], ["asia", "london", "newyork"], default="quiet")
    return sess


# ---------------------------------------------------------------------------
# Regime routing: merge M15 regime to M5, then apply sub-strategy per bar
# ---------------------------------------------------------------------------
def build_v10_signal(m5: pd.DataFrame, m15: pd.DataFrame,
                     vol_high_pct: float = 90.0, vol_low_pct: float = 10.0,
                     adx_strong: float = 25.0, adx_weak: float = 20.0,
                     **sub_params) -> pd.DataFrame:
    """Build V10 signals: classify M15 regime, route each M5 bar to sub-strategy."""
    # Classify M15 regime
    m15_regime = classify_regime(m15.copy(), vol_high_pct, vol_low_pct, adx_strong, adx_weak)
    # Merge regime to M5 by timestamp (as-of backward)
    m5 = m5.sort_values("ts").reset_index(drop=True)
    m15_reg = m15_regime[["ts", "regime", "atr14", "atr_pct", "adx", "ema34", "ema89"]].sort_values("ts")
    merged = pd.merge_asof(m5[["ts"]], m15_reg, on="ts", direction="backward")
    m5["h4_regime"] = merged["regime"].to_numpy()  # H1-level regime (from M15)

    # Also classify on M5 itself for finer signal generation
    m5_regime = classify_regime(m5.copy(), vol_high_pct, vol_low_pct, adx_strong, adx_weak)

    # Compute sub-strategy signals
    trend_df = trend_signal(m5.copy(), **{k: v for k, v in sub_params.items()
                                           if k in _trend_params()})
    range_df = range_signal(m5.copy(), **{k: v for k, v in sub_params.items()
                                          if k in _range_params()})
    breakout_df = breakout_signal(m5.copy(), **{k: v for k, v in sub_params.items()
                                                 if k in _breakout_params()})

    # Route: use regime to pick which sub-strategy signal to trust
    regime = m5_regime["regime"].to_numpy()
    trend_sig = trend_df["signal"].to_numpy()
    range_sig = range_df["signal"].to_numpy()
    breakout_sig = breakout_df["signal"].to_numpy()

    # In TREND regimes -> use trend strategy
    # In RANGE regime -> use range strategy
    # In UNSURE / ABNORMAL -> WAIT
    final_sig = np.full(len(m5), "WAIT", dtype=object)
    final_stop = np.full(len(m5), np.nan)
    final_target = np.full(len(m5), np.nan)

    # Trend regimes
    trend_mask = (regime == "TREND_UP") | (regime == "TREND_DOWN")
    final_sig[trend_mask] = trend_sig[trend_mask]
    final_stop[trend_mask] = trend_df["stop"].to_numpy()[trend_mask]
    final_target[trend_mask] = trend_df["target"].to_numpy()[trend_mask]

    # Range regime
    range_mask = regime == "RANGE"
    final_sig[range_mask] = range_sig[range_mask]
    final_stop[range_mask] = range_df["stop"].to_numpy()[range_mask]
    final_target[range_mask] = range_df["target"].to_numpy()[range_mask]

    # ABNORMAL and UNSURE remain WAIT

    m5["signal"] = final_sig
    m5["stop"] = final_stop
    m5["target"] = final_target
    m5["regime"] = regime
    return m5


def _trend_params():
    sig = inspect.signature(trend_signal)
    return set(sig.parameters.keys()) - {"df"}

def _range_params():
    sig = inspect.signature(range_signal)
    return set(sig.parameters.keys()) - {"df"}

def _breakout_params():
    sig = inspect.signature(breakout_signal)
    return set(sig.parameters.keys()) - {"df"}


import inspect  # needed at module level


# ---------------------------------------------------------------------------
# Vectorized backtest
# ---------------------------------------------------------------------------
def backtest_vectorized(feats: pd.DataFrame, slippage_pips: float = 0.5,
                        lot_size: float = 0.01) -> tuple[dict, list]:
    cost_pts_const = 2 * slippage_pips * (PIP / POINT)
    sig = feats["signal"].to_numpy()
    o = feats["open"].to_numpy()
    h = feats["high"].to_numpy()
    l = feats["low"].to_numpy()
    c = feats["close"].to_numpy()
    spr = feats["spread"].to_numpy() if "spread" in feats.columns else np.zeros(len(feats))
    stops = feats["stop"].to_numpy()
    targets = feats["target"].to_numpy()

    n = len(feats)
    entry = np.roll(o, -1)
    entry[-1] = np.nan

    signal_mask = (sig == "BUY") | (sig == "SELL")
    signal_mask[-1] = False
    signal_mask = _apply_gap_filter(signal_mask, gap=3)
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
                    reason = "stop"; exit_price = stop
                else:
                    reason = "target"; exit_price = target
            elif stop_hit[first_exit_idx]:
                reason = "stop"; exit_price = stop
            else:
                reason = "target"; exit_price = target

        gross = (exit_price - e) if s == "BUY" else (e - exit_price)
        cost_pts = 2 * sp + cost_pts_const
        net_pips = gross * POINT / PIP - cost_pts * POINT / PIP
        net_usd = net_pips * PIP * lot_size
        trades.append(Trade(
            entry_bar=i + 1, entry_price=e, side=s, stop=stop, target=target,
            exit_bar=exit_j, exit_price=exit_price, exit_reason=reason,
            points=gross, cost_pips=cost_pts * POINT / PIP, net_pips=net_pips,
            duration_bars=exit_j - (i + 1)))
        trades[-1].net_usd = net_usd
        equity.append(equity[-1] + net_usd)
        last_exit_bar = exit_j

    return compute_metrics(trades), trades


def _apply_gap_filter(signal_mask: np.ndarray, gap: int = 3) -> np.ndarray:
    result = signal_mask.copy()
    last_sig = -gap - 1
    for i in range(len(result)):
        if result[i]:
            if i - last_sig < gap:
                result[i] = False
            else:
                last_sig = i
    return result


# ---------------------------------------------------------------------------
# Parameter grid (sub-strategy params only; regime thresholds are coarse)
# ---------------------------------------------------------------------------
TREND_PARAMS = {"atr_mult_stop": [1.0, 1.5, 2.0], "rr": [2.0, 2.5, 3.0]}
RANGE_PARAMS = {"rsi_oversold": [25.0, 30.0], "rsi_overbought": [70.0, 75.0],
                "atr_mult_stop": [0.3, 0.5], "rr": [1.2, 1.5, 2.0]}
# Regime thresholds
REGIME_PARAMS = {"vol_high_pct": [90.0], "vol_low_pct": [10.0],
                 "adx_strong": [20.0, 25.0], "adx_weak": [15.0, 20.0]}


def build_param_grid():
    """Build cartesian product of all sub-strategy parameters."""
    keys = ["atr_mult_stop", "rr", "rsi_oversold", "rsi_overbought",
            "range_atr_mult", "range_rr", "adx_strong", "adx_weak"]
    vals = [
        TREND_PARAMS["atr_mult_stop"],
        TREND_PARAMS["rr"],
        RANGE_PARAMS["rsi_oversold"],
        RANGE_PARAMS["rsi_overbought"],
        RANGE_PARAMS["atr_mult_stop"],
        RANGE_PARAMS["rr"],
        REGIME_PARAMS["adx_strong"],
        REGIME_PARAMS["adx_weak"],
    ]
    combos = list(itertools.product(*vals))
    param_sets = []
    for combo in combos:
        param_sets.append({
            "atr_mult_stop": combo[0],
            "rr": combo[1],
            "rsi_oversold": combo[2],
            "rsi_overbought": combo[3],
            "range_atr_mult": combo[4],
            "range_rr": combo[5],
            "adx_strong": combo[6],
            "adx_weak": combo[7],
        })
    return param_sets


def main():
    print("=" * 70)
    print("V10 RESEARCH: Regime-Switching Multi-Strategy Engine (XAUUSD M5/M15)")
    print("=" * 70)

    m5_raw = load_m5()
    m15_raw = load_m15()
    print(f"Loaded M5: {len(m5_raw)} bars  ({m5_raw['ts'].iloc[0]} -> {m5_raw['ts'].iloc[-1]})")
    print(f"Loaded M15: {len(m15_raw)} bars  ({m15_raw['ts'].iloc[0]} -> {m15_raw['ts'].iloc[-1]})")

    # Pre-compute M15 regime classification
    print("\n[0] Pre-computing M15 regime features...")
    m15_feat = classify_regime(m15_raw.copy())
    regime_counts = m15_feat["regime"].value_counts()
    print(f"  M15 regime distribution (2 years):")
    for r, c in regime_counts.items():
        print(f"    {r}: {c} ({c/len(m15_feat)*100:.1f}%)")

    tr_df, va_df, te_df = train_val_test_split(m5_raw, 0.6, 0.2, 0.2)
    print(f"\nTRAIN: {len(tr_df)} bars | VAL: {len(va_df)} | TEST: {len(va_df)}")

    grid = build_param_grid()
    print(f"\n[1] Grid search on TRAIN ({len(grid)} combos)...")
    results = []
    for params in grid:
        m15_tr = m15_feat  # use full M15 for regime (as-of merge handles windowing)
        try:
            sigs = build_v10_signal(tr_df.copy(), m15_tr,
                                   adx_strong=params["adx_strong"],
                                   adx_weak=params["adx_weak"],
                                   vol_high_pct=params.get("vol_high_pct", 90.0),
                                   vol_low_pct=params.get("vol_low_pct", 10.0),
                                   atr_mult_stop=params["atr_mult_stop"],
                                   rr=params["rr"],
                                   rsi_oversold=params["rsi_oversold"],
                                   rsi_overbought=params["rsi_overbought"],
                                   range_atr_mult=params["range_atr_mult"],
                                   range_rr=params["range_rr"])
            m, _ = backtest_vectorized(sigs)
            net = m.get("net_pips", 0)
            pf = m.get("profit_factor", 0)
            nt = m.get("total_trades", 0)
            winr = m.get("win_rate", 0)
            results.append((net, pf, nt, winr, params))
        except Exception as e:
            print(f"  ERROR on {params}: {e}")
            continue

    results.sort(key=lambda r: (r[0] if np.isfinite(r[0]) else -1e18), reverse=True)
    print(f"\nTop 15 configs by TRAIN net_pips (min 15 trades):")
    shown = 0
    for net, pf, nt, winr, params in results:
        if nt >= 15 and shown < 15:
            print(f"  net={net:9.1f}  PF={pf:5.2f}  trades={nt:4d}  win%={winr*100:4.0f}  {params}")
            shown += 1

    pf_sorted = [r for r in results if r[2] >= 15]
    pf_sorted.sort(key=lambda r: r[1] if np.isfinite(r[1]) else -1, reverse=True)
    print(f"\nTop 5 by PROFIT FACTOR (min 15 trades):")
    for net, pf, nt, winr, params in pf_sorted[:5]:
        print(f"  PF={pf:5.2f}  net={net:9.1f}  trades={nt:4d}  win%={winr*100:.1f}  {params}")
    if not pf_sorted:
        print("  (none met 15-trade threshold)")
        print("\n" + "=" * 70)
        print("RESEARCH COMPLETE — no MT5 writes, no live trading, no fabrication.")
        return

    # Best config
    pf_viable = [r for r in pf_sorted if r[1] > 1.0]
    if pf_viable:
        best = max(pf_viable, key=lambda r: r[1])
    else:
        best = max(pf_sorted, key=lambda r: r[0])
    net, pf, nt, winr, params = best
    print(f"\n[2] Best config: net={net:.1f}  PF={pf:.2f}  trades={nt}  win%={winr*100:.0f}")
    print(f"  Params: {params}")

    # Validation
    print(f"\n[3] Validation ({len(va_df)} bars)...")
    sigs_v = build_v10_signal(va_df.copy(), m15_feat,
                              adx_strong=params["adx_strong"], adx_weak=params["adx_weak"],
                              vol_high_pct=90.0, vol_low_pct=10.0,
                              atr_mult_stop=params["atr_mult_stop"], rr=params["rr"],
                              rsi_oversold=params["rsi_oversold"], rsi_overbought=params["rsi_overbought"],
                              range_atr_mult=params["range_atr_mult"], range_rr=params["range_rr"])
    vm, _ = backtest_vectorized(sigs_v)
    print(f"  VAL: net={vm.get('net_pips', float('nan')):.1f}  "
          f"PF={vm.get('profit_factor', float('nan')):.2f}  "
          f"trades={vm.get('total_trades',0)}  "
          f"win%={vm.get('win_rate',0)*100:.0f}")

    # Walk-forward
    print(f"\n[4] Walk-forward OOS (fixed params, no re-tuning)...")
    windows = list(walk_forward_windows(len(m5_raw), train_frac=0.3, test_frac=0.1))
    oos_trades = 0
    is_net_list, oos_net_list = [], []
    for wn, ((tr_s, tr_e), (te_s, te_e)) in enumerate(windows):
        wt_df = m5_raw.iloc[tr_s:tr_e].copy()
        we_df = m5_raw.iloc[te_s:te_e].copy()
        wt_sigs = build_v10_signal(wt_df, m15_feat,
                                   adx_strong=params["adx_strong"], adx_weak=params["adx_weak"],
                                   vol_high_pct=90.0, vol_low_pct=10.0,
                                   atr_mult_stop=params["atr_mult_stop"], rr=params["rr"],
                                   rsi_oversold=params["rsi_oversold"], rsi_overbought=params["rsi_overbought"],
                                   range_atr_mult=params["range_atr_mult"], range_rr=params["range_rr"])
        we_sigs = build_v10_signal(we_df, m15_feat,
                                   adx_strong=params["adx_strong"], adx_weak=params["adx_weak"],
                                   vol_high_pct=90.0, vol_low_pct=10.0,
                                   atr_mult_stop=params["atr_mult_stop"], rr=params["rr"],
                                   rsi_oversold=params["rsi_oversold"], rsi_overbought=params["rsi_overbought"],
                                   range_atr_mult=params["range_atr_mult"], range_rr=params["range_rr"])
        im, _ = backtest_vectorized(wt_sigs)
        om, _ = backtest_vectorized(we_sigs)
        is_net_list.append(im.get("net_pips", 0))
        oos_net_list.append(om.get("net_pips", 0))
        oos_trades += om.get("total_trades", 0)
        print(f"  W{wn+1}: IS  net={im.get('net_pips',0):7.1f} (t{im.get('total_trades',0)}) "
              f"| OOS net={om.get('net_pips',0):7.1f} (t{om.get('total_trades',0)})")

    is_mean = np.nanmean(is_net_list) if is_net_list else 0
    oos_mean = np.nanmean(oos_net_list) if oos_net_list else 0
    deg = 1.0 - (oos_mean / is_mean) if is_mean else float("nan")
    print(f"\n  WF: windows={len(windows)} IS_mean={is_mean:.1f} "
          f"OOS_mean={oos_mean:.1f} deg={deg:.2f} OOS_trades={oos_trades}")

    # Full dataset
    print(f"\n[5] Full-dataset backtest...")
    full_sigs = build_v10_signal(m5_raw.copy(), m15_feat,
                                 adx_strong=params["adx_strong"], adx_weak=params["adx_weak"],
                                 vol_high_pct=90.0, vol_low_pct=10.0,
                                 atr_mult_stop=params["atr_mult_stop"], rr=params["rr"],
                                 rsi_oversold=params["rsi_oversold"], rsi_overbought=params["rsi_overbought"],
                                 range_atr_mult=params["range_atr_mult"], range_rr=params["range_rr"])
    full_m, full_trades = backtest_vectorized(full_sigs)
    print(f"  FULL: net={full_m.get('net_pips', float('nan')):.1f}  "
          f"PF={full_m.get('profit_factor', float('nan')):.2f}  "
          f"trades={full_m.get('total_trades',0)}  "
          f"win%={full_m.get('win_rate',0)*100:.0f}  "
          f"sharpe={full_m.get('sharpe',0):.2f}  "
          f"maxDD={full_m.get('max_drawdown_pips',0):.1f}")

    # Regime breakdown on full dataset
    print(f"\n[6] Regime breakdown (full dataset signals)...")
    regime_counts = full_sigs["regime"].value_counts()
    buy_by_regime = full_sigs[full_sigs["signal"] == "BUY"]["regime"].value_counts()
    sell_by_regime = full_sigs[full_sigs["signal"] == "SELL"]["regime"].value_counts()
    for r in regime_counts.index:
        print(f"  {r}: bars={regime_counts[r]}, buys={buy_by_regime.get(r,0)}, sells={sell_by_regime.get(r,0)}")

    # Monte Carlo
    print(f"\n[7] Monte Carlo robustness (on full-dataset trades)...")
    if full_trades:
        from montecarlo import run_monte_carlo, MCConfig
        mc = run_monte_carlo(full_trades, MCConfig(n_iterations=500))
        print(f"  net_mean={mc.net_mean:.1f}  net_p5={mc.net_p5:.1f}  "
              f"PF_mean={mc.profit_factor_mean:.2f}  PF_p5={mc.profit_factor_p5:.2f}  "
              f"ruin_prob={mc.ruin_prob:.2%}  robust={mc.is_robust}")

    print("\n" + "=" * 70)
    print("RESEARCH COMPLETE — no MT5 writes, no live trading, no fabrication.")
    print("All numbers computed from db/trading.db.")
    print("=" * 70)


if __name__ == "__main__":
    main()
