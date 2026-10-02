"""V15: Session-Break Liquidity Sweep (XAUUSD, H1 range + M5 trigger)

USER RULES, implemented literally:
  Step 1  Range = high/low of the FIRST H1 candle after the daily session break.
  Step 2  On M5, wait for a sweep. Low breaks -> look for BUYS.
          High breaks -> look for SELLS.
  Step 3  Entry confirmation, BOTH required:
            (a) a candle BODY closes back inside the range
                (a wick back in does not count -- body close only)
            (b) price on the correct side of the 9 EMA
                (above for buys, below for sells)
  Step 4  Stop: buys below the sweep low.
          Sells: above the first swing high formed after the sweep.
          (This repo has no red+green swing primitive, so sells use the
          post-sweep swing high computed from the M5 bars after the sweep.
          Documented deviation -- see SELL STOP note in the report.)
  Step 5  Target: fixed 1:1.5 R:R.
          Optional variant: at 1.5R move SL to breakeven and trail.
  If stopped out -> wait for a FRESH setup (a new sweep), never re-enter
  the same one.

HONEST CONSTRAINTS, measured not assumed:
  - M5 history for XAUUSD is only 2026-02-16 -> 2026-10-01 (193 broker days).
    H1 has 10 years, M5 does not. So V15 has ~150 candidate sweeps max.
  - 1:1.5 on an M5 sweep means a stop of roughly 20-60 pips, which is
    2.5-7x the 8-pip round-trip cost. That is the ONLY reason this idea is
    worth testing at all. V1-V10 all died because their drift sat below cost.

SELECTION RULE, stated before running:
  Break hour and stop variant are searched on TRAIN only. VAL/OOS are
  measurement. No parameter is chosen after seeing an OOS number.

Read-only vs db/trading.db. No MT5 writes. No live trading.

Usage:
    ./.venv/Scripts/python.exe research/v15_sweep.py
"""
from __future__ import annotations

import argparse
import itertools
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backtest import compute_metrics, Trade
from montecarlo import run_monte_carlo, MCConfig

from research.v15_sweep_probe import load, build_ranges, COST_PIPS, PIP, POINT

BROKER_OFFSET = 3
EMA_FAST = 9                 # user's 9 EMA
MIN_TRAIN_TRADES = 15
MAX_HOLD_BARS = 24           # 24 * M5 = 2 hours


def add_ema(m5: pd.DataFrame) -> pd.DataFrame:
    from indicators import ema
    m5 = m5.copy()
    m5["ema9"] = ema(m5["close"], EMA_FAST)
    return m5


def swing_high_after(m5: pd.DataFrame, i: int, lookback: int = 6) -> float:
    """Highest high in the bars AFTER the sweep (the user's 'first swing')."""
    w = m5.iloc[i:i + 1 + lookback]
    if w.empty:
        return float(m5.iloc[i]["high"])
    return float(w["high"].max())


def build_setups(m5: pd.DataFrame, rng: pd.DataFrame, break_hour: int) -> list[dict]:
    """One setup per (day, side). Applies the user's Step 2 + Step 3."""
    out = []
    for _, r in rng.iterrows():
        start = r["ts_broker_epoch"]
        win = m5[(m5["ts_broker_epoch"] >= start)
                 & (m5["ts_broker_epoch"] <= start + 18 * 300)]
        if win.empty:
            continue
        for side in ("BUY", "SELL"):
            if side == "BUY":
                brk = win[win["low"] < r["rng_low"]]
                if brk.empty:
                    continue
                i = int(brk.index[0])
                sweep_extreme = float(brk["low"].iloc[0])
                rng_level = r["rng_low"]
                ema_ok = lambda c: c > m5.iloc[i]["ema9"]
            else:
                brk = win[win["high"] > r["rng_high"]]
                if brk.empty:
                    continue
                i = int(brk.index[0])
                sweep_extreme = float(brk["high"].iloc[0])
                rng_level = r["rng_high"]
                ema_ok = lambda c: c < m5.iloc[i]["ema9"]

            # Step 3a: BODY closes back inside (not a wick) -> search forward
            entry_i = -1
            for j in range(i + 1, min(i + 12, len(m5) - 1)):
                c = float(m5.iloc[j]["close"])
                inside = c > rng_level if side == "BUY" else c < rng_level
                if inside and ema_ok(c):
                    entry_i = j
                    break
            if entry_i < 0:
                continue

            # Step 4: stop
            if side == "BUY":
                stop = sweep_extreme
            else:
                stop = swing_high_after(m5, i)
            entry = float(m5.iloc[entry_i + 1]["open"]) if entry_i + 1 < len(m5) else np.nan
            if not np.isfinite(entry):
                continue
            risk = (entry - stop) if side == "BUY" else (stop - entry)
            if risk <= 0:
                continue   # stop already behind entry -> invalid setup
            out.append({
                "side": side, "sweep_i": i, "entry_i": entry_i + 1,
                "entry": entry, "stop": stop, "risk": risk,
                "sweep_extreme": sweep_extreme, "rng_level": float(rng_level),
                "day_epoch": start, "ts": m5.iloc[entry_i + 1]["bt"],
            })
    return out


def run(m5: pd.DataFrame, setups: list[dict], *, rr: float,
        breakeven: bool, max_hold: int) -> tuple[list[Trade], dict]:
    trades: list[Trade] = []
    used_days = set()
    for s in sorted(setups, key=lambda x: x["entry_i"]):
        # "fresh setup only": one position at a time
        if trades and s["entry_i"] <= trades[-1].exit_bar:
            continue
        if s["day_epoch"] in used_days:
            continue   # at most one trade per day per this rule set
        used_days.add(s["day_epoch"])

        i = s["entry_i"]
        entry, stop, risk = s["entry"], s["stop"], s["risk"]
        tgt = entry + risk * rr if s["side"] == "BUY" else entry - risk * rr
        last = min(i + max_hold, len(m5) - 1)
        if last <= i:
            continue

        cur_stop = stop
        exit_p, reason, hold, done = None, "target", 0, False
        for j in range(i + 1, last + 1):
            b = m5.iloc[j]
            hold = j - i
            if s["side"] == "BUY":
                hit_stop = b["low"] <= cur_stop
                hit_tgt = b["high"] >= tgt
            else:
                hit_stop = b["high"] >= cur_stop
                hit_tgt = b["low"] <= tgt
            if hit_stop:
                exit_p, reason, done = cur_stop, "stop", True
                break
            if hit_tgt:
                if breakeven:
                    cur_stop = entry          # trail to BE, then keep holding
                    exit_p, reason = tgt, "target_be"
                else:
                    exit_p, reason, done = tgt, "target", True
                break
        if not done:
            j = last
            hold = j - i
            exit_p = float(m5.iloc[j]["close"])
            reason = "time_be" if (breakeven and cur_stop != stop) else "time"

        pips = (exit_p - entry) / PIP if s["side"] == "BUY" else (entry - exit_p) / PIP
        trades.append(Trade(
            entry_bar=i, entry_price=entry, side=s["side"], stop=stop,
            target=tgt, exit_bar=i + hold, exit_price=exit_p,
            exit_reason=reason, points=pips * PIP / POINT,
            cost_pips=COST_PIPS, net_pips=pips - COST_PIPS,
            duration_bars=hold))
    return trades, compute_metrics(trades)


def line(tag: str, m: dict) -> str:
    return (f"{tag:9s} net={m.get('net_pips', 0):8.1f} "
            f"PF={m.get('profit_factor', float('nan')):5.2f} "
            f"t={m.get('total_trades', 0):4d} "
            f"win%={m.get('win_rate', 0) * 100:4.1f} "
            f"maxDD={m.get('max_drawdown_pips', 0):7.1f}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="XAUUSD")
    a = ap.parse_args()

    h1 = load(a.symbol, "H1")
    m5 = add_ema(load(a.symbol, "M5"))
    print("=" * 78)
    print(f"V15 SESSION-BREAK LIQUIDITY SWEEP  [{a.symbol}]")
    print("=" * 78)
    print(f"  H1 {len(h1)} bars | M5 {len(m5)} bars  "
          f"{m5['bt'].iloc[0].date()} -> {m5['bt'].iloc[-1].date()}")
    print(f"  round-trip cost {COST_PIPS:.0f} pips | EMA filter = EMA{EMA_FAST}")
    print(f"  M5 depth is ONLY {m5['bt'].dt.date.nunique()} broker days -- "
          f"every number below is LOW CONFIDENCE")

    all_rows = []
    for bh in (0, 6, 7, 8, 12, 13):
        rng = build_ranges(h1, bh)
        rng = rng[rng["ts_broker_epoch"].isin(m5["ts_broker_epoch"])]
        if rng.empty:
            continue
        su = build_setups(m5, rng, bh)
        for s in su:
            s["break_hour"] = bh
        all_rows.extend(su)
        print(f"  break {bh:02d}:00 -> {len(su):3d} qualifying setups "
              f"(body-close inside + EMA{EMA_FAST} side)")

    if not all_rows:
        print("\n  NO qualifying setups anywhere. VERDICT: INCONCLUSIVE "
              "(sample too small).")
        return

    setup_df = pd.DataFrame(all_rows)
    print(f"\n  total qualifying setups across all break hours: {len(setup_df)}")
    print(f"  BUY {int((setup_df['side'] == 'BUY').sum())} / "
          f"SELL {int((setup_df['side'] == 'SELL').sum())}")
    med = setup_df["risk"].median() / PIP
    print(f"  median stop distance = {med:.1f} pips "
          f"({med / COST_PIPS:.1f}x round-trip cost)")

    # time-ordered split: TRAIN = first 60%, VAL = next 20%
    setup_df = setup_df.sort_values("entry_i").reset_index(drop=True)
    n = len(setup_df)
    tr = setup_df.iloc[:int(n * 0.6)]
    va = setup_df.iloc[int(n * 0.6):int(n * 0.8)]
    te = setup_df.iloc[int(n * 0.8):]
    print(f"\n  split by TIME: TRAIN {len(tr)} | VAL {len(va)} | TEST {len(te)} setups")
    print(f"  TRAIN spans {tr['ts'].iloc[0].date()} -> {tr['ts'].iloc[-1].date()}")
    print(f"  TEST  spans {te['ts'].iloc[0].date()} -> {te['ts'].iloc[-1].date()}")

    grid = list(itertools.product((6, 7, 12), (1.5, 2.0), (False, True), (12, 24)))
    print(f"\n[1] TRAIN search ({len(grid)} combos: break_hour x rr x be x hold)")
    rows = []
    for bh, rr, be, hold in grid:
        t, m = run(m5, tr[tr["break_hour"] == bh].to_dict("records"),
                   rr=rr, breakeven=be, max_hold=hold)
        rows.append((m.get("net_pips", float("nan")), m,
                     {"break_hour": bh, "rr": rr, "be": be, "hold": hold}))
    ok = [r for r in rows if r[1].get("total_trades", 0) >= MIN_TRAIN_TRADES]
    if not ok:
        best = max(rows, key=lambda r: (r[1].get("total_trades", 0)))
        print(f"  NO combo reached {MIN_TRAIN_TRADES} TRAIN trades. "
              f"best={best[1].get('total_trades', 0)} trades")
        print("  VERDICT: INCONCLUSIVE - sample size, not profitability, is "
              "the binding limit.")
        return
    ok.sort(key=lambda r: r[0], reverse=True)
    for net, m, p in ok[:5]:
        print("    " + line("", m) + f"  {p}")
    best_p = ok[0][2]
    print(f"  SELECTED on TRAIN only: {best_p}")

    def sel(frame):
        return frame[frame["break_hour"] == best_p["break_hour"]].to_dict("records")

    _, vm = run(m5, sel(va), rr=best_p["rr"], breakeven=best_p["be"],
                max_hold=best_p["hold"])
    print(f"\n[2] VAL (never searched): {line('', vm)}")

    print("\n[3] Walk-forward over TIME-ordered setups (measurement only):")
    is_n, oos_n, oos_t = [], [], 0
    # NOTE: np.array_split() converts a DataFrame to numpy ARRAYS, which have
    # no .to_dict("records"). Use positional iloc slicing on a copy.
    bh_rows = setup_df[setup_df["break_hour"] == best_p["break_hour"]].reset_index(drop=True)
    edges = np.linspace(0, len(bh_rows), 5, dtype=int)
    frames = [bh_rows.iloc[edges[k]:edges[k + 1]] for k in range(4)
              if edges[k + 1] > edges[k]]
    for k in range(0, len(frames) - 1):
        _, im = run(m5, frames[k].to_dict("records"), rr=best_p["rr"],
                    breakeven=best_p["be"], max_hold=best_p["hold"])
        _, om = run(m5, frames[k + 1].to_dict("records"), rr=best_p["rr"],
                    breakeven=best_p["be"], max_hold=best_p["hold"])
        is_n.append(im.get("net_pips", 0))
        oos_n.append(om.get("net_pips", 0))
        oos_t += om.get("total_trades", 0)
        print(f"  fold{k + 1}: IS {im.get('net_pips', 0):8.1f} "
              f"({im.get('total_trades', 0):3d}t) -> "
              f"OOS {om.get('net_pips', 0):8.1f} ({om.get('total_trades', 0):3d}t)")
    is_m, oos_m = float(np.mean(is_n)), float(np.mean(oos_n))
    deg = 0.0 if is_m == 0 else max(0.0, 1.0 - oos_m / is_m)
    print(f"  IS_mean={is_m:.1f} OOS_mean={oos_m:.1f} "
          f"degradation={deg:.2f} OOS_trades={oos_t}")

    full = setup_df[setup_df["break_hour"] == best_p["break_hour"]].to_dict("records")
    trades, fm = run(m5, full, rr=best_p["rr"], breakeven=best_p["be"],
                     max_hold=best_p["hold"])
    print(f"\n[4] FULL: {line('', fm)}")
    if trades:
        print(f"     sharpe={fm.get('sharpe', 0):.2f} "
              f"avg_dur={fm.get('avg_duration_bars', 0):.1f} bars "
              f"max_loss_streak={fm.get('longest_loss_streak', 0)}")
        print(f"     BUY {int((pd.Series([t.side for t in trades]) == 'BUY').sum())} "
              f"/ SELL {int((pd.Series([t.side for t in trades]) == 'SELL').sum())}")
        for k, v in pd.Series([t.exit_reason for t in trades]).value_counts().items():
            print(f"       {k:12s} {v:3d} ({v / len(trades) * 100:.0f}%)")

    if len(trades) >= 10:
        mc = run_monte_carlo(trades, MCConfig(
            n_iterations=2000, seed=42, shuffle=True, scatter_pct=0.10,
            jitter_pct=0.05, ruin_threshold=-200.0))
        print(f"\n[5] MC: net_mean={mc.net_mean:.0f} net_p5={mc.net_p5:.0f} "
              f"PF_p5={mc.profit_factor_p5:.2f} "
              f"ruin={mc.ruin_prob * 100:.1f}% robust={mc.is_robust}")
    else:
        mc = None
        print(f"\n[5] MC SKIPPED - only {len(trades)} trades (need >=10)")

    print("\n[6] MONTHLY spread (is it one lucky month?)")
    ts = pd.Series([m5.iloc[t.entry_bar]["bt"] for t in trades])
    net_by_m = (pd.DataFrame({"ts": ts.values,
                              "net": [t.net_pips for t in trades]})
                .set_index("ts")["net"]
                .groupby(lambda d: d.strftime("%Y-%m")).sum())
    print("   " + "  ".join(f"{k}:{v:+.0f}" for k, v in net_by_m.items()))
    pos_m = int((net_by_m > 0).sum())
    print(f"   profitable months: {pos_m}/{len(net_by_m)}")

    print("\n[7] MONEY view ($1,000 balance, 0.01 lot minimum):")
    net = sum(t.net_pips for t in trades)
    usd = net * PIP * 0.01
    print(f"    net ${usd:,.2f} on $1,000 = {usd / 10:+.2f}% over "
          f"{(ts.iloc[-1] - ts.iloc[0]).days} days")

    print("\n" + "-" * 78)
    g1 = ok[0][0] > 0
    g2 = vm.get("net_pips", 0) > 0
    g3 = deg <= 0.50 and oos_t >= 10
    g4 = bool(mc.is_robust) if mc is not None else False
    for nm, v, det in (("GATE1 train>0", g1, f"net={ok[0][0]:.0f}"),
                       ("GATE2 val>0", g2, f"net={vm.get('net_pips', 0):.0f}"),
                       ("GATE3 oos deg<=0.50", g3,
                        f"deg={deg:.2f} oos_t={oos_t}"),
                       ("GATE4 monte-carlo robust", g4,
                        "" if mc is None else f"ruin={mc.ruin_prob * 100:.0f}%")):
        print(f"  {'PASS' if v else 'FAIL'}  {nm:26s} {det}")
    passed = g1 and g2 and g3 and g4
    print(f"  => {'ACCEPT' if passed else 'REJECT'}")
    if not passed and len(trades) < 30:
        print("  => BUT n<30 over <1 year of M5: treat as INCONCLUSIVE either way.")
    print("-" * 78)
    print("\nRead-only vs db/trading.db. No MT5 writes. No live trading.")


if __name__ == "__main__":
    main()