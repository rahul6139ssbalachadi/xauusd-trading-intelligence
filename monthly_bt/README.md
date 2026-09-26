# Monthly Backtester (V11 / V12) — month-by-month, month-by-month results

## 1. Files created

| Path | What it is |
|---|---|
| `monthly_bt/__init__.py` | package docstring + module list |
| `monthly_bt/guard.py` | backtest-only safety guard (reports the live switch, never writes it) |
| `monthly_bt/data.py` | read-only SQLite loader, per-instrument unit model, provenance |
| `monthly_bt/strategies.py` | **adapters that import the existing V11/V12 code** + V12 gate-parity proof |
| `monthly_bt/engine.py` | trade simulator (reuses `backtest.Trade` / `compute_metrics`) |
| `monthly_bt/runner.py` | period slicing, month/year/range orchestration, run manifest |
| `monthly_bt/store.py` | `db/backtests.db` — backtest_runs / backtest_signals / backtest_trades |
| `monthly_bt/report.py` | text / markdown / CSV reports, V11-vs-V12 comparison |
| `monthly_bt/visual.py` | interactive HTML chart + the MT5 CSV bridge |
| `monthly_bt/cli.py`, `monthly_bt/__main__.py` | CLI |
| `mql5/MonthlyBT_Signals.mq5` | MT5 chart indicator (reads a file; no order calls) |
| `tests/test_monthly_bt.py` | 74 tests |
| `db/backtests.db` | created on demand — trade log |

## 2. Files changed

**None.** `git status` shows only untracked additions. In particular
`config/settings.toml` and `db/trading.db` are untouched.

## 3. How V11 works in the backtest

Signals come from the **existing** implementation, unmodified:

- `research.v11_d1_momentum.build_d1_features` — D1 features
- `research.v11_d1_momentum.compute_d1_signals` — the gate set, called with
  keyword-only params taken from `execution/run_v11_daily.py:PARAMS`
  (body_pct 0.95, min_atr 0.005, rr 2.0, atr_mult 1.5, hold 8 D1 bars)

Cross-check: the backtester reproduces **82 signals over the 10-year D1
history**, which is the exact count documented in CLAUDE.md and the live
cross-check. A test asserts that number, so a silent logic drift fails CI.

Entry = the **open of the bar after** the signal bar. SL = entry −
1.5 × D1-ATR(14). TP = entry + 2 × that distance.

## 4. How V12 works in the backtest

- `execution.run_v12_hourly.build_h1_features` — H1 features
- `execution.run_v12_hourly.PARAMS` — frozen (body_pct 0.95, min_atr 0.004,
  rr 3.0, atr_mult 0.8, hold 8 H1 bars)
- The four gate conditions are the **same four** the live
  `signal_on_last_closed_bar` applies, factored out so they can be
  evaluated bar by bar.

Parity is *proved*, not asserted: `verify_v12_gate_parity()` runs the live
function against 60 different bar positions and compares verdicts. Result:
**60 checked, 0 disagreements.** Cross-check: V12 on BTC 2026 gives
**104 trades**, matching the documented BTC study exactly.

Entry = next H1 bar's open. SL = entry − 0.8 × H1-ATR(14). TP = entry + 3 ×
that distance.

**No `V11_Backtest` / `V12_Backtest` duplicates exist** — a test fails the
build if any such module is added.

## 5. Platform used

**MetaTrader 5** — the platform this project already uses
(`config/mt5.toml` → `C:\Program Files\XM Global MT5\terminal64.exe`,
demo login 345982869). No TradingView or other charting library is used or
needed. `matplotlib` and `plotly` are not installed; adding them was
rejected.

## 6. How BUY/SELL signals are displayed

Two paths, both driven by the same signal dicts the report uses:

**A. Interactive HTML (primary)**
Self-contained canvas candlestick chart, no CDN, no network, no dependency:

- **filled green marker** = SIMULATED TRADE (a position was actually opened)
- **hollow blue marker** = SIGNAL only (no trade opened)
- red horizontal line = entry, orange = SL, purple = TP
- grey connector = entry → exit
- hover tooltip: BUY/SELL, strategy, entry, SL, TP, exit, exit reason, net
  P&L, R multiple, ATR, body percentile
- side panel lists every signal, with skipped ones and their reason
- drag to pan, wheel to zoom

**B. Inside MT5 itself**
`mql5/MonthlyBT_Signals.mq5` draws the same markers on a real MT5 chart of
`GOLD.i#` / `BTCUSD#` using the broker's own candles. It **only reads a
CSV** — no `OrderSend`, no `PositionOpen`, no account calls. A test asserts
those symbols are absent from the file.

Both strategies are LONG-only by design (V11 and V12 both require
`body > 0`), so a SELL marker is structurally impossible; the report
prints a SELL column that is always 0. This is not a bug.

## 7. Historical data

```
DATA SOURCE     db/trading.db (SQLite) <- MT5 demo, XMGlobal-MT5 login
                345982869, ingested by market_data/providers/mt5_provider.py
SYMBOL          canonical XAUUSD -> GOLD.i#   |   BTCUSD -> BTCUSD#
TIMEFRAME       D1 for V11, H1 for V12
TIMEZONE        broker server time = UTC+3 (EEST). The stored epoch is
                BROKER time, not UTC. Month buckets use broker time.
GRANULARITY     OHLC bars + tick_volume + recorded per-bar spread (points)
COVERAGE        XAUUSD D1 2,577 bars (2016-09-02 .. 2026-08-28)
                XAUUSD H1 59,313 (2016-09-01 .. 2026-08-29)
                BTCUSD  D1 268 / H1 6,426 (2026-01-01 .. 2026-09-25)
```

The DB is opened with SQLite `mode=ro`. Nothing is downloaded from the
internet. The DB is stale relative to the terminal (gold ends 2026-08-28
while the terminal has later bars) — re-ingest before trading conclusions.

## 8. Commands

One month:
```
./.venv/Scripts/python.exe -m monthly_bt --strategy V11 --symbol XAUUSD --month 2026-01
./.venv/Scripts/python.exe -m monthly_bt --strategy V12 --symbol BTCUSD --month 2026-01
```
Full year:
```
./.venv/Scripts/python.exe -m monthly_bt --strategy V11 --symbol XAUUSD --year 2026
./.venv/Scripts/python.exe -m monthly_bt --strategy both --symbol XAUUSD --year 2026
```
Several months / custom range / everything:
```
./.venv/Scripts/python.exe -m monthly_bt --strategy V12 --symbol XAUUSD --months 2026-01 2026-02 2026-03
./.venv/Scripts/python.exe -m monthly_bt --strategy V11 --symbol XAUUSD --start 2026-01-01 --end 2026-06-30
./.venv/Scripts/python.exe -m monthly_bt --strategy V11 --symbol XAUUSD --all
```
With the visual chart and the MT5 CSV:
```
./.venv/Scripts/python.exe -m monthly_bt --strategy V12 --symbol XAUUSD --months 2026-01 2026-08 --balance 100000 --chart
```
Other flags: `--balance`, `--slippage`, `--commission`, `--spread-mult`,
`--max-positions`, `--require-disarmed`, `--no-save`, `--symbols`,
`--export-mt5`, `--verify-parity`.

**Use `--balance 100000` (or larger) for gold.** At $10,000 the D1 stop
(1,500–3,200 pips wide) floors below the 0.01 minimum lot at 1% risk, so
**every** V11 signal is declined for sizing. The report says so explicitly.
That is the risk engine working, not a bug — it will never size above the
cap to force a fill.

## 9. Where things are saved

- Reports: `reports/monthly/V11_XAUUSD_2026.txt` / `.md` / `.csv`
- Visual chart: `reports/monthly/visual/V11_XAUUSD_<label>.html`
- MT5 CSV: `mql5/signals/V11_XAUUSD_<label>.csv`
- Trade log: `db/backtests.db` (tables `backtest_runs`, `backtest_signals`,
  `backtest_trades`), each run keyed by a `run_id` and carrying the git
  commit, params, data provenance, sim config, balance, risk and guard state.

## 10. How to view the visual backtest

1. Interactive HTML — open the file in any browser (no server needed).
   To serve it through the existing FastAPI backend, add a `FileResponse`
   route for `reports/monthly/visual/`.
2. Inside MT5:
   - `python -m monthly_bt ... --chart` writes `mql5/signals/*.csv`
   - copy that CSV to
     `C:\Users\HP\AppData\Roaming\MetaQuotes\Terminal\BB16F565FAAA6B23A20C26C49416FF05\MQL5\Files\signals\`
     (terminal data folder = the one whose `origin.txt` says
     `C:\Program Files\XM Global MT5`)
   - copy `mql5/MonthlyBT_Signals.mq5` to that terminal's `MQL5\Indicators\`,
     compile in MetaEditor, drag onto a `GOLD.i#` / `BTCUSD#` chart of the
     matching timeframe. Leave `InpCsv` empty to auto-load the newest file.

## 11. No look-ahead — how it is prevented

- Entry is always the **next** bar's open (`entry_bar == signal_bar + 1`),
  never the signal bar's own close.
- Features are built on the **full history** and only then sliced, so every
  EMA/ATR/rank value at bar *i* uses only bars ≤ *i*.
- A period slice keeps 200 leading warm-up bars; those are real
  full-history bars, and a signal is attributed to a period by the broker
  time of its **entry** bar, so a warm-up bar can never leak a signal into
  the period it precedes.
- **Proven, not claimed:** `test_truncated_history_does_not_change_past_signals`
  recomputes on a 70% prefix and asserts every signal inside the prefix is
  byte-identical to the full-history run. If any future bar influenced a
  past signal, that test fails.
- `test_indicators_only_use_past_bars` checks EMA(21) is unchanged when the
  series is extended.
- V11's feature builder computes `fwd1/fwd3/fwd5`; those columns are
  forward-looking **by construction** but are never read by
  `compute_d1_signals` (they exist only for the drift printout in
  `research/v11_d1_momentum.main()`). The engine never touches them.

## 12. Limitations and assumptions — read before trusting a number

1. **Swap is not modelled** ($0). V11 holds up to 8 D1 bars, so multi-day
   financing is a real, unmodelled cost. V11 net P&L is therefore
   **overstated**. Make it worse with `--slippage`, but swap needs broker
   data the DB does not have.
2. **Commission defaults to $0/lot** — correct for this account's CFD
   symbols; set `--commission` if that changes.
3. **No tick replay.** Only OHLC bars exist, so the intra-bar sequence of
   high/low is unknown. When a bar touches both SL and TP, the **closer**
   level is assumed to fill first (conservative). Real fills may differ.
4. **Timezone is UTC+3 year-round** (summer EEST). Winter is UTC+2, so
   winter broker hours are labelled one hour later than they really are.
   Month boundaries are unaffected; hour-of-day analysis is not.
5. **Measured spread, not per-tick spread.** The mean of the recorded
   per-bar spread over the period is used.
6. **Monthly periods are independent.** Each month's position slot and
   balance start fresh, so a trade opened on the last day of January and
   still open on 1 Feb is simulated twice (once per month) if it re-enters
   the next month. This is what "independent period" means; it is not a
   continuous equity curve.
7. **Signals are attributed to the month of the ENTRY bar**, and a trade may
   exit in the following month. The holding period is part of the
   strategy, so it is not truncated.
8. **The DB is stale** (gold ends 2026-08-28; BTC 2026-09-25). Re-ingest
   before drawing conclusions.
9. **V12 is duplicated in the repo** — `run_v12_hourly.py` re-implements the
   feature builder instead of importing the research one, unlike V11. The
   parity test proves they agree today. Consolidating them is a separate
   change to production code, deliberately not made here.
10. **BTCDaily warm-up**: the 268-bar BTC history means the EMA55/60-bar
    rank is only barely warmed. BTC numbers are low-confidence.

## 13. Test results

`./.venv/Scripts/python.exe -m pytest tests/ -q`

**359 passed, 0 regressions** (baseline was 285; +74 new in
`tests/test_monthly_bt.py`). 2m35s. One pre-existing Starlette/httpx
deprecation warning from `test_api.py`, unrelated to this work.

The 74 new tests cover: signal generation and V11/V12 parity, entry/SL/TP
arithmetic, position sizing and its refusals, trade execution and all four
exit paths, monthly filtering, timezone conversion, three independent
no-look-ahead proofs, P&L/metrics, the trade-log schema, report rendering,
chart self-containment, and the MQL5 indicator's read-only property.

Chart JS was additionally verified by extracting it and running the real
`draw()` under Node with a stubbed canvas: 3,910 strokes, 26 markers, 40
labels, no errors; `node --check` passes; all 26 markers verified to have
`SL < entry < TP` and to map to real candles.

## 14. Confirmation

**No live or demo trade was placed.** Nothing in this work can place one:
`monthly_bt` contains no `order_send`, no `MT5Gateway`, no `positions_get`
and no MetaTrader5 import — enforced by tokenizer-based tests over the
actual code, not just a comment. The research DB is opened `mode=ro`.
Output goes to a separate `db/backtests.db`. The MT5 indicator only reads a
CSV.

`config/settings.toml` is **unchanged** and still contains
`live_trading_enabled = true`, which is your deliberate 2026-09-22 setting
for the existing V11/V12 demo runners. I did not flip it in either
direction, and the backtester neither reads nor writes it (it only reports
it in each run's provenance). A test asserts the working copy still matches
`HEAD`.

Nothing under `D:\rahul_ai\hermes\` was modified, no broker credentials were
read or changed, and no historical data was overwritten.
