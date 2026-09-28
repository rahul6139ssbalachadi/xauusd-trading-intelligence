# SECURITY

What protects what, where the boundaries are, and what is deliberately absent.

---

## 1. The one rule that governs everything

**This build cannot trade real money. Not by configuration — by design.**

Three independent mechanisms enforce it, and each is sufficient on its own:

1. `config/settings.toml` → `live_trading_enabled = false`
2. `ExecutionEngine.pre_trade_checks()` asserts it on **every order** as the
   `live_trading_forbidden` check
3. `MT5Gateway.connect()` refuses any account whose `trade_mode` is not
   `ACCOUNT_TRADE_MODE_DEMO` — before any order method exists

Remove any one and the other two still hold. There is no configuration, API
call, or environment variable that turns real-money trading on.

---

## 2. The inverted switch

`LIVE_TRADING_ENABLED` is **not a permission**. It has inverted polarity:

| Value | Effect |
|---|---|
| `false` | Normal. Demo execution permitted; real money blocked. |
| `true` | **Blocks all execution.** Treated as a misconfiguration. |

This is confusing on purpose. Before 2026-09-26 a single flag meant both "run
orders" and "real money allowed" — a flag that can mean two opposite things
will eventually mean the wrong one at the wrong moment. The split makes the
dangerous value un-grantable rather than merely discouraged.

**Consequence for deployment:** if you ever see "the system won't trade",
check this flag first. `true` means stop, not go.

### Two stale documents

`setup.bat` and `SETUP_GUIDE.txt` both say "set `live_trading_enabled = true`".
**That instruction is wrong** and predates the split. `config/settings.toml`
and this document are authoritative. Correcting those two files is a known
outstanding item, not something to be confused about at 2am.

---

## 3. Secrets

### Where they live

**Only in `.env`**, which is git-ignored. `.env.example` is tracked and
contains no real values.

```bash
chmod 600 .env
```

### What must never enter git

| Item | Why |
|---|---|
| `.env` | API secret, bridge token, broker password |
| `config/mt5.toml` | account number and terminal path |
| `db/*.db` | the trading dataset — large, and not source |
| `backups/` | archives contain the database |
| `execution/journal.jsonl` | decisions, not source |

All are in `.gitignore` **and** `.dockerignore`. The Dockerfile never copies
`config/mt5.toml`.

### Generation

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(48))"
```

### The one hardcoded external path

`execution/account_status.py:33` and `research/send_deploy_report.py:17` read
Telegram credentials from:

```
D:\rahul_ai\hermes\hermes_telegram_bridge.py
```

That is a path in a **separate project you asked me not to touch**. It is a
module-level constant, so importing those modules on Linux is safe — only
`--telegram` raises `FileNotFoundError`. It is not a security exposure, but it
does mean Telegram notifications do not work on a server without that file.
Fixing it means either copying the bridge to the server or making the path an
env var; the latter is a one-line change that is a deliberate future decision,
not something to do silently inside a deployment refactor.

---

## 4. API security

### Authentication

JWT bearer tokens, `python-jose` HS256, 30-minute expiry. The legacy
`client`/`admin` roles scope which trades each user can see.

### The dev secret is refused in production

`API_SECRET_KEY` previously defaulted to `"dev-secret-change-in-production"` —
a **published string**. A deployment that forgot to set it would have issued
validly-signed tokens to anyone who knew it.

Now: `appconfig.check_production_readiness()` flags it, and with
`ENVIRONMENT=production` the API **refuses to start**. Verified working.

### CORS

The previous default was `allow_origins=["*"]` together with
`allow_credentials=True`. Browsers reject that combination outright, so a
split-origin deployment silently failed while looking configured.

Now: empty `ALLOWED_ORIGINS` means **same-origin only**, which is correct
behind a reverse proxy. Widen deliberately; never with `*`.

### Rate limiting

5 login attempts per IP per 60s. Now bounded at 4096 tracked IPs so a
long-lived process cannot grow the dict without limit. Per-process, so
multiple workers multiply the limit — acceptable for a single-user deployment,
not for a public service.

### What the API will never do

There is **no endpoint** that can:

- enable live trading
- place an order
- modify a strategy definition
- change risk limits
- change the account

`POST /api/control/{action}` validates against a closed allowlist of four
actions and returns `400` for anything else. `LIVE_TRADING_ENABLED` is read
from the environment once at process start and is not writable over the
network at all.

---

## 5. Network exposure

### Default posture

The API binds to `127.0.0.1:8000` in `docker-compose.prod.yml`. **Only the
server itself can reach it.** The reverse proxy is the single entry point.

### Before exposing it to the internet

- [ ] HTTPS with a real certificate (`certbot --nginx`)
- [ ] `API_SECRET_KEY` generated fresh, 32+ characters, in `.env`
- [ ] `.env` is `chmod 600`
- [ ] `TRADING_MODE=demo`, `LIVE_TRADING_ENABLED=false`
- [ ] `/health` confirms `mode: DEMO`
- [ ] Firewall: only 22, 80, 443 open. **Never 8000.**

### Plain HTTP over the internet

Unacceptable. Login passwords and JWTs cross in clear text. If you have no
domain, use **Cloudflare Tunnel**, **Tailscale**, or an SSH tunnel. All give
HTTPS without owning a domain or opening a port. See
`docs/SERVER_DEPLOYMENT.md` §11b.

### SSH

Key-based, password auth disabled, non-standard port if you like. The Windows
machine running MT5 is a separate exposure question and deserves the same care.

---

## 6. The bridge adapter (not currently used)

`BridgeExecutionAdapter` forwards order intents to a Windows machine. It is
disabled unless `BRIDGE_URL` **and** `BRIDGE_TOKEN` are both set — with no
token it returns `REFUSED` and sends nothing. There is deliberately no
anonymous fallback.

If you ever enable it:

- HTTPS only
- never expose the port publicly; VPN or source-IP restriction
- the listener must accept order intents **and nothing else** — never a generic
  "run this code" primitive
- log every intent, accepted or declined

---

## 7. Logging and redaction

`observability.py` installs a `SecretFilter` on the **root** logger, so it
covers third-party libraries as well as our own. It scrubs three ways:

1. structured `extra_data` / `context` / `data` dicts, by key name
2. named sensitive attributes (`X-Bridge-Token` and friends)
3. inline `key=value` patterns in the formatted message

Verified: `password=`, `token=`, and `X-Bridge-Token:` all render as
`***REDACTED***` in both text and JSON output.

Third-party loggers (`urllib3`, `httpx`, `httpcore`, `asyncio`) are pinned to
`WARNING` because they can echo URLs containing credentials.

This is a backstop, not a substitute for care at the call site. It reduces the
blast radius of a logging mistake; it does not make logging safe by default.

---

## 8. Data sensitivity

`db/trading.db` holds the full market dataset and the trade history. It is
git-ignored, excluded from the Docker build context, and excluded from backups
of secrets — but it *is* included in `backup.sh` archives by design, because
losing it loses years of research.

Those archives contain your trading data. Store them somewhere with the same
care as the database itself, and do not email them.

---

## 9. Reporting a vulnerability

This is a single-owner private repository, not a public service. If you find a
problem, it is between you and the machine it runs on — fix it, and note it in
the commit. There is no disclosure process because there is no third party.

That is a reason to be more careful, not less: **nobody else is looking.**
