# MT5 ARCHITECTURE

Why the server cannot trade, and what the alternatives are.

---

## 1. The hard constraint

`MetaTrader5` (the Python package) has **no Linux build**. It is a Windows-only
wheel that communicates with a Windows terminal process over a Windows IPC
mechanism. This is not a configuration problem that a container flag can solve.

Three facts follow:

1. It cannot be installed in a Linux container.
2. It cannot be imported in a Linux process, so any module importing it at the
   top level is unimportable on the server. `execution/mt5_gateway.py` is
   exactly such a module.
3. The MT5 terminal itself is a Windows GUI application. There is no supported
   headless Linux server mode.

Wine-based approaches exist in the folklore. **Do not use one for a trading
system.** The failure modes are subtle and quiet — a partially working IPC
layer that returns stale prices is far more dangerous than an honest "this is
unavailable".

**Conclusion: MT5 stays on Windows. Permanently.**

---

## 2. What the current code already does

The pre-existing code is already correctly partitioned, which is why this
refactor is mostly plumbing rather than rework.

```
market_data/providers/mt5_provider.py   read bars      (Windows)
execution/mt5_gateway.py                order path     (Windows)
        │
        ▼
execution/__init__.py  ExecutionEngine  checklist + risk veto   (platform-free)
        │
        ▼
execution/run_v11_daily.py / run_v12_hourly.py                (Windows)
```

`ExecutionEngine` itself imports no `MetaTrader5` — the gateway is injected, so
the engine is testable and portable. `monthly_bt/strategies.py` already reuses
the runners' exact gate functions, so a backtest cannot drift from what the
live runner would decide. That property is what makes the bridge below
trustworthy.

**Only 8 of ~30 modules import `MetaTrader5`.** Everything else is pure Python
and already runs on Linux.

---

## 3. The recommended split

```
┌────────────────────── LINUX SERVER ──────────────────────┐
│  worker.py          strategy evaluation on closed bars   │
│  api/               FastAPI + phone dashboard             │
│  db/trading.db      market data, trades, journals        │
│  EXECUTION_ADAPTER=null   ← places nothing              │
└──────────────────────────────┬───────────────────────────┘
                               │
        (optional, authenticated HTTPS)
                               ▼
┌──────────────────── WINDOWS MACHINE ─────────────────────┐
│  bridge client → ExecutionEngine → MT5Gateway → MT5      │
│                                                       │
│  HARD GUARDS, all unchanged:                          │
│    · account must be ACCOUNT_TRADE_MODE_DEMO          │
│    · login must equal the configured allowed_login    │
│    · live_trading_enabled must be false (true blocks) │
│    · strategy must be in approved.json                │
│    · kill switch must be absent                        │
│    · all 10 Section-23 pre-trade checks                │
└───────────────────────────────────────────────────────┘
```

### The critical property

**The bridge is not a bypass.** The Windows side runs the full
`ExecutionEngine` with the full pre-trade checklist before anything reaches the
broker. A signal forwarded from the server is a *proposal*. It is filtered by
exactly the same controls as a signal generated locally on Windows.

If you forward a signal and the Windows side executes it, every safety check
still ran. If the server sends garbage, the Windows side declines it.

### How they would communicate

The server-side adapter already exists: `BridgeExecutionAdapter` in
`execution/adapters.py`. It POSTs an `OrderIntent` to:

```
POST {BRIDGE_URL}/bridge/orders
Content-Type: application/json
X-Bridge-Token: {BRIDGE_TOKEN}
```

The Windows side would run a small HTTP listener that receives the intent,
constructs an `OrderRequest`, and hands it to `ExecutionEngine`.

**You do not need this to deploy.** The server is fully functional with
`EXECUTION_ADAPTER=null`: it evaluates strategies, records decisions, and shows
them on your phone. The bridge is only for the case where you want the server
to drive a terminal on another machine.

### If you do build it, these are the requirements

- HTTPS only. A plain-HTTP order bridge on a public network is unacceptable.
- `BRIDGE_TOKEN` is mandatory. The adapter returns `REFUSED` without it — there
  is deliberately no anonymous fallback.
- The bridge listener must reject any request whose token does not match, and
  must **not** expose a generic "run this Python" primitive. It accepts order
  intents and nothing else.
- Log every received intent, accepted or declined.
- Do not expose the bridge port publicly. Put it behind a VPN (Tailscale is
  the easiest) or restrict it by source IP.

---

## 4. The four adapters

| Adapter | What it does | Where it runs |
|---|---|---|
| `null` | Places nothing. The default, and correct for a Linux server. | anywhere |
| `demo` | Journals the order intent. No order placed. Useful for exercising the full pipeline on a server. | anywhere |
| `mt5` | Constructs the existing `MT5Gateway` and runs the engine. Requires a local terminal. | **Windows only** |
| `bridge` | Forwards the intent to a Windows machine over HTTPS. | server |

Select with `EXECUTION_ADAPTER`. An unrecognised value falls back to `null`, so
a typo can never escalate into something more powerful than intended.

### Why `null` is the default and not `demo`

`demo` sounds harmless, and it is — but it writes to the journal, and journal
rows are the audit trail that `/api/signals` displays. Defaulting to `null`
means a fresh deployment produces zero execution events until somebody
explicitly opts in. Fail silent, not fail chatty.

---

## 5. What each deployment can and cannot do

| | Linux server | Windows machine |
|---|---|---|
| Ingest bars from MT5 | ✗ | ✓ |
| Evaluate strategies on stored bars | ✓ | ✓ |
| Backtest / walk-forward / Monte Carlo | ✓ | ✓ |
| Serve the API and phone dashboard | ✓ | ✓ |
| Place a demo order | ✗ (adapter `null`) | ✓ |
| Read live account balance/equity | ✗ | ✓ |
| Hold the broker connection | ✗ | ✓ |

The server is not a degraded trading machine. It is the research, analysis, and
monitoring half — which is the half that benefits most from running 24/7 on
cheap hardware.

---

## 6. Verifying the split from the server

```bash
# The server knows it cannot trade, and says so.
curl -s localhost:8000/api/status | python3 -m json.tool
```

```json
{
  "mode": "DEMO",
  "live_trading_enabled": false,
  "execution_adapter": { "adapter": "null", "available": false }
}
```

That is a correctly configured server, not a misconfiguration.

---

## 7. If you later need the server to drive trades

Read this first: the evidence base does not support scaling up. V11 has 82
backtest trades over 10 years; V12 has 36 trades over 8 months. Those are
hypothesis-sized samples, and `signals_log` is empty, meaning no forward
evidence exists at all.

Building the bridge before there is forward data to collect would be optimising
the transport for a payload that does not yet exist. The correct order is:

1. Deploy the server with `EXECUTION_ADAPTER=null`.
2. Let the Windows runners run V11/V12 on the demo account.
3. Let `signals_log` accumulate real forward decisions for months.
4. Reassess with actual forward evidence, not backtest evidence.
5. Only then consider whether the server needs to drive anything.

**Live trading remains disabled in this build.** `live_trading_enabled = false`
is permanent, and `MT5Gateway.connect()` refuses any non-DEMO account
independently of any flag.
