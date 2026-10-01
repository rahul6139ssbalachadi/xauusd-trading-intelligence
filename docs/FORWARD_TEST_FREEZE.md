# FORWARD TEST FREEZE — V11 / V12

Frozen: 2026-10-01
Tag: `forward-test-v1`
Commit: `cc1d862`

This document pins the exact parameters, code revision and data revision that
the forward test runs on. If any of these change, the forward test is no
longer measuring the thing that was validated.

---

## 1. Commit hash

```
cc1d862  Activate BTCUSD and clean up dead imports
```

The three commits that make up this freeze:

| Commit | Subject |
|---|---|
| `51db065` | Add MT5 history ingestion tooling, fix bar-cap truncation, re-baseline data-dependent tests |
| `049f0c7` | Add BTCUSD DQR/V13 research and XAUUSD DQR quality-reversal study |
| `cc1d862` | Activate BTCUSD and clean up dead imports |

Verify what you are running:

```bash
cd D:/rahul_ai/trading
git rev-parse --short HEAD      # -> cc1d862
git describe --tags             # -> forward-test-v1
git status --short              # -> empty
```

The runner records the commit hash in every journal line, so a signal can be
traced back to the code that produced it. See Step 5 of the deployment brief.

---

## 2. Frozen V11 parameters

Source of truth: `strategy/defs/XAUUSD_D1_MOMENTUM_BREAKOUT_V11.json`
(`research_results.train.params` — the config that passed every gate), mirrored
in `execution/run_v11_daily.py::PARAMS`.

| Parameter | Value |
|---|---|
| `body_pct_threshold` | `0.95` |
| `trend_filter` | `True` |
| `min_atr_pct` | `0.005` |
| `rr` | `2.0` |
| `atr_mult_stop` | `1.5` |
| `max_holding_d1` | `8` |
| `risk_pct` | `0.01` (1%) |
| timeframe | D1 |
| direction | LONG only |
| magic | `20260922` |
| broker symbol | `GOLD.i#` |

### The trap this freeze exists to prevent

`research/v11_d1_momentum.py::compute_d1_signals` defaults to
`min_atr_pct=0.008`. The config that actually passed validation used **0.005**.
Copying the function default instead of the def JSON nearly shipped the wrong
volatility filter to live. The runner reads `PARAMS`, not the function default.
`tests/test_monthly_bt.py::TestStrategyReuse::test_v11_params_come_from_the_live_runner`
asserts the two agree.

---

## 3. Frozen V12 parameters

Source of truth: `strategy/defs/XAUUSD_H1_MOMENTUM_BREAKOUT_V12.json`,
mirrored in `execution/run_v12_hourly.py::PARAMS`.

| Parameter | Value |
|---|---|
| `body_pct_threshold` | `0.95` |
| `min_atr_pct` | `0.004` |
| `rr` | `3.0` |
| `atr_mult_stop` | `0.8` |
| `max_holding_h1` | `8` |
| `risk_pct` | `0.02` (2%) |
| timeframe | H1 |
| direction | LONG only |
| magic | `20260923` |
| broker symbol | `GOLD.i#` |

Both are listed in `execution/approved.json`.

---

## 4. ⚠ KNOWN OPEN RISK — V12's approval is not stable

Recorded here because the forward test is about to run on it.

`scripts/check_gate_stability.py` runs the section-25 acceptance gate over the
SAME strategy and parameters on two different data spans. Extending the
history changes the verdict:

| Span | Bars | IS trades | IS net | IS PF | Degradation | Verdict |
|---|---|---|---|---|---|---|
| old (to 2026-08-28) | 59,299 | 50 | **-45 pips** | **0.98** | **69.67** | **REJECT** |
| new (to 2026-10-01) | 61,854 | 60 | +759 pips | 1.31 | -2.38 | ACCEPT |

A verdict that flips because four months of history were prepended is
sample-sensitivity, not robustness. V12's edge lives in recent data only; its
2025-01-01-split in-sample is net-flat on the older span. That is the same
period-dependent shape that killed V1–V10.

**V11 passes on both spans.** Only V12 is affected.

Consequences for this forward test:

- V12 stays **demo-only**. Already the rule; now it has a measured reason.
- Do not re-tune V12 to make the in-sample positive. That is precisely the
  overfitting that produced V1–V10.
- Re-run `scripts/check_gate_stability.py` on a fixed calendar window before
  V12 is ever considered for a real account.

Run it yourself:

```bash
./.venv/Scripts/python.exe scripts/check_gate_stability.py
```

---

## 5. Frozen data revision

`db/trading.db`, ingested 2026-10-01. The database is git-ignored and is NOT
part of the tag — this table is how you confirm your copy matches.

| Symbol | TF | Bars | Span |
|---|---|---|---|
| XAUUSD | M1 | 73,208 | 2026-07-14 07:21 .. 2026-10-01 05:44 |
| XAUUSD | M5 | 44,578 | 2026-02-16 01:00 .. 2026-10-01 05:40 |
| XAUUSD | M15 | 51,713 | 2024-07-23 05:30 .. 2026-10-01 05:30 |
| XAUUSD | H1 | 61,854 | 2016-05-06 06:00 .. 2026-10-01 05:00 |
| XAUUSD | H4 | 16,047 | 2016-05-06 08:00 .. 2026-10-01 04:00 |
| XAUUSD | D1 | 2,685 | 2016-05-09 00:00 .. 2026-10-01 00:00 |
| BTCUSD | M1 | 101,754 | 2026-07-22 08:49 .. 2026-10-01 05:44 |
| BTCUSD | M5 | 98,429 | 2025-10-23 08:50 .. 2026-10-01 05:40 |
| BTCUSD | M15 | 70,240 | 2024-09-28 09:00 .. 2026-10-01 05:30 |
| BTCUSD | H4 | 18,806 | 2013-01-21 00:00 .. 2026-10-01 04:00 |
| BTCUSD | H1 | 71,370 | 2016-10-03 00:00 .. 2026-10-01 05:00 |
| BTCUSD | D1 | 3,931 | 2013-01-21 00:00 .. 2026-10-01 00:00 |

Verify your copy:

```bash
./.venv/Scripts/python.exe scripts/verify_ingest.py
./.venv/Scripts/python.exe scripts/check_ingest_integrity.py
```

### Known data hole (cannot be fixed)

XAUUSD **M1** has a 137.7h gap, 2026-08-18 07:20 -> 2026-08-24. The broker's
~30-day M1 retention rolled past it. It is reported by
`check_ingest_integrity.py`, not hidden, and cannot be backfilled. Any M1-only
finding is therefore low-confidence across that window.

---

## 6. Test baseline at freeze

```
./.venv/Scripts/python.exe -m pytest tests/ -q
622 passed, 1 skipped
```

The skip is the MetaTrader5 import guard on non-Windows CI. Use the venv
interpreter — bare `pytest` resolves to the system Python 3.14, which lacks the
project dependencies and yields a false failure.

---

## 7. What this freeze does NOT cover

- **No forward evidence exists yet.** `signals_log` is empty. Every V11/V12
  number above is in-sample, walk-forward or simulated. That is the entire
  point of the forward test.
- **The live MT5 order path is unverified end-to-end on this machine.** The
  terminal was logged out during prior work
  (`mt5.initialize failed [-6] Authorization failed`). Close this with a
  `--dry-run` on the machine that will actually trade.
- **Docker was never built** (not installed on the dev box). The Windows
  deployment path does not use it.
- V11's `max_holding_d1=8` time exit was not broker-side at freeze time; it
  needs the position-age check added in Step 2 of the deployment brief.

---

## 8. Changing anything here

Per CLAUDE.md sections 15 and 25, a parameter change is a NEW VERSION, never an
edit to the frozen one. Bump the version, re-run the full gate stack
(backtest -> out-of-sample -> walk-forward -> Monte Carlo -> paper), then get
explicit human approval via `self_improvement.promote()`. There is no code path
from `propose()` to `promote()`.
