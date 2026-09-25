"""Render the BTC study to Markdown + send a summary to Telegram."""
from __future__ import annotations

import json
import sys
from pathlib import Path

# This file lives at <root>/research/btc/, so the repo root is two levels up.
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

RES = ROOT / "research" / "btc" / "btc_v11_v12_results.json"
OUT = ROOT / "research" / "btc" / "REPORT.md"


def fmt(v, spec=".3f"):
    if v is None:
        return "n/a"
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    if f != f or f in (float("inf"), float("-inf")):
        return "n/a"
    return format(f, spec)


def table(data) -> str:
    r = data["results"]
    v11, v12 = r["V11"], r["V12"]
    b11, b12 = v11["base"], v12["base"]
    w11, w12 = v11["walk_forward"], v12["walk_forward"]
    m11, m12 = b11["mc"] or {}, b12["mc"] or {}
    rows = [
        ("Trades (full sample)", b11["metrics"].get("total_trades"),
         b12["metrics"].get("total_trades")),
        ("Net pips", b11["metrics"].get("net_pips"),
         b12["metrics"].get("net_pips")),
        ("Profit factor", b11["metrics"].get("profit_factor"),
         b12["metrics"].get("profit_factor")),
        ("Win rate %", 100 * (b11["metrics"].get("win_rate") or 0),
         100 * (b12["metrics"].get("win_rate") or 0)),
        ("Max drawdown pips", b11["metrics"].get("max_drawdown_pips"),
         b12["metrics"].get("max_drawdown_pips")),
        ("Sharpe", b11["metrics"].get("sharpe"),
         b12["metrics"].get("sharpe")),
        ("Round-trip cost $", b11["cost_pips_roundtrip"],
         b12["cost_pips_roundtrip"]),
        ("OOS trades", w11["oos_trades"], w12["oos_trades"]),
        ("OOS net pips", w11["oos_net_pips"], w12["oos_net_pips"]),
        ("OOS profit factor", w11["oos_pf"], w12["oos_pf"]),
        ("IS/OOS degradation", w11["degradation"], w12["degradation"]),
        ("MC net_p5", m11.get("net_p5"), m12.get("net_p5")),
        ("MC PF_p5", m11.get("profit_factor_p5"), m12.get("profit_factor_p5")),
        ("MC ruin prob %", 100 * (m11.get("ruin_prob") or 0),
         100 * (m12.get("ruin_prob") or 0)),
        ("MC robust", m11.get("is_robust"), m12.get("is_robust")),
    ]
    out = ["| Metric | V11 (D1) | V12 (H1) |", "|---|---|---|"]
    for label, a, b in rows:
        if isinstance(a, bool) or isinstance(b, bool):
            out.append(f"| {label} | {a} | {b} |")
        else:
            out.append(f"| {label} | {fmt(a)} | {fmt(b)} |")
    return "\n".join(out)


def cost_tables() -> str:
    r = json.loads(RES.read_text(encoding="utf-8"))["results"]
    out = []
    for name in ("V11", "V12"):
        out.append(f"**{name}**\n")
        out.append("| Case | Round-trip cost | Trades | Net pips | PF |")
        out.append("|---|---|---|---|---|")
        for row in r[name]["cost_stress"]:
            out.append(f"| {row['cost_case']} | ${row['cost_pips_roundtrip']:.2f} "
                       f"| {row['trades']} | {row['net_pips']:.1f} "
                       f"| {fmt(row['pf'])} |")
        out.append("")
    return "\n".join(out)


def regimes() -> str:
    r = json.loads(RES.read_text(encoding="utf-8"))["results"]
    out = []
    for name in ("V11", "V12"):
        reg = r[name].get("regime") or {}
        if not reg:
            continue
        out.append(f"**{name}**\n")
        out.append("| Regime | Trades | Net pips | Avg per trade |")
        out.append("|---|---|---|---|")
        for k, v in reg.items():
            out.append(f"| {k} | {v['n']} | {v['net']:.1f} | {v['avg']:.1f} |")
        out.append("")
    return "\n".join(out)


def main() -> int:
    data = json.loads(RES.read_text(encoding="utf-8"))
    r = data["results"]
    q1, q2 = r["V11"]["quality"]["D1"], r["V11"]["quality"]["H1"]

    doc = f"""# BTC V11 vs V12 — Quantitative Backtest & Robustness Study

Generated {data['generated']} | commit `{data['commit']}`

**READ-ONLY RESEARCH.** No orders were placed, no positions opened, no
strategy logic modified. Live XAUUSD V11/V12 trading was unaffected.

## 1. Executive Summary

Neither V11 nor V12 shows a robust, cost-surviving edge on BTC for the
2026 period available. Both fail the Monte Carlo robustness test.

- **V11 (D1):** 5 trades only. Positive nominal result (PF 1.161) but the
  in-sample window produced **zero** trades, so there is no in-sample basis
  to compare against. Monte Carlo PF_p5 = {fmt(r['V11']['base']['mc']['profit_factor_p5'])}
  with {100 * r['V11']['base']['mc']['ruin_prob']:.1f}% ruin probability —
  a 5-trade sample cannot support any conclusion.
- **V12 (H1):** 104 trades, PF {fmt(r['V12']['base']['metrics']['profit_factor'])},
  and it survives walk-forward (OOS PF {fmt(r['V12']['walk_forward']['oos_pf'])}
  on {r['V12']['walk_forward']['oos_trades']} trades, degradation
  {fmt(r['V12']['walk_forward']['degradation'])}). **However it flips to a
  loss under 1.5x spread**, and Monte Carlo PF_p5 =
  {fmt(r['V12']['base']['mc']['profit_factor_p5'])} with
  {100 * r['V12']['base']['mc']['ruin_prob']:.1f}% ruin probability.

The evidence does not currently support deploying either strategy on BTC.

## 2. Strategy Definitions

Logic reused verbatim from the gold implementation: long-only momentum
breakout, top-{100 * (1 - r['V11']['params']['body_pct_threshold']):.0f}%
bar body percentile, EMA21/EMA55 uptrend filter, ATR stop, fixed R:R
target, next-bar-open entry.

| | V11 | V12 |
|---|---|---|
| Timeframe | D1 | H1 |
| R:R | {r['V11']['params']['rr']} | {r['V12']['params']['rr']} |
| ATR stop | {r['V11']['params']['atr_mult_stop']}x | {r['V12']['params']['atr_mult_stop']}x |
| Max hold | 8 bars | 8 bars |
| min_atr_pct | {r['V11']['params']['min_atr_pct']} | {r['V12']['params']['min_atr_pct']} |

Only the **cost/unit model** was adapted. Gold's PIP=0.10 is meaningless at
BTC's price level, so 1 pip = $1.00 and the **real recorded spread** is used
rather than a flat assumption.

## 3. Data Source & Quality

Source: XMGlobal-MT5, symbol `BTCUSD#` (the only BTC instrument on this
account with a live quote; BTCGBP#/BTCEUR#/ETHBTC# quote 0.00).

| TF | Bars | Range | Dupes | Gaps | OHLC violations | Mean spread |
|---|---|---|---|---|---|---|
| D1 | {q1['bars']} | {q1['start']} to {q1['end']} | {q1['duplicates']} | {q1['gaps']} | {q1['ohlc_violations']} | ${q1['mean_spread_usd']:.2f} |
| H1 | {q2['bars']} | {q2['start']} to {q2['end']} | {q2['duplicates']} | {q2['gaps']} | {q2['ohlc_violations']} | ${q2['mean_spread_usd']:.2f} |

Nothing was repaired; figures are as received. BTC trades 24/7 so no
session assumptions were applied.

Price range D1: {q1['min_close']:.0f} – {q1['max_close']:.0f}. The series
contains a -13.2% single-day move on 2026-02-05 (on 217k ticks vs a
20-50k baseline), consistent with a real flash crash rather than a data
glitch.

## 4. Backtest Configuration

- Entry at the **next** bar's open — no same-bar execution, no look-ahead.
- Intrabar stop/target; when both are touched the **closer** level is
  assumed to fill first (conservative).
- Force-close at max holding.
- Costs: 2 x recorded spread + 2 x slippage per round trip.
- Long-only (both strategies are long-only by construction).

## 5. Full-Sample Results

{table(data)}

## 6. Cost Sensitivity

{cost_tables()}
V12 is the meaningful case: it is profitable at base cost and **loses money
once the spread widens by 50%**. That is the signature of an edge sitting
on or below the cost floor, not a real one.

## 7. Walk-Forward

Chronological 70/30, parameters never re-fit on the test side.

- **V11:** 0 in-sample trades, 5 out-of-sample. Degradation undefined. A
  5-trade strategy on a 268-bar daily series is not statistically testable.
- **V12:** 74 in-sample / 30 out-of-sample trades, OOS PF
  {fmt(r['V12']['walk_forward']['oos_pf'])} vs IS
  {fmt(r['V12']['walk_forward']['is_pf'])}, degradation
  {fmt(r['V12']['walk_forward']['degradation'])}. This is the one genuinely
  encouraging number in the study — but it is contradicted by the cost
  stress and Monte Carlo results.

## 8. Monte Carlo

2000 iterations, seed 42. Both strategies report `is_robust = False`.

- V11: net_p5 {fmt(r['V11']['base']['mc']['net_p5'], '.1f')}, PF_p5
  {fmt(r['V11']['base']['mc']['profit_factor_p5'])}, ruin
  {100 * r['V11']['base']['mc']['ruin_prob']:.1f}%
- V12: net_p5 {fmt(r['V12']['base']['mc']['net_p5'], '.1f')}, PF_p5
  {fmt(r['V12']['base']['mc']['profit_factor_p5'])}, ruin
  {100 * r['V12']['base']['mc']['ruin_prob']:.1f}%

## 9. Market-Regime Analysis

{regimes()}
V12's gross P&L is concentrated in the high-volatility regime
(+{r['V12']['regime']['high_vol']['net']:.0f}) and is negative in low
volatility ({r['V12']['regime']['low_vol']['net']:.0f}). That concentration
is itself a risk: the sample driving the result is the smaller, less
repeatable half.

## 10. Long vs Short

Not applicable. Both strategies are long-only by construction. This is a
genuine limitation of the study on BTC — a long-only momentum system in a
period containing a -13% day and a 33% drawdown is being tested in the
regime least suited to it.

## 11. Hansen SPA Test

**NOT COMPLETED.** Not implemented in this repository, and I did not add it
rather than ship an unvalidated statistical test. No SPA p-value is claimed.

## 12. Limitations

1. **Single period, ~9 months, one regime cycle.** 268 daily bars is a small
   sample for a daily-frequency strategy; 2026 BTC is dominated by one major
   drawdown and recovery.
2. **V11 cannot be statistically assessed** — 5 trades.
3. **Spread dominates.** At $21 mean spread, cost is 0.074% per round trip.
   Any edge below that is invisible, and this study's nominal profits are of
   that order.
4. **No commission/funding modelled** — the broker's BTC CFD does not
   charge commission, but overnight funding (swap) was not available and is
   therefore excluded, which flatters multi-day holds.
5. **Walk-forward is a single split**, not rolling windows. V12 deserves a
   proper rolling WF before any conclusion is drawn from the OOS number.
6. **Long-only** — no short-side evidence either way.
7. **No SPA**, no per-trade MAE/MFE decomposition, no equity curves
   rendered as charts.

## 13. Research Conclusion

The evidence does not currently support deploying V11 or V12 on BTC.

V11 cannot be evaluated on this sample. V12 is the more interesting result:
it holds up under walk-forward but fails under a 50% wider spread and fails
Monte Carlo, which is exactly the profile of a strategy whose apparent edge
is smaller than its execution uncertainty. Additional data covering more
regimes, a rolling walk-forward, funding-inclusive costs, and a proper SPA
test would be required before revisiting.

## 14. Reproducibility

```bash
./.venv/Scripts/python.exe scripts/ingest_btc_2026.py --start 2026-01-01 --tf D1,H1
./.venv/Scripts/python.exe research/btc_v11_v12_study.py
```

Raw results: `research/btc/btc_v11_v12_results.json`
Report: `research/btc/REPORT.md`
Commit: `{data['commit']}`
"""
    OUT.write_text(doc, encoding="utf-8")
    print(f"report -> {OUT}")

    tg = f"""<b>BTC RESEARCH — V11 vs V12 (2026)</b>

Read-only. No orders. Live gold trading unaffected.

<b>Data</b> BTCUSD# {q1['bars']}D1 / {q2['bars']}H1 bars, 0 gaps, 0 dupes
Jan-Sep 2026. Mean spread ${q1['mean_spread_usd']:.0f}

<b>V11 (D1)</b> 5 trades
PF {fmt(r['V11']['base']['metrics']['profit_factor'])} but
IS window had <b>0 trades</b> -> untestable
MC PF_p5 {fmt(r['V11']['base']['mc']['profit_factor_p5'])}, ruin {100 * r['V11']['base']['mc']['ruin_prob']:.0f}% — NOT robust

<b>V12 (H1)</b> 104 trades
Base PF {fmt(r['V12']['base']['metrics']['profit_factor'])}
1.5x spread -> PF {fmt(r['V12']['cost_stress'][1]['pf'])} LOSS
2x spread   -> PF {fmt(r['V12']['cost_stress'][2]['pf'])} LOSS
OOS PF {fmt(r['V12']['walk_forward']['oos_pf'])} on 30 trades (holds)
MC PF_p5 {fmt(r['V12']['base']['mc']['profit_factor_p5'])}, ruin {100 * r['V12']['base']['mc']['ruin_prob']:.0f}% — NOT robust

<b>Conclusion</b>
Evidence does NOT support deployment on BTC.
Edge sits at/below the cost floor. Hansen SPA NOT COMPLETED.
Full report: research/btc/REPORT.md"""
    from execution.account_status import send_telegram
    ok, desc = send_telegram(tg)
    print(f"telegram: {'sent' if ok else 'FAILED ' + desc}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
