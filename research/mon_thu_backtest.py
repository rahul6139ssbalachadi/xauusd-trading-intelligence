"""
MON-THU backtest: 2026-09-28 (Mon) .. 2026-10-01 (Thu), $1000 balance, 2% risk/trade.

Tests ALL approved/research strategies across ALL available timeframes separately,
with R:R 3.0 and 4.0.

Strategies:
  - V11: XAUUSD_D1_MOMENTUM_BREAKOUT  (EMA21>EMA55 + top-5% body, LONG-only)
  - V12: XAUUSD_H1_MOMENTUM_BREAKOUT  (same logic, H1 TF)
  - V13: BTCUSD_DQR_SWING_REVERSAL    (D1 level-sweep + rejection + impulse)

Logic is read-only: queries db/trading.db only. No MT5 writes.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from datetime import datetime, timezone

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from market_data import config as cfg
from indicators import ema, atr, rsi
from backtest import compute_metrics, Trade

DB = cfg.PROJECT_ROOT / cfg.load_settings()["paths"]["db"]

MON_START = int(datetime(2026, 9, 28, 0, 0, 0, tzinfo=timezone.utc).timestamp())
THU_END   = int(datetime(2026, 10, 2, 0, 0, 0, tzinfo=timezone.utc).timestamp())  # exclusive

BALANCE    = 1000.0
RISK_PCT   = 0.02          # 2% per trade = $20
RISK_USD   = BALANCE * RISK_PCT  # $20
RR_RATIOS  = [3.0, 4.0]

# Gold: 1 pip = $0.10 per standard lot; BTCUSD: 1 pip = $1 per lot
PIP_VAL = {"XAUUSD": 0.10, "BTCUSD": 1.0}
MIN_LOTS = 0.01
LOT_STEP = 0.01
MAX_LOTS = 10.0
SLIPPAGE_PIPS = 0.5  # round-trip slippage

# Per-TF min_atr_pct for V11 momentum (adaptive — lower TF = lower absolute ATR%)
TF_MIN_ATR_PCT = {
    "D1":  0.005,   # 0.5% daily ATR (typical for gold D1)
    "H1":  0.0015,  # 0.15% hourly
    "M15": 0.0008,  # 0.08% per 15min
    "M5":  0.0004,  # 0.04% per 5min
    "M1":  0.0002,  # 0.02% per minute
}

TF_ATR_MULT = {
    "D1":  1.5,
    "H1":  0.8,
    "M15": 1.0,
    "M5":  1.5,
    "M1":  1.5,
}

TF_RANK_WINDOW = {
    "D1":  60,
    "H1":  60,
    "M15": 60,
    "M5":  60,
    "M1":  20,
}


def load(symbol: str, tf: str, start_ts=None, end_ts=None) -> pd.DataFrame:
    """Load bars. Default filters to MON-THU window; pass start_ts/end_ts for
    broader pulls (e.g. full D1 history for level construction)."""
    if start_ts is None:
        start_ts = MON_START
    if end_ts is None:
        end_ts = THU_END
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


def v11_signals(df, body_pct_threshold=0.95, min_atr_pct=0.005, atr_mult_stop=1.5, rank_window=60):
    """V11 momentum: EMA21>EMA55 + top-5% body, long-only."""
    df = df.copy()
    df["ema21"] = ema(df["close"], 21)
    df["ema55"] = ema(df["close"], 55)
    df["atr14"] = atr(df["high"], df["low"], df["close"], 14)
    df["body"] = (df["close"] - df["open"]) / df["open"]
    df["body_abs"] = df["body"].abs()
    rw = max(20, rank_window // 3)
    df["body_pct"] = df["body_abs"].rolling(rank_window, min_periods=rw).rank(pct=True)

    signals = []
    for i in range(len(df)):
        row = df.iloc[i]
        if i < 55:
            continue
        if pd.isna(row["ema21"]) or pd.isna(row["ema55"]) or row["ema21"] <= row["ema55"]:
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
            "entry_bar": entry_bar,
            "entry_ts": df.iloc[entry_bar]["ts"],
            "entry_price": entry_price,
            "atr": atr_val,
            "stop": stop,
            "dir": "LONG",
            "bias_bar": i,
        })
    return signals


def backtest_priced(signals, df, *, rr, symbol, min_holding=1):
    pip_val = PIP_VAL[symbol]
    trades = []
    equity = 0.0
    n = len(df)

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

        if side == "LONG":
            risk_pips = (entry_price - stop) / pip_val
            target = entry_price + risk_pips * pip_val * rr
        else:
            risk_pips = (stop - entry_price) / pip_val
            target = entry_price - risk_pips * pip_val * rr

        if risk_pips <= 0 or np.isnan(risk_pips):
            continue

        lots = size_lots(RISK_USD, risk_pips, pip_val)
        if lots == 0:
            continue

        # intrabar exit
        exited = False
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
                # spread is in points; convert to $: spread_points * pip_value (since 1 point = 1 pip for XAUUSD? No)
                # XAUUSD: 1 pip = 0.10, 1 point = 0.01, so 1 pip = 10 points
                # spread column is in points (broker ticks). 1 pip = 10 points for XAUUSD
                pts_per_pip = 10 if symbol == "XAUUSD" else 1
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


def main():
    print("=" * 80)
    print("MON-THU BACKTEST: 2026-09-28 (Mon) .. 2026-10-01 (Thu)")
    print(f"Balance=${BALANCE:.0f}, Risk={RISK_PCT*100:.0f}%/trade (${RISK_USD:.0f}), R:R in {RR_RATIOS}")
    print("=" * 80)

    results_summary = []

    # ---- V11: XAUUSD momentum on all TFs ----
    print("\n--- V11: XAUUSD_D1_MOMENTUM_BREAKOUT (EMA21>EMA55 + top-5% body, LONG-only) ---\n")
    for tf in ["D1", "H1", "M15", "M5", "M1"]:
        df = load("XAUUSD", tf)
        if len(df) < 55:
            print(f"  [{tf}] {len(df)} bars — too few\n")
            continue
        min_atr = TF_MIN_ATR_PCT.get(tf, 0.005)
        atr_mult = TF_ATR_MULT.get(tf, 1.5)
        rw = TF_RANK_WINDOW.get(tf, 60)
        for rr in RR_RATIOS:
            sigs = v11_signals(df, body_pct_threshold=0.95, min_atr_pct=min_atr,
                               atr_mult_stop=atr_mult, rank_window=rw)
            m, trades = backtest_priced(sigs, df, rr=rr, symbol="XAUUSD", min_holding=1)
            nt = m.get("total_trades", 0)
            wr = m.get("win_rate", 0) if trades else 0
            pf = m.get("profit_factor", 0) if trades else 0
            net = m["net_usd"]
            fe = m["final_equity"]
            ret = m["return_pct"]
            pfusd = m["profit_factor_usd"]
            # count skipped (unsizeable at 0.01 floor)
            skipped = 0
            for s in sigs:
                stop = s.get("stop") or (s["entry_price"] - 1.5 * s["atr"])
                risk_pips = abs(s["entry_price"] - stop) / PIP_VAL["XAUUSD"]
                if size_lots(RISK_USD, risk_pips, PIP_VAL["XAUUSD"]) == 0:
                    skipped += 1
            print(f"  V11  XAUUSD {tf:3s} R:R={rr:.1f} | sigs={len(sigs):3d} trades={nt:3d} "
                  f"win={wr*100:4.0f}% PF={pf:.2f} PF$={pfusd:.2f} net=${net:+8.2f} "
                  f"final=${fe:.2f} ({ret:+.1f}%) skip={skipped}")
            results_summary.append(("V11", "XAUUSD", tf, rr, len(sigs), nt, wr, pf, pfusd, net, fe, ret, skipped))

    # ---- V12: XAUUSD H1 momentum (long-only, same logic) ----
    # H1 is already covered by V11's H1 row above. V12's def uses H1-specific params.
    # We run V12 as a separate strategy with H1 as its PRIMARY tf (not all TFs).
    print("\n--- V12: XAUUSD_H1_MOMENTUM_BREAKOUT (H1 momentum, 2% risk, R:R variable) ---\n")
    tf = "H1"
    df = load("XAUUSD", tf)
    if len(df) < 55:
        print(f"  [{tf}] {len(df)} bars — too few")
    else:
        for rr in RR_RATIOS:
            sigs = v11_signals(df, body_pct_threshold=0.95, min_atr_pct=0.0015,
                               atr_mult_stop=0.8, rank_window=60)
            m, trades = backtest_priced(sigs, df, rr=rr, symbol="XAUUSD", min_holding=1)
            nt = m.get("total_trades", 0)
            wr = m.get("win_rate", 0) if trades else 0
            pf = m.get("profit_factor", 0) if trades else 0
            net = m["net_usd"]
            fe = m["final_equity"]
            ret = m["return_pct"]
            pfusd = m["profit_factor_usd"]
            skipped = sum(1 for s in sigs if (lambda stop: size_lots(RISK_USD, abs(s["entry_price"] - stop) / PIP_VAL["XAUUSD"], PIP_VAL["XAUUSD"]) == 0)(
                s.get("stop") or (s["entry_price"] - 0.8 * s["atr"])))
            print(f"  V12  XAUUSD {tf:3s} R:R={rr:.1f} | sigs={len(sigs):3d} trades={nt:3d} "
                  f"win={wr*100:4.0f}% PF={pf:.2f} PF$={pfusd:.2f} net=${net:+8.2f} "
                  f"final=${fe:.2f} ({ret:+.1f}%) skip={skipped}")
            results_summary.append(("V12", "XAUUSD", tf, rr, len(sigs), nt, wr, pf, pfusd, net, fe, ret, skipped))

    # ---- V13: BTCUSD DQR level-sweep ----
    # V13 builds D1 swing levels from FULL D1 history (levels persist forward),
    # then checks for sweeps on lower TFs during MON-THU only.
    print("\n--- V13: BTCUSD_DQR_SWING_REVERSAL (D1 level-sweep + rejection + impulse) ---\n")
    # Load full D1 history for level construction (no date filter)
    d1_full = load("BTCUSD", "D1", start_ts=0, end_ts=int(2**31))
    if len(d1_full) < 10:
        print("  BTCUSD D1: too few bars, skip V13\n")
    else:
        d1_full["atr14"] = atr(d1_full["high"], d1_full["low"], d1_full["close"], 14)
        levels = []
        for i in range(2, len(d1_full) - 2):
            h = d1_full.iloc[i]
            if (h["high"] > d1_full.iloc[i-2]["high"] and h["high"] > d1_full.iloc[i-1]["high"]
                and h["high"] > d1_full.iloc[i+1]["high"] and h["high"] > d1_full.iloc[i+2]["high"]):
                levels.append({"price": h["high"], "type": "R", "ts": h["ts"], "atr14": h["atr14"]})
            if (h["low"] < d1_full.iloc[i-2]["low"] and h["low"] < d1_full.iloc[i-1]["low"]
                and h["low"] < d1_full.iloc[i+1]["low"] and h["low"] < d1_full.iloc[i+2]["low"]):
                levels.append({"price": h["low"], "type": "S", "ts": h["ts"], "atr14": h["atr14"]})
        # Only keep levels from before the MON-THU window
        levels = [l for l in levels if l["ts"].value <= MON_START * 1_000_000_000]
        print(f"  D1 levels (historical, valid for MON-THU): {len(levels)}\n")
        for tf in ["D1", "H1", "M15", "M5", "M1"]:
            df = load("BTCUSD", tf)
            if len(df) < 30:
                print(f"  [{tf}] {len(df)} bars — too few, skip")
                continue
            df["atr14"] = atr(df["high"], df["low"], df["close"], 14)
            for rr in RR_RATIOS:
                sigs = v13_signals(df, levels, rr=rr)
                m, trades = backtest_priced(sigs, df, rr=rr, symbol="BTCUSD", min_holding=1)
                nt = m.get("total_trades", 0)
                wr = m.get("win_rate", 0) if trades else 0
                pf = m.get("profit_factor", 0) if trades else 0
                net = m["net_usd"]
                fe = m["final_equity"]
                ret = m["return_pct"]
                pfusd = m["profit_factor_usd"]
                print(f"  V13  BTCUSD {tf:3s} R:R={rr:.1f} | sigs={len(sigs):3d} trades={nt:3d} "
                      f"win={wr*100:4.0f}% PF={pf:.2f} PF$={pfusd:.2f} net=${net:+8.2f} "
                      f"final=${fe:.2f} ({ret:+.1f}%)")
                results_summary.append(("V13", "BTCUSD", tf, rr, len(sigs), nt, wr, pf, pfusd, net, fe, ret, 0))

    # ---- Summary ----
    print("\n" + "=" * 80)
    print("SUMMARY: MON-THU 2026-09-28..10-01 ($1000, 2% risk, R:R 3.0 & 4.0)")
    print("=" * 80)
    hdr = f"{'Strat':6s} {'Sym':6s} {'TF':4s} {'RR':>4s} {'Sig':>4s} {'Trd':>4s} {'Win%':>5s} {'PF':>5s} {'PF$':>5s} {'Net$':>9s} {'Final$':>8s} {'Ret%':>6s} {'Skip':>4s}"
    print(hdr)
    print("-" * 80)
    for strat, sym, tf, rr, nsig, nt, wr, pf, pfusd, net, fe, ret, sk in sorted(results_summary, key=lambda x: x[9]):
        print(f"{strat:6s} {sym:6s} {tf:4s} {rr:4.1f} {nsig:4d} {nt:4d} {wr*100:5.0f} {pf:5.2f} {pfusd:5.2f} "
              f"{net:+9.2f} {fe:8.2f} {ret:+6.1f} {sk:4d}")

    print("\n" + "=" * 80)
    print("RESEARCH COMPLETE — no MT5 writes, no live trading, no fabrication.")
    print("All numbers computed from db/trading.db, MON-THU 2026-09-28..10-01.")
    print("=" * 80)


def v13_signals(df_exec, d1_levels, *, rr):
    """V13 DQR: sweep D1 S/R levels, rejection bar, impulse confirmation."""
    signals = []
    for i in range(len(df_exec)):
        row = df_exec.iloc[i]
        o, h, l, c = row["open"], row["high"], row["low"], row["close"]
        ts = row["ts"]
        for lvl in d1_levels:
            if ts < lvl["ts"]:
                continue
            lp = lvl["price"]
            tol = 0.1 * lvl["atr14"]
            if lvl["type"] == "S":
                if l >= lp - tol:
                    continue
                if c <= lp:
                    continue
                body = c - o
                if body <= 0:
                    continue
                close_pos = (c - l) / (h - l + 1e-9)
                if close_pos < 0.70:
                    continue
                if i + 1 >= len(df_exec):
                    continue
                nr = df_exec.iloc[i + 1]
                n_body = nr["close"] - nr["open"]
                if pd.isna(nr["atr14"]) or abs(n_body) < 0.80 * nr["atr14"]:
                    continue
                entry_bar = i + 2
                if entry_bar >= len(df_exec):
                    continue
                entry_price = df_exec.iloc[entry_bar]["open"]
                atr_val = nr["atr14"]
                stop = min(l, nr["low"]) - 1.5 * atr_val
                target = entry_price + (entry_price - stop) * rr
                signals.append({
                    "entry_bar": entry_bar, "entry_ts": df_exec.iloc[entry_bar]["ts"],
                    "entry_price": entry_price, "stop": stop, "target": target,
                    "atr": atr_val, "dir": "LONG",
                })
            elif lvl["type"] == "R":
                if h <= lp + tol:
                    continue
                if c >= lp:
                    continue
                body = o - c
                if body <= 0:
                    continue
                close_pos = (h - c) / (h - l + 1e-9)
                if close_pos < 0.70:
                    continue
                if i + 1 >= len(df_exec):
                    continue
                nr = df_exec.iloc[i + 1]
                n_body = nr["open"] - nr["close"]
                if pd.isna(nr["atr14"]) or abs(n_body) < 0.80 * nr["atr14"]:
                    continue
                entry_bar = i + 2
                if entry_bar >= len(df_exec):
                    continue
                entry_price = df_exec.iloc[entry_bar]["open"]
                atr_val = nr["atr14"]
                stop = max(h, nr["high"]) + 1.5 * atr_val
                target = entry_price - (stop - entry_price) * rr
                signals.append({
                    "entry_bar": entry_bar, "entry_ts": df_exec.iloc[entry_bar]["ts"],
                    "entry_price": entry_price, "stop": stop, "target": target,
                    "atr": atr_val, "dir": "SELL",
                })
    return signals


if __name__ == "__main__":
    main()
