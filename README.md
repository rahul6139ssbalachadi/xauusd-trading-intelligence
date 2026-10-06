# XAUUSD Trading Intelligence

An evidence-first quantitative trading research system for XAUUSD (gold) and
BTCUSD, built per the specification in `CLAUDE.md`.

> **This system cannot trade real money.** Not by configuration — by design.
> See [docs/SECURITY.md](docs/SECURITY.md).

---

## Status — read this before anything else

Three states that must never be conflated:

|| State | Status |
|---|---|---|
|| **Deployment-ready infrastructure** | Docker, compose, scripts, API, phone dashboard, health checks — written. **Never built or run: Docker is not installed on the dev machine.** |
|| **Trading-strategy validated** | V11 and V12 only, on 82 and 36 backtest trades. **Ten other strategies were tested and rejected.** |
|| **Live-trading approved** | **No.** `live_trading_enabled = false` is permanent, and the MT5 gateway refuses non-DEMO accounts independently. |

**No forward evidence exists.** `signals_log` is empty — not one live decision
has ever been recorded. Every performance figure in this repository is
in-sample or simulated. The API and dashboard say so explicitly rather than
letting a quiet dashboard read as a profitable one.

Profit is not guaranteed and is not claimed.

---

## Quick start (Windows Server)

### Prerequisites
- Windows Server (2016/2019/2022)
- Python 3.11+ 
- MetaTrader 5 terminal (XM Global)
- XM MT5 DEMO account

### Installation

```bash
# 1. Clone and set up
git clone https://github.com/rahul6139ssbalachadi/xauusd-trading-intelligence.git D:/trading
cd D:/trading

# 2. Create Python environment
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt -r requirements-api.txt

# 3. Configure MT5 connection
copy config\mt5.toml.example config\mt5.toml
# Edit config/mt5.toml: set your MT5 account number

# 4. Initialize database
python scripts/init_db.py

# 5. Configure environment
copy .env.example .env
# Edit .env: set API_SECRET_KEY

# 6. Run tests
.venv\Scripts\python.exe -m pytest tests/ -q
# Expect: 500 passed, 1 skipped
```

### Running the System

```bash
# API + Dashboard (port 8000)
.venv\Scripts\python.exe api\main.py

# Strategy worker (continuous evaluation)
.venv\Scripts\python.exe worker.py

# Daily paper trade execution (demo only)
.venv\Scripts\python.exe execution\run_v11_daily.py --dry-run
```

Access http://localhost:8000 or http://localhost:8000/m (mobile view)

---

## Quick start (Linux server)

```bash
git clone https://github.com/rahul6139ssbalachadi/xauusd-trading-intelligence.git /opt/trading
cd /opt/trading

# Python 3.11+ required (MT5 module is Windows-only)
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt -r requirements-api.txt

# Create configuration
cp .env.example .env
# Generate API_SECRET_KEY: python3 -c "import secrets; print(secrets.token_urlsafe(48))"

# Database (schema only - no market data on Linux without MT5)
python scripts/init_db.py

# Run
./scripts/preflight.sh
./scripts/start.sh
./scripts/status.sh
```

---

## Requirements

**Runtime:** Python 3.11+, `pandas`, `numpy`, `MetaTrader5`, `pytest`.

**API:** `fastapi`, `uvicorn`, `python-jose`, `passlib[bcrypt]`, `pydantic`.

**No Java. No Node.js. No PostgreSQL. No GPU.**

`MetaTrader5` is **Windows-only**. It cannot be installed or imported on Linux.
That single constraint shapes the whole architecture — see
[docs/MT5_ARCHITECTURE.md](docs/MT5_ARCHITECTURE.md).

---

## The central research finding

Ten strategies (V1–V10) were built and **all ten rejected** — profit factors
of 0.00–0.04. Trend-following, mean-reversion, momentum, candle-pattern,
volatility-breakout, regime-switching: every hypothesis failed.

The cause was structural, not a bug. Gold M5/M15 round-trip costs run ~0.11%
while the available drift is smaller, so costs swallow everything.

The fix was the timeframe. V11 (D1) and V12 (H1) have costs of ~0.013% and
price ranges large enough to leave room:

|| | Signals | Profit factor | Note |
|---|---|---|---|
| **V11** | D1 momentum breakout | 82 over 10 years | 1.64 in-sample | OOS exceeds IS, Monte Carlo ruin 0% |
| **V12** | H1 momentum breakout | 36 in 2026 | 2.20 | 6 of 8 months positive |

Both are live-approved **for a demo account**. Neither has forward evidence.

The failed experiments are preserved in `strategy/defs/` and `research/` on
purpose (CLAUDE.md §15). They are evidence.

---

## Architecture

```
phone/browser ──► nginx (TLS) ──► FastAPI ──► db/trading.db
                                        └──► strategy/defs/*.json
                                        └──► execution/journal.jsonl

worker.py ──► approved strategies ──► closed bars ──► journal decision
                                                    (never places orders)

Windows machine ──► MT5 terminal ──► ExecutionEngine ──► order
                     (the only order path in the repo)
```

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — full layer map
- [docs/DEPLOYMENT_AUDIT.md](docs/DEPLOYMENT_AUDIT.md) — the audit and its 15 findings
- [docs/MT5_ARCHITECTURE.md](docs/MT5_ARCHITECTURE.md) — why the server cannot trade
- [docs/SECURITY.md](docs/SECURITY.md) — controls, secrets, the inverted switch
- [docs/MOBILE_OPERATION.md](docs/MOBILE_OPERATION.md) — phone access, three methods
- [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) — every known failure mode

---

## Safety controls

Three independent mechanisms, each sufficient alone:

1. `config/settings.toml` → `live_trading_enabled = false`
2. `ExecutionEngine.pre_trade_checks()` asserts it on every order
3. `MT5Gateway.connect()` refuses any non-DEMO account

Plus the full 10-point pre-trade checklist, an approved-strategy gate, and a
kill switch that only a human can reset.

**`LIVE_TRADING_ENABLED` is not a permission — setting it to `true` *blocks*
execution.** This inverted polarity is deliberate; see
[docs/SECURITY.md](docs/SECURITY.md).

---

## Project layout

|| Path | Purpose |
|---|---|---|
|| `api/` | FastAPI: auth, client scoping, deployment endpoints, WebSocket |
|| `appconfig/` | env-driven configuration, safe defaults |
|| `execution/` | engine, MT5 gateway (Windows), execution adapters |
|| `worker.py` | headless strategy evaluation loop |
|| `health_checks.py` | 8 health checks; never raises |
|| `observability.py` | structured logging with secret redaction |
|| `strategy/`, `backtest/`, `validation/`, `risk/` | research core (Phases 5–10) |
|| `montecarlo/`, `candles/`, `paper/`, `reporting/` | robustness, patterns, journals |
|| `monthly_bt/` | month-by-month backtester + report rendering |
|| `research/` | 33 hypothesis harnesses, V1–V12 plus BTC |
|| `reports/` | every generated study, preserved |
|| `dashboard/mobile.html` | phone UI, one file, no build step |
|| `scripts/` | deployment: start/stop/restart/status/backup/restore |

---

## Common commands

```bash
# research
.venv\Scripts\python.exe runner/cli.py signal
.venv\Scripts\python.exe runner/cli.py report XAUUSD_D1_MOMENTUM_BREAKOUT_V11
python -m monthly_bt --help

# live (demo only)
.venv\Scripts\python.exe execution\run_v11_daily.py --dry-run
.venv\Scripts\python.exe execution\run_v12_hourly.py --dry-run
.venv\Scripts\python.exe execution\account_status.py

# deployment
./scripts/preflight.sh && ./scripts/start.sh && ./scripts/status.sh
```

---

## Backups

```bash
./scripts/backup.sh --keep 14
./scripts/restore.sh backups/trading-YYYYmmdd-HHMMSS.tar.gz
```

Archives contain the database and journals. Configure restore from your secure storage.

---

## Configuration files (committed for deployment)

| File | Purpose | Setup |
|------|---------|-------|
| `config/mt5.toml.example` | MT5 connection template | Copy to `mt5.toml` and set account number |
| `.env.example` | Environment variables | Copy to `.env` and set API_SECRET_KEY |
| `scripts/init_db.py` | Database schema creator | Run once to initialize |