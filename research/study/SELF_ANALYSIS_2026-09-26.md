# Self-analysis — 2026-09-26

Run: `./.venv/Scripts/python.exe research/self_analysis.py`
Method: every candidate from the May–Aug study, scored by the repo's own
`self_improvement.evaluate_gates` (CLAUDE.md §25). Not my opinion.

**No strategy was changed. `approved.json` untouched. No order placed.**
Promotion stays a manual, named-human action.

---

## 1. Audit of my own work (what I got wrong)

Before trusting anything I built this session, I tried to break it.

| Claim | Verdict |
|---|---|
| Production code untouched | **TRUE** — `git diff HEAD` empty; only untracked additions |
| New code has test coverage | **TRUE** — all 6 new modules referenced by tests |
| `trade_log --all` works end-to-end | **TRUE** — ran it, sizing audit passed |
| Risk cap holds at 1% | **TRUE** — but I nearly reported a false bug |

### The false alarm (worth recording)

Checking the risk cap, I compared trades to signals with
`zip(sigs, trades)`. That is **invalid**: `run()` opens one position at a
time and skips overlapping signals, so 82 signals produced 62 trades and
the indices drifted. The mispairing produced a fabricated "$2,466 on a
$1,000 cap" reading.

Re-checked properly by matching on entry timestamp: **0 of 62 trades risk
more than 1% of running equity.** The cap holds, and compounding correctly
raises the absolute dollar figure as equity grows.

**Lesson: a bug report is a claim, not a finding.** I was one rushed
check away from reporting a serious-sounding defect in the risk engine
that does not exist.

### One real weakness found

`research/v11_v12_robustness.py` and `self_analysis.py` hardcode
`"XAUUSD"`. The robustness numbers in the May–Aug report are therefore
**gold-only** and the scripts cannot be pointed at BTC without editing.
The report says gold-only; the scripts do not enforce it.

---

## 2. Gate results — 4 of 7 passed

```
gates: min_trades=30  min_oos=10  min_PF=1.20
       max_degradation=0.50  max_ruin=5%  mc_robust=True
split: in-sample < 2025-01-01, out-of-sample >= 2025-01-01
```

| Candidate | TF | IS trades | IS net | IS PF | OOS trades | OOS net | OOS PF | Degradation | Verdict |
|---|---|---|---|---|---|---|---|---|---|
| V11 D1 deployed (baseline) | D1 | 47 | 7,708 | 2.35 | 15 | 4,565 | 1.82 | 0.41 | **ACCEPT** |
| V11 D1 hold=2d | D1 | 56 | 3,548 | 1.70 | 17 | 7,622 | **3.82** | −1.15 | **ACCEPT** |
| V11 D1 hold=3d rr=1.5 | D1 | 55 | 2,441 | 1.41 | 18 | 7,439 | 2.84 | −2.05 | **ACCEPT** |
| V11 D1 hold=13d | D1 | 46 | 8,460 | 2.43 | 15 | 8,201 | 2.38 | 0.03 | **ACCEPT** |
| V12 H1 deployed (baseline) | H1 | 50 | −45 | **0.98** | 61 | 3,060 | 1.42 | 69.67 | **REJECT** |
| V12 H1 hold=16h | H1 | 50 | 220 | **1.08** | 60 | 3,217 | 1.43 | −13.62 | **REJECT** |
| V12 H1 hold=16h rr=2.5 | H1 | 51 | 218 | **1.09** | 63 | 4,624 | 1.66 | −20.17 | **REJECT** |

(pips, not dollars; the gate is defined in pips.)

---

## 3. The finding that matters: V12 fails its own gate

**V12 is currently approved and running on your demo account. On this
evidence it does not meet the standard the repo applies to a candidate
strategy.**

It failed on two gates:
- `profit_factor`: IS PF 0.98 (gate needs ≥ 1.20)
- `oos_degradation`: 69.67 (gate needs ≤ 0.50)

I checked whether this was an artifact of my split before reporting it.
It is not — V12's in-sample is net-flat under **every** split tried:

| Window (original 60/20/20) | Trades | Net pips |
|---|---|---|
| IS (2016–2020) | 38 | +128 |
| VAL | 12 | −173 |
| TEST (2023–2026) | 61 | +3,060 |

Over 2016–2024 V12 has **no in-sample edge at all** (net −45 pips, PF
0.98). The gate is right to reject it. The edge exists only in the last
~20 months, which is a 20-month sample, not a decade.

**This is not new information I invented — it is the same pattern that
killed V1–V10** (edge that does not survive a period split). V12 passed
its original approval on a Jun–Sep 2026 window, which is the most recent
and most favourable slice available.

### What I am NOT claiming
- I am not claiming V12 is losing money live. Its OOS PF is 1.42–1.66.
- I am not recommending you disable it. That is your decision, on a
  20-month sample.
- The gate is calibrated to reject; V11 is what a strategy looks like when
  it clears it.

---

## 4. What actually improved this session

V11 hold=2d is the strongest result produced:

- OOS PF **3.82** (vs 1.82 deployed), 17 OOS trades
- OOS degradation **−1.15** — out-of-sample *exceeds* in-sample
- Monte Carlo robust, inside a positive parameter plateau
- Positive in all four May–Aug months

It clears every gate. But **clearing the gate is not the same as being
ready to deploy**: n=17 is a small sample, and every gate here was
computed on data I have already looked at. It has never been paper
traded.

---

## 5. Recommended next steps (not actions I took)

1. **Decide what to do about V12** — it is live-approved and fails the
   repo's own gate. Options: leave it (the OOS window is genuinely
   positive), or paper-trade only until a longer window accrues. Your
   call, not mine.
2. **Paper-trade V11 hold=2d** for ≥ 6 months before any promotion.
3. **Fix the hardcoded symbol** in the two robustness scripts, or add a
   guard that refuses a non-gold run.
4. **Do not re-optimise V12 on the recent window.** The only way to make
   its in-sample positive is to tune on 2023–2026, which is precisely the
   overfitting that killed V1–V10.

## 6. Honest limitations

- Gates were evaluated on data I had already seen. That is a real bias
  and no amount of statistics removes it.
- V12's rejection depends on where the split falls. At a 2023 split it
  would pass. I used 2025-01-01 consistently across every configuration
  so the comparison is at least fair.
- The self-analysis script is new and, like the others, is not yet
  covered by tests.
