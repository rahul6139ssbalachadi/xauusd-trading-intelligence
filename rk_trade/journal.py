"""Per-user decision journal for RK Trade.

Append-only JSONL journal that records every signal decision per user.
This is the audit trail required by CLAUDE.md §22 — records signal,
timestamp, entry/stop/target/lots/risk, reasons, and result.

SIMULATION-ONLY: No execution path, no MT5 trade calls. The journal
records decisions for review; the user executes manually.

Uses atomic file locking (rk_trade.utils) to prevent corruption when
multiple processes append concurrently.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from rk_trade.signal_engine import SignalDecision
from rk_trade.utils import atomic_append_jsonl


class DecisionJournal:
    """Append-only journal for a single user's signal decisions."""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, decision: SignalDecision) -> None:
        """Record a signal decision atomically."""
        row = decision.to_dict()
        row["recorded_at"] = datetime.now(timezone.utc).isoformat()
        atomic_append_jsonl(self.path, row)

    def read(self, limit: int | None = None) -> list[dict]:
        """Read journal entries (most recent last)."""
        if not self.path.exists():
            return []
        lines = self.path.read_text(encoding="utf-8").splitlines()
        if limit:
            lines = lines[-limit:]
        return [json.loads(line) for line in lines if line.strip()]

    def summarize(self, limit: int = 100) -> dict:
        """Summarize the last `limit` decisions."""
        entries = self.read(limit)
        if not entries:
            return {"total": 0}
        signals = [e.get("signal", "WAIT") for e in entries]
        buys = signals.count("BUY")
        sells = signals.count("SELL")
        waits = signals.count("WAIT")
        confs = [e.get("confidence", 0) for e in entries
                 if e.get("signal") in ("BUY", "SELL")]
        return {
            "total": len(entries),
            "buys": buys,
            "sells": sells,
            "waits": waits,
            "avg_confidence": round(sum(confs) / len(confs), 1) if confs else 0.0,
            "planned_risk_usd": round(
                sum(e.get("risk_usd", 0) for e in entries
                    if e.get("signal") in ("BUY", "SELL")), 2),
        }
