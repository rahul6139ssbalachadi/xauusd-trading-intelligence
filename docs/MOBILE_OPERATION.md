# MOBILE OPERATION

Operating and monitoring the system from an Android phone.

---

## METHOD A — Mobile browser (recommended)

```
Android phone (Chrome)
      │  HTTPS
      ▼
nginx / Caddy
      │
      ▼
FastAPI  :8000
      │
      ▼
dashboard/mobile.html   →  engine, database, worker
```

Open: **`https://your-domain.com/m`**

Nothing to install. The dashboard is one self-contained HTML file — no build
step, no CDN, no JavaScript framework, no app store. It works in Chrome, Firefox,
and Samsung Internet.

### What you can see

| Card | What it tells you |
|---|---|
| **Banner** | `MODE: DEMO` + `LIVE TRADING DISABLED`. This is the first thing on screen, always. |
| **System** | API / worker / database / market-data state, each as a coloured pill. Problems are spelled out in plain English underneath. |
| **Latest signals** | BUY / SELL / WAIT for each approved strategy, with entry, SL and TP when a signal exists. |
| **Trades** | Open and closed trades, lots, entry, realised P/L. |
| **Strategies** | All 13 definitions, with the two live-approved ones marked `APPROVED`. The rejected V1–V10 are shown too, on purpose. |
| **Performance** | Net P/L, win rate, and an explicit statement that these figures are in-sample or simulated. |
| **Recent errors** | ERROR and CRITICAL log lines. |
| **Logs** | Tail of the decision journal. |
| **Session** | Who you are signed in as, and sign-out. |

### What you can control

Four buttons, and the list is closed:

- **Refresh data** — worker re-polls on its next tick
- **Pause strategy** — worker skips that strategy
- **Resume strategy** — clears the pause
- **Restart worker** — worker reloads config at its next tick

Every one of them writes an entry to `run/control_audit.jsonl`.

### What you deliberately cannot do

There is **no endpoint anywhere in this API** that can:

- enable live trading
- place an order
- modify a strategy definition
- change risk limits
- change the account

This is not a UI omission. The control endpoint validates against a fixed
allowlist of four action names and returns HTTP 400 for anything else. Live
trading is controlled by environment variables read once at process start, so
it is not reachable over the network at all. If you want to change it, you SSH
in and edit `.env` — deliberately, with a record in your shell history.

### Adding it to your home screen

Chrome → ⋮ menu → **Add to Home screen**. It launches full-screen with no
browser chrome, which makes it feel like an app.

---

## METHOD B — SSH from the phone

```
Android SSH client (Termius, JuiceSSH, ConnectBot)
      │  SSH :22
      ▼
Linux server
      │
      ▼
docker compose / scripts/*.sh
```

Best for: fixing things, reading logs in full, and anything the dashboard does
not cover.

Install **Termius** (free tier is enough) or **JuiceSSH** (fully free). Add a
host with your server IP and your SSH key.

```bash
# the operations you will actually use
cd /opt/trading/ai

./scripts/status.sh                    # state, health, last decisions
./scripts/logs.sh -n 200               # last 200 log lines
./scripts/logs.sh worker               # follow the worker
./scripts/restart.sh                   # bounce the stack
./scripts/backup.sh --keep 14          # back up
./scripts/update.sh                    # pull, rebuild, restart

docker compose -f docker-compose.prod.yml ps
docker compose -f docker-compose.prod.yml logs -f api
```

### Advantages
- Full control. Nothing is hidden behind a UI.
- Works even if the API is down — which is exactly when you need it.
- No browser, no session, no token expiry.

### Disadvantages
- Typing long commands on a phone keyboard is painful.
- Easy to run a destructive command by accident. `stop.sh --purge` prompts for
  the word `DELETE` for exactly this reason; respect that prompt.
- No visual overview. For "is it up and is it healthy", Method A is better.

**Recommendation:** use Method A for monitoring and Method B for intervention.
That is the split the dashboard and the scripts were designed around.

---

## METHOD C — Remote desktop

```
Android (RDP client)  →  Windows PC  →  MT5 terminal
```

**Only needed if you must interact with the MetaTrader5 terminal itself** —
checking a trade in the MT5 UI, manually closing something, or watching the
terminal because the system is your only account holder.

Options: **Microsoft Remote Desktop** app (RDP), or **RustDesk** / **AnyDesk**
(remote control, works through NAT, easier to set up).

### Advantages
- The only method that can see the actual broker terminal.
- Full mouse and keyboard, nothing to learn.

### Disadvantages
- Needs the Windows machine to be on and awake 24/7.
- A GUI OS over a phone screen is unpleasant to use.
- Largest attack surface of the three. An exposed RDP port is a standing
  invitation; prefer RustDesk/AnyDesk, or VPN plus RDP, never bare RDP to
  0.0.0.0.
- MT5 is the only thing here that genuinely needs a GUI. Nothing else in this
  project does.

### Recommendation
Set up a console power management policy on the Windows machine (disable sleep
and hibernate) so the terminal is always logged in. Keep RDP off unless you
need it; prefer RustDesk's on-demand connection.

---

## Which method for which task

| Task | Method |
|---|---|
| Is it up? Is it healthy? | A (banner + System card) |
| What signal fired? | A |
| What is the P/L? | A |
| Is the worker alive? | A, or `status.sh` |
| Read the full logs | B |
| Restart / update | B |
| Back up | B, or cron |
| Inspect the MT5 terminal | C |
| Enable live trading | Nowhere. Edit `.env` by hand, and reconsider first. |

---

## The honest caveat about what you will see

On first login, the Performance card will show zeros, and the signals list may
be short. That is not a bug:

- `signals_log` is currently **empty**. No live or forward decision has ever
  been recorded by this system.
- Every performance number available is in-sample or simulated. The V11/V12
  figures come from backtests over 82 and 36 trades respectively.

So a dashboard showing "0 trades, 0 P/L" is telling you the truth: the
measurement phase has not produced forward data yet. The health endpoint says
so explicitly under `forward_data`, and the Performance card carries the same
warning, precisely so that a quiet dashboard is not mistaken for a profitable
one.
