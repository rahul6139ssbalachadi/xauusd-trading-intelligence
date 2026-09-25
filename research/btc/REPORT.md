# BTC V11 vs V12 — Quantitative Backtest & Robustness Study

Generated 2026-09-25T17:11:35+00:00 | commit `aa7cab9`

**READ-ONLY RESEARCH.** No orders were placed, no positions opened, no
strategy logic modified. Live XAUUSD V11/V12 trading was unaffected.

## 1. Executive Summary

Neither V11 nor V12 shows a robust, cost-surviving edge on BTC for the
2026 period available. Both fail the Monte Carlo robustness test.

- **V11 (D1):** 5 trades only. Positive nominal result (PF 1.161) but the
  in-sample window produced **zero** trades, so there is no in-sample basis
  to compare against. Monte Carlo PF_p5 = 0.492
  with 15.6% ruin probability —
  a 5-trade sample cannot support any conclusion.
- **V12 (H1):** 104 trades, PF 1.099,
  and it survives walk-forward (OOS PF 1.148
  on 30 trades, degradation
  0.171). **However it flips to a
  loss under 1.5x spread**, and Monte Carlo PF_p5 =
  0.966 with
  8.9% ruin probability.

The evidence does not currently support deploying either strategy on BTC.

## 2. Strategy Definitions

Logic reused verbatim from the gold implementation: long-only momentum
breakout, top-5%
bar body percentile, EMA21/EMA55 uptrend filter, ATR stop, fixed R:R
target, next-bar-open entry.

| | V11 | V12 |
|---|---|---|
| Timeframe | D1 | H1 |
| R:R | 2.0 | 3.0 |
| ATR stop | 1.5x | 0.8x |
| Max hold | 8 bars | 8 bars |
| min_atr_pct | 0.005 | 0.004 |

Only the **cost/unit model** was adapted. Gold's PIP=0.10 is meaningless at
BTC's price level, so 1 pip = $1.00 and the **real recorded spread** is used
rather than a flat assumption.

## 3. Data Source & Quality

Source: XMGlobal-MT5, symbol `BTCUSD#` (the only BTC instrument on this
account with a live quote; BTCGBP#/BTCEUR#/ETHBTC# quote 0.00).

| TF | Bars | Range | Dupes | Gaps | OHLC violations | Mean spread |
|---|---|---|---|---|---|---|
| D1 | 268 | 2026-01-01 to 2026-09-25 | 0 | 0 | 0 | $21.21 |
| H1 | 6426 | 2026-01-01 to 2026-09-25 | 0 | 0 | 0 | $21.24 |

Nothing was repaired; figures are as received. BTC trades 24/7 so no
session assumptions were applied.

Price range D1: 58723 – 97554. The series
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

| Metric | V11 (D1) | V12 (H1) |
|---|---|---|
| Trades (full sample) | 5.000 | 104.000 |
| Net pips | 1226.478 | 2882.271 |
| Profit factor | 1.161 | 1.099 |
| Win rate % | 60.000 | 34.615 |
| Max drawdown pips | 4920.700 | 10202.332 |
| Sharpe | 0.062 | 0.040 |
| Round-trip cost $ | 52.425 | 52.477 |
| OOS trades | 5.000 | 30.000 |
| OOS net pips | 1256.197 | 1301.418 |
| OOS profit factor | 1.166 | 1.148 |
| IS/OOS degradation | n/a | 0.171 |
| MC net_p5 | -3835.574 | -929.412 |
| MC PF_p5 | 0.492 | 0.966 |
| MC ruin prob % | 15.600 | 8.900 |
| MC robust | False | False |

## 6. Cost Sensitivity

**V11**

| Case | Round-trip cost | Trades | Net pips | PF |
|---|---|---|---|---|
| BASE (real spread) | $52.43 | 5 | 1226.5 | 1.161 |
| STRESS 1.5x spread | $83.64 | 5 | 1070.4 | 1.140 |
| STRESS 2x spread | $124.85 | 5 | 864.4 | 1.112 |

**V12**

| Case | Round-trip cost | Trades | Net pips | PF |
|---|---|---|---|---|
| BASE (real spread) | $52.48 | 104 | 2882.3 | 1.099 |
| STRESS 1.5x spread | $83.71 | 104 | -366.5 | 0.988 |
| STRESS 2x spread | $124.95 | 104 | -4655.3 | 0.864 |

V12 is the meaningful case: it is profitable at base cost and **loses money
once the spread widens by 50%**. That is the signature of an edge sitting
on or below the cost floor, not a real one.

## 7. Walk-Forward

Chronological 70/30, parameters never re-fit on the test side.

- **V11:** 0 in-sample trades, 5 out-of-sample. Degradation undefined. A
  5-trade strategy on a 268-bar daily series is not statistically testable.
- **V12:** 74 in-sample / 30 out-of-sample trades, OOS PF
  1.148 vs IS
  1.077, degradation
  0.171. This is the one genuinely
  encouraging number in the study — but it is contradicted by the cost
  stress and Monte Carlo results.

## 8. Monte Carlo

2000 iterations, seed 42. Both strategies report `is_robust = False`.

- V11: net_p5 -3835.6, PF_p5
  0.492, ruin
  15.6%
- V12: net_p5 -929.4, PF_p5
  0.966, ruin
  8.9%

## 9. Market-Regime Analysis

**V11**

| Regime | Trades | Net pips | Avg per trade |
|---|---|---|---|
| low_vol | 5 | 1226.5 | 245.3 |

**V12**

| Regime | Trades | Net pips | Avg per trade |
|---|---|---|---|
| high_vol | 65 | 3347.8 | 51.5 |
| low_vol | 39 | -465.6 | -11.9 |

V12's gross P&L is concentrated in the high-volatility regime
(+3348) and is negative in low
volatility (-466). That concentration
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
Commit: `aa7cab9`
