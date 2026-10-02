"""
SEPTEMBER backtest: 2026-09-01 .. 2026-09-30, $1000 balance, 2% risk/trade.

Tests V11 (XAUUSD D1 momentum), V12 (XAUUSD H1 momentum), V13 (BTCUSD DQR)
across all available timeframes with adaptive R:R per TF.

Read-only: queries db/trading.db only. No MT5 writes.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
from pathlib import Path
from datetime import datetime, timezone

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from market_data import config as cfg
from indicators import ema, atr, rsi
from backtest import compute_metrics, Trade

DB = cfg.PROJECT_ROOT / cfg.load_settings()["paths"]["db"]

# Month window. Defaults to September 2026 so the original behaviour is
# unchanged; pass --month 2026-08 for any other calendar month. The window is
# [start, end) in UTC epoch seconds.
def _window(month: str):
    y, m = (int(x) for x in month.split("-"))
    start = datetime(y, m, 1, 0, 0, 0, tzinfo=timezone.utc)
    end = (datetime(y + 1, 1, 1, 0, 0, 0, tzinfo=timezone.utc) if m == 12
           else datetime(y, m + 1, 1, 0, 0, 0, tzinfo=timezone.utc))
    return int(start.timestamp()), int(end.timestamp())


SEP_START, SEP_END = _window(os.environ.get("REPORT_MONTH", "2026-09"))
MONTH_LABEL = time.strftime("%B %Y", time.gmtime(SEP_START))

BALANCE    = float(os.environ.get("REPORT_BALANCE", "1000"))
RISK_PCT   = float(os.environ.get("REPORT_RISK_PCT", "0.02"))
RISK_USD   = BALANCE * RISK_PCT

# Readable aliases used by the report banners.
MONTH_START, MONTH_END = SEP_START, SEP_END


def _d(epoch: int) -> str:
    return time.strftime("%Y-%m-%d %a", time.gmtime(epoch))


PIP_VAL = {"XAUUSD": 0.10, "BTCUSD": 1.0}
MIN_LOTS = 0.01
LOT_STEP = 0.01
MAX_LOTS = 10.0
SLIPPAGE_PIPS = 0.5

# Per-TF ATR params and adaptive R:R (higher TF = wider R:R is viable)
TF_CONFIG = {
    "D1":   {"min_atr_pct": 0.004, "atr_mult_stop": 1.5, "rank_window": 60, "rr": 2.5, "ema_fast": 21, "ema_slow": 55},
    "H1":   {"min_atr_pct": 0.0012, "atr_mult_stop": 1.0, "rank_window": 60, "rr": 3.0, "ema_fast": 21, "ema_slow": 55},
    "M15":  {"min_atr_pct": 0.0006, "atr_mult_stop": 1.2, "rank_window": 60, "rr": 3.0, "ema_fast": 21, "ema_slow": 55},
    "M5":   {"min_atr_pct": 0.0003, "atr_mult_stop": 1.5, "rank_window": 60, "rr": 3.5, "ema_fast": 13, "ema_slow": 34},
    "M1":   {"min_atr_pct": 0.00015, "atr_mult_stop": 1.5, "rank_window": 20, "rr": 4.0, "ema_fast": 8,  "ema_slow": 21},
}

# V13 D1 config (frozen from def)
V13_PARAMS = {
    "level_tol_atr": 0.10, "close_pos_min": 0.70, "impulse_atr": 0.80,
    "sweep_lookback": 1, "break_lookback": 0, "break_atr": 0.50,
    "max_level_age_d1": 120, "max_level_dist_atr": 3.0, "min_level_touches": 1,
    "min_sweep_atr": 0.25, "atr_mult_stop": 1.5, "rr": 1.5,
    "max_holding_bars": 32, "cooldown_bars": 4,
}


def load(symbol, tf, start_ts=None, end_ts=None):
    if start_ts is None: start_ts = SEP_START
    if end_ts is None: end_ts = SEP_END
    con = sqlite3.connect(DB)
    df = pd.read_sql_query(
        "SELECT ts_broker_epoch, open, high, low, close, tick_volume, spread "
        "FROM market_data WHERE symbol=? AND timeframe=? AND ts_broker_epoch >= ? "
        "AND ts_broker_epoch < ? AND source='mt5' ORDER BY ts_broker_epoch",
        con, params=(symbol, tf, start_ts, end_ts))
    con.close()
    df["ts"] = pd.to_datetime(df["ts_broker_epoch"], unit="s", utc=True)
    return df


def size_lots(risk_usd, stop_pips, pip_value, min_lots=0.01, lot_step=0.01, max_lots=10.0):
    if stop_pips <= 0 or np.isnan(stop_pips):
        return 0.0
    raw = risk_usd / (stop_pips * pip_value)
    if raw < min_lots:
        return 0.0
    lots = (raw // lot_step) * lot_step
    return round(min(lots, max_lots), 2)


def v11_signals(df, body_pct_threshold=0.95, min_atr_pct=0.005, atr_mult_stop=1.5,
                rank_window=60, ema_fast=21, ema_slow=55):
    """V11 momentum: EMA ema_fast > ema_slow + top-5% body, long-only."""
    df = df.copy()
    df["ema_f"] = ema(df["close"], ema_fast)
    df["ema_s"] = ema(df["close"], ema_slow)
    df["atr14"] = atr(df["high"], df["low"], df["close"], 14)
    df["body"] = (df["close"] - df["open"]) / df["open"]
    df["body_abs"] = df["body"].abs()
    rw = max(20, rank_window // 3)
    df["body_pct"] = df["body_abs"].rolling(rank_window, min_periods=rw).rank(pct=True)

    signals = []
    for i in range(len(df)):
        row = df.iloc[i]
        if i < max(ema_slow, 55):
            continue
        if pd.isna(row["ema_f"]) or pd.isna(row["ema_s"]) or row["ema_f"] <= row["ema_s"]:
            continue
        if pd.isna(row["body_pct"]) or row["body_pct"] < body_pct_threshold:
            continue
        if row["body"] <= 0:
            continue
        if pd.isna(row["atr14"]) or row["atr14"] / row["close"] < min_atr_pct:
            continue
        entry_bar = i + 1
        if entry_bar >= len(df):
            continue
        entry_price = df.iloc[entry_bar]["open"]
        atr_val = row["atr14"]
        stop = entry_price - atr_mult_stop * atr_val
        signals.append({
            "entry_bar": entry_bar, "entry_ts": df.iloc[entry_bar]["ts"],
            "entry_price": entry_price, "atr": atr_val, "stop": stop, "dir": "LONG",
        })
    return signals


def v13_signals(df_exec, d1_levels, *, rr, params):
    """V13 DQR: sweep at D1 swing level, rejection bar, impulse confirmation."""
    signals = []
    for i in range(len(df_exec)):
        row = df_exec.iloc[i]
        o, h, l, c = row["open"], row["high"], row["low"], row["close"]
        ts = row["ts"]
        for lvl in d1_levels:
            if ts < lvl["ts"]:
                continue
            lp = lvl["price"]
            tol = params["level_tol_atr"] * lvl["atr14"]
            min_sweep = params["min_sweep_atr"] * lvl["atr14"]
            if lvl["type"] == "S":
                # sweep: low pierces level by at least min_sweep
                if l >= lp - min_sweep:
                    continue
                if c <= lp:
                    continue
                body = c - o
                if body <= 0:
                    continue
                rng = h - l + 1e-9
                close_pos = (c - l) / rng
                if close_pos < params["close_pos_min"]:
                    continue
                if i + 1 >= len(df_exec):
                    continue
                nr = df_exec.iloc[i + 1]
                if pd.isna(nr["atr14"]):
                    continue
                n_body = nr["close"] - nr["open"]
                if abs(n_body) < params["impulse_atr"] * nr["atr14"]:
                    continue
                # decisive break check
                entry_bar = i + 2
                if entry_bar >= len(df_exec):
                    continue
                entry_price = df_exec.iloc[entry_bar]["open"]
                atr_val = nr["atr14"]
                stop = min(l, nr["low"]) - params["atr_mult_stop"] * atr_val
                target = entry_price + (entry_price - stop) * rr
                signals.append({
                    "entry_bar": entry_bar, "entry_ts": df_exec.iloc[entry_bar]["ts"],
                    "entry_price": entry_price, "stop": stop, "target": target,
                    "atr": atr_val, "dir": "LONG",
                })
            elif lvl["type"] == "R":
                if h <= lp + min_sweep:
                    continue
                if c >= lp:
                    continue
                body = o - c
                if body <= 0:
                    continue
                rng = h - l + 1e-9
                close_pos = (h - c) / rng
                if close_pos < params["close_pos_min"]:
                    continue
                if i + 1 >= len(df_exec):
                    continue
                nr = df_exec.iloc[i + 1]
                if pd.isna(nr["atr14"]):
                    continue
                n_body = nr["open"] - nr["close"]
                if abs(n_body) < params["impulse_atr"] * nr["atr14"]:
                    continue
                entry_bar = i + 2
                if entry_bar >= len(df_exec):
                    continue
                entry_price = df_exec.iloc[entry_bar]["open"]
                atr_val = nr["atr14"]
                stop = max(h, nr["high"]) + params["atr_mult_stop"] * atr_val
                target = entry_price - (stop - entry_price) * rr
                signals.append({
                    "entry_bar": entry_bar, "entry_ts": df_exec.iloc[entry_bar]["ts"],
                    "entry_price": entry_price, "stop": stop, "target": target,
                    "atr": atr_val, "dir": "SELL",
                })
    return signals


def backtest_priced(signals, df, *, rr, symbol, min_holding=1,
                    max_holding=None):
    """Backtest with fixed-fractional lot sizing ($20 risk per trade).

    HARD CAP: if max possible loss (stop_distance * lots * pip_value) > RISK_USD,
    skip the trade. This prevents BTC M1 blowups where a wide stop at 0.01 lots
    still loses >$20 per trade.
    """
    pip_val = PIP_VAL[symbol]
    pts_per_pip = 10 if symbol == "XAUUSD" else 1
    trades = []
    equity = 0.0
    n = len(df)
    if max_holding is None:
        max_holding = n - 1

    for sig in signals:
        i = sig["entry_bar"]
        if i >= n - 1:
            continue
        entry_price = sig["entry_price"]
        stop = sig.get("stop")
        atr_val = sig["atr"]
        side = sig["dir"]

        if stop is None:
            if side == "LONG":
                stop = entry_price - 1.5 * atr_val
            else:
                stop = entry_price + 1.5 * atr_val
        if "target" in sig and sig["target"] is not None:
            target = sig["target"]
        else:
            if side == "LONG":
                risk_pips = (entry_price - stop) / pip_val
                target = entry_price + risk_pips * pip_val * rr
            else:
                risk_pips = (stop - entry_price) / pip_val
                target = entry_price - risk_pips * pip_val * rr

        if side == "LONG":
            risk_pips = (entry_price - stop) / pip_val
        else:
            risk_pips = (stop - entry_price) / pip_val
        if risk_pips <= 0 or np.isnan(risk_pips):
            continue
        lots = size_lots(RISK_USD, risk_pips, pip_val)
        if lots == 0:
            continue
        # HARD LOSS CAP: ensure max loss (stop distance * lots * pip_value) <= RISK_USD
        max_loss = risk_pips * pip_val * lots
        if max_loss > RISK_USD * 1.05:  # 5% tolerance for rounding
            continue
        # COST CAP: if round-trip spread/slippage alone exceeds risk, skip (untradeable)
        sp = float(df.iloc[i].get("spread", 0) or 0)
        pts_per_pip = 10 if symbol == "XAUUSD" else 1
        spread_pips = sp / pts_per_pip
        cost = spread_pips * 2 * pip_val * lots + SLIPPAGE_PIPS * 2 * pip_val * lots
        if cost > RISK_USD * 0.5:  # skip if costs alone exceed 50% of risk budget
            continue

        exited = False
        exit_limit = min(i + max_holding, n - 1)
        for j in range(i + 1, n):
            jrow = df.iloc[j]
            jhi, jlo = jrow["high"], jrow["low"]
            if side == "LONG":
                stop_hit = jlo <= stop
                target_hit = jhi >= target
            else:
                stop_hit = jhi >= stop
                target_hit = jlo <= target
            if stop_hit or target_hit:
                d_stop = abs(entry_price - stop)
                d_target = abs(target - entry_price)
                if stop_hit and target_hit:
                    if d_stop <= d_target:
                        exit_price, reason = stop, "stop"
                    else:
                        exit_price, reason = target, "target"
                elif stop_hit:
                    exit_price, reason = stop, "stop"
                else:
                    exit_price, reason = target, "target"
                gross = (exit_price - entry_price) if side == "LONG" else (entry_price - exit_price)
                sp = float(df.iloc[j].get("spread", 0) or 0)
                spread_pips = sp / pts_per_pip
                cost = spread_pips * 2 * pip_val * lots + SLIPPAGE_PIPS * 2 * pip_val * lots
                net_usd = gross * pip_val * lots - cost
                trades.append(Trade(
                    entry_bar=i, entry_price=entry_price, side=side,
                    stop=stop, target=target,
                    exit_bar=j, exit_price=exit_price,
                    exit_reason=reason, points=gross, cost_pips=0,
                    net_pips=gross, duration_bars=j - i,
                    lots=lots, risk_usd=RISK_USD, net_usd=net_usd))
                exited = True
                break
            if j >= exit_limit:
                j = exit_limit
                jrow = df.iloc[j]
                exit_price = jrow["close"]
                gross = (exit_price - entry_price) if side == "LONG" else (entry_price - exit_price)
                cost = SLIPPAGE_PIPS * 2 * pip_val * lots
                net_usd = gross * pip_val * lots - cost
                trades.append(Trade(
                    entry_bar=i, entry_price=entry_price, side=side,
                    stop=stop, target=target,
                    exit_bar=j, exit_price=exit_price,
                    exit_reason="end", points=gross, cost_pips=0,
                    net_pips=gross, duration_bars=j - i,
                    lots=lots, risk_usd=RISK_USD, net_usd=net_usd))
                exited = True
                break
        if not exited:
            j = n - 1
            jrow = df.iloc[j]
            exit_price = jrow["close"]
            gross = (exit_price - entry_price) if side == "LONG" else (entry_price - exit_price)
            cost = SLIPPAGE_PIPS * 2 * pip_val * lots
            net_usd = gross * pip_val * lots - cost
            trades.append(Trade(
                entry_bar=i, entry_price=entry_price, side=side,
                stop=stop, target=target,
                exit_bar=j, exit_price=exit_price,
                exit_reason="end", points=gross, cost_pips=0,
                net_pips=gross, duration_bars=j - i,
                lots=lots, risk_usd=RISK_USD, net_usd=net_usd))

    m = compute_metrics(trades)
    net_usd_total = sum(t.net_usd for t in trades)
    gross_wins = sum(t.net_usd for t in trades if t.net_usd > 0)
    gross_losses = -sum(t.net_usd for t in trades if t.net_usd <= 0)
    pf_usd = gross_wins / gross_losses if gross_losses > 0 else (float("inf") if gross_wins > 0 else 0)
    m["net_usd"] = net_usd_total
    m["final_equity"] = BALANCE + net_usd_total
    m["return_pct"] = (net_usd_total / BALANCE) * 100
    m["profit_factor_usd"] = pf_usd
    return m, trades


def d1_swing_levels(d1_df):
    """Extract D1 swing highs/lows (fractal L2/R2) with ATR."""
    d1_df = d1_df.copy()
    d1_df["atr14"] = atr(d1_df["high"], d1_df["low"], d1_df["close"], 14)
    levels = []
    for i in range(2, len(d1_df) - 2):
        h = d1_df.iloc[i]
        if (h["high"] > d1_df.iloc[i-2]["high"] and h["high"] > d1_df.iloc[i-1]["high"]
            and h["high"] > d1_df.iloc[i+1]["high"] and h["high"] > d1_df.iloc[i+2]["high"]):
            levels.append({"price": h["high"], "type": "R", "ts": h["ts"], "atr14": h["atr14"]})
        if (h["low"] < d1_df.iloc[i-2]["low"] and h["low"] < d1_df.iloc[i-1]["low"]
            and h["low"] < d1_df.iloc[i+1]["low"] and h["low"] < d1_df.iloc[i+2]["low"]):
            levels.append({"price": h["low"], "type": "S", "ts": h["ts"], "atr14": h["atr14"]})
    return levels


def fmt_line(strat, sym, tf, rr, nsig, nt, wr, pf, pfusd, net, fe, ret):
    return (f"{strat:6s} {sym:6s} {tf:3s} {rr:4.1f} {nsig:4d} {nt:4d} "
            f"{wr*100:5.0f}% {pf:5.2f} {pfusd:5.2f} {net:+10.2f} {fe:8.2f} {ret:+6.1f}%")


def main():
    print("=" * 90)
    print(f"{MONTH_LABEL.upper()} BACKTEST: {_d(MONTH_START)} .. {_d(MONTH_END - 1)}")
    print(f"Balance=${BALANCE:.0f}, Risk={RISK_PCT*100:.0f}%/trade (${RISK_USD:.0f}), adaptive R:R per TF")
    print("=" * 90)

    results = []
    header = f"{'Strat':6s} {'Sym':6s} {'TF':4s} {'RR':>4s} {'Sig':>4s} {'Trd':>4s} {'Win%':>5s} {'PF':>5s} {'PF$':>5s} {'Net$':>10s} {'Final$':>8s} {'Ret%':>6s}"
    sep = "-" * 90

    # ---- V11: XAUUSD momentum ----
    print("\n--- V11: XAUUSD_D1_MOMENTUM_BREAKOUT (EMA21>EMA55 + top-5% body, LONG-only) ---\n")
    print(header)
    print(sep)
    for tf in ["D1", "H1", "M15", "M5", "M1"]:
        df = load("XAUUSD", tf)
        if len(df) < 55:
            print(f"  V11  XAUUSD {tf:3s}  — {len(df)} bars, too few\n")
            continue
        cfg_tf = TF_CONFIG[tf]
        sigs = v11_signals(df, body_pct_threshold=0.95,
                           min_atr_pct=cfg_tf["min_atr_pct"],
                           atr_mult_stop=cfg_tf["atr_mult_stop"],
                           rank_window=cfg_tf["rank_window"],
                           ema_fast=cfg_tf["ema_fast"], ema_slow=cfg_tf["ema_slow"])
        rr = cfg_tf["rr"]
        m, trades = backtest_priced(sigs, df, rr=rr, symbol="XAUUSD", min_holding=1)
        nt = m.get("total_trades", 0)
        wr = m.get("win_rate", 0) if trades else 0
        pf = m.get("profit_factor", 0) if trades else 0
        net = m["net_usd"]
        fe = m["final_equity"]
        ret = m["return_pct"]
        pfusd = m["profit_factor_usd"]
        skipped = sum(1 for s in sigs if (lambda stop: size_lots(RISK_USD,
            abs(s["entry_price"] - stop) / PIP_VAL["XAUUSD"], PIP_VAL["XAUUSD"]) == 0)(
            s.get("stop") or (s["entry_price"] - 1.5 * s["atr"])))
        print(fmt_line("V11", "XAUUSD", tf, rr, len(sigs), nt, wr, pf, pfusd, net, fe, ret) +
              f"  skip={skipped}")
        results.append(("V11", "XAUUSD", tf, rr, len(sigs), nt, wr, pf, pfusd, net, fe, ret))

    # ---- V12: XAUUSD H1 momentum ----
    print("\n--- V12: XAUUSD_H1_MOMENTUM_BREAKOUT (H1 momentum, 0.8xATR stop, LONG-only) ---\n")
    print(header)
    print(sep)
    tf = "H1"
    df = load("XAUUSD", tf)
    if len(df) < 55:
        print(f"  V12  XAUUSD {tf:3s}  — {len(df)} bars, too few\n")
    else:
        cfg_tf = TF_CONFIG["H1"]
        sigs = v11_signals(df, body_pct_threshold=0.95,
                           min_atr_pct=0.0015,
                           atr_mult_stop=0.8,
                           rank_window=cfg_tf["rank_window"],
                           ema_fast=cfg_tf["ema_fast"], ema_slow=cfg_tf["ema_slow"])
        rr = 3.0  # V12 def selected R:R=3.0
        m, trades = backtest_priced(sigs, df, rr=rr, symbol="XAUUSD", min_holding=1)
        nt = m.get("total_trades", 0)
        wr = m.get("win_rate", 0) if trades else 0
        pf = m.get("profit_factor", 0) if trades else 0
        net = m["net_usd"]
        fe = m["final_equity"]
        ret = m["return_pct"]
        pfusd = m["profit_factor_usd"]
        skipped = sum(1 for s in sigs if (lambda stop: size_lots(RISK_USD,
            abs(s["entry_price"] - stop) / PIP_VAL["XAUUSD"], PIP_VAL["XAUUSD"]) == 0)(
            s.get("stop") or (s["entry_price"] - 0.8 * s["atr"])))
        print(fmt_line("V12", "XAUUSD", tf, rr, len(sigs), nt, wr, pf, pfusd, net, fe, ret) +
              f"  skip={skipped}")
        results.append(("V12", "XAUUSD", tf, rr, len(sigs), nt, wr, pf, pfusd, net, fe, ret))

    # ---- V13: BTCUSD DQR ----
    print("\n--- V13: BTCUSD_DQR_SWING_REVERSAL (D1 level-sweep + rejection + impulse) ---\n")
    print(header)
    print(sep)
    # Load FULL D1 history for level construction
    d1_full = load("BTCUSD", "D1", start_ts=0, end_ts=MONTH_END)
    if len(d1_full) < 10:
        print("  BTCUSD D1: too few bars\n")
    else:
        levels = d1_swing_levels(d1_full)
        levels = [l for l in levels if l["ts"].value <= SEP_START * 1_000_000_000]
        print(f"  D1 levels (historical, valid for {MONTH_LABEL}): {len(levels)}\n")
        for tf in ["D1", "H1", "M15", "M5", "M1"]:
            df = load("BTCUSD", tf)
            if len(df) < 30:
                print(f"  V13  BTCUSD {tf:3s}  — {len(df)} bars, too few")
                continue
            df["atr14"] = atr(df["high"], df["low"], df["close"], 14)
            rr = V13_PARAMS["rr"]  # 1.5 per def
            sigs = v13_signals(df, levels, rr=rr, params=V13_PARAMS)
            m, trades = backtest_priced(sigs, df, rr=rr, symbol="BTCUSD",
                                        min_holding=1, max_holding=V13_PARAMS["max_holding_bars"])
            nt = m.get("total_trades", 0)
            wr = m.get("win_rate", 0) if trades else 0
            pf = m.get("profit_factor", 0) if trades else 0
            net = m["net_usd"]
            fe = m["final_equity"]
            ret = m["return_pct"]
            pfusd = m["profit_factor_usd"]
            print(fmt_line("V13", "BTCUSD", tf, rr, len(sigs), nt, wr, pf, pfusd, net, fe, ret))
            results.append(("V13", "BTCUSD", tf, rr, len(sigs), nt, wr, pf, pfusd, net, fe, ret))

    # ---- Summary table ----
    print("\n" + "=" * 90)
    print(f"SUMMARY: {MONTH_LABEL.upper()} (${BALANCE:.0f}, {RISK_PCT*100:.0f}% risk, adaptive R:R)")
    print("=" * 90)
    print(header)
    print(sep)
    for strat, sym, tf, rr, nsig, nt, wr, pf, pfusd, net, fe, ret in sorted(results, key=lambda x: x[9]):
        print(fmt_line(strat, sym, tf, rr, nsig, nt, wr, pf, pfusd, net, fe, ret))

    print("\n" + "=" * 90)
    print("RESEARCH COMPLETE — no MT5 writes, no live trading, no fabrication.")
    print(f"All numbers computed from db/trading.db, {MONTH_LABEL}.")
    print("=" * 90)


if __name__ == "__main__":
    _ap = argparse.ArgumentParser(description=__doc__)
    _ap.add_argument("--month", default=os.environ.get("REPORT_MONTH", "2026-09"),
                     help="calendar month as YYYY-MM (default 2026-09)")
    _ap.add_argument("--balance", type=float,
                     default=float(os.environ.get("REPORT_BALANCE", "1000")))
    _ap.add_argument("--risk-pct", type=float,
                     default=float(os.environ.get("REPORT_RISK_PCT", "0.02")),
                     help="risk per trade as a fraction, e.g. 0.02 = 2%")
    _a = _ap.parse_args()
    # Re-derive the window and money model from the CLI/env, since the module
    # constants were already bound at import time.
    SEP_START, SEP_END = _window(_a.month)
    MONTH_START, MONTH_END = SEP_START, SEP_END
    MONTH_LABEL = time.strftime("%B %Y", time.gmtime(SEP_START))
    BALANCE = _a.balance
    RISK_PCT = _a.risk_pct
    RISK_USD = BALANCE * RISK_PCT
    globals().update(SEP_START=SEP_START, SEP_END=SEP_END, MONTH_START=MONTH_START,
                     MONTH_END=MONTH_END, MONTH_LABEL=MONTH_LABEL, BALANCE=BALANCE,
                     RISK_PCT=RISK_PCT, RISK_USD=RISK_USD)
    main()
