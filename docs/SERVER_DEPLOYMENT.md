# SERVER DEPLOYMENT

Target: a **fresh Ubuntu 22.04 or 24.04 server**.
Assumes: a clean machine, SSH access as a sudo-capable user, and the repository pushed to a git remote.

Every command is exact. Run them in order.

---

## 0. What you are deploying, and what you are not

**On the server (Linux):** strategy engine, research/backtesting, FastAPI, the
phone dashboard, the SQLite database, the background worker, monitoring.

**Not on the server:** MetaTrader5. It is a Windows-only package with no Linux
build. The MT5 terminal stays on your Windows machine. The server's execution
adapter defaults to `null`, which means it computes and displays signals and
places **no orders at all**. That is the intended configuration.

If you later want the server to propose trades that your Windows machine
executes, see `docs/MT5_ARCHITECTURE.md`. You do not need it to deploy.

**Server sizing:** 2 vCPU, 2GB RAM, 20GB disk is comfortable. The whole thing
is pandas plus a small API; the database is ~19MB.

---

## 1. Install Docker and Git

```bash
sudo apt update
sudo apt upgrade -y
sudo apt install -y git curl ca-certificates

# Docker Engine + Compose v2, from the official repository
sudo apt install -y docker.io docker-compose-v2
sudo systemctl enable --now docker

# Allow your user to run docker without sudo (optional but convenient)
sudo usermod -aG docker "$USER"
newgrp docker          # or log out and back in
```

Verify:

```bash
docker --version
docker compose version
```

Both must print a version. `docker compose` (v2, with a space) is required —
the old `docker-compose` with a hyphen is not supported by these scripts.

---

## 2. Clone the repository

```bash
sudo mkdir -p /opt/trading && sudo chown "$USER":"$USER" /opt/trading
git clone <your-git-remote-url> /opt/trading/ai
cd /opt/trading/ai
```

Private repo? Use a read-only deploy key rather than your personal token:

```bash
ssh-keygen -t ed25519 -C "trading-ai-deploy" -f ~/.ssh/trading_ai_deploy
cat ~/.ssh/trading_ai_deploy.pub       # add this as a deploy key on the repo
ssh-keyscan github.com >> ~/.ssh/known_hosts
git clone git@github.com:<owner>/<repo>.git /opt/trading/ai
```

---

## 3. Create `.env`

```bash
cd /opt/trading/ai
cp .env.example .env
```

Generate the API secret:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(48))"
```

Put that value in `.env` as `API_SECRET_KEY`. It is the one value in the file
that must not stay empty.

**Confirm the two safety lines are correct before you go any further:**

```bash
grep -E '^(TRADING_MODE|LIVE_TRADING_ENABLED|EXECUTION_ADAPTER)=' .env
```

Expected:

```
TRADING_MODE=demo
LIVE_TRADING_ENABLED=false
EXECUTION_ADAPTER=null
```

If `TRADING_MODE=live` appears, the API will refuse to start. That is
intentional — this build cannot trade real money. See `docs/SECURITY.md`.

---

## 4. Configure secrets

Everything sensitive lives in `.env` only. `.env` is git-ignored; `.env.example`
is tracked and safe to commit.

| Variable | Needed on the server? | Notes |
|---|---|---|
| `API_SECRET_KEY` | **yes** | Signs JWTs. Generate as above. |
| `TRADING_MODE` | yes | Must be `demo`. |
| `LIVE_TRADING_ENABLED` | yes | Must be `false` (true *blocks*). |
| `EXECUTION_ADAPTER` | yes | `null` is correct for Linux. |
| `DATABASE_URL` | no | Leave empty; SQLite path is used. |
| `MT5_LOGIN`, `MT5_PASSWORD`, `MT5_TERMINAL_PATH` | **no** | MT5 is Windows-only. Leave empty on the server. |
| `BRIDGE_URL`, `BRIDGE_TOKEN` | no | Only if using the Windows bridge. |
| `TELEGRAM_*` | no | Notifications are optional. |

Lock the file down:

```bash
chmod 600 .env
```

---

## 5. Get the database in place

**This step is not optional and is the most common failure.** Both databases are
git-ignored by design, so a fresh clone has none. Without it the API starts but
`/health` reports the database as down, and every analysis returns nothing.

Two options.

**Option A — copy the existing database from your laptop** (recommended; keeps
the 10 years of history):

```bash
# on your Windows machine, from the repo:
scp db/trading.db <user>@<server-ip>:/opt/trading/ai/db/trading.db

# if you also want the backtest database:
scp db/backtests.db <user>@<server-ip>:/opt/trading/ai/db/backtests.db
```

**Option B — ingest on the server.** Not possible without MT5. Ingestion scripts
need a terminal. So if you have no database to copy, run this on Windows and
then copy the result:

```bash
python scripts/ingest_extended_gold.py
```

Verify:

```bash
ls -lh db/
python3 -c "
import sqlite3
c = sqlite3.connect('db/trading.db')
print('market_data rows:', c.execute('SELECT COUNT(*) FROM market_data').fetchone()[0])
"
```

A healthy database has well over 100,000 rows. Zero means the copy did not work.

---

## 6. Build the containers

```bash
cd /opt/trading/ai
chmod +x scripts/*.sh
docker compose -f docker-compose.prod.yml build
```

Expect a few minutes on first build. It compiles nothing native and installs
no Java.

---

## 7. Start the services

```bash
./scripts/start.sh
```

`start.sh` refuses to proceed if `.env` is missing, if `API_SECRET_KEY` is
empty, or if `TRADING_MODE` is `live`. It then builds, starts both services,
and polls `/health` until the API answers.

Check what came up:

```bash
docker compose -f docker-compose.prod.yml ps
```

Expect `trading-api` and `trading-worker`, both `running`, API showing
`(healthy)`.

---

## 8. Check health

```bash
curl -s http://127.0.0.1:8000/health | python3 -m json.tool
```

A healthy deployment looks roughly like:

```json
{
  "status": "degraded",
  "mode": "DEMO",
  "live_trading_enabled": false,
  "database": "ok",
  "worker": "ok",
  "market_data": "ok",
  "execution": "degraded",
  "forward_data": "degraded"
}
```

Read those three non-`ok` states carefully, because they are not noise:

- `execution: degraded` — **expected.** The adapter is `null`, so no orders can
  be placed. Correct on a Linux server.
- `forward_data: degraded` — **expected today.** It means no live/forward
  decision has ever been recorded. Every performance number in this system is
  in-sample or simulated until that counter moves.
- `market_data: ok` — the newest D1 bar is recent. If this says `down`, the
  database is stale and you need to re-ingest on Windows and copy it over.

The one that should never be tolerated:

- `status: down` with `database: down` — the database did not make it. Go back
  to step 5.

The full `checks` object explains each one in plain English.

---

## 9. View logs

```bash
./scripts/logs.sh              # follow everything
./scripts/logs.sh api          # just the API
./scripts/logs.sh -n 200       # last 200 lines, no follow
docker compose -f docker-compose.prod.yml logs -f worker
```

Logs are JSON, one object per line, to stdout and to `/app/logs/*.log` inside
the container. Secrets are redacted before they are written — see
`docs/SECURITY.md`.

---

## 10. Restart services

```bash
./scripts/restart.sh              # recreate containers, reuse the image
./scripts/restart.sh --rebuild    # rebuild the image too (after a code change)
```

Both are idempotent. Neither touches the database.

---

## 11. Expose it (reverse proxy + HTTPS)

The API is bound to `127.0.0.1:8000`, so right now only the server itself can
reach it. To use it from your phone you need a reverse proxy.

### 11a. With a domain

```bash
# 1. point an A record at the server's public IP, wait for it to resolve
# 2. install the proxy and certbot
sudo apt install -y nginx certbot python3-certbot-nginx

# 3. install the config
sudo cp deploy/nginx-trading-ai.conf /etc/nginx/sites-available/trading-ai
sudo ln -s /etc/nginx/sites-available/trading-ai /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx

# 4. get a certificate (this also rewrites the config to add the redirect)
sudo certbot --nginx -d your-domain.com
```

Then open `https://your-domain.com/m` on your phone.

### 11b. With only an IP

Comment out the TLS server block in the config and uncomment the plain-HTTP
block at the bottom, then set the API to listen on all interfaces:

```bash
# in .env:
API_BIND=0.0.0.0

sudo nginx -t && sudo systemctl reload nginx
./scripts/restart.sh
```

Browse to `http://<server-ip>/m`.

**Read this before you do it.** Plain HTTP sends your login password and JWT
in clear text. That is fine on a home LAN. It is not fine on the public
internet. If this server is reachable from the internet and you have no domain,
use a tunnel instead: Cloudflare Tunnel, Tailscale, or an SSH port forward from
your phone. All three give you HTTPS without owning a domain or opening a port.

---

## 12. Update from git

```bash
cd /opt/trading/ai
./scripts/update.sh
```

This takes a backup first, then `git pull --ff-only`, then rebuilds and
restarts. If you have uncommitted local changes it stops and asks. If the pull
is not a fast-forward it refuses rather than creating a merge commit.

Verify afterwards with `./scripts/status.sh`.

---

## 13. Back up the database

```bash
./scripts/backup.sh              # one archive
./scripts/backup.sh --keep 14    # and keep only the newest 14
```

Archives land in `backups/trading-YYYYmmdd-HHMMSS.tar.gz`. The script verifies
each archive is readable before reporting success.

**Secrets are never included.** `.env` and `config/mt5.toml` are excluded by
construction. Restoring a backup therefore does **not** restore your API secret
— that stays in your password manager, which is where it belongs.

Automate it with cron:

```bash
crontab -e
# daily at 03:17, keep 14 days
17 3 * * * /opt/trading/ai/scripts/backup.sh --keep 14 >> /var/log/trading-backup.log 2>&1
```

---

## 14. Restore the database

```bash
./scripts/restore.sh backups/trading-20260928-030000.tar.gz
```

It shows you what is in the archive, stops the stack, makes a safety copy of
the current database as `db/trading.db.pre-restore-<timestamp>`, extracts, and
stops there. It does not restart anything — inspect first, then
`./scripts/start.sh`.

The safety copy means a bad restore is itself recoverable.

---

## 15. Stop everything safely

```bash
./scripts/stop.sh
```

Stops the containers and **preserves** all data. To also delete the database
and journals:

```bash
./scripts/stop.sh --purge
```

This prompts for the word `DELETE` first. Volumes are named
(`trading-db`, `trading-runtime`, `trading-logs`) so they never accumulate as
anonymous leftovers.

---

## 16. Local development vs production

| | Local (Windows laptop) | Server (Linux) |
|---|---|---|
| Compose file | `docker-compose.dev.yml` | `docker-compose.prod.yml` |
| `ENVIRONMENT` | `development` | `production` |
| API secret | dev default is fine | required, ≥32 chars |
| Source | bind-mounted, live reload | baked into the image |
| API bind | `0.0.0.0:8000` (phone on LAN) | `127.0.0.1:8000` (proxy only) |
| Log format | text | JSON |
| Secret behaviour | server refuses to start on a dev secret | enforced |

Local without Docker is still the fastest path and needs nothing new:

```bash
.venv\Scripts\python.exe api\main.py
.venv\Scripts\python.exe worker.py
```

---

## 17. Operating from your phone

See `docs/MOBILE_OPERATION.md` for the three methods (browser, SSH, remote
desktop) and what each can and cannot do.

---

## 18. First-run checklist

```bash
./scripts/preflight.sh    # checks docker, .env, secret, data, disk
./scripts/start.sh
./scripts/status.sh
curl -s localhost:8000/health | python3 -m json.tool
./scripts/backup.sh       # take the first backup now
```

Then, from your phone: open `https://your-domain.com/m` (or the IP), sign in,
and confirm the banner says **MODE: DEMO** and **LIVE TRADING DISABLED**.
