# ARCHITECTURE

How the system is put together after the deployment refactor, and why.

---

## 1. The organising constraint

`MetaTrader5` is Windows-only. Everything else in this project is pure Python
and already runs headless on Linux.

That single fact determines the whole shape. The system is split along exactly
the line that MT5 draws:

- **Server (Linux):** research, strategy evaluation, API, dashboard, database,
  monitoring.
- **Windows machine:** the broker terminal and the order path.

Neither half needs the other to be useful. The server computes and displays;
Windows executes. See `docs/MT5_ARCHITECTURE.md`.

---

## 2. Layers

```
┌─────────────────────────────────────────────────────────────────┐
│  PRESENTATION                                                   │
│    dashboard/mobile.html    phone, no build step, no CDN        │
│    reports/*.html           existing static dashboards          │
│    mobile/                  Flutter native client (unchanged)    │
└─────────────────────────────────────────────────────────────────┘
┌─────────────────────────────────────────────────────────────────┐
│  API  api/main.py + api/deploy.py                               │
│    JWT auth · client scoping · /health · /ws · control allowlist│
└─────────────────────────────────────────────────────────────────┘
┌─────────────────────────────────────────────────────────────────┐
│  SERVICES                                                       │
│    worker.py           strategy evaluation on closed bars       │
│    health_checks.py    8 checks, never raises                   │
│    observability.py    structured logging + secret redaction    │
│    appconfig/          env-driven configuration                │
└─────────────────────────────────────────────────────────────────┘
┌─────────────────────────────────────────────────────────────────┐
│  EXECUTION  execution/                                          │
│    adapters.py     null | demo | mt5 | bridge                   │
│    __init__.py     ExecutionEngine: 10-point checklist          │
│    mt5_gateway.py  the ONLY order_send path  (Windows)          │
└─────────────────────────────────────────────────────────────────┘
┌─────────────────────────────────────────────────────────────────┐
│  RESEARCH  (unchanged, all platform-free)                       │
│    indicators/ market_structure/ strategy/ backtest/           │
│    validation/ risk/ montecarlo/ candles/ paper/ reporting/     │
│    self_improvement/ monthly_bt/ research/                      │
└─────────────────────────────────────────────────────────────────┘
┌─────────────────────────────────────────────────────────────────┐
│  DATA  market_data/ (providers, storage) · db/trading.db        │
└─────────────────────────────────────────────────────────────────┘
```

The research layer is untouched by this refactor. It was already correct, and
it is the part that produced every result in `CLAUDE.md`.

---

## 3. Request and decision paths

### Reading (phone → server)

```
phone → nginx (TLS) → FastAPI → db/trading.db
                              → strategy/defs/*.json
                              → execution/journal.jsonl
```

`/ws` pushes a status snapshot every 15s so the phone does not poll.

### Deciding (worker)

```
worker tick
  → read approved strategies from execution/approved.json
  → honour run/control/*.pause
  → load closed bars from db/trading.db
  → call the SAME signal function the live runner uses
  → journal the decision (BUY / SELL / WAIT + entry/SL/TP)
  → rewrite run/worker.heartbeat
```

The worker **never** places an order. It computes and records. The signal
functions it calls are the runner's own, so the server's decision cannot drift
from what Windows would decide.

### Executing (Windows)

```
run_v11_daily.py / run_v12_hourly.py
  → MT5Gateway.connect()      refuses non-DEMO, refuses wrong login
  → signal on last CLOSED bar
  → risk sizing via risk/
  → ExecutionEngine.pre_trade_checks()   10 checks + kill switch + approved
  → journal the decision BEFORE the order
  → order_send()              the only such call in the repo
```

---

## 4. Configuration

One module, `appconfig/`, reads the environment. Every value has a safe
default, and importing the module never raises.

Precedence: **environment variable → `config/*.toml` → built-in default.**

`market_data/config.py` was extended so `config/mt5.toml` is now optional: the
`MT5_TERMINAL_PATH` / `MT5_LOGIN` / `BROKER_SYMBOL` variables satisfy it. That
file is git-ignored, so a container never had it.

### The inverted switch

`LIVE_TRADING_ENABLED` is **not a permission**. It matches
`config/settings.toml`:

| Value | Meaning |
|---|---|
| `false` | normal. Demo execution permitted, real-money blocked. |
| `true` | **blocks everything.** It is a misconfiguration tripwire. |

This is confusing and it is deliberate. Before 2026-09-26 a single
`live_trading_enabled` flag meant both "run orders" and "real money allowed".
A flag that can mean two opposite things is a flag that will eventually mean
the wrong one at the wrong moment. The split makes the dangerous value
un-grantable rather than merely discouraged.

`ExecutionEngine.pre_trade_checks()` asserts this on **every order** as
`live_trading_forbidden`, independent of any other check.

---

## 5. Health and monitoring

`health_checks.py` runs eight checks. Each returns `ok`, `degraded`, or `down`.

| Check | Detects |
|---|---|
| `safety` | `TRADING_MODE=live`, or the blocking switch flipped |
| `config` | production-readiness problems |
| `database` | missing file, missing table, empty `market_data` |
| `market_data` | stale feed — newest D1 bar older than the threshold |
| `worker` | worker stopped, via heartbeat mtime |
| `execution` | adapter unavailable or misconfigured |
| `strategies` | `approved.json` missing or unreadable |
| `forward_data` | **no forward evidence recorded** |

`collect_health()` never raises. A health endpoint that 500s tells an operator
nothing, so an exception inside a check becomes a `down` result.

**The design rule:** a health endpoint that reports `ok` for an empty database
is worse than none, because it converts a visible problem into an invisible one.
An empty `market_data` table is `degraded` with an explanation. Zero recorded
forward signals is `degraded` with an explanation. Neither is `ok`.

---

## 6. Execution adapters

`ExecutionAdapter` is an ABC with four implementations. The interface is
two methods: `available()` and `place(OrderIntent)`.

| Adapter | Behaviour | Default |
|---|---|---|
| `null` | refuses everything | **yes** |
| `demo` | journals intent, places nothing | no |
| `mt5` | existing `ExecutionEngine` + `MT5Gateway` (Windows) | no |
| `bridge` | POSTs to a Windows machine over HTTPS | no |

Three properties hold across all of them:

1. **Unknown names fall back to `null`.** A typo cannot escalate privilege.
2. **`MetaTrader5` is imported lazily**, inside a method. This is what allows
   the module to be imported on Linux at all.
3. **No adapter can widen what the engine permits.** They are subordinate to
   `approved.json`, the kill switch, and the gateway's own account check.

---

## 7. Persistence

| What | Where | In git? |
|---|---|---|
| Market data, trades, journals | `db/trading.db` | no — by design |
| Backtest runs | `db/backtests.db` | no — by design |
| Decision journal | `execution/journal.jsonl` | no |
| Worker heartbeat, control flags | `run/` | no |
| Strategy definitions | `strategy/defs/*.json` | yes |
| Research results | `reports/`, `research/study/` | yes |

**Both databases are git-ignored.** That is the single most common deployment
failure: a fresh clone has no data, and every query succeeds returning zero
rows. `preflight.sh` and `/health` both check for it.

In Docker these are named volumes (`trading-db`, `trading-runtime`,
`trading-logs`), never image content.

---

## 8. What is deliberately not here

- **No PostgreSQL.** The schema is small, SQLite is already correct, and
  migrating would risk the research dataset for no operational gain at this
  scale. A `DATABASE_URL` is accepted and recognised for a future migration.
- **No Java.** Grep for `java`/`javac`/`.jar` returns zero hits, and the
  deployment adds none. This is a property worth protecting.
- **No frontend build step.** The phone dashboard is one HTML file. No npm, no
  bundler, no CDN.
- **No new authentication system.** The existing JWT flow is kept intact.
  Replacing it during a deployment refactor would be an unnecessary risk.
- **No live-trading path.** Out of scope by rule and by evidence.

---

## 9. The three states that must never be conflated

1. **Deployment-ready infrastructure** — the subject of this refactor. A
   passing Docker build proves this and only this.
2. **Trading-strategy validated** — V11 and V12 only, and only on 82 and 36
   backtest trades. Ten other strategies were tested and rejected.
3. **Live-trading approved** — **no.** `live_trading_enabled = false` is
   permanent in this build, and `MT5Gateway.connect()` refuses any non-DEMO
   account regardless of configuration.

The evidence-first validation requirements in `CLAUDE.md` are unchanged. Nothing
in this refactor weakens a single risk control, and nothing in it constitutes
evidence that any strategy is profitable.
