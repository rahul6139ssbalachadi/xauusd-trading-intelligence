# DEPLOYMENT AUDIT

Repository: `D:\rahul_ai\trading`
Date: 2026-09-28
Commit at audit: `9ff4e65` (working tree clean)
Status: **read-only audit — no project code was modified to produce this document**

---

## 1. Current architecture

### 1.1 Layer map (as actually built)

```
  market_data/        DB layer + providers (MT5, CSV) + storage + validation
      |               config loader (TOML) is the single config entry point
      v
  indicators/         ema, sma, rsi, atr, macd, adx, bollinger        (116 LOC)
  market_structure/   swings, BOS/CHoCH, sessions, volatility regime  (236 LOC)
      |
      v
  strategy/           Strategy dataclass, build_features(), evaluate() (322 LOC)
      |
      v
  backtest/           event-driven, no look-ahead, realistic costs      (267 LOC)
      |
      v
  validation/         train/val/test split + walk-forward                (198 LOC)
      |
      v
  risk/               fixed-fractional sizing + hard guards             (133 LOC)
      |
      v
  paper/              journal + paper_run (SIMULATION ONLY)             (370 LOC)
      |
      v
  reporting/          ExperimentRegistry + text/HTML report renderers    (183 LOC)

  Cross-cutting:
  montecarlo/         shuffle/scatter/jitter robustness, ruin prob      (189 LOC)
  candles/            7 candle-pattern detectors + statistics          (313 LOC)
  execution/          ExecutionEngine + MT5Gateway + runners (Phase 14) (1198 LOC)
  self_improvement/   propose() -> gates -> PENDING; promote() is human-gated (329 LOC)
  monthly_bt/         month-by-month backtester + reports + MQL5 export (2197 LOC)
  rk_trade/           multi-tenant strategy/journal config (JSON files)
  research/           33 hypothesis harnesses V1..V12 + BTC study       (10435 LOC)
  runner/cli.py       read-only CLI (market, signal, backtest, report, ...)
  api/main.py         FastAPI: JWT auth + client-scoped reads + 2 dashboards
  scripts/            ingest / probe / dashboard build helpers
  mobile/             Flutter client (Riverpod, dio, secure storage)
  mql5/               3 MQ5 experts + exported signal CSVs
```

### 1.2 Strategy identity

| Version | File | Symbol | Timeframes | Status |
|---|---|---|---|---|
| V1–V10 | `strategy/defs/XAUUSD_*_V{1..10}.json` | XAUUSD | M15+M5 | **all REJECTED** (PF 0.00–0.04) |
| V11 | `strategy/defs/XAUUSD_D1_MOMENTUM_BREAKOUT_V11.json` | XAUUSD | D1 | live-approved (IS PF 1.64, OOS > IS, MC ruin 0%) |
| V12 | `strategy/defs/XAUUSD_H1_MOMENTUM_BREAKOUT_V12.json` | XAUUSD | H1 | live-approved (14 trades, PF 2.20) |

V11 is implemented in `research/v11_d1_momentum.py` (feature/signal functions) and
invoked live by `execution/run_v11_daily.py` with frozen `PARAMS`.
V12 is the H1 sibling in `research/v11_h1_monthly_2026.py` /
`execution/run_v12_hourly.py`.

The rejected strategies are **preserved on purpose** (CLAUDE.md §15). They are
evidence and must not be deleted.

### 1.3 Backtesting modules

- `backtest/__init__.py` — generic event-driven engine. Entry at next bar open,
  intrabar stop/target with closest-level tie-break, force-close at end.
- `monthly_bt/` — the production-grade one. 12 modules: `engine.py`, `store.py`
  (separate `db/backtests.db`), `report.py`, `visual.py` (HTML), `strategies.py`
  (reuses the *live* runner gate set), `guard.py`, `cli.py`.
  Critical property: `monthly_bt/strategies.py` imports the **exact** gate
  functions from `execution/run_v11_daily.py` / `run_v12_hourly.py`, so a
  backtest cannot silently diverge from what the live runner would do.
- `montecarlo/__init__.py` — robustness on top of a trade sequence.
- `validation/__init__.py` — walk-forward windows + degradation reporting.

### 1.4 MT5 integration

```
MetaTrader5 (pypi, Windows-only wheel)
        |
        v
market_data/providers/mt5_provider.py     read: bars, symbol reconcile
        |
        v
execution/mt5_gateway.py   MT5Gateway    the ONLY order_send path in the repo
        |
        v
execution/__init__.py      ExecutionEngine  Section-23 checklist + risk veto
        |
        v
execution/run_v11_daily.py / run_v12_hourly.py   bar-close runners
```

`MT5Gateway.connect()` (execution/mt5_gateway.py:33-54) enforces, in order:
`mt5.initialize(path=terminal_path)` → `account_info()` not None →
`trade_mode == ACCOUNT_TRADE_MODE_DEMO` → `login == allowed_login`.
A REAL account is refused before any order method exists.

`ExecutionEngine.pre_trade_checks()` adds a second, per-order re-check of the
same conditions plus the 10-point Section-23 checklist and a kill-switch gate.

### 1.5 Databases

| File | Engine | Purpose | In git? |
|---|---|---|---|
| `db/trading.db` | SQLite, 19MB | market_data (187,029 rows), trades, users, clients, equity_curve, performance_summary, risk_state, signals_log | **no** (`.gitignore:9 *.db`) |
| `db/backtests.db` | SQLite | backtest_runs / _signals / _trades | no (same rule) |
| `db/schema/*.sql` | — | 4 canonical schema files | yes |

Both are git-ignored by design. A fresh clone therefore has **no data** and no
app tables until a schema is applied and bars are ingested.

### 1.6 Configuration

| File | Tracked | Purpose |
|---|---|---|
| `config/settings.toml` | yes | the two safety switches + `[paths]` |
| `config/risk_limits.toml` | yes | Section-20 defaults |
| `config/symbols.toml` | yes | canonical symbol registry + broker name mapping |
| `config/mt5.toml` | **no** (` .gitignore:13`) | terminal_path, allowed_login, symbol |

`market_data/config.py` is the single loader. `load_mt5_config()` raises a
clear `FileNotFoundError` when the file is absent, which is the correct
fail-closed behaviour.

### 1.7 Frontend / dashboard

Two existing surfaces, both static HTML, both with **no build step**:

- `reports/client_dashboard.html` — client view, `const API = 'http://localhost:8000'` (line 191).
- `reports/dashboard.html` — admin view.

Served by `api/main.py` at `/dashboard` and `/admin` via `FileResponse`, using a
**relative** path (`"reports/client_dashboard.html"`), so the API only starts
correctly when the working directory is the repo root.

`mobile/` is a Flutter app (Riverpod + dio + flutter_secure_storage + fl_chart).
It is a native client, not a web build.

### 1.8 Entry points

| Purpose | Command | Headless? |
|---|---|---|
| API + dashboards | `PYTHONPATH=<repo> .venv/Scripts/python.exe api/main.py` | yes |
| V11 daily | `.venv/Scripts/python.exe execution/run_v11_daily.py [--dry-run]` | yes |
| V12 hourly | `.venv/Scripts/python.exe execution/run_v12_hourly.py [--dry-run]` | yes |
| Account report | `execution/account_status.py [--json] [--telegram]` | yes |
| Heartbeat | `execution/heartbeat.py` | yes |
| Cron entrypoints | `execution/scheduled_live.py V11\|V12`, `scheduled_daily.py` | yes |
| CLI | `runner/cli.py <cmd>` | yes |
| Backtests | `monthly_bt/__main__.py`, `python -m monthly_bt` | yes |
| Ingest | `scripts/ingest_extended_gold.py` etc. | yes |
| Tests | `.venv/Scripts/python.exe -m pytest tests/ -q` | yes |

No GUI is required for any of these. The only GUI dependency in the entire
project is the MT5 terminal itself, which is a broker application, not part of
this codebase.

---

## 2. Dependencies

### 2.1 Runtime (`requirements.txt`)

```
pandas>=2.1
numpy>=1.26
MetaTrader5>=5.0
pytest>=8.0        (dev, but listed as runtime)
```

### 2.2 API (`requirements-api.txt`)

```
fastapi>=0.104
uvicorn[standard]>=0.24
python-jose[cryptography]>=3.3
passlib[bcrypt]>=1.7
python-multipart>=0.0.6
pydantic>=2.5
pydantic-settings>=2.1
```

### 2.3 Assessment

- Total real runtime surface is small — 4 core + 7 API packages. This is a
  deployment asset, not a liability.
- `MetaTrader5` is the only package with a hard OS constraint.
- `pytest` in `requirements.txt` should move to a dev requirement file. Non-blocking.
- `python-jose` is unmaintained upstream but is what the existing auth tests
  pin. Replacing it means touching the auth path, which is out of scope for a
  deployment refactor. Flagged, not changed.
- `pydantic-settings` is already a declared dependency but is **not imported
  anywhere** — configuration is hand-rolled TOML + `os.getenv`. This is the
  natural hook for the env-var layer (Phase 2).

---

## 3. Windows-only components

| Component | Evidence | Runs on Linux? |
|---|---|---|
| `MetaTrader5` pypi package | Windows-only wheel; needs a running Windows terminal | **No** |
| `market_data/providers/mt5_provider.py` | `import MetaTrader5` | No |
| `execution/mt5_gateway.py` | `import MetaTrader5` | No |
| `execution/run_v11_daily.py` / `run_v12_hourly.py` | `import MetaTrader5` inside `load_d1_recent`; connect to a gateway | No (but the DB fallback path is pure pandas) |
| `execution/account_status.py`, `heartbeat.py` | `import MetaTrader5` | No |
| `rk_trade/live_feed.py` | `import MetaTrader5` | No |
| 8 `scripts/*.py` ingest/probe helpers | `import MetaTrader5` | No |
| `setup.bat`, `setup_tasks.bat`, `enable_dev_mode.bat` | cmd batch | No (Linux uses the `scripts/*.sh` added in Phase 7) |

### 3.1 Windows path hard-codings in production code

Only **two**, both in the Telegram notification helper, not in the trading path:

- `execution/account_status.py:33` — `BRIDGE = Path(r"D:\rahul_ai\hermes\hermes_telegram_bridge.py")`
- `research/send_deploy_report.py:17` — same constant

Both are module-level constants; the file is only *read* inside `_load_creds()`,
so importing these modules on Linux is safe. Only `--telegram` raises
`FileNotFoundError`. A third reference exists in `execution/scheduled_live.py`
/`scheduled_daily.py` which import from `account_status`.

Outside Python: `config/mt5.toml` holds a Windows terminal path, and
`SETUP_GUIDE.txt` / `setup.bat` are Windows-specific documents.

### 3.2 No Java anywhere

Grep for `java`, `javac`, `.jar` across all Python: **zero hits**. No JVM, no
Gradle, no JAR. The project is pure Python + Dart. This is a genuine property
worth protecting, so the deployment must not introduce a Java-based step.

---

## 4. Linux-compatible components

Everything except the 8 MT5-importing modules and the 3 `.bat` files:

- `indicators/`, `market_structure/`, `strategy/`, `backtest/`, `validation/`,
  `risk/`, `paper/`, `reporting/`, `montecarlo/`, `candles/`,
  `self_improvement/`, `monthly_bt/`, `rk_trade/` (minus `live_feed.py`)
- `api/main.py` — pure FastAPI + `sqlite3`. Fully portable.
- `runner/cli.py` — fully portable.
- `research/*.py` — all portable; they read `db/trading.db`, not the terminal.
- `market_data/storage.py`, `csv_provider.py`, `validation.py`, `diagnostics.py`
- `market_data/providers/csv_provider.py` — the non-MT5 provider path
- `db/schema/*.sql` — SQLite DDL, portable
- `mobile/` Flutter **web** target — portable, though it needs the Flutter SDK
- `mql5/*.mq5` — these are MetaEditor sources, compiled by MT5's own toolchain
  on Windows. They are *artifacts*, not part of the Linux runtime.

---

## 5. Server requirements

### 5.1 For the recommended split deployment

**Linux server (Ubuntu 22.04/24.04), 2 vCPU / 2GB RAM / 20GB disk is ample:**

- Docker Engine 24+ with Compose v2
- Python 3.11 (inside the container only — nothing needs a host Python)
- `db/trading.db` present, either copied in or re-ingested

**Windows machine (any existing desktop is enough):**

- MT5 terminal, logged into the DEMO account
- Python 3.11 + the same repo checkout
- The `execution/` runner scripts

**No Java. No GPU. No external database server is required** — SQLite is the
system of record today and this audit does not change that.

### 5.2 The single hard constraint

`MetaTrader5` has no Linux build. There is no Wine path worth relying on for a
trading system, and MT5's own documentation does not support headless Linux
server operation. **MT5 must stay on Windows.** This drives the whole
recommended architecture.

---

## 6. Potential deployment problems

Ordered by how likely they are to actually bite.

### P1 — The trading runners cannot run on the server at all
`execution/run_v11_daily.py` imports `MetaTrader5` and connects to a terminal
with an explicit `terminal_path`. On Linux it fails at `mt5.initialize`.
**Consequence:** a naive "move everything to the server" silently loses the
ability to place demo orders.
**Fix:** ExecutionAdapter split. The server keeps strategy evaluation, data,
API, and monitoring; a Windows bridge owns the terminal.

### P2 — No data on a fresh clone
Both databases are git-ignored. A clone gives an API that serves empty lists and
a backtester that cannot run. Silent, because every query succeeds and returns
zero rows.
**Fix:** an explicit bootstrap that applies `db/schema/*.sql` and ingests bars,
plus a health check that reports market-data staleness instead of returning
"ok" on an empty table.

### P3 — The two safety switches are documented backwards in two places
`config/settings.toml` is correct and heavily commented, but:
- `setup.bat` final message: "Edit config/settings.toml — set live_trading_enabled = true"
- `SETUP_GUIDE.txt:41`: "live_trading_enabled = true"

Since 2026-09-26 `live_trading_enabled = true` **BLOCKS** execution; it is not a
permission. Anyone following the printed setup instructions on a fresh server
gets a system that refuses to trade and no clear reason why.
**Fix:** correct both documents; the config comment stays authoritative.

### P4 — The Telegram path is a hard-coded Windows path outside the repo
`D:\rahul_ai\hermes\hermes_telegram_bridge.py`. Missing on a server. Only
`--telegram` breaks, so this fails late and quietly — the report is generated,
printed, and never delivered.
**Fix:** env-var override with a documented default; log a WARNING (not a
traceback) when the file is absent.

### P5 — Two `live_trading_enabled` variables with opposite polarity and different names
`config/settings.toml` uses `live_trading_enabled` as a **blocker**.
`monthly_bt` reports `live_trading_enabled=True` in its own header
(`reports/monthly/*.md`: "live_trading_enabled=True (reported, never modified)")
— that is the *local variable* meaning "orders are capable in this simulator",
i.e. the opposite sense. Two same-named variables, opposite meanings, one of
them in a generated report a human will read.
**Fix:** rename the simulator-local variable; never let a report field named
`live_trading_enabled` mean "simulation only".

### P6 — The API resolves dashboards relative to the CWD
`api/main.py:434` → `FileResponse("reports/client_dashboard.html")`. Started
from anywhere but the repo root, `/dashboard` and `/admin` return 500. Under
Docker/systemd/uWSGI the CWD is frequently not the repo root.
**Fix:** resolve against the module's parent directory.

### P7 — CORS defaults to `*` with credentials enabled
`api/main.py:25` → `ALLOWED_ORIGINS = "*"` combined with
`allow_credentials=True`. Browsers reject that combination outright, so the
static dashboards are relying on the wildcard being ignored. Any real
origin-split deployment (phone on a different origin, reverse proxy on a
different port) will fail.
**Fix:** default to same-origin only; require an explicit env var to widen.

### P8 — The client dashboard hard-codes `http://localhost:8000`
`reports/client_dashboard.html:191`. On a phone, "localhost" is the phone. The
dashboard cannot work as shipped.
**Fix:** derive the API base from `window.location.origin`.

### P9 — The API secret has a shipped default
`api/main.py:22` → `TRADING_SECRET_KEY` defaults to
`"dev-secret-change-in-production"`. A deployment that forgets to set it issues
validly-signed JWTs to anyone who knows a published string.
**Fix:** refuse to start in production mode with the default value.

### P10 — Rate limiting is an unbounded in-process dict
`api/main.py:30`. Grows forever within a process lifetime, and is per-process,
so N workers give N× the limit. Fine for one demo user, wrong for exposure.
**Fix:** document, cap the stored windows.

### P11 — `signals_log` and `equity_curve` are empty
Zero rows. Every performance figure the system can show is therefore in-sample
or simulated. This is not a deployment bug, but it means a deployed dashboard
will show zeros and could be misread as "the system is broken" or, worse, as
"no losses yet".
**Fix:** surface an explicit "no forward data recorded" state in the API and
phone UI, and have `/health` report it.

### P12 — The DB price series is stale relative to the terminal
At audit time the last ingested XAUUSD close is 4456.22 while the terminal
quotes ~4196.85. Any dashboard or analysis fed from the DB is describing older
data. The live runners are unaffected (they read fresh bars from the terminal),
so this is a *reporting* staleness, not a trading error.
**Fix:** health check reports last-bar age; a re-ingest task restores parity.

### P13 — Docker is not installed on the development machine
`docker --version` → command not found. Any Dockerfile authored here can be
linted and reasoned about but **cannot be built or run locally** as the project
stands.
**Fix:** either install Docker, or have the build verified on the target server
and reported honestly as unverified-locally.

### P14 — `.venv` is Windows-shaped
`.venv/Scripts/python.exe` and Windows console scripts. A Linux deployment must
rebuild the venv inside the image; it cannot copy this one.

### P15 — `MetaTrader5` is imported at module top-level in `mt5_gateway.py`
`import MetaTrader5 as mt5` at line 14. That is why the package cannot even be
*installed* on Linux, let alone imported. An adapter design that imports the
gateway lazily is required — not merely a runtime check.

---

## 7. Recommended architecture

### 7.1 The governing constraint

MT5 is Windows-only, and the *only* thing MT5 provides is (a) fresh bars and
(b) the order path. Everything else in this project — indicators, structure,
strategy logic, backtesting, validation, risk, journaling, reporting, the API,
the dashboard — is pure Python and already proven headless.

So split along exactly that line.

```
┌──────────────────────── LINUX SERVER ─────────────────────────┐
│                                                                │
│  dashboard (mobile-first HTML, no build step)                 │
│        │  HTTPS                                               │
│        v                                                      │
│  nginx / Caddy  ──►  FastAPI  (:8000)                         │
│                          │  JWT auth, /health, /api/*         │
│                          │  WebSocket /ws                      │
│                          v                                    │
│                    db/trading.db (SQLite, volume)             │
│                          │                                    │
│                          v                                    │
│  worker / scheduler                                          │
│    - strategy evaluation on closed bars (no terminal needed   │
│      when fed from the DB; live bars arrive via the bridge)   │
│    - heartbeat, staleness checks, alerting                    │
│                                                                │
│  ExecutionAdapter                                             │
│    - NullExecutionAdapter      (default; refuses everything)  │
│    - DemoExecutionAdapter      (records intent, places no      │
│                                real order — safe default)     │
│    - MT5ExecutionAdapter       (imports MetaTrader5 lazily;   │
│                                disabled unless explicitly      │
│                                configured; Windows only)       │
│                                                                │
└────────────────────────────────────────────────────────────────┘
                          ▲
                          │  HTTPS + mTLS / shared secret
                          │  POST /bridge/orders  (Windows only)
                          │  POST /bridge/bars    (Windows only)
┌──────────────────── WINDOWS MACHINE ─────────────────────────┐
│  MT5 terminal (DEMO)  <──►  bridge client                     │
│                            execution/run_v11_daily.py         │
│                            execution/run_v12_hourly.py        │
└────────────────────────────────────────────────────────────────┘
```

### 7.2 Why this shape

1. **The server can never accidentally place a real order.** It has no MT5, and
   the adapter that would need it is disabled by default and imports its
   dependency lazily. The failure mode is "adapter unavailable", not "order
   sent".
2. **The Windows machine keeps the property it already has** — a real terminal
   with a real login, guarded by the existing gateway.
3. **The research half becomes genuinely server-shaped.** Backtests,
   walk-forward, Monte Carlo, month-by-month studies and the report renderers
   all run headless today. Moving them to a Linux box makes them runnable on
   demand from a phone.
4. **No Java, no new database server, no build toolchain** is introduced. SQLite
   stays the system of record. This is a deployment refactor, not a rewrite.

### 7.3 Explicitly out of scope

- Migrating SQLite → PostgreSQL. The current schema is small, SQLite is
  already correct, and migrating would risk the research dataset for no
  operational gain at this scale. Documented as a later decision.
- Replacing `python-jose`. Touching the auth path during a deployment
  refactor is a needless risk.
- Rewriting the Flutter client. It stays as-is; the phone path is served by
  the browser dashboard, which needs no SDK.
- Enabling live trading. Out of scope by rule and by evidence.

---

## 8. Audit conclusion

| Question | Answer |
|---|---|
| Can the research engine run on Linux today? | **Yes**, unchanged, as-is. |
| Can MT5 execution run on Linux? | **No.** Windows-only, permanently. |
| Can the API + phone dashboard run on Linux? | **Yes**, after the CWD, CORS and secret fixes listed as P6/P7/P9. |
| Is the project Docker-ready? | **No** — no Dockerfile, no compose, no `.dockerignore` exist yet. |
| Is Docker installed locally to verify a build? | **No.** Builds must be verified elsewhere or reported as unverified. |
| Is anything lost by a fresh clone? | **Yes** — both databases (git-ignored by design). |
| Is the safety posture sound? | **Yes.** The gateway/engine double-check is genuine and tested; the weakness is documentation, not code. |

The three states the project must never be conflated into:

- **deployment-ready infrastructure** — the subject of Phases 2–16
- **trading-strategy validated** — V11/V12 only, and only on 36–82 trades
- **live-trading approved** — **no.** `live_trading_enabled = false` is
  permanent in this build, and the MT5 gateway refuses REAL accounts by design.

Passing a Docker build proves only the first of these.
