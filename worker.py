"""Background worker — strategy evaluation on closed bars.

Runs headless on a Linux server with no broker connection. It does three
things on a tick:

  1. Rewrites run/worker.heartbeat so /health can detect it.
  2. Evaluates the approved strategies against the CLOSED bars in the
     database, and journals the decision (BUY / SELL / WAIT + entry/SL/TP).
  3. Hands any resulting order intent to the configured ExecutionAdapter.

CRITICAL DISTINCTION
    Step 2 reads bars from the local database, which on a server is updated
    by the Windows bridge (or by a manual ingest). It does NOT talk to MT5.
    So the signal this worker produces here is identical in logic to the
    Windows runner — monthly_bt/strategies.py already reuses those exact
    gate functions — but it is computed against whatever data the server
    currently holds. The journal therefore distinguishes:

        decision_evaluated  (server-side, from stored bars)
        order_placed        (only ever true on the Windows side, with a
                             gateway-verified DEMO account)

    A phone showing "BUY" from this worker means the strategy wants a trade.
    It does NOT mean a trade exists.

SAFETY
    - Cannot enable live trading. No such capability.
    - The adapter defaults to null, so a fresh deployment places nothing.
    - Pause flags in run/control/*.pause are honoured.
    - Every tick is logged; every decision is journaled.
"""
from __future__ import annotations

import json
import logging
import os
import signal
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from appconfig import get_config
from observability import setup_logging

log = logging.getLogger("worker")

ROOT = Path(__file__).resolve().parent
RUN_DIR = ROOT / "run"
CONTROL_DIR = RUN_DIR / "control"
HEARTBEAT = RUN_DIR / "worker.heartbeat"
JOURNAL = ROOT / "execution" / "journal.jsonl"

# How often to evaluate. V11 is a D1 strategy, so an hourly evaluation is
# ample and cheap; the D1 gate itself means most ticks are a no-op.
TICK_SECONDS = int(os.environ.get("WORKER_TICK_SECONDS", "3600"))


# ---------------------------------------------------------------------------
def beat(detail: dict | None = None) -> None:
    """Refresh the heartbeat file. /health reads its mtime."""
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "pid": os.getpid(),
        "detail": detail or {},
    }
    tmp = HEARTBEAT.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    os.replace(tmp, HEARTBEAT)          # atomic: /health never sees a partial file


def is_paused(name: str = "all") -> bool:
    return (CONTROL_DIR / f"{name}.pause").exists() or (CONTROL_DIR / "all.pause").exists()


def consume_flag(fname: str) -> bool:
    p = CONTROL_DIR / fname
    if p.exists():
        p.unlink(missing_ok=True)
        return True
    return False


def journal(event: dict) -> None:
    JOURNAL.parent.mkdir(parents=True, exist_ok=True)
    with JOURNAL.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, default=str) + "\n")


# ---------------------------------------------------------------------------
def approved_strategies() -> list[dict]:
    path = ROOT / "execution" / "approved.json"
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("approved", [])
    except (OSError, json.JSONDecodeError) as exc:
        log.error("approved.json unreadable: %s", exc)
        return []


def load_bars(cfg, timeframe: str, limit: int = 400):
    """Load the most recent bars for XAUUSD at `timeframe`, oldest->newest.

    Returns None when there is no data, which the caller reports as a
    skipped evaluation rather than a failure.
    """
    db = cfg.effective_db_path
    if not db.exists():
        return None
    try:
        with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
            return conn.execute(
                "SELECT ts_broker_epoch, open, high, low, close, spread "
                "FROM market_data WHERE symbol='XAUUSD' AND timeframe=? "
                "AND source='mt5' ORDER BY ts_broker_epoch DESC LIMIT ?",
                (timeframe, limit),
            ).fetchall()
    except sqlite3.Error as exc:
        log.error("bar query failed: %s", exc)
        return None


def evaluate(timeframe: str, strategy_module: str, version: str,
             strategy_name: str) -> dict | None:
    """Run the approved strategy's gate on the last CLOSED bar.

    Imports the SAME function the live runner uses, so the server-side
    decision cannot drift from what Windows would decide. Returns a dict
    describing the decision, or None if the module/data is unavailable.
    """
    import pandas as pd

    rows = load_bars(get_config(), timeframe)
    if not rows:
        return {"status": "SKIPPED", "reason": f"no {timeframe} bars stored",
                "strategy": strategy_name, "version": version}
    df = pd.DataFrame(rows, columns=["ts_broker_epoch", "open", "high",
                                     "low", "close", "spread"])
    df = df.iloc[::-1].reset_index(drop=True)
    df["ts"] = pd.to_datetime(df["ts_broker_epoch"], unit="s", utc=True)

    try:
        mod = __import__(strategy_module, fromlist=["signal_on_last_closed_bar"])
        sig_fn = getattr(mod, "signal_on_last_closed_bar", None)
        if sig_fn is None:
            return {"status": "SKIPPED",
                    "reason": f"{strategy_module} has no signal function",
                    "strategy": strategy_name, "version": version}
        sig = sig_fn(df)
    except Exception as exc:  # noqa: BLE001
        log.exception("evaluation failed for %s", strategy_name)
        return {"status": "ERROR", "reason": f"{type(exc).__name__}: {exc}",
                "strategy": strategy_name, "version": version}

    if sig is None:
        return {"status": "WAIT", "reason": "no signal on last closed bar",
                "strategy": strategy_name, "version": version, "signal": "WAIT"}

    # A signal exists. Report the plan WITHOUT acting on it: sizing needs
    # account equity, which only the Windows side has (a real DEMO account).
    return {
        "status": "SIGNAL",
        "signal": sig.get("signal", "BUY"),
        "strategy": strategy_name, "version": version,
        "timeframe": timeframe,
        "bar_ts": sig.get("bar_ts"),
        "entry_ref": sig.get("entry_ref"),
        "atr": sig.get("atr"),
        "body_pct": sig.get("body_pct"),
        "note": "signal computed server-side from stored bars; no order placed here",
    }


# Strategies the worker knows how to evaluate, mapped to their real modules.
KNOWN = {
    ("XAUUSD_D1_MOMENTUM_BREAKOUT", "V11"): ("D1", "execution.run_v11_daily"),
    ("XAUUSD_H1_MOMENTUM_BREAKOUT", "V12"): ("H1", "execution.run_v12_hourly"),
}


def tick() -> list[dict]:
    cfg = get_config()
    results: list[dict] = []
    for entry in approved_strategies():
        key = (entry.get("name"), entry.get("version"))
        label = f"{key[0]} {key[1]}"
        if is_paused(key[1] or "all") or is_paused(key[0] or "all"):
            results.append({"strategy": label, "status": "PAUSED"})
            continue
        if key not in KNOWN:
            results.append({"strategy": label, "status": "SKIPPED",
                            "reason": "no server-side evaluator for this strategy"})
            continue
        tf, module = KNOWN[key]
        r = evaluate(tf, module, key[1], key[0])
        r = r or {"strategy": label, "status": "SKIPPED", "reason": "no result"}
        r["adapter"] = cfg.execution_adapter
        r["mode"] = cfg.safe_mode_label
        results.append(r)
        journal({"ts": datetime.now(timezone.utc).isoformat(),
                 "event": "decision_evaluated", "source": "worker",
                 **r})
    beat({"adapter": cfg.execution_adapter, "evaluated": len(results)})
    return results


def main() -> int:
    setup_logging()
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    control = CONTROL_DIR / "restart.request"
    control.unlink(missing_ok=True)

    running = True

    def handle(signum, _frame):
        nonlocal running
        log.warning("received signal %s — shutting down", signum)
        running = False

    signal.signal(signal.SIGINT, handle)
    signal.signal(signal.SIGTERM, handle)

    cfg = get_config()
    log.info("worker starting: mode=%s adapter=%s tick=%ss",
             cfg.safe_mode_label, cfg.execution_adapter, TICK_SECONDS)
    # A heartbeat at startup means /health is accurate immediately, not only
    # after the first full tick.
    beat({"starting": True})

    if consume_flag("refresh.request"):
        log.info("refresh requested at startup")

    while running:
        try:
            for r in tick():
                log.info("tick result: %s", json.dumps(r, default=str))
        except Exception:  # noqa: BLE001
            # A tick that raises must not kill the worker: the heartbeat is
            # the thing /health depends on, and a dead worker is worse than
            # a failing evaluation.
            log.exception("tick failed; worker continuing")
            beat({"error": "tick failed"})

        if consume_flag("restart.request"):
            log.info("restart requested; reloading config and continuing")
            from appconfig import reset_config_cache
            reset_config_cache()
            cfg = get_config()

        for _ in range(TICK_SECONDS):
            if not running:
                break
            time.sleep(1)

    HEARTBEAT.unlink(missing_ok=True)
    log.info("worker stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
