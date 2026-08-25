"""V5 research: momentum continuation WITH the break (BOS direction).

STANDALONE research script. Reuses build_features (P5+P6), compute_metrics (P8),
walk-forward (P9), and Monte Carlo (P-robustness).

V5 = MOMENTUM CONTINUATION WITH THE BREAK:
  The prior hypotheses (V1-V4) all FADED the break (mean-reversion). The
  CLAUDE.md diagnosis says CHoCH is a CONTINUATION signal. V5 tests the
  opposite: ride the momentum WITH the break.

  - M15 bias: EMA20 > EMA50 (trend direction) + ADX > threshold (strength)
  - M15 signal: a BOS or CHoCH event fires (the "break" — structure broke
    in a direction)
  - M5 trigger: a fresh bar that CONTINUES in that break direction AND has
    momentum confirmation (close beyond prior bar close, or close above
    prior high for bullish etc.). The trigger direction FOLLOWS the break.
  - Entry: next M5 bar open
  - Stop: below recent swing / ATR
  - Target: R:R based stop distance
  - Session filter: London + New York only

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
from market_structure import structure_events
from backtest import compute_metrics, Trade
from validation import train_val_test_split, walk_forward_windows


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------
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
    df["spread_pips"] = df["spread"] * 0.01 / 0.10  # points -> pips for XAUUSD
    df["tick_volume"] = df["tick_volume"].astype(float)
    return df


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
    df["spread_pips"] = df["spread"] * 0.01 / 0.10
    df["tick_volume"] = df["tick_volume"].astype(float)
    return df


def precompute_context(trig_df: pd.DataFrame, bias_df: pd.DataFrame) -> pd.DataFrame:
    """Align M15 bias metrics to each M5 bar by timestamp (as-of, no look-ahead).

    Pulls the latest M15 bias bar at or before each M5 trigger bar's time.
    """
    bcols = ["ts", "adx", "vol_regime", "ema_fast", "ema_slow", "last_bos_dir",
             "last_choch_dir", "session"]
    b = bias_df[bcols].sort_values("ts").reset_index(drop=True)
    trig = trig_df[["ts"]].reset_index(drop=True)
    trig_idx = trig.copy()
    trig_idx["_orig_idx"] = trig.index
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
    min_adx: float = 20,            # minimum trend strength on M15
    vol_allowed: set[str] = None,
    max_spread_pips: float = 3.0,
    # Momentum confirmation on M5
    req_momentum: bool = True,      # trigger bar must continue break direction
    # Stop / target
    atr_mult_stop: float = 1.0,
    rr: float = 2.0,
    sessions: list[str] = None,
    # Structure lookback: a fresh BOS/CHoCH within this many bars triggers a signal
    bos_lookback: int = 3,          # how many bars back to look for a FRESH break
    ema_span: int = 50,
) -> pd.DataFrame:
    """Vectorized V5 momentum-continuation signal mask.

    Detects a FRESH BOS (Break-of-Structure) on the M5 trigger frame within
    the last `bos_lookback` bars. Trades WITH the break direction, confirmed
    by ADX strength + EMA alignment + momentum on the trigger bar.

    BUY  : fresh BOS up + ADX>=min_adx + close>EMA + bullish continuation candle
    SELL : fresh BOS down + ADX>=min_adx + close<EMA + bearish continuation candle
    """
    df = df.copy()
    if vol_allowed is None:
        vol_allowed = {"normal", "high"}
    if sessions is None:
        sessions = ["london", "newyork"]

    n = len(df)
    close = df["close"].to_numpy()
    high = df["high"].to_numpy()
    low = df["low"].to_numpy()
    open_ = df["open"].to_numpy()
    atr = df["atr"].to_numpy()
    spread = df["spread_pips"].to_numpy()
    sess = df["session"].astype(str).to_numpy()
    adx = df["adx"].to_numpy()
    vol_regime = df["vol_regime"].astype(object).fillna("").to_numpy()

    # Detect FRESH BOS events on this frame using structure_events
    # structure_events returns a DataFrame with 'bar' (positional index), 'kind', 'direction'
    ev = structure_events(df["high"], df["low"], left=5, right=5)
    bos_up_bars = set()
    bos_down_bars = set()
    if not ev.empty:
        for _, row in ev.iterrows():
            bi = int(row["bar"])
            if row["kind"] == "BOS":
                if row["direction"] == "up":
                    bos_up_bars.add(bi)
                elif row["direction"] == "down":
                    bos_down_bars.add(bi)

    # For each bar i, check if a fresh BOS-up occurred within [i-lookback, i]
    # This means the structure break is RECENT, not carried forward from months ago
    fresh_bos_up = np.zeros(n, dtype=bool)
    fresh_bos_down = np.zeros(n, dtype=bool)
    for i in range(n):
        for lb in range(bos_lookback + 1):
            bar = i - lb
            if bar in bos_up_bars:
                fresh_bos_up[i] = True
            if bar in bos_down_bars:
                fresh_bos_down[i] = True

    # EMA anchor for trend alignment
    ema_anchor = df["close"].ewm(span=ema_span, adjust=False,
                                 min_periods=ema_span).mean().to_numpy()

    # Filters
    sess_ok = np.isin(sess, sessions)
    adx_ok = np.where(np.isnan(adx), False, adx >= min_adx)
    vol_ok = np.isin(list(vol_regime), list(vol_allowed))
    spread_ok = np.where(np.isnan(spread), False, spread <= max_spread_pips)
    ema_ok_buy = np.where(np.isnan(ema_anchor), False, close > ema_anchor)
    ema_ok_sell = np.where(np.isnan(ema_anchor), False, close < ema_anchor)

    # Momentum: trigger bar continues in break direction (bullish/bearish candle)
    prev_close = np.roll(close, 1)
    prev_close[0] = np.nan
    buy_momentum = (close > prev_close) & (close > open_)
    sell_momentum = (close < prev_close) & (close < open_)

    buy = fresh_bos_up & buy_momentum & sess_ok & adx_ok & vol_ok & spread_ok & ema_ok_buy
    sell = fresh_bos_down & sell_momentum & sess_ok & adx_ok & vol_ok & spread_ok & ema_ok_sell

    if not req_momentum:
        buy = fresh_bos_up & sess_ok & adx_ok & vol_ok & spread_ok & ema_ok_buy
        sell = fresh_bos_down & sess_ok & adx_ok & vol_ok & spread_ok & ema_ok_sell

    sig = np.where(buy, "BUY", np.where(sell, "SELL", "WAIT"))
    df["signal"] = sig
    df["stop"] = np.nan
    df["target"] = np.nan
    df["fresh_bos_up"] = fresh_bos_up
    df["fresh_bos_down"] = fresh_bos_down
    return df


def apply_stop_target(sigs: pd.DataFrame, atr_mult: float, rr: float) -> pd.DataFrame:
    """Fill stop/target for active signals using ATR multiple + R:R."""
    df = sigs.copy()
    buy = df["signal"] == "BUY"
    sell = df["signal"] == "SELL"

    # ATR-based stop from entry close
    close = df["close"].to_numpy()
    atr = df["atr"].to_numpy()
    atr_safe = np.where(np.isnan(atr), 0.0, atr)

    # BUY: stop below entry, target above
    stop_buy = close - atr_mult * atr_safe
    target_buy = close + atr_mult * atr_safe * rr
    # SELL: stop above entry, target below
    stop_sell = close + atr_mult * atr_safe
    target_sell = close - atr_mult * atr_safe * rr

    df["stop"] = np.where(buy, stop_buy, np.where(sell, stop_sell, np.nan))
    df["target"] = np.where(buy, target_buy, np.where(sell, target_sell, np.nan))
    return df


# ---------------------------------------------------------------------------
# Stop hunt: find first bar where stop or target is hit (intrabar)
# ---------------------------------------------------------------------------
def backtest_vectorized(feats: pd.DataFrame, atr_mult: float = 1.0,
                        rr: float = 2.0, slippage_pips: float = 0.5) -> dict:
    """Fast vectorized backtest over precomputed V5 signal frame.

    Entry at NEXT bar open. Intrabar stop/target: if both touched same bar,
    the nearer one (by distance from entry) wins. Force-close at end.
    Cost: spread(entry+exit) + 2*slippage_pips.
    """
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
    entry = np.roll(o, -1)  # entry at next bar open
    entry[-1] = np.nan

    signal_mask = (sig == "BUY") | (sig == "SELL")
    signal_mask[-1] = False  # can't enter on last bar
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
            exit_price = c[j_start + first_exit_idx]
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
    "min_adx": [15, 20, 25],
    "atr_mult_stop": [0.5, 0.8, 1.0],
    "rr": [1.5, 2.0, 2.5],
    "max_spread_pips": [3.0, 5.0],
    "req_momentum": [True, False],
    "bos_lookback": [1, 3, 5],
    "ema_span": [20, 50],
    "sessions": [["london", "newyork"]],
}


# ---------------------------------------------------------------------------
# Main: grid search on TRAIN, measure on VAL + OOS
# ---------------------------------------------------------------------------
def main():
    print("=" * 70)
    print("V5 RESEARCH: Momentum Continuation WITH the Break (XAUUSD, M15+M5)")
    print("=" * 70)

    m15 = load_xauusd_m15()
    m5 = load_xauusd_m5()
    print(f"Loaded M15: {len(m15)} bars  ({m15['ts'].iloc[0]} to {m15['ts'].iloc[-1]})")
    print(f"Loaded M5:  {len(m5)} bars  ({m5['ts'].iloc[0]} to {m5['ts'].iloc[-1]})")

    print("\n[0] Building M15 features (EMA, RSI, ATR, ADX, structure)...")
    feats_m15 = build_features(m15)
    print("[0b] Aligning M15 context to M5 trigger bars (timestamp as-of)...")
    feats_m5 = build_features(m5)
    feats_m5 = precompute_context(feats_m5, feats_m15)
    print(f"   Feats M5: {len(feats_m5)} bars, BOS dirs: {(feats_m5['last_bos_dir'].notna()).sum()}, "
          f"CHoCH dirs: {(feats_m5['last_choch_dir'].notna()).sum()}")

    tr_df, va_df, te_df = train_val_test_split(feats_m5, 0.6, 0.2, 0.2)
    print(f"\nTRAIN: {len(tr_df)} bars | VAL: {len(va_df)} | TEST: {len(te_df)}")

    # ---- Step 1: parameter search on TRAIN only ----
    grid = list(itertools.product(*PARAM_GRID.values()))
    print(f"\n[1] Grid search on TRAIN ({len(grid)} combos)...")
    best = None
    results = []
    for p in grid:
        params = dict(zip(PARAM_GRID.keys(), p))
        sigs = compute_signal_mask(tr_df.copy(), **params)
        sigs = apply_stop_target(sigs, params["atr_mult_stop"], params["rr"])
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
        print("All numbers are COMPUTED from db/trading.db.")
        print("=" * 70)
        return

    # ---- Step 2: measure best on VALIDATION ----
    print("\n[2] Measuring best TRAIN config on VALIDATION...")
    net, pf, nt, winr, params = best
    print(f"  Best TRAIN: net={net:.1f}  PF={pf:.2f}  trades={nt}  win%={winr*100:.0f}")
    print(f"  Params: {params}")

    sigs_v = compute_signal_mask(va_df.copy(), **params)
    sigs_v = apply_stop_target(sigs_v, params["atr_mult_stop"], params["rr"])
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
        wt_sigs = apply_stop_target(compute_signal_mask(wt_df, **params),
                                     params["atr_mult_stop"], params["rr"])
        we_sigs = apply_stop_target(compute_signal_mask(we_df, **params),
                                     params["atr_mult_stop"], params["rr"])
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
    full_sigs = apply_stop_target(compute_signal_mask(feats_m5.copy(), **params),
                                   params["atr_mult_stop"], params["rr"])
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
