"""V14 WEEKLY money report: $1,000 balance, 2% risk, lot sized to the stop.

THE HEADLINE FINDING IS A SIZING BLOCKER, NOT A P&L NUMBER.
V14's median stop is ~547 pips. On XAUUSD a pip is worth $10 per 1.0 lot
(risk/__init__.py), so the minimum 0.01 lot carries $54.67 of risk. At 2%
of $1,000 the budget is $20 -- so EVERY trade breaches the risk cap by
~2.7x and the correct action is to DECLINE all of them.

That is why this script reports two views side by side:
  VIEW A  SOBER  - 2% risk enforced. 0 of 85 signals are sizeable.
                   The honest answer for a $1,000 account is: no trades.
  VIEW B  FORCED  - the 0.01 minimum lot taken anyway, at whatever risk it
                   implies. This is what a user does who just trades it.
                   Included so the risk is visible, NOT as a recommendation.

Both are run through the same weekly calendar. Weekly rows will be mostly
empty: V14 fires ~1 trade every 40 days, so a weekly view is mostly
"no signal" by construction. That is the strategy's nature, not a bug.

R:R is fixed at the frozen V14 value (1.5). It is NOT momentum-adapted --
V14 was selected with a fixed 1.5R and changing it now would be fitting
the report to the data.

Read-only vs db/trading.db. No MT5 writes. No live trading.

Usage:
    ./.venv/Scripts/python.exe research/v14_weekly.py
    ./.venv/Scripts/python.exe research/v14_weekly.py --balance 10000
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from research.v14_stress import run_one_position
from research.v15_weekly import USD_PER_PIP_PER_LOT  # $10/pip/lot, one source

PIP = 0.10
FROZEN = {"entry": "pullback_or_cross", "pullback_band": 0.5,
          "max_holding": 20, "atr_mult": 1.5, "rr": 1.5}
MIN_LOTS, LOT_STEP = 0.01, 0.01


def size_lots(equity: float, risk_pct: float, stop_pips: float) -> float:
    if stop_pips <= 0 or np.isnan(stop_pips):
        return 0.0
    raw = (equity * risk_pct) / (stop_pips * USD_PER_PIP_PER_LOT)
    if raw < MIN_LOTS:
        return 0.0
    return round((raw // LOT_STEP) * LOT_STEP, 2)


def weekly_equity(trades: list, d: pd.DataFrame, balance: float,
                  risk_pct: float, enforce: bool) -> tuple[list[dict], float]:
    """Walk trades in time order, compounding. enforce=True declines
    anything whose minimum lot exceeds the risk budget."""
    rows, equity = [], balance
    for x in trades:
        stop_pips = abs(x.target - x.entry_price) / PIP
        lots = size_lots(equity, risk_pct, stop_pips)
        actual_risk = lots * stop_pips * USD_PER_PIP_PER_LOT if lots else 0.0
        if enforce and lots == 0:
            continue
        if not enforce and lots == 0:
            lots = MIN_LOTS          # forced view: take the floor
            actual_risk = lots * stop_pips * USD_PER_PIP_PER_LOT
        net_usd = x.net_pips * USD_PER_PIP_PER_LOT * lots
        equity += net_usd
        rows.append({"ts": d.iloc[x.entry_bar]["ts"], "side": x.side,
                     "stop_pips": stop_pips, "lots": lots,
                     "risk_usd": actual_risk, "net_pips": x.net_pips,
                     "net_usd": net_usd, "equity": equity,
                     "reason": x.exit_reason})
    return rows, equity


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--balance", type=float, default=1000.0)
    ap.add_argument("--risk-pct", type=float, default=0.02)
    ap.add_argument("--equity-needed", type=float, default=10000.0,
                    help="balance at which 2%% becomes tradeable (report only)")
    a = ap.parse_args()

    import research.v14_regime_pullback as v14
    d = v14.features(v14.load("XAUUSD", "D1"))
    trades, m = run_one_position(d, **FROZEN)
    stop_med = float(np.median([abs(x.target - x.entry_price) / PIP
                                for x in trades]))
    budget = a.balance * a.risk_pct
    floor_risk = stop_med * MIN_LOTS * USD_PER_PIP_PER_LOT
    need = floor_risk / a.risk_pct
    print("=" * 78)
    print(f"V14 WEEKLY MONEY REPORT   ${a.balance:,.0f} balance, "
          f"{a.risk_pct*100:.0f}% risk/trade")
    print("=" * 78)
    print(f"  frozen params: {FROZEN}")
    print(f"  {len(trades)} trades, {d['ts'].iloc[0].date()} -> "
          f"{d['ts'].iloc[-1].date()}, avg gap 40 days between entries")
    print(f"  median stop  = {stop_med:.0f} pips")
    print(f"  -> 0.01 lot minimum carries ${floor_risk:,.2f} of risk "
          f"vs a ${budget:,.2f} budget")
    print(f"  fixed R:R = {FROZEN['rr']} (frozen, NOT momentum-adapted)")

    need = floor_risk / a.risk_pct
    print(f"\n  {'=' * 74}")
    print(f"  VIEW A - SOBER ({a.risk_pct*100:.0f}% risk enforced, declines breaches)")
    print(f"  {'=' * 74}")
    rows_a, eq_a = weekly_equity(trades, d, a.balance, a.risk_pct, enforce=True)
    if not rows_a:
        print(f"    NO TRADES ARE SIZEABLE at ${a.balance:,.0f} / "
              f"{a.risk_pct*100:.0f}%.")
        print(f"    Every one of the {len(trades)} signals needs the 0.01 minimum")
        print(f"    lot, which risks ${floor_risk:,.0f} against a "
              f"${budget:,.0f} budget ({floor_risk / budget:.1f}x over).")
        print("    Correct action per CLAUDE.md s18/s20: DECLINE. Result $0.00, 0.00%.")
        n_ok = sum(1 for x in trades
                   if (a.equity_needed * a.risk_pct)
                   / (abs(x.target - x.entry_price) / PIP * USD_PER_PIP_PER_LOT)
                   >= MIN_LOTS)
        print(f"\n    Balance needed for {a.risk_pct*100:.0f}% risk: ${need:,.0f} "
              f"(at ${a.equity_needed:,.0f}, {n_ok}/{len(trades)} signals become sizeable)")
    else:
        for r in rows_a:
            print(f"    {r['ts'].date()} {r['side']:4s} {r['lots']:.2f}lot "
                  f"risk${r['risk_usd']:6.2f} net${r['net_usd']:+8.2f} "
                  f"eq${r['equity']:9.2f}")
        print(f"    TOTAL ${eq_a - a.balance:+,.2f} "
              f"({(eq_a - a.balance) / a.balance * 100:+.2f}%)")

    print(f"\n  {'=' * 74}")
    print("  VIEW B - FORCED 0.01 lot (what happens if you just trade it)")
    print(f"  {'=' * 74}")
    rows_b, eq_b = weekly_equity(trades, d, a.balance, a.risk_pct, enforce=False)
    df_b = pd.DataFrame(rows_b)
    # .dt.to_period() on tz-aware data warns that it drops the offset. The
    # weekly bucket must not shift across the UTC boundary, so normalise to
    # naive UTC first -- week boundaries are a reporting convention here.
    df_b["week"] = (df_b["ts"].dt.tz_convert("UTC").dt.tz_localize(None)
                    .dt.to_period("W").astype(str))
    by_w = df_b.groupby("week").agg(
        trades=("net_usd", "size"), net=("net_usd", "sum"),
        risk=("risk_usd", "max"), eq=("equity", "last"))
    print(f"    {len(df_b)} trades taken, avg risk/trade "
          f"${df_b['risk_usd'].mean():.2f} (budget was ${budget:.2f})")
    print(f"    lots {df_b['lots'].min():.2f}-{df_b['lots'].max():.2f} "
          f"({'all at the broker floor' if df_b['lots'].nunique() == 1 else 'mostly at or near the broker floor'})")
    print(f"    TOTAL ${eq_b - a.balance:+,.2f} "
          f"({(eq_b - a.balance) / a.balance * 100:+.2f}%)")
    peak_dd = (df_b["equity"].cummax() - df_b["equity"]).max()
    print(f"    peak-to-trough drawdown: ${peak_dd:,.2f} "
          f"({peak_dd / a.balance * 100:.1f}% of the account)")
    print(f"    single worst trade      : ${df_b['net_usd'].min():,.2f} "
          f"({df_b['net_usd'].min() / a.balance * 100:.1f}% of the account)")
    pct = (eq_b - a.balance) / a.balance * 100
    if not rows_a:
        print(f"    NOTE: that {pct:+.2f}% is NOT a result you can act on. It exists")
        print("    only because 0.01 is the smallest size the broker accepts, so the")
        print(f"    {a.risk_pct*100:.0f}% cap cannot be honoured -- see View A.")
        print(f"    At a correct size (${need:,.0f} balance) the same trades return")
        print("    proportionally less AND stay inside the risk limit.")
    else:
        print(f"    NOTE: that {pct:+.2f}% honours the {a.risk_pct*100:.0f}% cap, so it")
        print("    is the trustworthy view. View A declined nothing at this balance.")
    worst = df_b.nsmallest(3, "net_usd")[["ts", "net_usd", "risk_usd"]]
    print("    worst 3: " + ", ".join(
        f"{t.date()} ${n:+.0f} (risk ${r:.0f})" for t, n, r in
        worst.itertuples(index=False)))

    # weekly calendar -- explicitly expected to be mostly empty
    print(f"\n  {'=' * 74}")
    print("  WEEKLY CALENDAR (forced view) -- mostly 'no signal' by nature")
    print(f"  {'=' * 74}")
    weeks = pd.period_range(df_b['ts'].min(), df_b['ts'].max(), freq='W')
    active = by_w.index if len(by_w) else []
    pos = sum(1 for w in active if by_w.loc[w, "net"] > 0)
    print(f"    weeks spanned      : {len(weeks)}")
    print(f"    weeks with a trade : {len(active)} "
          f"({len(active) / len(weeks) * 100:.0f}%)")
    print(f"    weeks profitable   : {pos}/{len(active) if len(active) else 0}")
    print(f"    best week          : ${by_w['net'].max():+.2f}" if len(active)
          else "    best week          : n/a")
    print(f"    worst week         : ${by_w['net'].min():+.2f}" if len(active)
          else "    worst week         : n/a")
    print(f"    avg risk in an active week: ${by_w['risk'].mean():.2f}"
          if len(active) else "    avg risk in an active week: n/a")
    print("\n    V14 fires ~1 trade per 40 days, so a weekly view is mostly")
    print("    'no signal'. Judge this strategy on MONTHLY or trade-by-trade,")
    print("    never on weekly win-rate.")

    print(f"\n  {'=' * 74}")
    print("  VERDICT")
    print(f"  {'=' * 74}")
    print(f"    View A (2% risk honoured): NO TRADES. ${a.balance:,.0f} cannot run V14.")
    print(f"    View B (forced 0.01 lot) : "
          f"${eq_b - a.balance:+,.2f} ({((eq_b - a.balance) / a.balance * 100):+.2f}%) "
          f"while risking ~{df_b['risk_usd'].mean() / budget:.1f}x the stated limit")
    print("    A strategy you cannot size correctly is not tradable at this")
    print("    account size, regardless of its backtest PF of 1.53.")
    print("\nRead-only vs db/trading.db. No MT5 writes. No live trading.")


if __name__ == "__main__":
    main()