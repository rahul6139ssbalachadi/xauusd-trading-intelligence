"""Deployment API router (Phases 4, 5, 6, 11).

Added to api/main.py via include_router. Kept in its own module so the
original auth/trading endpoints in api/main.py are untouched and the
existing 30-odd API tests keep their exact contract.

What is here:
  GET  /health                      unauthenticated liveness+readiness
  GET  /api/status                  mode, safety, adapter, forward data
  GET  /api/system                  same + versions + config problems
  GET  /api/strategies              on-disk strategy defs + approval state
  GET  /api/signals                 latest decisions from the journal
  GET  /api/trades                  (re-exported by main.py; this is the
                                    deployment-shaped variant)
  GET  /api/performance             summary from DB + monthly study reports
  GET  /api/backtests               monthly_bt runs
  GET  /api/worker                  worker liveness + last decisions
  GET  /api/logs                    recent log lines, tail of a file
  POST /api/control/{action}        SAFE controls only (see allowlist)
  WS   /ws                          live status push

SAFETY OF THE CONTROL ENDPOINT
    There is no endpoint anywhere in this file that can enable live
    trading, place an order, or modify a strategy. The action allowlist
    below is the whole surface: refresh, pause, resume, worker-restart.
    "Enable live trading" is deliberately NOT implemented, and
    LIVE_TRADING_ENABLED is not writable over the network at all — it is
    read from the environment at process start.

    Pause/resume are recorded in a control file that the WORKER reads. They
    cannot by themselves make the worker send an order: the ExecutionEngine
    gates (approved.json, kill switch, DEMO account verification) are
    untouched by this API.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field

from appconfig import get_config, check_production_readiness
from health_checks import collect_health

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[1]
ROUTER = APIRouter(prefix="", tags=["deployment"])

# Where the worker looks for pause flags. Created on demand.
CONTROL_DIR = ROOT / "run" / "control"

# The complete set of things this API is allowed to do.
# Adding to this list is a deliberate, reviewable act.
ALLOWED_ACTIONS = frozenset({
    "refresh-data",     # ask the worker to re-poll market data
    "pause-strategy",   # write a pause flag
    "resume-strategy",  # clear a pause flag
    "restart-worker",   # request a worker restart at next tick
})


# ---------------------------------------------------------------------------
# Auth dependency — imported lazily to avoid a circular import with main.py
# ---------------------------------------------------------------------------
def require_user():
    """Return main.get_current_user. Defined as a factory so main.py can
    inject its real dependency object at include time."""
    from api.main import get_current_user
    return get_current_user


def require_admin():
    from api.main import TokenData, get_current_user, HTTPException as HE
    return get_current_user, TokenData, HE


# ---------------------------------------------------------------------------
# /health  — unauthenticated, but reveals no secrets
# ---------------------------------------------------------------------------
@ROUTER.get("/health")
async def health() -> dict:
    """Liveness + readiness.

    Unauthenticated on purpose: a load balancer and a container healthcheck
    must both be able to call it. The payload contains no credentials, no
    paths beyond the DB file location, and no account numbers.
    """
    return collect_health()


# ---------------------------------------------------------------------------
# /api/status
# ---------------------------------------------------------------------------
@ROUTER.get("/api/status")
async def status() -> dict:
    cfg = get_config()
    health = collect_health(cfg)
    from execution.adapters import get_adapter
    adapter = get_adapter(cfg)
    return {
        "mode": cfg.safe_mode_label,
        "live_trading_enabled": cfg.live_trading_enabled,
        "live_trading_blocked": cfg.live_trading_enabled,
        "banner": ("LIVE TRADING DISABLED" if not cfg.live_trading_enabled
                   else "*** MISCONFIGURED: EXECUTION BLOCKED ***"),
        "execution_adapter": adapter.status(),
        "health": {k: v for k, v in health.items() if k != "checks"},
        "ts": health["ts"],
    }


# ---------------------------------------------------------------------------
# /api/system
# ---------------------------------------------------------------------------
@ROUTER.get("/api/system")
async def system() -> dict:
    cfg = get_config()
    problems = check_production_readiness(cfg)
    import platform
    import sys
    return {
        "config": cfg.as_public_dict(),
        "runtime": {
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "root": str(ROOT),
        },
        "production_readiness": {
            "ready": not problems,
            "problems": problems,
        },
        "health": collect_health(cfg),
    }


# ---------------------------------------------------------------------------
# /api/strategies
# ---------------------------------------------------------------------------
@ROUTER.get("/api/strategies")
async def strategies() -> dict:
    """Every strategy definition on disk, plus which are live-approved.

    Read-only. The rejected V1-V10 are reported too, on purpose: the
    project preserves failed experiments as evidence and a dashboard that
    hides them is misleading.
    """
    defs_dir = ROOT / "strategy" / "defs"
    approved_path = ROOT / "execution" / "approved.json"
    approved: dict[str, dict] = {}
    if approved_path.exists():
        try:
            data = json.loads(approved_path.read_text(encoding="utf-8"))
            for a in data.get("approved", []):
                approved[f"{a.get('name')} {a.get('version', '')}".strip()] = a
        except (OSError, json.JSONDecodeError) as exc:
            log.error("approved.json unreadable: %s", exc)

    out = []
    if defs_dir.exists():
        for p in sorted(defs_dir.glob("*.json")):
            item: dict[str, Any] = {"file": p.name, "name": p.stem}
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                item["error"] = f"unreadable: {exc}"
                out.append(item)
                continue
            for key in ("symbol", "market", "timeframes", "version", "status"):
                if key in d:
                    item[key] = d[key]
            item["live_approved"] = p.stem in approved
            if item["live_approved"]:
                item["approval"] = {
                    k: approved[p.stem].get(k)
                    for k in ("approved_on", "risk_pct", "max_spread_pips", "magic")
                }
            rr = d.get("research_results") or {}
            if isinstance(rr, dict) and rr:
                item["has_research_results"] = True
            out.append(item)

    return {
        "count": len(out),
        "live_approved": sorted(approved.keys()),
        "strategies": out,
    }


# ---------------------------------------------------------------------------
# /api/signals
# ---------------------------------------------------------------------------
def _journal_path() -> Path:
    return ROOT / "execution" / "journal.jsonl"


@ROUTER.get("/api/signals")
async def signals(limit: int = Query(50, ge=1, le=500)) -> dict:
    """Latest decisions, newest first.

    Reads execution/journal.jsonl, which every decision (pass OR block) is
    written to before any order is attempted. This is the decision trail,
    not a summary.
    """
    path = _journal_path()
    if not path.exists():
        return {
            "source": str(path.name), "present": False, "count": 0,
            "signals": [],
            "note": "No decision journal yet. The runners have not executed.",
        }
    rows: list[dict] = []
    try:
        # Read the tail rather than the whole 10MB+ file. The paper journal is
        # separate (journal/papertrade_journal.jsonl) and not read here.
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"cannot read journal: {exc}")
    total = len(rows)
    newest = rows[-limit:][::-1]
    return {
        "source": path.name, "present": True,
        "total_decisions": total, "count": len(newest),
        "signals": newest,
    }


# ---------------------------------------------------------------------------
# /api/trades
# ---------------------------------------------------------------------------
# NOTE: the authenticated, client-scoped GET /api/trades already lives in
# api/main.py and is the canonical one. It is deliberately NOT redefined
# here, and this system-wide view is NOT mounted at /api/trades/... either:
# api/main.py already owns the path parameter /api/trades/{trade_id}, so any
# sibling under that prefix would be shadowed by whichever route was
# registered first. /api/system/trades has no such collision.


@ROUTER.get("/api/system/trades")
async def all_trades(limit: int = Query(100, ge=1, le=1000),
                     status: str | None = Query(None, pattern="^(open|closed)$")) -> dict:
    cfg = get_config()
    db = cfg.effective_db_path
    if not db.exists():
        return {"present": False, "count": 0, "trades": [],
                "note": "No database. See docs/SERVER_DEPLOYMENT.md step 5."}
    try:
        with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
            conn.row_factory = sqlite3.Row
            names = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            if "trades" not in names:
                return {"present": False, "count": 0, "trades": [],
                        "note": "trades table not present in the database."}
            q = "SELECT * FROM trades"
            params: list = []
            if status:
                q += " WHERE status = ?"
                params.append(status)
            q += " ORDER BY COALESCE(entry_time, created_at) DESC LIMIT ?"
            params.append(limit)
            rows = [dict(r) for r in conn.execute(q, params)]
            closed = conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(pnl_usd),0), COALESCE(SUM(pnl_pips),0) "
                "FROM trades WHERE status='closed'").fetchone()
    except sqlite3.Error as exc:
        raise HTTPException(status_code=500, detail=f"database error: {exc}")
    return {
        "present": True, "count": len(rows), "trades": rows,
        "closed_summary": {
            "trades": closed[0], "net_pnl_usd": closed[1], "net_pnl_pips": closed[2],
        },
        "caveat": (
            "Rows in this table are a MIX of simulated and demo fills. "
            "Rows with pnl_usd set at creation time are seeded sample data, "
            "not broker-confirmed executions. Verify against the MT5 "
            "terminal before treating any figure as real."
        ),
    }


# ---------------------------------------------------------------------------
# /api/performance
# ---------------------------------------------------------------------------
@ROUTER.get("/api/performance")
async def performance() -> dict:
    """Summary metrics, with the provenance of each number made explicit."""
    cfg = get_config()
    db = cfg.effective_db_path
    out: dict[str, Any] = {"present": False, "ts": datetime.now(timezone.utc).isoformat()}

    if db.exists():
        try:
            with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
                conn.row_factory = sqlite3.Row
                names = {r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")}
                if "performance_summary" in names:
                    out["summary"] = [dict(r) for r in conn.execute(
                        "SELECT * FROM performance_summary")]
                if "equity_curve" in names:
                    # The shipped db/trading.db was created with an `ts`
                    # column, while db/schema/trading_app.sql (and the test
                    # fixture) use `date`. Both shapes exist in the wild, so
                    # probe for the column rather than assuming either.
                    cols = {r[1] for r in conn.execute("PRAGMA table_info(equity_curve)")}
                    date_col = "date" if "date" in cols else ("ts" if "ts" in cols else None)
                    if date_col:
                        eq = conn.execute(
                            f"SELECT COUNT(*) c, MIN({date_col}) a, MAX({date_col}) b "
                            f"FROM equity_curve").fetchone()
                        out["equity_points"] = eq["c"]
                        out["equity_range"] = [eq["a"], eq["b"]]
                        out["equity_date_column"] = date_col
                    else:
                        out["equity_points"] = None
                        out["equity_note"] = "equity_curve has no date/ts column"
        except sqlite3.Error as exc:
            out["error"] = f"database error: {exc}"

    # The monthly studies are the real performance evidence in this project.
    reports = ROOT / "reports" / "monthly"
    studies = []
    if reports.exists():
        for md in sorted(reports.glob("*.md")):
            studies.append({"file": md.name, "kind": "monthly_backtest"})
    out["studies"] = studies
    out["provenance"] = (
        "Backtest and paper figures are SIMULATED or IN-SAMPLE. "
        "forward_signals_recorded is the count of live decisions; while it is "
        "0, nothing here is evidence of live profitability."
    )
    try:
        from health_checks import check_forward_data
        fc = check_forward_data(cfg)
        out["forward_signals_recorded"] = fc.data.get("signals", 0)
        out["forward_data_state"] = fc.state
    except Exception:  # noqa: BLE001
        out["forward_signals_recorded"] = None
    return out


# ---------------------------------------------------------------------------
# /api/backtests
# ---------------------------------------------------------------------------
@ROUTER.get("/api/backtests")
async def backtests(limit: int = Query(25, ge=1, le=200)) -> dict:
    """Recent monthly_bt runs, plus on-disk report files."""
    out: dict[str, Any] = {"present": False, "runs": [], "reports": []}

    bt_db = get_config().backtests_db_path
    if bt_db.exists():
        try:
            with sqlite3.connect(f"file:{bt_db}?mode=ro", uri=True) as conn:
                conn.row_factory = sqlite3.Row
                names = {r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")}
                if "backtest_runs" in names:
                    out["present"] = True
                    out["runs"] = [dict(r) for r in conn.execute(
                        "SELECT * FROM backtest_runs ORDER BY run_ts_utc DESC LIMIT ?",
                        (limit,))]
        except sqlite3.Error as exc:
            out["error"] = f"backtest database error: {exc}"
    else:
        out["note"] = (f"{bt_db} not present. Run: python -m monthly_bt ... "
                       f"(it is git-ignored like the other databases).")

    reports = ROOT / "reports" / "monthly"
    if reports.exists():
        out["reports"] = [
            {"file": p.name, "bytes": p.stat().st_size}
            for p in sorted(reports.glob("*.md"))
        ]
    out["visual_reports"] = [
        p.name for p in sorted((reports / "visual").glob("*.html"))
    ] if (reports / "visual").exists() else []
    return out


# ---------------------------------------------------------------------------
# /api/worker
# ---------------------------------------------------------------------------
@ROUTER.get("/api/worker")
async def worker_status() -> dict:
    from health_checks import check_worker
    cfg = get_config()
    wc = check_worker(cfg)
    return {
        "state": wc.state, "detail": wc.detail, **wc.data,
        "control_dir": str(CONTROL_DIR),
        "pause_flags": sorted(p.name for p in CONTROL_DIR.glob("*.pause"))
        if CONTROL_DIR.exists() else [],
        "note": (
            "The worker is the scheduled process that evaluates strategies on "
            "closed bars. Its absence means no new signals are being produced. "
            "Under Docker: docker compose -f docker-compose.prod.yml ps worker"
        ),
    }


# ---------------------------------------------------------------------------
# /api/logs
# ---------------------------------------------------------------------------
@ROUTER.get("/api/logs")
async def logs(lines: int = Query(100, ge=1, le=2000),
               file: str = Query("journal", pattern="^(journal|worker|api)$")) -> dict:
    """Tail a log file. Read-only, path-constrained to three known names.

    The `file` parameter is validated against an allowlist and is never used
    to build an arbitrary path, so this cannot be turned into a file-read
    primitive.
    """
    mapping = {
        "journal": _journal_path(),
        "worker": ROOT / "run" / "worker.log",
        "api": Path(get_config().log_file) if get_config().log_file else None,
    }
    path = mapping[file]
    if path is None or not path.exists():
        return {"present": False, "file": file,
                "note": f"No {file} log at the configured location.",
                "hint": "Set LOG_FILE to enable file logging (stdout is always used).",
                "lines": []}
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            tail = f.readlines()[-lines:]
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"cannot read log: {exc}")
    return {"present": True, "file": file, "path": str(path),
            "count": len(tail), "lines": [ln.rstrip("\n") for ln in tail]}


# ---------------------------------------------------------------------------
# /api/errors  — recent CRITICAL/ERROR lines, for the phone "recent errors" view
# ---------------------------------------------------------------------------
@ROUTER.get("/api/errors")
async def errors(lines: int = Query(50, ge=1, le=500)) -> dict:
    cfg = get_config()
    if not cfg.log_file:
        return {"present": False, "errors": [],
                "note": "LOG_FILE is unset. Errors go to stdout only "
                        "(docker compose logs worker)."}
    path = Path(cfg.log_file)
    if not path.exists():
        return {"present": False, "errors": []}
    found: list[str] = []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if '"ERROR"' in line or '"CRITICAL"' in line or " ERROR " in line:
                    found.append(line.rstrip("\n"))
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"cannot read log: {exc}")
    return {"present": True, "count": len(found),
            "errors": found[-lines:]}


# ---------------------------------------------------------------------------
# Control endpoints — the ENTIRE mutation surface
# ---------------------------------------------------------------------------
class ControlRequest(BaseModel):
    action: str = Field(..., description="One of: " + ", ".join(sorted(ALLOWED_ACTIONS)))
    target: str | None = Field(None, max_length=64,
                               description="Optional strategy name, e.g. V11")
    reason: str | None = Field(None, max_length=280)


def _safe_target(target: str | None) -> str:
    """Constrain a target to a bare filename stem. No separators, no dots."""
    if not target:
        return "all"
    cleaned = "".join(ch for ch in target if ch.isalnum() or ch in "_-")
    if not cleaned:
        raise HTTPException(status_code=400, detail="invalid target")
    return cleaned[:64]


@ROUTER.post("/api/control/{action}")
async def control(action: str, req: ControlRequest) -> dict:
    """Safe administrative controls.

    NOTE WHAT IS ABSENT: there is no action here that can enable live
    trading, place an order, or modify a strategy definition. Those are not
    reachable over HTTP by design, not by omission.
    """
    if action not in ALLOWED_ACTIONS:
        raise HTTPException(
            status_code=400,
            detail=f"action '{action}' is not permitted. Allowed: "
                   f"{sorted(ALLOWED_ACTIONS)}. Live-trading controls do not "
                   f"exist in this API.",
        )
    target = _safe_target(req.target)
    CONTROL_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).isoformat()
    record = {"ts": stamp, "action": action, "target": target,
              "reason": req.reason, "requested_via": "api"}
    out: dict[str, Any] = {"action": action, "target": target, "ts": stamp}

    if action == "pause-strategy":
        p = CONTROL_DIR / f"{target}.pause"
        p.write_text(json.dumps(record), encoding="utf-8")
        out["effect"] = f"worker will skip {target} at its next tick"
        out["file"] = str(p)
    elif action == "resume-strategy":
        p = CONTROL_DIR / f"{target}.pause"
        existed = p.exists()
        p.unlink(missing_ok=True)
        out["effect"] = (f"pause flag for {target} removed" if existed
                         else f"{target} was not paused — no change")
    elif action == "restart-worker":
        p = CONTROL_DIR / "restart.request"
        p.write_text(json.dumps(record), encoding="utf-8")
        out["effect"] = "worker will restart at its next tick"
        out["file"] = str(p)
    elif action == "refresh-data":
        p = CONTROL_DIR / "refresh.request"
        p.write_text(json.dumps(record), encoding="utf-8")
        out["effect"] = "worker will re-poll market data at its next tick"
        out["file"] = str(p)

    # Audit every control action.
    audit = ROOT / "run" / "control_audit.jsonl"
    try:
        with audit.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
    except OSError as exc:
        log.error("could not write control audit: %s", exc)
    log.info("control action=%s target=%s", action, target)
    return out


@ROUTER.get("/api/control/audit")
async def control_audit(lines: int = Query(50, ge=1, le=500)) -> dict:
    audit = ROOT / "run" / "control_audit.jsonl"
    if not audit.exists():
        return {"present": False, "entries": []}
    try:
        with audit.open("r", encoding="utf-8", errors="replace") as f:
            rows = [json.loads(ln) for ln in f if ln.strip()]
    except (OSError, json.JSONDecodeError) as exc:
        return {"present": False, "entries": [], "error": str(exc)}
    return {"present": True, "count": len(rows), "entries": rows[-lines:]}


# ---------------------------------------------------------------------------
# WebSocket /ws — live status push
# ---------------------------------------------------------------------------
def _status_snapshot() -> dict:
    cfg = get_config()
    h = collect_health(cfg)
    return {
        "type": "status",
        "ts": h["ts"],
        "status": h["status"],
        "mode": cfg.safe_mode_label,
        "live_trading_enabled": cfg.live_trading_enabled,
        "database": h["database"],
        "worker": h["worker"],
        "market_data": h["market_data"],
        "execution": h["execution"],
    }


@ROUTER.websocket("/ws")
async def ws_status(websocket: WebSocket) -> None:
    """Push a status snapshot every 15s.

    Deliberately unauthenticated but content-free beyond what /health
    already exposes publicly. A phone dashboard polls this instead of
    hammering the REST endpoints, and gets the DEMO/LIVE banner pushed
    without a page reload.
    """
    await websocket.accept()
    try:
        while True:
            await websocket.send_json(_status_snapshot())
            await asyncio.sleep(15)
    except WebSocketDisconnect:
        return
    except Exception as exc:  # noqa: BLE001
        log.debug("websocket closed: %s", exc)
        return


__all__ = ["ALLOWED_ACTIONS", "CONTROL_DIR", "ROUTER"]
