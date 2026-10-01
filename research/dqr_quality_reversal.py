"""DQR — Daily Quality Reversal (Liquidity Sweep + HTF Level + Momentum Confirmation)

Converts a DISCRETIONARY idea into an objective, deterministic, backtestable spec.

SPEC COVERAGE
  STEP 1  Important-level detection      -> build_d1_levels() / level availability
  STEP 2  Liquidity sweep detection      -> detect_sweep()
  STEP 3  Rejection confirmation         -> detect_sweep() (close_position, body dir)
  STEP 4+ AGENT-DESIGNED (owner spec was truncated after STEP 3).
         Every such rule is flagged AGENT_DESIGNED in LABELLED_DEFAULTS below.
         These are MY choices, not the owner's, and are open to revision.

NO LOOK-AHEAD
  * A D1 level only becomes usable AFTER the D1 bar that defines it has CLOSED
    (t >= d1_open + 1 day). Verified by build_d1_levels() -> known_from_ts.
  * Sweep + confirmation are detected on CLOSED execution-timeframe bars only.
  * Entry is at the OPEN of the bar AFTER the confirmation bar closes.
  * Exits are simulated on subsequent bars; no future data enters a decision.

MULTIPLE DEFINITIONS, NO WINNER-PICKING
  Three independent level families and three independent sweep-strength
  definitions are evaluated and REPORTED SIDE BY SIDE (see FAMILY_GRID).
  Selection, if any, is done on TRAIN only and then measured on VAL/OOS.

DATA HONESTY
  XAUUSD / GOLD.i# (XM demo) in db/trading.db:
    D1  2577 bars  2016-09-02 .. 2026-08-28   (10 yr)
    H1  59313 bars 2016-09-01 .. 2026-08-28   (10 yr)
    M15 48200 bars 2024-08-13 .. 2026-08-28   (2 yr ONLY)
  Execution on M15 (the owner's stated setup TF) is therefore a 2-YEAR test.
  Execution on H1 is offered as a 10-year cross-check of the same logic, not
  as a substitute — the two are reported separately and never merged.

Read-only: queries db/trading.db only. No MT5, no orders, no fabrication.

Usage:
    ./.venv/Scripts/python.exe research/dqr_quality_reversal.py
"""
from __future__ import annotations

import itertools
import sqlite3
import sys
from datetime import timedelta
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from market_data import config as cfg
from indicators import atr
from backtest import compute_metrics, Trade
from validation import train_val_test_split, walk_forward_windows
from montecarlo import run_monte_carlo, MCConfig

DB = cfg.PROJECT_ROOT / cfg.load_settings()["paths"]["db"]

# Per-symbol contract. `point` is the broker's minimum price increment (MT5
# `symbol_info.point`); costs are then derived in PRICE UNITS, never in
# hard-coded "pips", because a BTC pip is ~$10 while a gold pip is $0.10.
#
#   XAUUSD -> GOLD.i#  point 0.01, pip 0.10, spread 15 pts  = $0.15/side
#   BTCUSD -> BTC/USD  point 0.01, spread 2250 pts = $22.5/side (0.032%)
#
# BTCUSD costs are ~4x gold's as a fraction of price, which materially changes
# which strategies are viable. Costs are read from the DB spread column where
# present, so these are fallbacks, not overrides.
SYMBOLS = {
    "XAUUSD": dict(btc=False, point=0.01, pip=0.10, fallback_spread_pips=3.0,
                   fallback_slippage_pips=1.0),
    "BTCUSD": dict(btc=True, point=0.01, pip=1.00, fallback_spread_pips=225.0,
                   fallback_slippage_pips=5.0),
}

DAY = pd.Timedelta(days=1)

# --------------------------------------------------------------------------
# AGENT-DESIGNED DEFAULTS (owner's spec truncated after STEP 3)
# --------------------------------------------------------------------------
LABELLED_DEFAULTS = {
    "entry": "AGENT_DESIGNED: entry at OPEN of the bar following the "
             "confirmation bar (closest possible no-look-ahead entry).",
    "stop": "AGENT_DESIGNED: stop beyond the sweep extreme by atr_mult_stop "
            "x execution-TF ATR, so the stop sits where the sweep thesis is "
            "provably wrong rather than at an arbitrary distance.",
    "target": "AGENT_DESIGNED: fixed R multiple (rr), NOT the next level. "
              "Level-based targets are an obvious variant to add later.",
    "exit": "AGENT_DESIGNED: stop / target / max_holding_bars, checked "
            "intrabar, closer level wins ties (pessimistic on same-bar ties).",
    "confirmation": "AGENT_DESIGNED: an impulsive bar in the trade direction "
                    "immediately after the sweep bar (body >= impulse_atr x "
                    "ATR and closing past the sweep bar's opposite extreme).",
    "decisive_break": "AGENT_DESIGNED: if any of the last break_lookback "
                      "closed execution bars closed decisively beyond the level "
                      "by break_atr x ATR, the level is considered BROKEN and "
                      "the setup is skipped (spec item 6: reassess, not trade).",
    "cooldown": "AGENT_DESIGNED: no new entry within cooldown_bars of the "
                "previous entry; overlapping sweep clusters are one idea.",
    "level_age": "AGENT_DESIGNED: a level expires after max_level_age_d1 D1 "
                 "bars. Older untested levels are noise, not structure.",
}

# level families: independent definitions, reported side by side
FAMILY_GRID = {
    "pdh_pdl": dict(
        desc="Prior D1 high / prior D1 low only (fewest parameters)",
        swing_left=0, swing_right=0, min_touches=1,
    ),
    "swings": dict(
        desc="Confirmed D1 swing highs/lows (left/right fractal, "
             "right bars required -> no look-ahead)",
        swing_left=2, swing_right=2, min_touches=1,
    ),
    "touches": dict(
        desc="D1 swing levels; min_touches is a SIGNAL parameter applied "
             "dynamically at signal time (levels are re-tested over time), "
             "tried at 1, 2, 3",
        swing_left=2, swing_right=2, min_touches=1, touch_atr=0.30,
    ),
}

# sweep-strength families: independent definitions of a REAL sweep
SWEEP_FAMILIES = {
    "any_penetration": dict(min_sweep_atr=0.00),
    "clean_penetration": dict(min_sweep_atr=0.10),
    "deep_penetration": dict(min_sweep_atr=0.25),
}


# ==========================================================================
# DATA
# ==========================================================================
def load(tf: str, symbol: str = "XAUUSD") -> pd.DataFrame:
    con = sqlite3.connect(DB)
    df = pd.read_sql_query(
        "SELECT ts_broker_epoch, open, high, low, close, tick_volume, spread "
        "FROM market_data WHERE symbol=? AND timeframe=? AND source='mt5' "
        "ORDER BY ts_broker_epoch",
        con, params=(symbol, tf))
    con.close()
    # tz-naive UTC throughout: levels are compared/sorted by timestamp in
    # several places and mixing naive/aware raises. Broker offset is irrelevant
    # for these comparisons (all bars share one clock).
    df["ts"] = pd.to_datetime(df["ts_broker_epoch"], unit="s")
    return df.reset_index(drop=True)


def build_exec_features(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["atr14"] = atr(df["high"], df["low"], df["close"], 14)
    df["rng"] = df["high"] - df["low"]
    df["body"] = df["close"] - df["open"]
    df["body_atr"] = df["body"].abs() / df["atr14"]
    df["close_pos"] = np.where(df["rng"] > 0,
                               (df["close"] - df["low"]) / df["rng"], 0.5)
    df["bar"] = np.arange(len(df))
    return df


# ==========================================================================
# STEP 1 — LEVEL DETECTION (D1), with explicit availability time
# ==========================================================================
def build_d1_levels(d1: pd.DataFrame, family: dict) -> list[dict]:
    """Return D1 levels, each with the timestamp from which it is USABLE.

    A level defined by D1 bar k becomes usable only at d1.ts[k] + 1 day
    (when bar k has closed on the broker's daily boundary).
    """
    fam = FAMILY_GRID[family] if isinstance(family, str) else family
    is_pdh_pdl = (fam["swing_left"] == 0 and fam["swing_right"] == 0)
    n = len(d1)
    highs = d1["high"].to_numpy()
    lows = d1["low"].to_numpy()
    ts = d1["ts"].to_numpy()
    L, R = fam["swing_left"], fam["swing_right"]
    tol_for_touches = 0.30   # provisional ATR-agnostic price bucket; refined
                             # by ATR at availability time (see level_touch_pct)

    raw: list[dict] = []
    for k in range(n):
        known_from = pd.Timestamp(ts[k]) + DAY
        if is_pdh_pdl:
            if k < 2:
                continue
            raw.append(dict(price=highs[k - 1], kind="R", known_from=known_from,
                            born=k))
            raw.append(dict(price=lows[k - 1], kind="S", known_from=known_from,
                            born=k))
        else:
            if k < L + R + 1:
                continue
            if R and highs[k] == max(highs[k - L:k + R + 1]):
                raw.append(dict(price=highs[k], kind="R", known_from=known_from,
                                born=k))
            if R and lows[k] == min(lows[k - L:k + R + 1]):
                raw.append(dict(price=lows[k], kind="S", known_from=known_from,
                                born=k))
    return raw


def enrich_levels(d1: pd.DataFrame, raw: list[dict], *,
                  min_touches: int, touch_atr: float = 0.30) -> list[dict]:
    """Compute level_strength (touch count) and level_age support.

    A touch of level P by D1 bar j requires |close_j - P| within
    touch_atr x ATR_j, and j <= (last D1 bar that had closed by
    known_from) - i.e. strictly historical, never future.

    NOTE: at known_from only the defining bar itself is in scope, so this
    function returns the level with strength=1 as a PLACEHOLDER. The real
    touch count is DYNAMIC (a level can be retested months later) and is
    computed in detect_signals() via d1_touch_count(), which only ever sees
    D1 bars that have already closed.
    """
    atr_series = atr(d1["high"], d1["low"], d1["close"], 14).to_numpy()
    closes = d1["close"].to_numpy()
    ts = d1["ts"].to_numpy()
    out = []
    for lv in raw:
        born = lv["born"]
        known_from = pd.Timestamp(lv["known_from"])
        # last D1 index fully closed at known_from
        hist = np.searchsorted(ts, np.datetime64(known_from), side="left")
        hist = int(np.clip(hist - 1, 0, len(d1) - 1))
        # touch count is DYNAMIC -> handled at signal time (see docstring).
        # min_touches is therefore enforced in detect_signals via
        # min_level_touches, not here; do NOT silently drop levels.
        out.append(dict(price=float(lv["price"]), kind=lv["kind"],
                        known_from=known_from, born_d1=born,
                        level_strength=1,
                        level_age_d1=hist - born))
    return out


class D1TouchCounter:
    """Caching touch counter.

    Recomputing ATR inside d1_touch_count() on every signal is O(bars*levels)
    ATR passes and made the grid unusably slow. Precompute the D1 arrays once,
    then answer a query in one vectorised pass over the CLOSED prefix only.
    Touch counts only change when a new D1 bar closes, so results are memoised
    per (rounded price, last-closed-D1-index).
    """

    def __init__(self, d1: pd.DataFrame, touch_atr: float = 0.30):
        self.a = atr(d1["high"], d1["low"], d1["close"], 14).to_numpy()
        self.c = d1["close"].to_numpy()
        self.t = d1["ts"].to_numpy()
        self.touch_atr = touch_atr
        self._cache: dict = {}
        self._ok: np.ndarray = (~np.isnan(self.a)) & (self.a > 0)

    def count(self, price: float, up_to: pd.Timestamp) -> int:
        hist = int(np.searchsorted(
            self.t, np.datetime64(pd.Timestamp(up_to)), side="left")) - 1
        if hist < 0:
            return 0
        key = (round(price, 2), hist)
        hit = self._cache.get(key)
        if hit is None:
            ok = self._ok[: hist + 1]
            hit = int((ok & (np.abs(self.c[: hist + 1] - price)
                              <= self.touch_atr * self.a[: hist + 1])).sum())
            self._cache[key] = hit
        return hit


def d1_touch_count(d1: pd.DataFrame, price: float, up_to: pd.Timestamp, *,
                   touch_atr: float = 0.30) -> int:
    """Count D1 closes that came within touch_atr*ATR of `price`, using ONLY
    D1 bars whose close time is <= up_to (no look-ahead).

    Convenience single-shot wrapper; use D1TouchCounter inside hot loops.
    """
    return D1TouchCounter(d1, touch_atr).count(price, up_to)


# ==========================================================================
# STEP 2/3 — SWEEP + REJECTION CONFIRMATION (execution TF)
# ==========================================================================
def detect_signals(exec_df: pd.DataFrame, levels: list[dict], *,
                   d1: pd.DataFrame,
                   touch_counter: "D1TouchCounter | None" = None,
                   min_sweep_atr: float,
                   level_tol_atr: float,
                   close_pos_min: float,
                   sweep_lookback: int,
                   impulse_atr: float,
                   break_lookback: int,
                   break_atr: float,
                   max_level_age_d1: int,
                   max_level_dist_atr: float,
                   min_level_touches: int = 1,
                   touch_atr: float = 0.30) -> list[dict]:
    """Return raw candidate entries (no holding/cooldown applied yet).

    LONG  : exec low < level - tol*ATR (support swept) AND close > level
            (rejection) AND close_position >= close_pos_min AND bullish body
    SHORT : mirrored
    Confirmation: the bar right after the sweep bar is impulsive in the trade
    direction (body >= impulse_atr*ATR, close past the sweep bar's far side).
    Entry: open of the bar AFTER the confirmation bar.
    """
    if exec_df.empty or not levels:
        return []

    e = exec_df
    o = e["open"].to_numpy()
    h = e["high"].to_numpy()
    l = e["low"].to_numpy()
    c = e["close"].to_numpy()
    b = e["body"].to_numpy()
    a = e["atr14"].to_numpy()
    ts = e["ts"].to_numpy()
    cp = e["close_pos"].to_numpy()
    ba = e["body_atr"].to_numpy()
    n = len(e)
    if n < 60:
        return []

    # order levels by availability for an incremental pointer
    lv_sorted = sorted(levels, key=lambda x: x["known_from"])

    signals: list[dict] = []
    ptr = 0
    active: list[dict] = []          # levels usable at current bar
    last_used_ts: dict[float, int] = {}
    act_price = np.empty(0)          # vectorised price view of `active`
    act_meta: list[dict] = []

    for i in range(5, n):
        now = pd.Timestamp(ts[i])
        while ptr < len(lv_sorted) and lv_sorted[ptr]["known_from"] <= now:
            active.append(lv_sorted[ptr])
            ptr += 1
        if (i % 50 == 0 or not active) and active:
            act_price = np.array([x["price"] for x in active])
            act_meta = list(active)
        if not active:
            continue
        if np.isnan(a[i - 1]) or a[i - 1] <= 0:
            continue

        atr_now = a[i - 1]
        px = c[i - 1]

        # ---------- candidate levels: vectorised distance filter ----------
        near = np.nonzero(np.abs(act_price - px) <= max_level_dist_atr * atr_now)[0]
        if near.size == 0:
            continue
        cands = [act_meta[k] for k in near]

        # ---------- decisive-break guard (spec item 6) ----------
        if break_lookback > 0:
            seg_c = c[max(0, i - 1 - break_lookback): i]
            seg_a = np.nanmean(a[max(0, i - 1 - break_lookback): i])
            if not np.isnan(seg_a) and seg_a > 0:
                min_c, max_c = seg_c.min(), seg_c.max()
                for lv in cands:
                    broken = ((min_c < lv["price"] - break_atr * seg_a)
                              if lv["kind"] == "S" else
                              (max_c > lv["price"] + break_atr * seg_a))
                    if broken:
                        break
                else:
                    cands = []
            if not cands:
                continue

        # ---------- sweep search over the last sweep_lookback bars ----------
        for j in range(i - 1, max(0, i - 1 - sweep_lookback), -1):
            atr_j = a[j]
            if np.isnan(atr_j) or atr_j <= 0:
                continue
            # confirmation bar = j+1, entry bar = j+2
            if j + 2 > n - 1:
                continue
            for lv in cands:
                age_d1 = (now - pd.Timestamp(lv["known_from"])).days
                if age_d1 > max_level_age_d1:
                    continue
                # dynamic touch count using only D1 bars closed by `now`
                if min_level_touches > 1:
                    tc = (touch_counter or D1TouchCounter(d1, touch_atr)).count(
                        lv["price"], now)
                    if tc < min_level_touches:
                        continue
                else:
                    tc = 1

                # ---- LONG: swept support, closed back above ----
                if lv["kind"] == "S":
                    pen = (lv["price"] - l[j]) / atr_j          # >0 = penetrated
                    if pen < max(min_sweep_atr, level_tol_atr):
                        continue
                    if c[j] <= lv["price"]:
                        continue
                    if b[j] <= 0:
                        continue
                    if cp[j] < close_pos_min:
                        continue
                    conf = j + 1
                    if ba[conf] < impulse_atr or b[conf] <= 0:
                        continue
                    if c[conf] <= h[j]:
                        continue
                    side = "BUY"
                    wick = (min(c[j], o[j]) - l[j]) / atr_j
                    entry_price = o[j + 2]
                else:
                    pen = (h[j] - lv["price"]) / atr_j
                    if pen < max(min_sweep_atr, level_tol_atr):
                        continue
                    if c[j] >= lv["price"]:
                        continue
                    if b[j] >= 0:
                        continue
                    if (1.0 - cp[j]) < close_pos_min:
                        continue
                    conf = j + 1
                    if ba[conf] < impulse_atr or b[conf] >= 0:
                        continue
                    if c[conf] >= l[j]:
                        continue
                    side = "SELL"
                    wick = (h[j] - max(c[j], o[j])) / atr_j
                    entry_price = o[j + 2]

                if not np.isfinite(entry_price) or entry_price <= 0:
                    continue

                # stop_anchor = the sweep extreme (thesis invalidation point).
                # atr_mult_stop / rr are applied by the BACKTEST, not here, so
                # the signal set is independent of exit parameters.
                stop_anchor = l[j] if side == "BUY" else h[j]
                atr_conf = a[conf]
                if np.isnan(atr_conf) or atr_conf <= 0:
                    continue

                key = round(lv["price"], 2)
                prev = last_used_ts.get(key)
                if prev is not None and (i - prev) < 10:
                    continue
                last_used_ts[key] = i

                signals.append(dict(
                    signal_bar=int(j + 2), entry_bar=int(j + 2),
                    entry_ts=pd.Timestamp(ts[j + 2]),
                    sweep_ts=pd.Timestamp(ts[j]),
                    side=side, level_price=float(lv["price"]),
                    level_kind=lv["kind"],
                    level_strength=int(tc),
                    level_age_d1=int(age_d1),
                    penetration_atr=float(pen),
                    sweep_wick_atr=float(wick),
                    candle_range=float(l[j] if side == "SELL" else h[j]),
                    close_position=float(cp[j]),
                    atr_normalized_sweep=float(pen),
                    stop_anchor=float(stop_anchor),
                    atr_at_conf=float(atr_conf),
                    entry_price=float(entry_price),
                ))
    return signals


# ==========================================================================
# BACKTEST (execution TF bars, intrabar stop/target, pessimistic ties)
# ==========================================================================
def backtest_signals(exec_df: pd.DataFrame, signals: list[dict], *,
                     rr: float, atr_mult_stop: float,
                     max_holding_bars: int, cooldown_bars: int,
                     spread_pips: float, slippage_pips: float,
                     pip: float = 0.10) -> tuple:
    """Simulate signals on execution-TF bars.

    Costs are handled in PRICE UNITS (cost_price) and converted to pips only
    for reporting, so the same code is correct for gold (pip 0.10) and BTC
    (pip 1.00). The per-bar DB `spread` column is authoritative when present;
    spread_pips/slippage_pips are fallbacks.
    """
    e = exec_df
    o = e["open"].to_numpy(); h = e["high"].to_numpy()
    l = e["low"].to_numpy(); c = e["close"].to_numpy()
    spread_pts = (e["spread"].to_numpy() if "spread" in e else None)
    n = len(e)
    fallback_spread_price = spread_pips * pip
    slip_price = slippage_pips * pip

    trades: list[Trade] = []
    blocked = 0
    last_entry = -10**9
    for s in signals:
        i = s["entry_bar"]
        if i >= n - 1:
            continue
        if i - last_entry < cooldown_bars:
            continue
        side, ep = s["side"], s["entry_price"]
        # stop beyond the sweep extreme (the thesis-invalidation point)
        anchor = s["stop_anchor"]
        atr_conf = s["atr_at_conf"]
        stop = (anchor - atr_mult_stop * atr_conf if side == "BUY"
                else anchor + atr_mult_stop * atr_conf)
        if side == "BUY" and stop >= ep:
            continue          # stop already breached at entry -> invalid geometry
        if side == "SELL" and stop <= ep:
            continue
        risk = abs(ep - stop)
        if risk <= 0:
            continue
        target = (ep + rr * risk if side == "BUY" else ep - rr * risk)
        maxj = min(i + max_holding_bars, n - 1)
        if maxj <= i:
            continue
        exit_price = reason = None
        for j in range(i + 1, maxj + 1):
            hj, lj = h[j], l[j]
            if side == "BUY":
                sh, th = lj <= stop, hj >= target
                if sh and th:
                    if abs(ep - stop) <= abs(target - ep):
                        exit_price, reason = stop, "stop"
                    else:
                        exit_price, reason = target, "target"
                elif sh:
                    exit_price, reason = stop, "stop"
                elif th:
                    exit_price, reason = target, "target"
                else:
                    continue
                gross = exit_price - ep
            else:
                sh, th = hj >= stop, lj <= target
                if sh and th:
                    if abs(ep - stop) <= abs(target - ep):
                        exit_price, reason = stop, "stop"
                    else:
                        exit_price, reason = target, "target"
                elif sh:
                    exit_price, reason = stop, "stop"
                elif th:
                    exit_price, reason = target, "target"
                else:
                    continue
                gross = ep - exit_price
            break
        if exit_price is None:
            j = maxj
            exit_price, reason = c[j], "end"
            gross = (exit_price - ep) if side == "BUY" else (ep - exit_price)

        # Round-trip cost in PRICE units: spread paid twice (entry+exit) plus
        # slippage twice. DB spread is in broker points (multiply by `point`).
        if spread_pts is not None and not np.isnan(spread_pts[i]):
            spr = spread_pts[i] * 0.01          # point = 0.01 for both symbols
        else:
            spr = fallback_spread_price
        cost_price = 2 * spr + 2 * slip_price
        cost = cost_price / pip                 # pips, for Trade/metrics
        net = gross / pip - cost
        trades.append(Trade(
            entry_bar=i, entry_price=ep, side=side, stop=stop, target=target,
            exit_bar=j, exit_price=exit_price, exit_reason=reason,
            points=gross / 0.01, cost_pips=cost, net_pips=net,
            duration_bars=j - i))
        last_entry = i
    return trades, compute_metrics(trades)


# ==========================================================================
# PARAMETER SPACE (signal params searched; exit params reported separately)
# ==========================================================================
SIGNAL_GRID = {
    "level_tol_atr":   [0.10, 0.25],
    "close_pos_min":   [0.50, 0.70],
    "impulse_atr":     [0.40, 0.80],
    "sweep_lookback":  [1, 3],
    "break_lookback":  [0, 5],
    "break_atr":       [0.50],
    "max_level_age_d1": [120],
    "max_level_dist_atr": [3.0],
    "min_level_touches": [1, 2],
}
EXIT_GRID = {
    "atr_mult_stop":   [1.0, 1.5, 2.0],
    "rr":              [1.5, 2.0, 3.0],
    "max_holding_bars": [8, 16, 32],
    "cooldown_bars":   [4],
}


def fmt(m: dict) -> str:
    if not m or m.get("total_trades", 0) == 0:
        return "no trades"
    return (f"net={m.get('net_pips', float('nan')):9.1f} "
            f"PF={m.get('profit_factor', float('nan')):5.2f} "
            f"t={m['total_trades']:4d} "
            f"win%={m.get('win_rate', 0)*100:4.0f} "
            f"sharpe={m.get('sharpe', 0):5.2f} "
            f"maxDD={m.get('max_drawdown_pips', 0):8.1f}")


def run_family_family(exec_df: pd.DataFrame, d1: pd.DataFrame,
                      exec_tf: str, spec: dict) -> dict:
    pip = spec["pip"]
    spr_pips = spec["fallback_spread_pips"]
    slip_pips = spec["fallback_slippage_pips"]
    counter = D1TouchCounter(d1, touch_atr=0.30)
    print("=" * 78)
    print(f"DQR RESEARCH :: execution TF = {exec_tf} "
          f"({exec_df['ts'].iloc[0].date()} .. {exec_df['ts'].iloc[-1].date()}, "
          f"{len(exec_df)} bars)")
    print("=" * 78)

    tr_df, va_df, te_df = train_val_test_split(exec_df, 0.6, 0.2, 0.2)
    print(f"split by TIME: TRAIN {tr_df['ts'].iloc[0].date()}..{tr_df['ts'].iloc[-1].date()} | "
          f"VAL {va_df['ts'].iloc[0].date()}..{va_df['ts'].iloc[-1].date()} | "
          f"TEST {te_df['ts'].iloc[0].date()}..{te_df['ts'].iloc[-1].date()}")
    # Build levels on the FULL D1 series but they are availability-gated, so
    # slicing the execution frame cannot leak the future (memory note: build
    # features on full data, slice by time).
    raw_all = {fam: build_d1_levels(d1, FAMILY_GRID[fam]) for fam in FAMILY_GRID}
    levels_all = {
        fam: enrich_levels(d1, raw_all[fam],
                           min_touches=FAMILY_GRID[fam]["min_touches"],
                           touch_atr=FAMILY_GRID[fam].get("touch_atr", 0.30))
        for fam in FAMILY_GRID}
    for fam in FAMILY_GRID:
        ns = sum(1 for x in levels_all[fam] if x["kind"] == "S")
        print(f"  levels[{fam:>10}] = {len(levels_all[fam]):5d} "
              f"(support {ns}, resistance {len(levels_all[fam])-ns})  "
              f"-> {FAMILY_GRID[fam]['desc']}")
    print()

    report: dict = {}
    for fam, fdef in FAMILY_GRID.items():
        for sname, sdef in SWEEP_FAMILIES.items():
            tag = f"{fam}/{sname}"
            key = dict(SIGNAL_GRID)
            key["min_sweep_atr"] = [sdef["min_sweep_atr"]]
            if fam != "touches":
                key["min_level_touches"] = [1]   # only meaningful for 'touches'
            combos = list(itertools.product(*key.values()))
            knames = list(key.keys())

            rows = []
            for combo in combos:
                p = dict(zip(knames, combo))
                sigs = detect_signals(tr_df, levels_all[fam], d1=d1,
                                      touch_counter=counter, **p)
                best_exit, best_net, best_m = None, -float("inf"), None
                for ex in itertools.product(*EXIT_GRID.values()):
                    epar = dict(zip(EXIT_GRID.keys(), ex))
                    _, m = backtest_signals(
                        tr_df, sigs, spread_pips=spr_pips, slippage_pips=slip_pips,
                        pip=pip, **epar)
                    net = m.get("net_pips", float("nan")) if m.get("total_trades") else -float("inf")
                    if m.get("total_trades", 0) >= 5 and pd.notna(net) and net > best_net:
                        best_net, best_exit, best_m = net, epar, m
                rows.append((best_net, best_exit,
                             best_m if best_m else {"total_trades": 0},
                             p, len(sigs)))
            rows.sort(key=lambda r: r[0], reverse=True)
            n_any = max(r[4] for r in rows)
            print(f"--- {tag}: max TRAIN signals across grid = {n_any}")
            if rows[0][0] == -float("inf"):
                print("    NO config produced >=5 TRAIN trades. Hypothesis "
                      "does not fire often enough on this data. INCONCLUSIVE.\n")
                report[tag] = None
                continue
            for net, ex, m, p, ns in rows[:3]:
                print(f"    TRAIN net={net:8.1f} PF={m.get('profit_factor',0):5.2f} "
                      f"t={m.get('total_trades',0):3d} "
                      f"tol={p['level_tol_atr']} cp={p['close_pos_min']} "
                      f"imp={p['impulse_atr']} lb={p['sweep_lookback']} "
                      f"brk={p['break_lookback']} age={p['max_level_age_d1']} "
                      f"tou={p['min_level_touches']} | exit {ex}")
            best_net, best_exit, best_m, best_p, _ = rows[0]

            # ---- measure the TRAIN-chosen config on VAL ----
            va_sigs = detect_signals(va_df, levels_all[fam], d1=d1,
                             touch_counter=counter, **best_p)
            _, vm = backtest_signals(va_df, va_sigs, spread_pips=spr_pips,
                                     slippage_pips=slip_pips, pip=pip, **best_exit)
            print(f"    VAL  {fmt(vm)}")

            # ---- walk-forward, fixed params, no re-tuning ----
            is_n, oos_n, oos_t = [], [], 0
            for (a, b), (x, y) in walk_forward_windows(len(exec_df),
                                                       train_frac=0.5,
                                                       test_frac=0.25):
                w1 = exec_df.iloc[a:b]
                w2 = exec_df.iloc[x:y]
                _, im = backtest_signals(
                    w1, detect_signals(w1, levels_all[fam], d1=d1,
                                 touch_counter=counter, **best_p),
                    spread_pips=spr_pips, slippage_pips=slip_pips, pip=pip, **best_exit)
                _, om = backtest_signals(
                    w2, detect_signals(w2, levels_all[fam], d1=d1,
                                 touch_counter=counter, **best_p),
                    spread_pips=spr_pips, slippage_pips=slip_pips, pip=pip, **best_exit)
                is_n.append(im.get("net_pips", 0) or 0)
                oos_n.append(om.get("net_pips", 0) or 0)
                oos_t += om.get("total_trades", 0) or 0
            is_m = float(np.mean(is_n)) if is_n else 0.0
            oos_m = float(np.mean(oos_n)) if oos_n else 0.0
            deg = 1.0 - (oos_m / is_m) if is_m else float("nan")
            print(f"    WF   windows={len(is_n)} IS_mean={is_m:8.1f} "
                  f"OOS_mean={oos_m:8.1f} deg={deg:6.2f} OOS_trades={oos_t}")

            # ---- full dataset + Monte Carlo ----
            fs = detect_signals(exec_df, levels_all[fam], d1=d1,
                             touch_counter=counter, **best_p)
            ft, fm = backtest_signals(exec_df, fs, spread_pips=spr_pips,
                                      slippage_pips=slip_pips, pip=pip, **best_exit)
            print(f"    FULL {fmt(fm)}")
            if fm.get("total_trades", 0) >= 10:
                mc = run_monte_carlo(ft, MCConfig(n_iterations=2000, seed=42,
                                                  shuffle=True, scatter_pct=0.10,
                                                  jitter_pct=0.05,
                                                  ruin_threshold=-200.0))
                print(f"    MC   net_mean={mc.net_mean:.1f} net_p5={mc.net_p5:.1f} "
                      f"PF_p5={mc.profit_factor_p5:.2f} "
                      f"ruin={mc.ruin_prob*100:.1f}% robust={mc.is_robust}")
            # direction split
            nb = sum(1 for t in ft if t.side == "BUY")
            ns = len(ft) - nb
            print(f"    split: BUY={nb} SELL={ns}")
            print()
            report[tag] = dict(params=best_p, exit=best_exit, train=best_m,
                               val=vm, full=fm, oos_trades=oos_t, deg=deg)
    return report


def main(argv: list[str] | None = None):
    import argparse
    ap = argparse.ArgumentParser(description="DQR research harness")
    ap.add_argument("--symbol", default="XAUUSD", choices=sorted(SYMBOLS))
    ap.add_argument("--tfs", default="M15,H1",
                    help="execution timeframes, comma separated. BTCUSD has "
                         "no M15/M5 history in the DB, so use H1 only.")
    args = ap.parse_args(argv)

    sym = args.symbol
    spec = SYMBOLS[sym]
    tfs = [t for t in args.tfs.split(",") if t]

    print("=" * 78)
    print(f"DQR — DAILY QUALITY REVERSAL (liquidity sweep + D1 level + momentum)")
    print(f"symbol={sym}  pip={spec['pip']}  "
          f"fallback_spread={spec['fallback_spread_pips']}pips  "
          f"fallback_slippage={spec['fallback_slippage_pips']}pips")
    print("=" * 78)
    for k, v in LABELLED_DEFAULTS.items():
        print(f"  {k}: {v}")
    print()

    d1 = build_exec_features(load("D1", sym))
    print(f"D1 level TF: {len(d1)} bars "
          f"({d1['ts'].iloc[0].date()} .. {d1['ts'].iloc[-1].date()})")

    reports = {}
    for tf in tfs:
        raw = load(tf, sym)
        if len(raw) < 500:
            print(f"\n!! {sym} {tf}: only {len(raw)} bars in DB — skipped "
                  f"(insufficient history for this strategy family)\n")
            continue
        exec_df = build_exec_features(raw)
        reports[tf] = run_family_family(exec_df, d1, tf, spec)

    print("=" * 78)
    print(f"SUMMARY {sym} (no winner picked; every family reported as measured)")
    print("=" * 78)
    for tf, rep in reports.items():
        for tag, r in (rep or {}).items():
            if r is None:
                print(f"  {tf:>3} {tag:<28} INCONCLUSIVE (too few signals)")
                continue
            print(f"  {tf:>3} {tag:<28} FULL {fmt(r['full'])}  deg={r['deg']:.2f}")
    print()
    print("Read-only. No MT5 writes, no orders, no fabricated numbers.")
    print(f"All metrics COMPUTED from db/trading.db {sym} bars.")


if __name__ == "__main__":
    main()
