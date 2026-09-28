"""Health checks (Phase 11).

Every check returns a (state, detail) pair where state is one of:

    ok        working as intended
    degraded  working, but something a human should look at
    down      not working

Design rule: a health endpoint that returns "ok" for an empty database is
worse than no health endpoint at all, because it converts a visible problem
into an invisible one. So an unreachable-but-present database is "down", and
an EMPTY market-data table is "degraded" with an explanation, not "ok".

No check here can place an order, and no check exposes a secret.
"""
from __future__ import annotations

import logging
import os
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from appconfig import AppConfig, get_config, check_production_readiness

log = logging.getLogger(__name__)

OK, DEGRADED, DOWN = "ok", "degraded", "down"

# A component older than this is assumed stopped. Workers tick far more
# often than this, so a gap means a real outage rather than a quiet moment.
WORKER_STALE_AFTER_SECONDS = 900          # 15 min


@dataclass
class CheckResult:
    name: str
    state: str
    detail: str = ""
    data: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"state": self.state, "detail": self.detail, **({"data": self.data} if self.data else {})}


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------
def check_database(cfg: AppConfig) -> CheckResult:
    db = cfg.effective_db_path
    if not db.exists():
        return CheckResult(
            "database", DOWN,
            f"database file not found at {db}. It is git-ignored by design; "
            f"run scripts/ingest_extended_gold.py or copy db/trading.db in. "
            f"See docs/SERVER_DEPLOYMENT.md.",
        )
    try:
        with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
            names = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            if "market_data" not in names:
                return CheckResult(
                    "database", DOWN,
                    f"{db} exists but has no market_data table. "
                    f"Apply db/schema/*.sql and re-ingest.",
                )
            rows = conn.execute("SELECT COUNT(*) FROM market_data").fetchone()[0]
    except sqlite3.Error as exc:
        return CheckResult("database", DOWN, f"sqlite error: {exc}")
    except OSError as exc:
        return CheckResult("database", DOWN, f"cannot read {db}: {exc}")

    if rows == 0:
        return CheckResult(
            "database", DEGRADED,
            f"connected, but market_data is EMPTY. A fresh clone has no bars "
            f"because db/trading.db is git-ignored. Ingest before trusting "
            f"any analysis.",
            {"rows": 0, "path": str(db)},
        )
    return CheckResult("database", OK, f"{rows:,} bars", {"rows": rows, "path": str(db)})


def check_market_data_freshness(cfg: AppConfig) -> CheckResult:
    """How old is the newest bar? This is the 'stale market data' detector."""
    db = cfg.effective_db_path
    if not db.exists():
        return CheckResult("market_data", DOWN, "no database; see the database check")
    try:
        with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
            names = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            if "market_data" not in names:
                return CheckResult("market_data", DEGRADED, "market_data table absent")
            row = conn.execute(
                "SELECT MAX(ts_broker_epoch) FROM market_data WHERE timeframe='D1'"
            ).fetchone()
    except sqlite3.Error as exc:
        return CheckResult("market_data", DOWN, f"sqlite error: {exc}")
    if not row or row[0] is None:
        return CheckResult(
            "market_data", DEGRADED,
            "no D1 bars stored. The daily strategies (V11) cannot evaluate.",
        )
    newest = int(row[0])
    age_h = (time.time() - newest) / 3600.0
    # Broker epochs are UTC+3, so a 3h tolerance absorbs the offset plus a
    # day of not running. Anything beyond that is genuinely stale.
    if age_h <= 24 + 3:
        state, detail = OK, f"newest D1 bar {age_h:.1f}h old"
    elif age_h <= cfg.health_stale_after_hours:
        state, detail = DEGRADED, f"newest D1 bar is {age_h:.1f}h old (STALE)"
    else:
        state, detail = DOWN, f"newest D1 bar is {age_h:.1f}h old — data feed looks dead"
    return CheckResult("market_data", state, detail,
                       {"newest_epoch": newest, "age_hours": round(age_h, 2)})


def check_worker(cfg: AppConfig) -> CheckResult:
    """Worker liveness via a heartbeat file the worker rewrites each tick."""
    hb = Path(os.environ.get("WORKER_HEARTBEAT_FILE", "")) if os.environ.get(
        "WORKER_HEARTBEAT_FILE") else cfg.trading_db_path.parent.parent / "run" / "worker.heartbeat"
    if not hb.exists():
        return CheckResult(
            "worker", DOWN,
            f"no heartbeat at {hb}. Either the worker has never run, or it is "
            f"stopped. The API and dashboard still serve; no strategy is "
            f"evaluating on a timer.",
        )
    try:
        age = time.time() - hb.stat().st_mtime
    except OSError as exc:
        return CheckResult("worker", DOWN, f"cannot stat heartbeat: {exc}")
    if age > WORKER_STALE_AFTER_SECONDS:
        return CheckResult("worker", DOWN,
                           f"heartbeat is {age:.0f}s old — worker stopped",
                           {"age_seconds": round(age, 1)})
    return CheckResult("worker", OK, f"heartbeat {age:.0f}s old",
                       {"age_seconds": round(age, 1)})


def check_strategy_approved(cfg: AppConfig | None = None) -> CheckResult:
    """Which strategies are approved, and is the data there?"""
    path = Path(__file__).resolve().parent / "execution" / "approved.json"
    if not path.exists():
        return CheckResult("strategies", DOWN, f"{path} missing")
    import json
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return CheckResult("strategies", DOWN, f"approved.json unreadable: {exc}")
    approved = data.get("approved", [])
    if not approved:
        return CheckResult("strategies", DEGRADED, "no strategies are approved")
    names = [f"{a.get('name', '?')} {a.get('version', '')}".strip() for a in approved]
    return CheckResult("strategies", OK, f"{len(approved)} approved",
                       {"approved": names})


def check_execution_adapter(cfg: AppConfig) -> CheckResult:
    from execution.adapters import get_adapter
    adapter = get_adapter(cfg)
    info = adapter.status()
    if adapter.name == "null":
        return CheckResult(
            "execution", DEGRADED,
            "EXECUTION_ADAPTER=null — signals are computed and displayed but "
            "no orders can be placed. This is the correct setting for a "
            "Linux server (see docs/MT5_ARCHITECTURE.md).",
            info,
        )
    if not adapter.available():
        return CheckResult("execution", DEGRADED,
                           f"adapter '{adapter.name}' is configured but not available",
                           info)
    return CheckResult("execution", OK, f"adapter '{adapter.name}' available", info)


def check_forward_data(cfg: AppConfig) -> CheckResult:
    """Is there ANY forward (non-backtest) evidence recorded?

    Reported because it is the single most misleading thing a deployed
    dashboard can imply. A system with zero recorded signals is not
    "profitable so far", it is "unmeasured".
    """
    db = cfg.effective_db_path
    if not db.exists():
        return CheckResult("forward_data", DEGRADED, "no database yet")
    try:
        with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
            names = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            if "signals_log" not in names:
                return CheckResult(
                    "forward_data", DEGRADED,
                    "signals_log table absent. Nothing has recorded a live "
                    "decision; all performance shown is in-sample or simulated.",
                )
            n = conn.execute("SELECT COUNT(*) FROM signals_log").fetchone()[0]
    except sqlite3.Error as exc:
        return CheckResult("forward_data", DEGRADED, f"sqlite error: {exc}")
    if n == 0:
        return CheckResult(
            "forward_data", DEGRADED,
            "signals_log is EMPTY. No live/forward decision has ever been "
            "recorded. Every performance number in this system is "
            "in-sample or simulated.",
            {"signals": 0},
        )
    return CheckResult("forward_data", OK, f"{n} forward signals recorded", {"signals": n})


def check_config(cfg: AppConfig) -> CheckResult:
    problems = check_production_readiness(cfg)
    if problems:
        return CheckResult("config", DEGRADED,
                           f"{len(problems)} production-readiness problem(s)",
                           {"problems": problems})
    return CheckResult("config", OK, "configuration is production-safe")


def check_safety_switches(cfg: AppConfig) -> CheckResult:
    """The headline safety state, surfaced so a phone glance confirms it."""
    data = {
        "trading_mode": cfg.safe_mode_label,
        "demo_execution_enabled": cfg.demo_execution_enabled,
        "live_trading_enabled": cfg.live_trading_enabled,
    }
    if cfg.live_trading_enabled:
        return CheckResult("safety", DOWN,
                           "LIVE_TRADING_ENABLED is true, which BLOCKS "
                           "execution. This is a misconfiguration.", data)
    if cfg.is_live_mode:
        return CheckResult("safety", DOWN, "TRADING_MODE=live is not supported "
                           "by this build.", data)
    return CheckResult("safety", OK, "DEMO mode, live trading blocked", data)


# ---------------------------------------------------------------------------
# Aggregate
# ---------------------------------------------------------------------------
ALL_CHECKS: tuple[Callable[[AppConfig], CheckResult], ...] = (
    check_safety_switches,
    check_config,
    check_database,
    check_market_data_freshness,
    check_worker,
    check_execution_adapter,
    check_strategy_approved,
    check_forward_data,
)


def collect_health(cfg: AppConfig | None = None,
                   checks: tuple | None = None) -> dict:
    """Run every check and build the /health payload.

    Never raises: a health endpoint that 500s tells an operator nothing.
    An unexpected exception inside a check becomes a `down` result.
    """
    cfg = cfg or get_config()
    results: list[CheckResult] = []
    for fn in (checks or ALL_CHECKS):
        name = fn.__name__.removeprefix("check_")
        try:
            results.append(fn(cfg))
        except Exception as exc:  # noqa: BLE001
            log.exception("health check %s raised", name)
            results.append(CheckResult(
                name, DOWN, f"check raised {type(exc).__name__}: {exc}"))

    states = [r.state for r in results]
    overall = DOWN if DOWN in states else (DEGRADED if DEGRADED in states else OK)
    return {
        "status": "healthy" if overall == OK else overall,
        "mode": cfg.safe_mode_label,
        "live_trading_enabled": cfg.live_trading_enabled,
        "database": _state_of(results, "database"),
        "worker": _state_of(results, "worker"),
        "market_data": _state_of(results, "market_data"),
        "execution": _state_of(results, "execution"),
        "forward_data": _state_of(results, "forward_data"),
        "ts": datetime.now(timezone.utc).isoformat(),
        "checks": {r.name: r.to_dict() for r in results},
    }


def _state_of(results: list[CheckResult], name: str) -> str:
    for r in results:
        if r.name == name:
            return r.state
    return "unknown"


__all__ = ["ALL_CHECKS", "CheckResult", "DEGRADED", "DOWN", "OK",
           "collect_health"]
