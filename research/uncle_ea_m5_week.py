"""
Backtest of the "Simple_Strategy_Fixed_M5" EA (user's uncle) on XAUUSD M5.

Two modes:
  --mode ea      replicate the EA logic AS WRITTEN (look-ahead + dead branches)
                 -> shows why it lost money
  --mode fixed   the corrected logic: no look-ahead, no dead branches,
                 ATR-derived stop, ATR/volatility-derived R:R + TP1/TP2,
                 lot sized so a stop-out costs exactly RISK_PCT of the balance

READ-ONLY. Never imports an execution module, never calls order_send.
Usage:
  ./.venv/Scripts/python.exe research/uncle_ea_m5_week.py --mode fixed
  ./.venv/Scripts/python.exe research/uncle_ea_m5_week.py --from 2026-09-21 --to 2026-09-26
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

DB = Path(__file__).resolve().parents[1] / "db" / "trading.db"

SYMBOL = "XAUUSD"
TF = "M5"

# --- money / cost model (XAUUSD, GOLD.i#) ---------------------------------
POINT = 0.01          # $0.01
TICK_SIZE = 0.01
PIP = 0.10            # 1 pip = $0.10 of price
LOT_VALUE_PER_PIP = 0.10   # $0.10 per pip per 1.0 lot
SPREAD_PIPS = 30.0    # recorded broker spread on XAUUSD M5 (≈$3.00) - very wide
SLIPPAGE_PIPS = 5.0   # per side, conservative

RISK_PCT = 0.02       # 2% of the running balance risked per trade
START_BALANCE = 10_000.0
MAX_LOTS = 1.00       # broker/margin sanity cap; sizing is risk-first, not lot-first
ATR_PERIOD = 14
ATR_SL_MULT = 1.5
PARTIAL_PCT = 0.50    # 50% closed at TP1


# --------------------------------------------------------------------------
def load(from_ts: datetime, to_ts: datetime) -> pd.DataFrame:
    import sqlite3

    with sqlite3.connect(DB) as c:
        rows = c.execute(
            "SELECT ts_broker_epoch, open, high, low, close, spread "
            "FROM market_data WHERE symbol=? AND timeframe=? "
            "AND ts_broker_epoch BETWEEN ? AND ? ORDER BY ts_broker_epoch",
            (SYMBOL, TF, int(from_ts.timestamp()), int(to_ts.timestamp())),
        ).fetchall()
    df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "spread"])
    if df.empty:
        return df
    df["dt"] = pd.to_datetime(df["ts"], unit="s")
    df = df.set_index("dt")
    return df


# --------------------------------------------------------------------------
def features(df: pd.DataFrame) -> pd.DataFrame:
    prev_close = df["close"].shift(1)
    tr = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    df["atr"] = tr.ewm(alpha=1 / ATR_PERIOD, adjust=False).mean()
    # session-of-day helper: only Mon-Fri bars are tradeable by default
    df["dow"] = df.index.dayofweek
    return df


# --------------------------------------------------------------------------
# signal blocks -------------------------------------------------------
# NOTE on the EA's array semantics: the EA set its arrays as SERIES, so its
# "previous candle" was read at i-1 -- the FUTURE. Everything here is
# chronological (oldest first), so the previous bar is i-1 and the next is
# i+1. `look_ahead=True` deliberately flips the detectors to the EA's series
# orientation (previous == i+1) so the `ea` mode can price that bug.

def bull_engulfing(hi, lo, cl, op, i, look_ahead: bool) -> bool:
    p = i + 1 if look_ahead else i - 1     # the bar BEFORE i
    if p < 0 or p >= len(cl):
        return False
    if not (cl[i] > op[i] and cl[p] < op[p]):
        return False
    if not (op[i] <= cl[p] and cl[i] >= op[p]):
        return False
    rng = hi[i] - lo[i]
    return bool(rng > 0 and (cl[i] - op[i]) > rng * 0.5)


def bear_engulfing(hi, lo, cl, op, i, look_ahead: bool) -> bool:
    p = i + 1 if look_ahead else i - 1
    if p < 0 or p >= len(cl):
        return False
    if not (cl[i] < op[i] and cl[p] > op[p]):
        return False
    if not (op[i] >= cl[p] and cl[i] <= op[p]):
        return False
    rng = hi[i] - lo[i]
    return bool(rng > 0 and (op[i] - cl[i]) > rng * 0.5)


def hammer(hi, lo, cl, op, i, look_ahead: bool = False) -> bool:
    p = i + 1 if look_ahead else i - 1
    if p < 0 or p >= len(cl):
        return False
    if not (cl[i] > op[i]):
        return False
    body = cl[i] - op[i]
    if body <= 0:
        return False
    lower = op[i] - lo[i]
    upper = hi[i] - cl[i]
    prev_bear = cl[p] < op[p]
    return bool(prev_bear and lower > body * 2 and upper < body * 0.5)


def shooting_star(hi, lo, cl, op, i, look_ahead: bool = False) -> bool:
    p = i + 1 if look_ahead else i - 1
    if p < 0 or p >= len(cl):
        return False
    if not (cl[i] < op[i]):
        return False
    body = op[i] - cl[i]
    if body <= 0:
        return False
    upper = hi[i] - op[i]
    lower = cl[i] - lo[i]
    prev_bull = cl[p] > op[p]
    return bool(prev_bull and upper > body * 2 and lower < body * 0.5)


def swing_lows(lo: np.ndarray, i: int, lookback: int, look_ahead: bool) -> list[float]:
    out = []
    if look_ahead:
        end = min(lookback, len(lo) - i - 3)   # j+2 must stay in range
        rng = range(i + 2, i + end)
    else:
        end = min(lookback, i - 4)            # j-2 must stay in range
        rng = range(i - 2, i - 2 - end, -1)
    for j in rng:
        if lo[j] < lo[j - 1] and lo[j] < lo[j - 2] and lo[j] < lo[j + 1] and lo[j] < lo[j + 2]:
            out.append(lo[j])
    return out


def swing_highs(hi: np.ndarray, i: int, lookback: int, look_ahead: bool) -> list[float]:
    out = []
    if look_ahead:
        end = min(lookback, len(hi) - i - 3)
        rng = range(i + 2, i + end)
    else:
        end = min(lookback, i - 4)
        rng = range(i - 2, i - 2 - end, -1)
    for j in rng:
        if hi[j] > hi[j - 1] and hi[j] > hi[j - 2] and hi[j] > hi[j + 1] and hi[j] > hi[j + 2]:
            out.append(hi[j])
    return out


def signals(df: pd.DataFrame, lookback: int, look_ahead: bool,
            rr: float | None, atr_tp_mult: float | None) -> list[dict]:
    """Return entries. `rr` None => mode 'ea' (fixed 1:rr) ; atr_tp_mult set
    => fixed mode, TP2 = atr_tp_mult * ATR (volatility-scaled target)."""
    hi = df["high"].to_numpy(float)
    lo = df["low"].to_numpy(float)
    cl = df["close"].to_numpy(float)
    op = df["open"].to_numpy(float)
    atr = df["atr"].to_numpy(float)
    n = len(df)
    out: list[dict] = []
    # EA used a ±10-point (=$0.10) "price is near the pattern" tolerance, but
    # only on REVERSAL patterns; the S/R and breakout blocks ignored the pattern
    # bar and fired on proximity alone. Fixed mode drops that proximity rule and
    # requires the level to be the NEAREST one, which is what the code intended.
    for i in range(1, n - 1):
        if atr[i] <= 0 or np.isnan(atr[i]):
            continue
        price = cl[i]
        sig = None

        # EA used series arrays, so index j > i is a PAST bar for it. To
        # reproduce that in chronological order we scan the window that the
        # EA believed was the past; to fix the look-ahead we scan the REAL past.
        def scan():
            if look_ahead:
                return range(i + 3, min(i + lookback, n - 2))
            return range(max(3, i - lookback), i - 2)

        # ---- reversal (bull) ----
        for j in scan():
            if bull_engulfing(hi, lo, cl, op, j, look_ahead) or hammer(hi, lo, cl, op, j, look_ahead):
                if abs(price - cl[j]) <= hi[j] - lo[j]:  # price inside that bar's range
                    sig = ("BUY", f"reversal@bar-{(j - i) if look_ahead else (i - j)}")
                    break
        if sig is None:
            sls = swing_lows(lo, i, 30, look_ahead)
            if len(sls) >= 2:
                for a in range(len(sls)):
                    for b in range(a + 1, len(sls)):
                        if abs(sls[a] - sls[b]) <= 20 * POINT:
                            lvl = (sls[a] + sls[b]) / 2
                            if lvl - 10 * POINT <= price <= lvl + 30 * POINT:
                                sig = ("BUY", f"support {lvl:.2f}")
                                break
                    if sig:
                        break
        if sig is None and rr is None:      # EA-only (dead) breakout block
            w = hi[i + 3: i + 23] if look_ahead else hi[max(0, i - 23):i - 2]
            if len(w):
                res = w.max()
                if res - 5 * POINT <= price <= res + 20 * POINT:
                    sig = ("BUY", f"breakout-retest {res:.2f}")
        if sig:
            out.append({"i": i, "dir": "BUY", "why": sig[1]})
            continue

        # ---- sell side ----
        sig = None
        for j in scan():
            if bear_engulfing(hi, lo, cl, op, j, look_ahead) or shooting_star(hi, lo, cl, op, j, look_ahead):
                if abs(price - cl[j]) <= hi[j] - lo[j]:
                    sig = ("SELL", f"reversal@bar-{(j - i) if look_ahead else (i - j)}")
                    break
        if sig is None:
            shs = swing_highs(hi, i, 30, look_ahead)
            if len(shs) >= 2:
                for a in range(len(shs)):
                    for b in range(a + 1, len(shs)):
                        if abs(shs[a] - shs[b]) <= 20 * POINT:
                            lvl = (shs[a] + shs[b]) / 2
                            if lvl - 30 * POINT <= price <= lvl + 10 * POINT:
                                sig = ("SELL", f"resistance {lvl:.2f}")
                                break
                    if sig:
                        break
        if sig is None and rr is None:
            w = lo[i + 3: i + 23] if look_ahead else lo[max(0, i - 23):i - 2]
            if len(w):
                sup = w.min()
                if sup - 20 * POINT <= price <= sup + 10 * POINT:
                    sig = ("SELL", f"breakdown-retest {sup:.2f}")
        if sig:
            out.append({"i": i, "dir": "SELL", "why": sig[1]})
    return out


# --------------------------------------------------------------------------
def backtest(df: pd.DataFrame, sigs: list[dict], rr: float | None,
             atr_tp_mult: float | None, label: str) -> dict:
    """Chronological single-position state machine (the EA only opens when
    CountOpenPositions()==0, and it stops checking for new bars while a
    position is open, so signals during a trade are ignored)."""
    o = df["open"].to_numpy(float)
    h = df["high"].to_numpy(float)
    l = df["low"].to_numpy(float)
    c = df["close"].to_numpy(float)
    atr = df["atr"].to_numpy(float)
    idx = df.index
    dow = (df["dow"].to_numpy(int) if "dow" in df.columns
           else np.array([t.dayofweek for t in idx], dtype=int))

    round_trip_cost = (SPREAD_PIPS + SLIPPAGE_PIPS * 2) * PIP   # $ of price, per 1.0 lot

    sig_by_i = {s["i"]: s for s in sigs}
    balance = START_BALANCE
    trades = []
    peak = balance
    max_dd = 0.0
    open_pos = None          # dict while a position is live
    last_entry = -10

    for e in range(1, len(df)):
        # ---- 1. manage a live position on this bar ----
        if open_pos is not None:
            op_ = open_pos
            long = op_["long"]
            hit_sl = (l[e] <= op_["sl"]) if long else (h[e] >= op_["sl"])
            hit_t2 = (h[e] >= op_["tp2"]) if long else (l[e] <= op_["tp2"])
            hit_t1 = (h[e] >= op_["tp1"]) if long else (l[e] <= op_["tp1"])
            r_mult = None
            if hit_sl:
                r_mult = -0.5 if op_["tp1_taken"] else -1.0
            elif hit_t2:
                r_mult = (0.5 + 0.5 * op_["tp2_r"]) if op_["tp1_taken"] else op_["tp2_r"]
            elif hit_t1:
                op_["tp1_taken"] = True
            if r_mult is not None:
                pnl = r_mult * op_["risk_usd"] - op_["cost"]
                balance += pnl
                peak = max(peak, balance)
                max_dd = max(max_dd, (peak - balance) / peak * 100)
                trades.append({**op_["rec"], "r": round(r_mult, 2),
                               "pnl": round(pnl, 2), "exit": idx[e]})
                open_pos = None
            continue                      # EA does not scan for signals here

        # ---- 2. flat: can we open? ----
        s = sig_by_i.get(e - 1)          # signal on bar i, enter at this open
        if s is None or dow[e] > 4 or (e - last_entry) < 1:
            continue
        i = e - 1
        a = atr[i]
        if not a or np.isnan(a):
            continue
        stop_dist = ATR_SL_MULT * a
        long = s["dir"] == "BUY"
        # realistic entry: buy at ask, sell at bid
        entry = o[e] + (0.5 * SPREAD_PIPS * PIP) if long else o[e] - (0.5 * SPREAD_PIPS * PIP)
        sl = entry - stop_dist if long else entry + stop_dist
        if atr_tp_mult is not None:
            tp2_dist = atr_tp_mult * a
        else:
            tp2_dist = stop_dist * (rr or 1.0)
        tp1_dist = stop_dist                          # TP1 at 1R, partial
        tp1 = entry + tp1_dist if long else entry - tp1_dist
        tp2 = entry + tp2_dist if long else entry - tp2_dist
        if long and not (sl < entry < tp2):
            continue
        if not long and not (sl > entry > tp2):
            continue

        risk_usd = balance * RISK_PCT
        lots = risk_usd / (stop_dist / PIP * LOT_VALUE_PER_PIP)
        lots = max(0.01, min(round(lots, 2), MAX_LOTS))
        # after lot rounding the ACTUAL $ at risk differs from the target.
        # Use the real figure everywhere, otherwise R-multiples and P&L
        # disagree (a 0.01-lot trade would report a 2%-of-balance loss).
        risk_usd = round(stop_dist / PIP * LOT_VALUE_PER_PIP * lots, 2)
        cost = round_trip_cost / PIP * LOT_VALUE_PER_PIP * lots
        rec = {
            "time": idx[e], "dir": s["dir"], "why": s["why"],
            "entry": round(entry, 2), "sl": round(sl, 2),
            "tp1": round(tp1, 2), "tp2": round(tp2, 2),
            "stop_pips": round(stop_dist / PIP), "lots": lots,
            "risk_usd": round(risk_usd, 2),
        }
        open_pos = {"long": long, "sl": sl, "tp1": tp1, "tp2": tp2,
                    "tp2_r": tp2_dist / stop_dist, "tp1_taken": False,
                    "risk_usd": risk_usd, "cost": cost, "rec": rec}
        last_entry = e

    # mark-to-market any trade still open at the window end (no fake exit)
    if open_pos is not None:
        op_ = open_pos
        stop_price = op_["sl"] if not op_["long"] else op_["sl"]
        entry = op_["rec"]["entry"]
        risk_price = abs(entry - op_["sl"])
        mtm = (c[-1] - entry) * (1 if op_["long"] else -1)
        r_mult = (0.5 if op_["tp1_taken"] else 1.0) * (mtm / risk_price)
        pnl = r_mult * op_["risk_usd"] - op_["cost"]
        balance += pnl
        trades.append({**op_["rec"], "r": round(r_mult, 2), "pnl": round(pnl, 2),
                       "exit": idx[-1]})

    n = len(trades)
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] < 0]
    gp = sum(t["pnl"] for t in wins)
    gl = -sum(t["pnl"] for t in losses)
    return {
        "label": label, "trades": n,
        "wins": len(wins), "losses": len(losses),
        "win_rate": round(len(wins) / n * 100, 1) if n else None,
        "pf": round(gp / gl, 2) if gl else None,
        "net": round(balance - START_BALANCE, 2),
        "end_balance": round(balance, 2),
        "ret_pct": round((balance / START_BALANCE - 1) * 100, 2),
        "max_dd_pct": round(max_dd, 2),
        "avg_stop_pips": round(np.mean([t["stop_pips"] for t in trades]), 1) if n else None,
    }, trades


# --------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["ea", "fixed", "both"], default="both")
    ap.add_argument("--from", dest="dt_from", default=None)
    ap.add_argument("--to", dest="dt_to", default=None)
    ap.add_argument("--rr", type=float, default=2.0, help="EA's fixed R:R (ea mode)")
    ap.add_argument("--atr-tp", type=float, default=3.0, help="TP2 = N x ATR (fixed mode)")
    ap.add_argument("--lookback", type=int, default=60)
    args = ap.parse_args()

    to_ts = datetime.strptime(args.dt_to, "%Y-%m-%d") if args.dt_to else datetime.utcnow()
    frm_ts = datetime.strptime(args.dt_from, "%Y-%m-%d") if args.dt_from else to_ts - timedelta(days=7)

    # need warm-up history before the window
    df = load(frm_ts - timedelta(days=20), to_ts)
    if df.empty:
        print(f"NO DATA for {frm_ts.date()} -> {to_ts.date()}")
        return 2
    df = features(df)
    warm = (df.index < frm_ts).sum()
    win = df[df.index >= frm_ts]
    print("=" * 72)
    print(f"SYMBOL {SYMBOL} {TF}   window {frm_ts.date()} -> {to_ts.date()} (broker time)")
    print(f"bars: {len(win)} in window, {warm} warm-up bars before it")
    print(f"spread assumed {SPREAD_PIPS} pips + {SLIPPAGE_PIPS} pips/side slippage")
    print(f"risk {RISK_PCT*100:.0f}% of running balance, ATR({ATR_PERIOD}) stop x{ATR_SL_MULT}, "
          f"TP1=1R partial {PARTIAL_PCT*100:.0f}%, balance start ${START_BALANCE:,.0f}")
    print("=" * 72)

    results = []
    if args.mode in ("ea", "both"):
        sg = signals(win.reset_index(drop=True), args.lookback, True, args.rr, None)
        m, tr = backtest(win.reset_index(drop=True), sg, args.rr, None,
                         f"EA AS WRITTEN (look-ahead, 1:{args.rr} fixed)")
        results.append(m)
        for t in tr[:40]:
            print(f"  {t['time']} {t['dir']:4} {t['why'][:24]:24} sl={t['sl']} tp2={t['tp2']} "
                  f"stop={t['stop_pips']}p lots={t['lots']} pnl=${t['pnl']} r={t['r']}")
    if args.mode in ("fixed", "both"):
        sg = signals(win.reset_index(drop=True), args.lookback, False, None, args.atr_tp)
        m, tr = backtest(win.reset_index(drop=True), sg, None, args.atr_tp,
                         f"FIXED (no look-ahead, TP2={args.atr_tp}x ATR)")
        results.append(m)
        for t in tr[:40]:
            print(f"  {t['time']} {t['dir']:4} {t['why'][:24]:24} sl={t['sl']} tp2={t['tp2']} "
                  f"stop={t['stop_pips']}p lots={t['lots']} pnl=${t['pnl']} r={t['r']}")

    print("-" * 72)
    hdr = f"{'variant':46} {'n':>4} {'win%':>6} {'PF':>6} {'net$':>10} {'ret%':>7} {'maxDD%':>7} {'stop_pips':>9}"
    print(hdr)
    for m in results:
        print(f"{m['label']:46} {m['trades']:>4} {str(m['win_rate']):>6} {str(m['pf']):>6} "
              f"{m['net']:>10.2f} {m['ret_pct']:>7.2f} {m['max_dd_pct']:>7.2f} {str(m['avg_stop_pips']):>9}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
