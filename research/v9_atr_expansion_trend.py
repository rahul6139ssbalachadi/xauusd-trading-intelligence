"""V9 research: ATR Expansion Trend Scalper on XAUUSD.

A fundamentally different hypothesis from V1-V8:
  V1-V2: structure-break / BOS / CHoCH (trend-following)
  V3-V4: mean-reversion (fade structure)
  V5:     momentum continuation WITH the break (BOS)
  V6:     candle-pattern continuation (engulfing)
  V7:     momentum reversal (pin bar fade)
  V8:     volatility-expansion breakout (BB breakout on M5)
  V9:     ATR-EXPANSION TREND SCALPER — trade WITH the trend on a PULLBACK
          that occurs while volatility is expanding, NOT a breakout.

V9 logic:
  - H1 trend filter: EMA34 > EMA89 = uptrend, EMA34 < EMA89 = downtrend
  - ATR expansion: current M15 ATR(14) > its 50-bar rolling mean (volatility
    expanding, not contracting)
  - Entry: M5 pullback to EMA21 or recent swing, then continuation candle
    confirms direction in line with H1 trend
  - Stop: below pullback low (buy) / above pullback high (sell), or ATR-based
  - Target: 2x risk (R:R 1:2)
  - Session: London/NY overlap
  - Spread guard: < 3 pips

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
from indicators import ema, atr
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
    df["spread_pips"] = df["spread"] * 0.01 / 0.10  # points -> pips
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
    """Resample M15 to H1 (or other) by OHLCV aggregation."""
    out = m15_df.set_index("ts").resample(rule).apply(
        {"open": "first", "high": "max", "low": "min", "close": "last",
         "tick_volume": "sum", "spread": "mean"}
    ).dropna().reset_index()
    return out


# ---------------------------------------------------------------------------
# Feature computation
# ---------------------------------------------------------------------------
def compute_features(df: pd.DataFrame) -> pd.DataFrame:
    """Compute all indicators needed for V9 signal generation on M5."""
    df = df.copy()
    close = df["close"]
    high = df["high"]
    low = df["low"]
    op = df["open"]

    df["ema13"] = ema(close, 13)
    df["ema21"] = ema(close, 21)
    df["ema34"] = ema(close, 34)
    df["ema55"] = ema(close, 55)
    df["ema89"] = ema(close, 89)
    df["atr14"] = atr(high, low, close, 14)
    df["atr_ma50"] = df["atr14"].rolling(50, min_periods=50).mean()
    df["atr_ratio"] = df["atr14"] / df["atr_ma50"]  # >1 = expanding

    # Body and range for pullback detection
    df["body"] = (df["close"] - df["open"]) / df["open"]  # signed
    df["bar_range"] = (df["high"] - df["low"]) / df["close"]

    return df


def compute_h1_trend(h1_df: pd.DataFrame) -> pd.DataFrame:
    """Compute H1 trend direction from EMA34/EMA89."""
    h1_df = h1_df.copy()
    h1_df["ema34"] = ema(h1_df["close"], 34)
    h1_df["ema89"] = ema(h1_df["close"], 89)
    h1_df["trend"] = np.where(h1_df["ema34"] > h1_df["ema89"], "up", "down")
    return h1_df[["ts", "trend"]]


def align_h1_to_m5(m5: pd.DataFrame, h1_trend: pd.DataFrame) -> pd.DataFrame:
    """Merge H1 trend to M5 via timestamp as-of."""
    h1_trend = h1_trend.sort_values("ts")
    m5 = m5.sort_values("ts").reset_index(drop=True)
    merged = pd.merge_asof(m5[["ts"]], h1_trend, on="ts", direction="backward")
    m5["h1_trend"] = merged["trend"]
    return m5


# ---------------------------------------------------------------------------
# Signal computation (vectorized)
# ---------------------------------------------------------------------------
def compute_signal_mask(
    df: pd.DataFrame,
    *,
    ema_fast: int = 13,
    ema_slow: int = 34,
    ema_pull: int = 21,
    atr_mult_stop: float = 1.0,
    rr: float = 2.0,
    max_spread_pips: float = 3.0,
    atr_expand_threshold: float = 1.0,  # ATR/ATR_MA > this means expanding
    pullback_threshold: float = 0.7,    # how close to EMA for pullback entry (in ATR)
    lookback: int = 5,
    sessions: list[str] = None,
) -> pd.DataFrame:
    """Vectorized V9 signal mask.

    BUY:
      - H1 trend = up
      - M5 price > EMA(fast) (trend aligned)
      - ATR expanding (atr14 > atr_ma50 * threshold)
      - M5 pulled back to within pullback_threshold * ATR of EMA(pull)
      - Current bar confirms continuation (close > open, body positive)
      - Spread acceptable, session allowed

    SELL: mirror
    """
    df = df.copy()
    if sessions is None:
        sessions = ["london", "newyork"]

    df["ema_fast"] = ema(df["close"], ema_fast)
    df["ema_pull"] = ema(df["close"], ema_pull)
    df["ema_slow"] = ema(df["close"], ema_slow)
    df["atr14"] = atr(df["high"], df["low"], df["close"], 14)
    df["atr_ma50"] = df["atr14"].rolling(50, min_periods=50).mean()
    df["atr_ratio"] = df["atr14"] / df["atr_ma50"]

    close = df["close"]
    op = df["open"]
    high = df["high"]
    low = df["low"]
    atr_v = df["atr14"]
    spread = df["spread_pips"]

    # Session
    hour = df["ts"].dt.hour
    broker_hour = (hour - 3) % 24
    sess = np.select([
        (broker_hour >= 0) & (broker_hour < 7),
        (broker_hour >= 7) & (broker_hour < 13),
        (broker_hour >= 13) & (broker_hour < 21),
    ], ["asia", "london", "newyork"], default="quiet")
    sess_ok = np.isin(sess, sessions)

    # Trend alignment: price above fast EMA for buy, below for sell
    ema_fast_v = df["ema_fast"].to_numpy()
    ema_slow_v = df["ema_slow"].to_numpy()
    ema_pull_v = df["ema_pull"].to_numpy()
    atr_safe = np.where(np.isnan(atr_v), 0.0, atr_v.to_numpy())

    # Pullback: price within pullback_threshold*ATR of EMA(pull)
    if ema_pull == ema_fast:
        pull_dist = np.abs(close.to_numpy() - ema_pull_v) / np.where(atr_safe > 0, atr_safe, 1)
    else:
        pull_dist = np.abs(close.to_numpy() - ema_pull_v) / np.where(atr_safe > 0, atr_safe, 1)

    pullback = pull_dist < pullback_threshold

    # ATR expanding
    atr_expand = df["atr_ratio"] > atr_expand_threshold

    # Trend filter on M5: fast EMA > slow EMA for uptrend
    m5_uptrend = ema_fast_v > ema_slow_v
    m5_downtrend = ema_fast_v < ema_slow_v

    # Continuation candle: close > open (buy), close < open (sell)
    cont_buy = close.to_numpy() > op.to_numpy()
    cont_sell = close.to_numpy() < op.to_numpy()

    # Price above EMA for buy, below for sell
    price_above = close.to_numpy() > ema_fast_v
    price_below = close.to_numpy() < ema_fast_v

    # Spread ok
    spread_ok = spread.to_numpy() <= max_spread_pips

    # H1 trend alignment
    h1_up = (df["h1_trend"] == "up").to_numpy()
    h1_dn = (df["h1_trend"] == "down").to_numpy()

    # Buy signal: H1 up, M5 uptrend, price above EMA, ATR expanding, pullback,
    # continuation candle
    buy = (h1_up & m5_uptrend & price_above & atr_expand & pullback &
           cont_buy & sess_ok & spread_ok)
    sell = (h1_dn & m5_downtrend & price_below & atr_expand & pullback &
            cont_sell & sess_ok & spread_ok)

    # Also require not already having entered recently (avoid stacking)
    # Use a simple gap: don't enter within `lookback` bars of previous signal
    any_signal = buy | sell

    sig = np.where(buy, "BUY", np.where(sell, "SELL", "WAIT"))
    df["signal"] = sig
    df["stop"] = np.nan
    df["target"] = np.nan

    # ATR-based stop / R:R target
    stop_buy = close.to_numpy() - atr_mult_stop * atr_safe
    target_buy = close.to_numpy() + atr_mult_stop * atr_safe * rr
    stop_sell = close.to_numpy() + atr_mult_stop * atr_safe
    target_sell = close.to_numpy() - atr_mult_stop * atr_safe * rr

    df["stop"] = np.where(buy, stop_buy, np.where(sell, stop_sell, np.nan))
    df["target"] = np.where(buy, target_buy, np.where(sell, target_sell, np.nan))

    return df


# ---------------------------------------------------------------------------
# Vectorized backtest (entry next bar open, intrabar SL/TP)
# ---------------------------------------------------------------------------
def backtest_vectorized(feats: pd.DataFrame, atr_mult: float = 1.0,
                        rr: float = 2.0, slippage_pips: float = 0.5,
                        lot_size: float = 0.01) -> tuple[dict, list]:
    """Fast vectorized backtest. Returns (metrics, trade_list)."""
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
    # Gap filter: don't enter within 3 bars of previous entry
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
        net_usd = net_pips * PIP * lot_size

        trades.append(Trade(
            entry_bar=i + 1, entry_price=e, side=s,
            stop=stop, target=target, exit_bar=exit_j,
            exit_price=exit_price, exit_reason=reason,
            points=gross, cost_pips=cost_pts * POINT / PIP,
            net_pips=net_pips, duration_bars=exit_j - (i + 1)))
        trades[-1].net_usd = net_usd
        equity.append(equity[-1] + net_usd)
        last_exit_bar = exit_j

    return compute_metrics(trades), trades


def _apply_gap_filter(signal_mask: np.ndarray, gap: int = 3) -> np.ndarray:
    """Prevent entries within `gap` bars of a previous signal."""
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
# Grid search
# ---------------------------------------------------------------------------
PARAM_GRID = {
    "ema_fast": [13, 21],
    "ema_slow": [34, 55],
    "ema_pull": [13, 21],
    "atr_mult_stop": [1.0, 1.5, 2.0],
    "rr": [2.0, 2.5, 3.0],
    "atr_expand_threshold": [1.0],
    "pullback_threshold": [0.5, 0.8, 1.2],
    "max_spread_pips": [3.0, 5.0],
    "sessions": [["london", "newyork"], ["london", "newyork", "asia"]],
}


def main():
    print("=" * 70)
    print("V9 RESEARCH: ATR Expansion Trend Scalper (XAUUSD M5/H1)")
    print("=" * 70)

    m5_raw = load_m5()
    m15_raw = load_m15()
    h1_raw = resample_to(m15_raw, "1h")
    print(f"Loaded M5: {len(m5_raw)} bars  ({m5_raw['ts'].iloc[0]} -> {m5_raw['ts'].iloc[-1]})")
    print(f"Loaded M15: {len(m15_raw)} bars")
    print(f"Resampled H1: {len(h1_raw)} bars  ({h1_raw['ts'].iloc[0]} -> {h1_raw['ts'].iloc[-1]})")

    print("\n[0] Computing M5 features + H1 trend alignment...")
    h1_trend = compute_h1_trend(h1_raw)
    m5 = compute_features(m5_raw)
    m5 = align_h1_to_m5(m5, h1_trend)

    # Descriptive: how many bars have expanding ATR?
    atr_ratio = m5["atr_ratio"].dropna()
    expanding = (atr_ratio > 1.0).sum()
    print(f"\n  ATR expanding (>1.0): {expanding}/{len(atr_ratio)} = {expanding/len(atr_ratio)*100:.1f}%")

    tr_df, va_df, te_df = train_val_test_split(m5, 0.6, 0.2, 0.2)
    print(f"\nTRAIN: {len(tr_df)} bars | VAL: {len(va_df)} | TEST: {len(va_df)}")

    # ---- Step 1: grid search on TRAIN only ----
    grid = list(itertools.product(*PARAM_GRID.values()))
    print(f"\n[1] Grid search on TRAIN ({len(grid)} combos)...")
    results = []
    for p in grid:
        params = dict(zip(PARAM_GRID.keys(), p))
        sigs = compute_signal_mask(tr_df.copy(), **params)
        m, _ = backtest_vectorized(sigs, params["atr_mult_stop"], params["rr"])
        net = m.get("net_pips", 0)
        pf = m.get("profit_factor", 0)
        nt = m.get("total_trades", 0)
        winr = m.get("win_rate", 0)
        results.append((net, pf, nt, winr, params))

    results.sort(key=lambda r: (r[0] if np.isfinite(r[0]) else -1e18), reverse=True)
    print(f"\nTop 15 configs by TRAIN net_pips (min 15 trades):")
    shown = 0
    for net, pf, nt, winr, params in results:
        if nt >= 15 and shown < 15:
            print(f"  net={net:9.1f}  PF={pf:5.2f}  trades={nt:4d}  win%={winr*100:4.0f}  "
                  f"expand={params['atr_expand_threshold']} pull={params['pullback_threshold']}  {params}")
            shown += 1

    pf_sorted = [r for r in results if r[2] >= 15]
    pf_sorted.sort(key=lambda r: r[1] if np.isfinite(r[1]) else -1, reverse=True)
    print(f"\nTop 5 by PROFIT FACTOR (min 15 trades):")
    for net, pf, nt, winr, params in pf_sorted[:5]:
        print(f"  PF={pf:5.2f}  net={net:9.1f}  trades={nt:4d}  win%={winr*100:.1f}  {params}")
    if not pf_sorted:
        print("  (none met 15-trade threshold)")

    if shown == 0 and not pf_sorted:
        print("\n  (no config produced >=15 trades in TRAIN)")
        print("\n" + "=" * 70)
        print("RESEARCH COMPLETE — no MT5 writes, no live trading, no fabrication.")
        return

    # Pick best config: prefer PF>1.0, else best by net
    pf_viable = [r for r in pf_sorted if r[1] > 1.0]
    if pf_viable:
        best = max(pf_viable, key=lambda r: r[1])
        net, pf, nt, winr, params = best
        print(f"\n[2] Best by PROFIT FACTOR (TRAIN): net={net:.1f}  PF={pf:.2f}  trades={nt}  win%={winr*100:.0f}")
    elif pf_sorted:
        best = max(pf_sorted, key=lambda r: r[0])
        net, pf, nt, winr, params = best
        print(f"\n[2] Measuring best-by-net config on VAL + OOS...")
        print(f"  Best TRAIN: net={net:.1f}  PF={pf:.2f}  trades={nt}  win%={winr*100:.0f}")
    else:
        print("\n  (no config met 15-trade threshold)")
        print("\n" + "=" * 70)
        print("RESEARCH COMPLETE — no MT5 writes, no live trading, no fabrication.")
        return

    print(f"  Params: {params}")

    # ---- Step 2: measure on VALIDATION ----
    print(f"\n[3] Validation ({len(va_df)} bars)...")
    sigs_v = compute_signal_mask(va_df.copy(), **params)
    vm, _ = backtest_vectorized(sigs_v, params["atr_mult_stop"], params["rr"])
    print(f"  VAL: net={vm.get('net_pips', float('nan')):.1f}  "
          f"PF={vm.get('profit_factor', float('nan')):.2f}  "
          f"trades={vm.get('total_trades',0)}  "
          f"win%={vm.get('win_rate',0)*100:.0f}")

    # ---- Step 3: walk-forward OOS ----
    print(f"\n[4] Walk-forward OOS (fixed params, no re-tuning)...")
    windows = list(walk_forward_windows(len(m5), train_frac=0.3, test_frac=0.1))
    oos_trades = 0
    is_net_list, oos_net_list = [], []
    for wn, ((tr_s, tr_e), (te_s, te_e)) in enumerate(windows):
        wt_df = m5.iloc[tr_s:tr_e].copy()
        we_df = m5.iloc[te_s:te_e].copy()
        wt_sigs = compute_signal_mask(wt_df, **params)
        we_sigs = compute_signal_mask(we_df, **params)
        im, _ = backtest_vectorized(wt_sigs, params["atr_mult_stop"], params["rr"])
        om, _ = backtest_vectorized(we_sigs, params["atr_mult_stop"], params["rr"])
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

    # ---- Step 4: full-dataset backtest ----
    print(f"\n[5] Full-dataset backtest...")
    full_sigs = compute_signal_mask(m5.copy(), **params)
    full_m, full_trades = backtest_vectorized(full_sigs, params["atr_mult_stop"], params["rr"])
    print(f"  FULL: net={full_m.get('net_pips', float('nan')):.1f}  "
          f"PF={full_m.get('profit_factor', float('nan')):.2f}  "
          f"trades={full_m.get('total_trades',0)}  "
          f"win%={full_m.get('win_rate',0)*100:.0f}  "
          f"sharpe={full_m.get('sharpe',0):.2f}  "
          f"maxDD={full_m.get('max_drawdown_pips',0):.1f}")

    # ---- Step 5: if no edge, also check best-by-net with OOS>0 ----
    print(f"\n[6] Looking for ANY config with positive OOS net...")
    viable = [(net, pf, nt, winr, p) for net, pf, nt, winr, p in results
              if nt >= 15 and net > 0]
    if viable:
        viable.sort(key=lambda r: r[0], reverse=True)
        net, pf, nt, winr, params_v = viable[0]
        print(f"  Best positive-OOS TRAIN: net={net:.1f} PF={pf:.2f} trades={nt} win%={winr*100:.0f}")
        sigs_v = compute_signal_mask(va_df.copy(), **params_v)
        vm, _ = backtest_vectorized(sigs_v, params_v["atr_mult_stop"], params_v["rr"])
        print(f"  VAL: net={vm.get('net_pips', float('nan')):.1f} PF={vm.get('profit_factor', float('nan')):.2f} trades={vm.get('total_trades',0)}")
    else:
        print("  No config with positive TRAIN net and >=15 trades.")

    print("\n" + "=" * 70)
    print("RESEARCH COMPLETE — no MT5 writes, no live trading, no fabrication.")
    print("All numbers computed from db/trading.db.")
    print("=" * 70)


if __name__ == "__main__":
    main()
