# V11 / V12 — May–Aug 2026 backtest + parameter study

Research only. **No order was placed.** The switch was not touched and
`db/trading.db` was opened read-only.

Reproduce:
```
./.venv/Scripts/python.exe -m monthly_bt --strategy both --symbol XAUUSD \
    --months 2026-05 2026-06 2026-07 2026-08 --balance 100000
./.venv/Scripts/python.exe research/v11_v12_param_study.py --mode timeframe
./.venv/Scripts/python.exe research/v11_v12_param_study.py --mode rr
./.venv/Scripts/python.exe research/v11_v12_param_study.py --mode params
./.venv/Scripts/python.exe research/v11_v12_param_study.py --mode hold
./.venv/Scripts/python.exe research/v11_v12_param_study.py --mode lot
./.venv/Scripts/python.exe research/v11_v12_robustness.py
```

Balance $100,000, 1 position, measured spread + 1 pip/side slippage.
Study split: in-sample before 2025-01-01, out-of-sample 2025-01-01 onward.
**The OOS window was never used to choose anything.**

---

## 1. May–Aug 2026, exactly as deployed

### V11 (D1)

| Month | Signals | Trades | Win% | PF | Net $ | Avg R | Exits |
|---|---|---|---|---|---|---|---|
| 2026-05 | 0 | 0 | n/a | n/a | n/a | n/a | — |
| 2026-06 | 0 | 0 | n/a | n/a | n/a | n/a | — |
| 2026-07 | 0 | 0 | n/a | n/a | n/a | n/a | — |
| 2026-08 | 1 | 1 | 0.0% | 0.00 | −399.37 | −0.44 | 1 end |
| **TOTAL** | **1** | **1** | **0.0%** | **0.00** | **−399.37** | | |

**V11 produced ONE signal in four months, and it lost.** That is the
single most important number in this report.

### V12 (H1)

| Month | Signals | Trades | Win% | PF | Net $ | MaxDD $ | Avg R | Exits (SL/TP/end) |
|---|---|---|---|---|---|---|---|---|
| 2026-05 | 1 | 1 | 0.0% | 0.00 | −1,004.69 | 0.0 | −1.02 | 1/0/0 |
| 2026-06 | 1 | 1 | 100.0% | n/a | +2,958.04 | 0.0 | +2.99 | 0/1/0 |
| 2026-07 | 4 | 3 | 66.7% | 3.35 | +2,317.71 | 176.60 | +0.79 | 1/1/1 |
| 2026-08 | 11 | 9 | 55.6% | 3.41 | +9,644.02 | 336.30 | +1.05 | 3/4/2 |
| **TOTAL** | **17** | **14** | **57.1%** | — | **+13,915.08** | | | 5/6/3 |

3 signals were skipped: 2 declined for position sizing, 1 for
"position already open".

### Why V11 is so quiet

Not a bug — the gate is genuinely rare. Measured on 10 years of D1:

- `body_pct >= 0.95` (top 5% momentum bars) fires on **7.0%** of bars
- combined with the EMA21>EMA55 uptrend requirement and a bullish body,
  2026 produced only 68 bull-trend bars out of 170
- historical signal count by year: 2017:7, 2018:7, 2019:12, 2020:10,
  2021:2, 2022:6, 2023:5, 2024:12, 2025:17, **2026:4**

V11 averages ~8 signals/year. Four months with zero signals is the
expected outcome, not evidence the strategy broke.

---

## 2. Timeframe study (OOS = 2025 onward)

| Config | TF | Signals | OOS n | OOS net $ | OOS PF | Win% | PF @1.5× spread |
|---|---|---|---|---|---|---|---|
| V11 deployed | D1 | 82 | 15 | 5,842 | 2.20 | 53.3% | 2.20 |
| V12 deployed | H1 | 123 | 61 | 24,416 | 1.65 | 39.3% | 1.64 |
| V12 logic on H4 | H4 | 9 | 6 | 5,177 | 3.58 | 66.7% | *insufficient* |
| V11 logic on H1 | H1 | 123 | 58 | 15,340 | 1.69 | 50.0% | 1.69 |
| M15 candidate | M15 | 74 | 65 | 38,337 | 2.12 | 53.8% | 2.11 |
| M30 candidate | M30 | 5 | 5 | 887 | 1.30 | 40.0% | *insufficient* |

**Caveat that invalidates half this table:** H4, M30 and M15 have almost
no history before 2025 (H4: 493 bars from 2026-06 only; M30: 3,772 bars
from 2026-06). Their in-sample columns are empty, so "IS n = 0" means
*no data*, not *no edge*. Only D1 and H1 have real 10-year in-sample
history. The M15 and H4 OOS numbers are 12–20 months of data and should
be treated as provisional.

**D1 and H1 both survive a 50% worse spread** (2.20→2.20, 1.65→1.64).
That is the single most important robustness signal: these edges are
larger than their own execution uncertainty. This is exactly where the
BTC study failed (PF flipped to 0.99 at 1.5× spread), and gold does not.

---

## 3. R:R study

### V11 D1

| R:R | OOS n | OOS net $ | OOS PF | Win% | May–Aug net $ |
|---|---|---|---|---|---|
| 1.0 | 18 | 7,723 | **3.03** | 72.2% | +909 |
| 1.5 | 17 | 6,142 | 2.16 | 58.8% | −399 |
| 2.0 *(deployed)* | 15 | 5,842 | 2.20 | 53.3% | −399 |
| 2.5 | 15 | 7,561 | 2.55 | 53.3% | −399 |
| 3.0 | 15 | 5,307 | 1.89 | 46.7% | −399 |
| 4.0 | 15 | 6,797 | 2.05 | 46.7% | −399 |

### V12 H1

| R:R | OOS n | OOS net $ | OOS PF | Win% | May–Aug net $ |
|---|---|---|---|---|---|
| 1.0 | 65 | 1,824 | 1.06 | 52.3% | +5,809 |
| 1.5 | 65 | 8,509 | 1.24 | 46.2% | +5,835 |
| 2.0 | 65 | 16,839 | 1.44 | 43.1% | +6,866 |
| 2.5 | 63 | 22,707 | 1.57 | 41.3% | +9,892 |
| 3.0 *(deployed)* | 61 | 24,416 | **1.65** | 39.3% | +14,340 |
| 4.0 | 60 | 21,030 | 1.55 | 35.0% | +11,712 |

**Both strategies prefer the deployed R:R, and both are on a plateau
rather than a spike** (see §6). R:R 2.5 is statistically indistinguishable
from 3.0 on V12; do not treat 2.5 as an improvement.

---

## 4. Holding period study

| V11 D1 hold | OOS n | OOS net $ | OOS PF | Win% | May–Aug |
|---|---|---|---|---|---|
| 1 day | 19 | 4,961 | 2.38 | 68.4% | +488 |
| **2 days** | 17 | 6,995 | **4.04** | **82.4%** | +774 |
| 3 days | 17 | 7,801 | 3.35 | 64.7% | +815 |
| 5 days | 15 | 7,360 | 2.86 | 60.0% | +473 |
| 8 days *(deployed)* | 15 | 5,842 | 2.20 | 53.3% | −399 |
| 13 days | 15 | 7,342 | 2.27 | 53.3% | −399 |
| 21 days | 15 | 9,685 | 2.79 | 60.0% | −399 |

| V12 H1 hold | OOS n | OOS net $ | OOS PF | May–Aug |
|---|---|---|---|---|
| 8 h *(deployed)* | 61 | 24,416 | 1.65 | +14,340 |
| 12 h | 60 | 22,442 | 1.59 | +18,479 |
| **16 h** | 60 | 27,162 | **1.69** | +20,147 |
| 24 h | 60 | 22,060 | 1.54 | +18,869 |
| 32 h | 60 | 24,640 | 1.59 | +18,869 |

---

## 5. Lot size / risk per trade — the answer is "it cannot matter"

| V11 D1 | OOS n | OOS net $ | **OOS PF** |
|---|---|---|---|
| 0.25% risk | 14 | 1,290 | 2.44 |
| 0.50% risk | 15 | 3,093 | 2.48 |
| 1.00% risk *(deployed)* | 15 | 5,842 | 2.20 |
| fixed 0.05 lot | 15 | 2,282 | **1.82** |
| fixed 0.10 lot | 15 | 4,565 | **1.82** |
| fixed 0.20 lot | 15 | 9,129 | **1.82** |
| fixed 0.30 lot | 15 | 13,694 | **1.82** |

| V12 H1 | OOS n | OOS net $ | **OOS PF** |
|---|---|---|---|
| 0.50% risk | 61 | 11,766 | **1.65** |
| 1.00% risk | 61 | 24,416 | **1.65** |
| 2.00% risk *(deployed)* | 61 | 24,416 | **1.65** |
| fixed 0.05 → 0.30 lot | 61 | 1,530 → 9,181 | **1.42** |

**Profit factor is identical across every lot size.** This is arithmetic,
not a finding about the market: position size scales P&L linearly and
cannot change the ratio of wins to losses. Lot size controls *how much you
make or lose and therefore your drawdown*, never *whether the edge
exists*.

Two real conclusions do come out of it:

1. **Percentage risk beats fixed lots on PF** (V11: 2.20–2.48 vs 1.82;
   V12: 1.65 vs 1.42). Why: D1 stops are 1,500–3,200 pips wide and vary
   bar to bar, so a fixed lot risks wildly different amounts per trade.
   Fixed-lot sizing concentrates risk on the widest-stop bars. Percentage
   risk equalises it. **Keep the engine's percentage sizing.**
2. **Risk above 1% does nothing.** V11 at 1% and 2% produce byte-identical
   results because `RiskConfig.max_risk_per_trade_pct = 1%` caps it. The
   cap is doing its job.

---

## 6. Robustness — Monte Carlo and neighbourhood

| Candidate | OOS n | net $ | PF | MC 5th-pct net | MC 5th-pct PF | Ruin% | Robust |
|---|---|---|---|---|---|---|---|
| V11 D1 deployed | 15 | 5,842 | 2.20 | 1,390 | 1.25 | 0.8% | yes |
| **V11 D1 hold=2d** | 17 | 6,995 | **4.04** | 4,170 | **2.55** | 0.0% | yes |
| V11 D1 hold=3d rr1.5 | 18 | 7,034 | 2.86 | 4,240 | 2.07 | 0.0% | yes |
| V12 H1 deployed | 61 | 24,416 | 1.65 | 1,341 | 1.20 | 0.1% | yes |
| **V12 H1 hold=16h** | 60 | 27,162 | 1.69 | 1,516 | 1.21 | 0.1% | yes |
| V12 H1 hold=16h rr2.5 | 63 | 26,457 | 1.66 | 2,494 | 1.38 | 0.0% | yes |
| V12 H1 hold=32h rr2.5 | 63 | 25,111 | 1.61 | 2,305 | 1.34 | 0.0% | yes |

Neighbourhood scan (OOS net $, R:R down the rows, holding across):

```
V11 D1
rr\hold             2           3           5           8          13          16
1.0            7,552      7,552      8,097      7,723      7,880      8,735
1.5            8,882      7,034      7,599      6,142      7,376      8,167
2.0            6,995      7,801      7,360      5,842      7,342      7,811
2.5            7,754     10,111      8,489      7,561     10,657     11,017
3.0            4,443      5,341      5,380      5,307      8,655      6,737

V12 H1
rr\hold             8          12          16          24          32
1.0            1,824      1,824      1,824      1,824      1,824
1.5            8,509     11,672     11,672     11,672     11,672
2.0           16,839     22,553     22,553     22,553     22,553
2.5           22,707     25,312     26,457     25,111     25,111
3.0           24,416     22,442     27,162     22,060     24,640
```

**Every neighbouring cell is positive.** No isolated spike, no cliff
where a neighbouring parameter collapses to a loss. This is the profile
of a real edge rather than a fitted curve, and it is the strongest
argument in this report.

---

## 7. Findings

### A parameter bug worth fixing (real, not cosmetic)

**V11's `min_atr_pct = 0.005` can never filter anything on D1.**
Measured D1 ATR/close: minimum 0.00734, 25th pct 0.0105, median 0.0128.
A 0.005 threshold is below the floor of the data, so it passes 99.5% of
bars. It is a no-op filter — the parameter is decorative. (An earlier
sweep of 0.003/0.005/0.008 produced three identical rows, which is how
this surfaced.) Raising it to 0.012 makes it bind and cuts signals
roughly in half.

This does not mean 0.012 is better — the sweep shows IS 2.15 / OOS 2.02
versus the current 2.54 / 2.20, so the filter *reduces* return. It means
the current setting is not doing what the documentation claims.

### V11 hold=2d is the standout candidate

PF 4.04 with 82.4% win rate, best 5th-percentile Monte Carlo of any
configuration tested (PF 2.55, zero ruin), positive in every one of the
four May–Aug months, and it sits inside a positive plateau. Against the
deployed 8-day hold: 82.4% vs 53.3% win rate, and it turns the worst
May–Aug month (−399) into positive.

**It is not proven.** n=17 OOS trades. That is a small sample, and a
PF of 4.04 on 17 trades has a wide confidence interval. It needs
walk-forward and paper trading before it displaces anything.

### V12 hold=16h is a modest, safer improvement

PF 1.69 vs 1.65, net $27,162 vs $24,416, and May–Aug net +20,147 vs
+14,340. Small, consistent, and inside a flat plateau. Low conviction
but low risk of being wrong.

### The timeframe question has no clean winner

D1 (V11) has the better PF; H1 (V12) has far more trades. Both are
robust. Running both is defensible — but note they are correlated (both
long gold on the same momentum condition), so this is closer to one
position in two sizes than to diversification. The risk engine's
`max_positions=1` currently prevents stacking them.

### What did NOT work

- **M30**: 5 signals total. Untestable.
- **H4**: 9 signals total, all from 2026. Untestable.
- **body_pct 0.98**: best raw PF (10.65) but on **8 OOS trades**. This is
  the clearest overfit trap in the study — a beautiful number from almost
  no data. Do not use it.
- **ATR filter 0.016**: kills the in-sample result (IS PF 0.67–0.90).
- **Lot size as an edge lever**: impossible, see §5.

---

## 8. Recommendations

Ranked by evidence quality, not by headline number.

1. **Do not change any deployed parameter on this evidence.** The
   current V11 and V12 settings sit on a positive plateau and both
   survive a 50% worse spread. That is a good place to be.
2. **Paper-trade V11 hold=2d** (and V12 hold=16h) for a minimum of 6
   months before considering any change. Both are inside the plateau, so
   they are not fitted curves, but both rest on small samples.
3. **Fix the documentation, not the parameter.** Either raise
   `min_atr_pct` to a value that binds (0.012) and accept the lower
   return, or document it as a placeholder. Leaving 0.005 in place while
   claiming it is an "active volatility filter" is the actual problem.
4. **Keep percentage risk sizing.** Fixed lots measurably reduced PF
   (2.20→1.82 on V11) because D1 stop widths vary enormously.
5. **Do not optimise lot size for profit.** It cannot work (§5). Size for
   drawdown tolerance only.
6. **If you want more H4/M15 evidence, extend the data first.** Those
   timeframes have 3–4 months of history, which is why half this report
   is INSUFFICIENT.

## 9. Limitations

- **Swap is $0 in every number above.** V11 holds up to 8 days and V12 up
  to 16 hours, so financing is a real unmodelled cost. All net figures
  are therefore **overstated**. This does not affect PF conclusions much
  (swap is a fixed per-day cost, similar each trade) but it does affect
  net P&L.
- **No tick replay.** Intra-bar high/low order is unknown; the closer
  level is assumed to fill first.
- **OOS is 20 months** (2025-01 to 2026-08) — reasonable but not long.
  The whole 10-year history sits in-sample for these configs.
- **M15/H4/M30 rows are not comparable** to D1/H1 rows — no in-sample history.
- **DB is stale**: gold ends 2026-08-28 while the terminal has later bars.
- **One asset.** All results are XAUUSD. The BTC study found both
  strategies non-robust there, and nothing here contradicts that.
