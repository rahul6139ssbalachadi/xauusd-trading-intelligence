# TROUBLESHOOTING

Ordered by how often each one actually happens.

---

## The API will not start

### `ConfigError: Refusing to start in production`

Working as intended. Read the listed problems.

```bash
docker compose -f docker-compose.prod.yml logs api | grep -A5 "Refusing"
```

Almost always one of:

| Problem | Fix |
|---|---|
| `API_SECRET_KEY is still the shipped development default` | `python3 -c "import secrets; print(secrets.token_urlsafe(48))"` → put it in `.env` |
| `TRADING_MODE=live is not supported` | set `TRADING_MODE=demo` |
| `LIVE_TRADING_ENABLED=true blocks execution` | set it to `false` |
| `EXECUTION_ADAPTER=mt5 requires MT5_TERMINAL_PATH` | you do not want `mt5` on a Linux server — use `null` |

### `Address already in use`

```bash
sudo ss -ltnp | grep :8000
# or, if a previous stack is lingering
docker compose -f docker-compose.prod.yml down
```

### Import error on startup

You are almost certainly running from the wrong directory, or using the wrong
interpreter.

```bash
cd /opt/trading/ai
python3 -c "import appconfig, health_checks, observability; print('ok')"
```

---

## The database is missing or empty

**The single most common deployment failure.** Both databases are git-ignored,
so a fresh clone has none — and every query still *succeeds*, returning zero
rows. It fails quietly.

```bash
ls -lh db/
curl -s localhost:8000/health | python3 -m json.tool | grep -A3 database
```

Healthy output looks like `"database": "ok"`. `down` means the file is absent.
`degraded` with "market_data is EMPTY" means the file exists but has no bars.

**Fix** — copy from your Windows machine:

```bash
scp db/trading.db <user>@<server>:/opt/trading/ai/db/trading.db
```

You cannot ingest on the server: the ingest scripts need a Windows MT5 terminal.

---

## `market_data: down` — "data feed looks dead"

The newest D1 bar is more than `HEALTH_STALE_AFTER_HOURS` old (default 48h,
plus 3h for the broker's UTC+3 offset).

**This is not a live feed.** The server reads stored bars. The database is only
as fresh as the last ingest. Re-ingest on Windows, then copy the database over.

```bash
# on Windows
python scripts/ingest_extended_gold.py
# then
scp db/trading.db <user>@<server>:/opt/trading/ai/db/trading.db
./scripts/restart.sh
```

### Price mismatch between the terminal and the dashboard

Expected until the above is done. The last ingested close was 4456.22 while the
terminal quoted ~4196.85 at the time of the audit. The **live runners read
fresh bars from the terminal** and are unaffected; anything fed from the
database is describing older data.

---

## `worker: down`

```bash
docker compose -f docker-compose.prod.yml ps worker
docker compose -f docker-compose.prod.yml logs --tail=50 worker
```

The worker writes `run/worker.heartbeat` every tick. `/health` reads its mtime
and reports `down` if it is older than 15 minutes.

Causes:
- the container is not running → `docker compose ... up -d worker`
- `WORKER_TICK_SECONDS` is set very high → the first tick may not have happened yet
- the worker crashed → check logs; it is designed to survive a failing tick, not a fatal error at import

The API and dashboard still work without the worker. You just get no new
signals.

---

## No signals appear

Working as intended, almost always. `WAIT` is a real decision, not an error.

V11 fires roughly 8 times a year. V12 roughly 14–16 times a month. If you have
been running for a day and seen nothing, that is the expected outcome.

Check the journal to confirm the worker is actually evaluating:

```bash
tail -5 execution/journal.jsonl
```

```bash
curl -s 'localhost:8000/api/signals?limit=5' | python3 -m json.tool
```

Look for `"event": "decision_evaluated"`. If the file is empty, the worker is
not ticking.

---

## The phone dashboard will not load

### `404` on `/m`

`dashboard/mobile.html` is not in the image. It is tracked in git — so either
you are running an old image:

```bash
./scripts/restart.sh --rebuild
```

### `Cannot reach the server`

The API is bound to `127.0.0.1:8000`, so it is **only reachable from the
server itself.** That is the correct default. You need a reverse proxy.

- with a domain → `docs/SERVER_DEPLOYMENT.md` §11a
- IP only → set `API_BIND=0.0.0.0` in `.env` and use the plain-HTTP nginx block
- over the internet with no domain → use a tunnel. **Do not serve raw HTTP
  publicly**; your password and JWT are clear text.

### Login works but every panel is empty

Almost always the database problem above. The panels are honest: an empty
database shows empty, not fake data.

### The banner says the wrong thing

The banner reads directly from the server's environment. If it says `LIVE`,
check `TRADING_MODE` in `.env` and restart. It cannot be wrong by accident.

---

## Control buttons do nothing

They are not instant. Each writes a flag; the worker acts on it at its next
tick — up to `WORKER_TICK_SECONDS` (default one hour).

```bash
ls run/control/                # flags are visible here
curl -s localhost:8000/api/worker | python3 -m json.tool
```

Every action is audited:

```bash
cat run/control_audit.jsonl
```

### `400` on any control action

Correct. The allowlist is four actions: `refresh-data`, `pause-strategy`,
`resume-strategy`, `restart-worker`. Anything else — including anything
resembling "enable live trading" — is rejected. That is by design.

---

## Windows-specific

### MT5 will not start on the server

**Check whether it is Server Core.** MT5 is a GUI application and cannot run
without a desktop shell. `Server Manager → Add Roles and Features` should
show "Desktop Experience". If it is Core, MT5 is not an option on that
machine.

### The V11/V12 runner declines

Read the checklist it prints. It names the failing check. The common ones:

- `account_is_demo` — the terminal is logged into a real account
- `account_login_expected` — wrong demo account
- `kill_switch_inactive` — `execution/KILL_SWITCH` exists; **deleting it is the
  only reset, by design** (CLAUDE.md §24)
- `position_size_valid` — with ATR stops, some gold stops are wide enough that
  0.25% risk floors to 0 lots. The engine declines rather than exceeding the
  cap. Correct behaviour.
- `no_duplicate_position` — declines if **any** position is open on the symbol,
  including your manual mobile trades. Intentional.

### `.bat` and `.sh`

`scripts/*.sh` are for Linux. On Windows use:

```bash
.venv\Scripts\python.exe api\main.py
.venv\Scripts\python.exe worker.py
.venv\Scripts\python.exe execution\run_v11_daily.py --dry-run
```

Scheduling is `setup_tasks.bat` (Administrator). MT5 config goes in
`config/mt5.toml` there, since the env-var path is the Linux convention.

---

## Docker problems

### `permission denied` running docker

```bash
sudo usermod -aG docker "$USER"
newgrp docker        # or log out and back in
```

### `docker compose` not found

You have the old v1. Install the v2 plugin: `sudo apt install docker-compose-v2`.
These scripts use `docker compose` (with a space).

### The image fails to build

Note the specific layer. A failure during `pip install` is usually a network or
index problem; a failure during `COPY` means a directory named in the
Dockerfile does not exist. Every `COPY` line names a real directory.

---

## Performance and capacity

| Symptom | Cause | Action |
|---|---|---|
| `/health` slow (>5s) | 19MB SQLite counted on every call | expected on cold cache; fine warm |
| Disk filling | Docker logs, backups, journal | `./scripts/backup.sh --keep 14`; `docker system prune` |
| Memory climbing | rate-limit dict | bounded at 4096 IPs now |
| API slow under repeated load | single uvicorn worker by design | single-user deployment; scale only if needed |

---

## Getting help

When asking, include the output of:

```bash
./scripts/status.sh
curl -s localhost:8000/health | python3 -m json.tool
docker compose -f docker-compose.prod.yml ps
docker compose -f docker-compose.prod.yml logs --tail=100 api
```

The `/health` payload is designed to be self-explanatory: every non-`ok` state
carries a plain-English `detail` explaining what it means and what to do.
