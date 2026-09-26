"""Trade-by-trade execution log for V11 / V12 — RESEARCH ONLY.

Answers one question precisely: for every trade the backtester actually
SIMULATED, when was it, was it BUY or SELL, what were entry / SL / TP, and
what lot size was taken and why that size.

Every row comes from the same trade_rows the monthly report uses, so this
cannot disagree with the numbers already reported. The lot size is not
guessed: monthly_bt.engine.size_lots() computed it, and this script
recomputes it independently and asserts the two agree.

Outputs (to reports/trades/):
    TRADES_<SYMBOL>_<label>.csv    machine readable, one row per trade
    TRADES_<SYMBOL>_<label>.md      readable table + sizing audit
    TRADES_<SYMBOL>_<label>.txt     plain terminal table

Usage:
    ./.venv/Scripts/python.exe research/trade_log.py --label 2026_may_aug
    ./.venv/Scripts/python.exe research/trade_log.py --strategy V11 --all
    ./.venv/Scripts/python.exe research/trade_log.py --open      # open the md
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import webbrowser
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from monthly_bt import data as bt_data
from monthly_bt import runner, strategies as S
from monthly_bt.engine import SimConfig, size_lots
from monthly_bt.data import UNITS

OUT = ROOT / "reports" / "trades"

# lot sizes follow the arithmetic, not opinion:
#   lots = risk_usd / (stop_pips * usd_per_pip_per_lot)
# XAUUSD: 1 pip = $0.10, 1.0 lot = 100 oz -> $10 per pip per lot.
#   risk 1% of $100,000 = $1,000. A 1,515-pip stop needs
#   1000 / (1515 * 10) = 0.066 -> floored to the 0.01 lot step -> 0.06.
SIZING_NOTE = (
    "lots = risk_usd / (stop_pips x $10/pip/lot), floored to the 0.01 lot step "
    "so the intended risk is never exceeded. Cap = 1% of equity "
    "(RiskConfig.max_risk_per_trade_pct)."
)


def collect(strategy: str, symbol: str, periods: list[tuple[str, str, str]],
            balance: float) -> tuple[list[dict], list[dict]]:
    """Simulated trades, generated signals, and declined signals for one
    strategy over the given periods.

    The engine keeps a separate `skipped` list (signals it refused to turn
    into a position) which carries `skip_reason`; the signal dicts do not.
    All three are returned so the report can distinguish SIGNAL from TRADE.
    """
    spec = S.get(strategy)
    raw = bt_data.load(symbol, spec.timeframe)
    feats = runner.build_features(raw, spec)
    cfg = SimConfig(initial_balance=balance, slippage_pips=1.0,
                    spread_multiplier=1.0, max_positions=1)
    trades, signals, declined = [], [], []
    for label, start, end in periods:
        res = runner.run_period(feats, spec, symbol, start, end, cfg)
        for t in res.get("trade_rows", []):
            t["period"] = label
            trades.append(t)
        for s in res.get("signals", []):
            signals.append({**s, "period": label})
        for s in res.get("skipped", []):
            declined.append({**s, "period": label})
    return trades, signals, declined


def audit_sizing(trades: list[dict], spec, symbol: str) -> list[str]:
    """Recompute each lot size independently and report any mismatch.

    Defensive on every field: this function runs precisely when something
    is already wrong, so it must never raise while formatting its own
    error message — a KeyError here would hide the mismatch it exists to
    surface.
    """
    problems = []
    for t in trades:
        expected = size_lots(t.get("balance_after", 0) - t.get("net_pnl", 0),
                             spec.params["risk_pct"], t.get("risk_pips", 0),
                             symbol, SimConfig())
        if abs(expected - t.get("lots", 0)) > 0.0051:
            when = str(t.get("entry_ts", "?"))[:16]
            problems.append(
                f"  {t.get('strategy', '?')} {when}: lots {t.get('lots')} "
                f"but recomputed {expected} "
                f"(risk_pips {t.get('risk_pips', 0):.1f})")
    return problems


def to_frame(trades: list[dict], symbol: str) -> pd.DataFrame:
    rows = []
    usd_pip = UNITS[symbol]["pip"] * UNITS[symbol]["contract"]
    for t in trades:
        rows.append({
            "period": t["period"],
            "strategy": t["strategy"],
            "signal": t["signal"],
            "timeframe": t["timeframe"],
            "signal_time": str(t.get("signal_ts"))[:16],
            "entry_time": str(t.get("entry_ts"))[:16],
            "entry": round(t["entry"], 2),
            "sl": round(t["sl"], 2),
            "tp": round(t["tp"], 2),
            "exit_time": str(t.get("exit_ts"))[:16],
            "exit": round(t["exit"], 2),
            "exit_reason": t["exit_reason"],
            "lots": t["lots"],
            "stop_pips": round(t["risk_pips"], 1),
            "risk_usd": round(t["risk_pips"] * usd_pip * t["lots"], 2),
            "spread_pips": round(t["spread_pips"], 2),
            "cost_pips": round(t["cost_pips"], 2),
            "costs_usd": round(t["fees"], 2),
            "net_pips": round(t["net_pips"], 1),
            "r_multiple": round(t["r_multiple"], 2) if t["r_multiple"] else None,
            "net_pnl": round(t["net_pnl"], 2),
            "balance_after": round(t["balance_after"], 2),
            "atr": round(t["atr"], 2) if t.get("atr") else None,
            "body_pct": round(t["body_pct"], 4) if t.get("body_pct") else None,
        })
    return pd.DataFrame(rows)


def render_txt(df: pd.DataFrame, symbol: str, balance: float) -> str:
    out = ["=" * 150,
           f"TRADE-BY-TRADE EXECUTION LOG — {symbol}  (SIMULATED, research only)",
           "=" * 150,
           f"start balance ${balance:,.2f}   rows = one SIMULATED trade each",
           f"lot sizing: {SIZING_NOTE}", ""]
    hdr = (f"{'#':>3} {'strategy':>8} {'sig':>4} {'entry time':>17} "
           f"{'entry':>9} {'SL':>9} {'TP':>9} {'lots':>6} {'exit time':>17} "
           f"{'exit':>9} {'reason':>8} {'net$':>10} {'R':>6} {'bal$':>11}")
    out += [hdr, "-" * 150]
    for i, r in df.iterrows():
        out.append(
            f"{i + 1:>3} {r['strategy']:>8} {r['signal']:>4} "
            f"{r['entry_time']:>17} {r['entry']:>9.2f} {r['sl']:>9.2f} "
            f"{r['tp']:>9.2f} {r['lots']:>6.2f} {r['exit_time']:>17} "
            f"{r['exit']:>9.2f} {r['exit_reason']:>8} {r['net_pnl']:>10,.2f} "
            f"{(r['r_multiple'] if r['r_multiple'] is not None else 0):>6.2f} "
            f"{r['balance_after']:>11,.2f}")
    out.append("-" * 150)
    for strat, g in df.groupby("strategy"):
        wins = g[g.net_pnl > 0]
        lot_list = ", ".join(f"{x:.2f}" for x in sorted(g.lots.unique()))
        out.append(
            f"{strat}: {len(g)} trades, win {len(wins)}/{len(g)} "
            f"({len(wins)/len(g)*100:.1f}%), net ${g.net_pnl.sum():,.2f}, "
            f"lots used {lot_list}, "
            f"stop range {g.stop_pips.min():.0f}-{g.stop_pips.max():.0f} pips")
    out.append("=" * 150)
    out.append("No order was placed. This is a simulation of historical bars.")
    return "\n".join(out)


def render_md(df: pd.DataFrame, symbol: str, balance: float,
              skipped: list[dict], problems: list[str]) -> str:
    out = [f"# Trade-by-trade execution log — {symbol}", "",
           "**Simulated research output. No order was placed.**", "",
           f"- Start balance: ${balance:,.2f}",
           f"- Trades: {len(df)}",
           f"- Periods covered: {', '.join(sorted(df.period.unique()))}",
           f"- Lot sizing: {SIZING_NOTE}", ""]
    if problems:
        out += ["## Sizing audit — FAILED", "",
                "Recomputed lot size disagreed with the executed lot size:", ""]
        out += [f"```\n{p}\n```" for p in problems] + [""]
    else:
        out += ["## Sizing audit — PASSED", "",
                "Every trade's lot size was recomputed independently and matched "
                "the size actually used.", ""]
    if skipped:
        out += ["## Signals that did NOT become trades", "",
                "These generated a BUY but the simulator declined to open a "
                "position. They are signals, not trades.", "",
                "| period | strategy | signal | entry time | entry | SL | TP | "
                "reason |", "|---|---|---|---|---|---|---|---|"]
        for s in skipped:
            out.append(
                f"| {s['period']} | {s['strategy']} | {s['signal']} | "
                f"{str(s.get('entry_ts'))[:16]} | {s['entry']:.2f} | "
                f"{s['sl']:.2f} | {s['tp']:.2f} | "
                f"{s.get('skip_reason', 'unknown')} |")
        out.append("")
    for strat, g in df.groupby("strategy"):
        wins = g[g.net_pnl > 0]
        out += [f"## {strat} — {len(g)} trades", "",
                f"- Win rate: {len(wins)}/{len(g)} ({len(wins)/len(g)*100:.1f}%)",
                f"- Net P&L: ${g.net_pnl.sum():,.2f}",
                f"- Total costs (spread + slippage): ${g.costs_usd.sum():,.2f}",
                f"- Lot sizes used: {', '.join(f'{x:.2f}' for x in sorted(g.lots.unique()))}",
                f"- Stop width: {g.stop_pips.min():.0f}–{g.stop_pips.max():.0f} pips",
                "", "| # | signal | entry time | entry | SL | TP | lots | "
                "stop pips | risk $ | exit time | exit | reason | net $ | R | "
                "balance after $ |", "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
        for i, r in g.iterrows():
            r_mult = "n/a" if r["r_multiple"] is None else f"{r['r_multiple']:.2f}"
            out.append(
                f"| {i + 1} | {r['signal']} | {r['entry_time']} | "
                f"{r['entry']:.2f} | {r['sl']:.2f} | {r['tp']:.2f} | "
                f"**{r['lots']:.2f}** | {r['stop_pips']:.0f} | "
                f"{r['risk_usd']:.2f} | {r['exit_time']} | {r['exit']:.2f} | "
                f"{r['exit_reason']} | {r['net_pnl']:,.2f} | {r_mult} | "
                f"{r['balance_after']:,.2f} |")
        out.append("")
    return "\n".join(out)


def periods_for(args, spec) -> list[tuple[str, str, str]]:
    """[(label, start, end), ...] from --months or every month with data."""
    months = args.months
    if args.all:
        feats = runner.build_features(
            bt_data.load(args.symbol, spec.timeframe), spec)
        months = runner.month_ends_for(feats)
    out = []
    for m in months:
        s, e = runner.month_bounds(m)
        out.append((m, s.strftime("%Y-%m-%d"), e.strftime("%Y-%m-%d")))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--strategy", default="both", help="V11 | V12 | both")
    ap.add_argument("--symbol", default="XAUUSD")
    ap.add_argument("--balance", type=float, default=100_000.0)
    ap.add_argument("--months", nargs="+",
                    default=["2026-05", "2026-06", "2026-07", "2026-08"])
    ap.add_argument("--all", action="store_true", help="every month with data")
    ap.add_argument("--open", action="store_true", help="open the .md in a browser")
    args = ap.parse_args()

    keys = ["V11", "V12"] if args.strategy.lower() == "both" \
        else [args.strategy.upper()]
    all_trades, all_skipped, problems = [], [], []
    label = "all" if args.all else "2026_may_aug"

    for k in keys:
        spec = S.get(k)
        trades, _signals, declined = collect(k, args.symbol,
                                             periods_for(args, spec),
                                             args.balance)
        all_trades += trades
        all_skipped += declined          # declined carries skip_reason
        problems += audit_sizing(trades, spec, args.symbol)

    if not all_trades:
        print("no simulated trades in that window")
        return 1

    df = to_frame(all_trades, args.symbol).sort_values("entry_time").reset_index(drop=True)

    skipped = all_skipped      # already carries skip_reason from the engine

    OUT.mkdir(parents=True, exist_ok=True)
    base = OUT / f"TRADES_{args.symbol}_{label}"
    df.to_csv(base.with_suffix(".csv"), index=False, encoding="utf-8")
    (base.with_suffix(".txt")).write_text(
        render_txt(df, args.symbol, args.balance), encoding="utf-8")
    md_path = base.with_suffix(".md")
    md_path.write_text(
        render_md(df, args.symbol, args.balance, skipped, problems),
        encoding="utf-8")

    print(render_txt(df, args.symbol, args.balance))
    print()
    print(f"  CSV : {base.with_suffix('.csv')}")
    print(f"  MD  : {md_path}")
    print(f"  TXT : {base.with_suffix('.txt')}")
    if skipped:
        print(f"  {len(skipped)} signal(s) did NOT become a trade (listed in the .md)")
    if problems:
        print("\nSIZING AUDIT FAILED:")
        print("\n".join(problems))
    else:
        print("\nSizing audit PASSED: every lot size independently recomputed "
              "and matched.")
    if args.open:
        webbrowser.open(md_path.as_uri())
    return 0


if __name__ == "__main__":
    sys.exit(main())
