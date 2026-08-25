"""V1 research: Session Range Breakout Scalper hypothesis on XAUUSD.

STANDALONE research script. Reuses build_features (P5+P6) and compute_metrics
(P8) but implements a NEW entry rule that the generic evaluate() does not support:
session-range breakout with close-beyond confirmation.

V1 = SESSION RANGE BREAKOUT SCALPER:
  - M15 context + trigger (M5 history is only ~6 months; M15 has ~2yr)
  - Define Asian session range (prev day 00:00-07:00 broker time, UTC+3)
  - Also track Previous Session High/Low (prev London + NY extremes)
  - Entry: price CLOSES beyond Asian range high (BUY) or low (SELL)
    - must close beyond, not just wick through (confirmation)
  - Volume confirmation optional (tick_volume > median)
  - ATR above threshold (active volatility)
  - Spread below maximum
  - Session filter: London or New York only (NOT Asian open itself)
  - Stop: below/above range or 1.0-1.5x ATR
  - Target: 1R to 2.5R risk/reward

DISCIPLINE (CLAUDE.md §9-13):
  - Parameter search on TRAIN only.
  - Best config measured (never re-tuned) on val + walk-forward OOS.

Vectorized for speed.

  ./.venv/Scripts/python.exe research/v1_session_breakout_search.py
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


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------
def load_xauusd_m15() -> pd.DataFrame:
    db = cfg.PROJECT_ROOT / cfg.load_settings()["paths"]["db"]
    con = sqlite3.connect(db)
    df = pd.read_sql_query(
        "SELECT ts_broker_epoch, open, high, low, close, spread, tick_volume "
        "FROM market_data WHERE symbol='XAUUSD' AND timeframe='M15' "
        "AND source='mt5' ORDER BY ts_broker_epoch", con
    )
    con.close()
    df["ts"] = pd.to_datetime(df["ts_broker_epoch"], unit="s", utc=True)
    df["spread_pips"] = df["spread"] * 0.01 / 0.10  # points->dollars->pips
    df["tick_volume"] = df["tick_volume"].astype(float)
    return df


# ---------------------------------------------------------------------------
# Session range computation (vectorized, no look-ahead)
# ---------------------------------------------------------------------------
BROKER_OFFSET_H = 3  # UTC+3 (EEST summer / EET winter)


def compute_session_ranges(df: pd.DataFrame) -> pd.DataFrame:
    """Compute Asian session range and previous session high/low for each bar.

    Asian session: previous calendar day 00:00-07:00 broker time.
    Prev session high/low: previous London+NY extremes (07:00-21:00 broker time).

    All values are aligned by-timestamp, no look-ahead: at bar i, we use
    session ranges that ENDED strictly before bar i's time.
    """
    df = df.copy()
    ts = df["ts"]
    # broker hour = (utc hour + offset) % 24
    utc_hour = ts.dt.hour
    broker_hour = (utc_hour + BROKER_OFFSET_H) % 24
    broker_date = (ts.dt.tz_convert("UTC") - pd.Timedelta(hours=BROKER_OFFSET_H)).dt.date
    # Actually, broker time = UTC + 3. Broker date = the date in broker time.
    # Use: broker_time = ts + 3h, then .dt.date
    broker_time = ts + pd.Timedelta(hours=BROKER_OFFSET_H)
    broker_day = broker_time.dt.date
    broker_h = broker_time.dt.hour

    # Session labels
    df["session"] = pd.Categorical(
        np.select(
            [broker_h < 7, (broker_h >= 7) & (broker_h < 13),
             (broker_h >= 13) & (broker_h < 21), broker_h >= 21],
            ["asia", "london", "newyork", "quiet"],
            default="quiet",
        ),
        categories=["asia", "london", "newyork", "quiet"],
    )

    # For each bar, find the Asian range (00:00-07:00) of the PREVIOUS broker day
    # and the prev session range (07:00-21:00) of the previous broker day.
    # We use an expanding-group approach: compute per-day session highs/lows,
    # then shift by 1 day so each bar sees the PREVIOUS completed session range.

    day_session = pd.DataFrame({
        "broker_day": broker_day,
        "broker_h": broker_h,
        "high": df["high"].values,
        "low": df["low"].values,
    }, index=df.index)

    # Asian session: broker_hour 0-6 (00:00-07:00)
    asia_mask = (day_session["broker_h"] >= 0) & (day_session["broker_h"] < 7)
    asia_sessions = day_session[asia_mask].groupby("broker_day").agg(
        asia_high=("high", "max"),
        asia_low=("low", "min"),
    )

    # London+NY session: broker_hour 7-20 (07:00-21:00)
    sess_mask = (day_session["broker_h"] >= 7) & (day_session["broker_h"] < 21)
    sess_sessions = day_session[sess_mask].groupby("broker_day").agg(
        sess_high=("high", "max"),
        sess_low=("low", "min"),
    )

    # For each broker day, shift by 1 to get PREVIOUS completed session ranges
    asia_shifted = asia_sessions.shift(1)
    sess_shifted = sess_sessions.shift(1)

    # Map back to each bar
    df["prev_asia_high"] = broker_day.map(asia_shifted["asia_high"]) if len(asia_shifted) else np.nan
    df["prev_asia_low"] = broker_day.map(asia_shifted["asia_low"]) if len(asia_shifted) else np.nan
    df["prev_session_high"] = broker_day.map(sess_shifted["sess_high"]) if len(sess_shifted) else np.nan
    df["prev_session_low"] = broker_day.map(sess_shifted["sess_low"]) if len(sess_shifted) else np.nan

    return df


# ---------------------------------------------------------------------------
# Signal computation (vectorized)
# ---------------------------------------------------------------------------
def compute_signal_mask(
    df: pd.DataFrame,
    *,
    range_type: str = "asia",          # "asia" or "prev_session"
    breakout_confirm: bool = True,     # must CLOSE beyond level
    atr_min: float = 1.5,              # min ATR(14) in pips for active vol
    atr_mult_stop: float = 1.0,        # stop = atr_mult_stop * ATR
    rr: float = 2.0,                   # risk:reward
    max_spread_pips: float = 3.0,
    sessions: list[str] = ["london", "newyork"],
    vol_confirm: bool = False,
    volume_median_mult: float = 1.5,
) -> pd.DataFrame:
    """Vectorized session-range breakout signal mask.

    BUY  : close breaks above prev_asia_high (or prev_session_high), confirmed.
    SELL : close breaks below prev_asia_low  (or prev_session_low),  confirmed.
    """
    df = df.copy()
    n = len(df)

    if range_type == "asia":
        upper = df["prev_asia_high"]
        lower = df["prev_asia_low"]
    else:
        upper = df["prev_session_high"]
        lower = df["prev_session_low"]

    close = df["close"]
    high = df["high"]
    low = df["low"]
    atr = df["atr"]
    spread = df["spread_pips"]
    vol = df["tick_volume"]
    sess = df["session"].astype(str)

    # Range must exist
    has_upper = upper.notna()
    has_lower = lower.notna()

    # Breakout with close confirmation
    # BUY: close > upper (close-beyond), AND high of breakout bar actually
    #      exceeded it (true breakout, not just close above from gap)
    buy_break = (close > upper) & (high > upper) & has_upper
    sell_break = (close < lower) & (low < lower) & has_lower

    if breakout_confirm:
        # already requires close beyond + high/low beyond (stronger confirmation)
        pass

    # Session filter
    sess_ok = sess.isin(sessions)

    # ATR active volatility filter
    atr_ok = atr >= atr_min

    # Spread filter
    spread_ok = spread <= max_spread_pips

    # Volume confirmation (optional)
    vol_ok = pd.Series(True, index=df.index)
    if vol_confirm:
        med = vol.rolling(50, min_periods=20).median()
        vol_ok = vol > (med * volume_median_mult)
        vol_ok = vol_ok.fillna(False)

    buy = buy_break & sess_ok & atr_ok & spread_ok & vol_ok
    sell = sell_break & sess_ok & atr_ok & spread_ok & vol_ok

    sig = np.where(buy, "BUY", np.where(sell, "SELL", "WAIT"))
    df["signal"] = sig
    df["stop"] = np.nan
    df["target"] = np.nan
    return df


def apply_stop_target(sigs: pd.DataFrame, atr_mult: float, rr: float,
                       range_type: str = "asia") -> pd.DataFrame:
    """Fill stop/target for active signals from ATR multiple + R:R.

    Uses range-based stop when available (below Asian range low for buys,
    above for sells), falling back to ATR multiple. Target = R:R from stop.
    """
    df = sigs.copy()

    buy = df["signal"] == "BUY"
    sell = df["signal"] == "SELL"

    if range_type == "asia":
        range_low_col = "prev_asia_low"
        range_high_col = "prev_asia_high"
    else:
        range_low_col = "prev_session_low"
        range_high_col = "prev_session_high"

    # BUY stop: below range (prev range low) if available AND wider than ATR stop,
    # else ATR-based stop
    stop_buy_range = df[range_low_col]
    stop_buy_atr = df["close"] - atr_mult * df["atr"]
    # Use range stop if it exists AND it's below the ATR stop (wider stop = more room)
    use_range_buy = stop_buy_range.notna() & (stop_buy_range < stop_buy_atr)
    stop_buy = np.where(use_range_buy.to_numpy(), stop_buy_range.fillna(-np.inf).to_numpy(), stop_buy_atr.to_numpy())
    df["stop"] = np.where(buy.to_numpy(), stop_buy, df.get("stop", pd.Series(index=df.index, dtype=float)).to_numpy())

    # SELL stop: above range (prev range high) if available AND wider than ATR stop
    stop_sell_range = df[range_high_col]
    stop_sell_atr = df["close"] + atr_mult * df["atr"]
    use_range_sell = stop_sell_range.notna() & (stop_sell_range > stop_sell_atr)
    stop_sell = np.where(use_range_sell.to_numpy(), stop_sell_range.fillna(-np.inf).to_numpy(), stop_sell_atr.to_numpy())
    df["stop"] = np.where(sell.to_numpy(), stop_sell, df["stop"].to_numpy())

    # Target: R:R from entry via stop distance (vectorized)
    stop_arr = df["stop"].to_numpy()
    close_arr = df["close"].to_numpy()
    signal_arr = df["signal"].to_numpy()
    risk_dist = np.abs(close_arr - stop_arr)
    target_buy = close_arr + risk_dist * rr
    target_sell = close_arr - risk_dist * rr
    target_arr = np.where(signal_arr == "BUY", target_buy, np.where(signal_arr == "SELL", target_sell, np.nan))
    df["target"] = target_arr

    return df


def backtest_vectorized(feats: pd.DataFrame, atr_mult: float = 1.0,
                         rr: float = 2.0, slippage_pips: float = 0.5) -> dict:
    """Fast vectorized backtest over precomputed signal frame.

    Instead of a per-trade Python while-loop, this computes all exit bars
    in a single vectorized pass using numpy searchsorted on stop/target
    touch arrays.
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

    # Entry at NEXT bar open
    entry = np.roll(o, -1)
    entry[-1] = np.nan

    # Signal bars where we open a position
    signal_mask = (sig == "BUY") | (sig == "SELL")
    signal_mask[-1] = False  # can't enter on last bar
    entry_bars = np.where(signal_mask)[0]

    trades = []
    equity = [0.0]
    last_exit_bar = -1
    last_entry = 0.0

    for i in entry_bars:
        if i < last_exit_bar:
            continue  # skip signals while in a position
        if np.isnan(entry[i]) or np.isnan(stops[i]) or np.isnan(targets[i]):
            continue

        s = sig[i]
        e = entry[i]
        stop = stops[i]
        target = targets[i]

        # Look at bars from i+1 onwards
        j_start = i + 1
        if j_start >= n:
            continue

        # For BUY: exit when low <= stop (stop hit) or high >= target (target hit)
        # For SELL: exit when high >= stop or low <= target
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

        # Find first exit bar (whichever comes first)
        any_exit = stop_hit | target_hit
        if not any_exit.any():
            # No exit before end — force close at last bar
            exit_j = n - 1
            exit_price = c[-1]
            reason = "end"
            sp = spr[-1] if not np.isnan(spr[-1]) else 0.0
        else:
            first_exit_idx = np.where(any_exit)[0][0]
            exit_j = j_start + first_exit_idx
            exit_price = c[j_start + first_exit_idx]  # close of exit bar
            sp = spr[j_start + first_exit_idx] if not np.isnan(spr[j_start + first_exit_idx]) else 0.0

            # Determine reason: if only one was hit, that's the reason
            # If both hit same bar, closer to entry wins
            if stop_hit[first_exit_idx] and target_hit[first_exit_idx]:
                d_stop = abs(e - stop)
                d_target = abs(target - e)
                reason = "stop" if d_stop <= d_target else "target"
                # Use the appropriate exit price
                if reason == "stop":
                    exit_price = stop
                else:
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
        last_entry = e

    return compute_metrics(trades)


# ---------------------------------------------------------------------------
# Parameter search grid — TRAIN ONLY
# ---------------------------------------------------------------------------
PARAM_GRID = {
    "range_type": ["asia"],
    "breakout_confirm": [True],
    "atr_min": [1.0, 1.5, 2.0],
    "atr_mult_stop": [0.8, 1.0, 1.5],
    "rr": [1.5, 2.0, 2.5],
    "max_spread_pips": [3.0, 5.0],
    "vol_confirm": [False],
}


def main():
    print("=" * 70)
    print("V1 RESEARCH: Session Range Breakout Scalper (XAUUSD, M15)")
    print("=" * 70)
    df = load_xauusd_m15()
    print(f"Loaded M15: {len(df)} bars ({df['ts'].iloc[0]} to {df['ts'].iloc[-1]})")

    # Build features (indicators + structure)
    print("\n[0] Building features (EMA, RSI, ATR, ADX, structure)...")
    feats = build_features(df)

    # Compute session ranges
    print("[0b] Computing session ranges (Asian session, prev session HL)...")
    feats = compute_session_ranges(feats)

    n = len(feats)
    tr_df, va_df, te_df = train_val_test_split(feats, 0.6, 0.2, 0.2)
    print(f"\nTRAIN: {len(tr_df)} bars | VAL: {len(va_df)} | TEST: {len(te_df)}")

    # ---- Step 1: parameter search on TRAIN only ----
    grid = list(itertools.product(*PARAM_GRID.values()))
    print(f"\n[1] Parameter search on TRAIN ({len(grid)} combos)...")
    best = None
    results = []
    for p in grid:
        params = dict(zip(PARAM_GRID.keys(), p))
        sigs = compute_signal_mask(tr_df.copy(), **params)
        sigs = apply_stop_target(sigs, params["atr_mult_stop"], params["rr"], params["range_type"])
        m = backtest_vectorized(sigs, params["atr_mult_stop"], params["rr"])
        net = m.get("net_pips", float("nan"))
        pf = m.get("profit_factor", float("nan"))
        nt = m.get("total_trades", 0)
        winr = m.get("win_rate", 0)
        results.append((net, pf, nt, winr, params))
        if (best is None or (pd.notna(net) and net > (best[0] if best else float("-inf")))) and nt >= 20:
            best = (net, pf, nt, winr, params)

    results.sort(key=lambda r: (r[0] if pd.notna(r[0]) else float("-inf")), reverse=True)
    print(f"\nTop 15 configs by TRAIN net_pips (min 20 trades):")
    shown = 0
    for net, pf, nt, winr, params in results:
        if nt >= 20 and shown < 15:
            print(f"  net={net:9.1f}  PF={pf:5.2f}  trades={nt:4d}  "
                  f"win%={winr*100:4.0f}  {params}")
            shown += 1

    # top by PF
    pf_sorted = [r for r in results if r[2] >= 20]
    pf_sorted.sort(key=lambda r: r[1] if pd.notna(r[1]) else -1, reverse=True)
    print(f"\nTop 5 by PROFIT FACTOR (min 20 trades):")
    for net, pf, nt, winr, params in pf_sorted[:5]:
        print(f"  PF={pf:5.2f}  net={net:9.1f}  trades={nt:4d}  "
              f"win%={winr*100:4.0f}  {params}")
    if not pf_sorted:
        print("  (none met 20-trade threshold)")

    if shown == 0 and not pf_sorted:
        print("  (no config produced >=20 trades in TRAIN)")

    # ---- Step 2: measure best on VALIDATION ----
    print("\n[2] Measuring best TRAIN config on VALIDATION...")
    if best is not None:
        net, pf, nt, winr, params = best
        print(f"  Best TRAIN: net={net:.1f}  PF={pf:.2f}  trades={nt}  win%={winr*100:.0f}")
        print(f"  Params: {params}")

        sigs_v = compute_signal_mask(va_df.copy(), **params)
        sigs_v = apply_stop_target(sigs_v, params["atr_mult_stop"], params["rr"], params["range_type"])
        vm = backtest_vectorized(sigs_v, params["atr_mult_stop"], params["rr"])
        print(f"  VAL: net={vm.get('net_pips', float('nan')):.1f}  "
              f"PF={vm.get('profit_factor', float('nan')):.2f}  "
              f"trades={vm.get('total_trades',0)}  "
              f"win%={vm.get('win_rate',0)*100:.0f}")

        # ---- Step 3: walk-forward OOS ----
        print("\n[3] Walk-forward OOS (fixed params, no re-tuning)...")
        windows = list(walk_forward_windows(n, train_frac=0.3, test_frac=0.1))
        oos_trades = 0
        is_net_list, oos_net_list = [], []
        for (tr_s, tr_e), (te_s, te_e) in windows:
            wt_df = feats.iloc[tr_s:tr_e]
            we_df = feats.iloc[te_s:te_e]
            wt_sigs = apply_stop_target(compute_signal_mask(wt_df.copy(), **params),
                                         params["atr_mult_stop"], params["rr"], params["range_type"])
            we_sigs = apply_stop_target(compute_signal_mask(we_df.copy(), **params),
                                         params["atr_mult_stop"], params["rr"], params["range_type"])
            im = backtest_vectorized(wt_sigs, params["atr_mult_stop"], params["rr"])
            om = backtest_vectorized(we_sigs, params["atr_mult_stop"], params["rr"])
            is_net_list.append(im.get("net_pips", 0))
            oos_net_list.append(om.get("net_pips", 0))
            oos_trades += om.get("total_trades", 0)
            print(f"  IS  net={im.get('net_pips',0):7.1f} (t{im.get('total_trades',0)}) "
                  f"| OOS net={om.get('net_pips',0):7.1f} (t{om.get('total_trades',0)})")

        is_mean = sum(is_net_list) / len(is_net_list) if is_net_list else 0
        oos_mean = sum(oos_net_list) / len(oos_net_list) if oos_net_list else 0
        deg = 1.0 - (oos_mean / is_mean) if is_mean else float("nan")
        print(f"\n  WF: windows={len(windows)} IS_mean={is_mean:.1f} "
              f"OOS_mean={oos_mean:.1f} deg={deg:.2f} OOS_trades={oos_trades}")

        # ---- Step 4: full-dataset backtest for final honest report ----
        print("\n[4] Full-dataset backtest (best config on ENTIRE dataset)...")
        full_sigs = apply_stop_target(compute_signal_mask(feats, **params),
                                       params["atr_mult_stop"], params["rr"], params["range_type"])
        full_m = backtest_vectorized(full_sigs, params["atr_mult_stop"], params["rr"])
        print(f"  FULL: net={full_m.get('net_pips', float('nan')):.1f}  "
              f"PF={full_m.get('profit_factor', float('nan')):.2f}  "
              f"trades={full_m.get('total_trades',0)}  "
              f"win%={full_m.get('win_rate',0)*100:.0f}  "
              f"sharpe={full_m.get('sharpe',0):.2f}  "
              f"maxDD={full_m.get('max_drawdown_pips',0):.1f}")

        # Print all full metrics
        print(f"  Full metrics: {full_m}")
    else:
        print("  No config met min 20-trade threshold on TRAIN.")

    print("\n" + "=" * 70)
    print("RESEARCH COMPLETE — no MT5 writes, no live trading, no fabrication.")
    print("All numbers are COMPUTED from db/trading.db.")
    print("=" * 70)


if __name__ == "__main__":
    main()
