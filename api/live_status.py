"""Live status for the dashboard: reader health + latest signals.

READ-ONLY. This module reads two files and a SQLite table. It never
connects to MT5 and never places an order, so it is safe to expose on an
unauthenticated status endpoint for a phone on the local network.

WHAT IT SHOWS
    - Is the 24/7 read service alive, and how stale is its data? This is
      the single most important field: the previous deployment served
      month-old bars as if they were live.
    - The most recent signal per approved strategy, with the full payload
      (strategy + number, symbol, direction, entry, SL, TP, timestamp,
      confidence, reason).
    - Open positions as last seen by the reader.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HEALTH = ROOT / "run" / "mt5reader_health.json"
JOURNAL = ROOT / "journal" / "signals.jsonl"
APPROVED = ROOT / "execution" / "approved.json"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def read_health() -> dict:
    """Reader health, or a clear 'never ran' if the service is not up."""
    if not HEALTH.exists():
        return {
            "running": False,
            "stale": True,
            "connected": False,
            "last_ok": None,
            "stale_seconds": None,
            "detail": "read service has never run — no live MT5 data",
        }
    try:
        d = json.loads(HEALTH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        return {"running": False, "stale": True, "connected": False,
                "last_ok": None, "stale_seconds": None,
                "detail": f"health file unreadable: {e}"}

    try:
        last = datetime.fromisoformat(d.get("last_ok", ""))
        age = (_now() - last).total_seconds()
    except (ValueError, TypeError):
        age = None

    d["running"] = True
    d["stale_seconds"] = None if age is None else round(age, 1)
    d["stale"] = age is None or age > 300          # 5 minutes
    d["detail"] = ("data is stale — the read service has not completed a "
                   "cycle recently" if d["stale"] else "live")
    return d


def read_latest_signals(limit: int = 40) -> list[dict]:
    """Most recent signals, newest first, de-duplicated per strategy.

    The journal is append-only JSONL, one Signal dict per line. We keep
    the LAST occurrence of each (strategy, direction-is-a-trade) pair so
    the dashboard shows the current state, not every historical tick.
    """
    if not JOURNAL.exists():
        return []
    latest: dict[str, dict] = {}
    try:
        with JOURNAL.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue          # a torn final line must not 500 the API
                key = f"{d.get('strategy','')}:{d.get('strategy_number','')}"
                latest[key] = d
    except OSError:
        return []

    out = sorted(latest.values(),
                  key=lambda d: d.get("timestamp", ""), reverse=True)
    return out[:limit]


def read_open_positions() -> list[dict]:
    """Positions from the reader's health file, if it recorded any.

    The reader does not persist positions per cycle (that would be a
    write-heavy table for a rarely-changing fact), so we surface the count
    and let the reader's own /status be the detail view. Kept here so the
    dashboard has one shape to render.
    """
    h = read_health()
    n = h.get("positions")
    return [{"open_positions": n, "as_of": h.get("last_ok")}] if n is not None else []


def read_approved() -> list[dict]:
    if not APPROVED.exists():
        return []
    try:
        return json.loads(APPROVED.read_text(encoding="utf-8")).get("approved", [])
    except (json.JSONDecodeError, OSError):
        return []


def status() -> dict:
    """One payload the dashboard renders in a single request."""
    return {
        "generated_at": _now().isoformat(),
        "reader": read_health(),
        "approved_strategies": read_approved(),
        "signals": read_latest_signals(),
        "positions": read_open_positions(),
        # Loud, explicit banner so the UI never implies trading is live.
        "execution": {
            "mode": "demo",
            "live_money": False,
            "note": ("Demo only. No real-money order can be sent by this "
                     "build."),
        },
    }
