"""Canonical signal payload (§32 structured agent output).

Every signal the system produces — whether it came from the D1 runner,
the H1 runner, the worker, or a research re-simulation — is expressed as
a `Signal`. One dataclass, one schema, so the journal, the CSV that the
MT5 chart indicator draws, the API, and the dashboard can never disagree
about what a signal was.

WHY A SINGLE DATACLASS
    The project previously carried three near-identical shapes: a dict in
    execution/scheduled_live.py, a JournalEntry in paper/__init__.py, and
    a row in mql5/signals/*.csv. Each grew its own field list, and they
    drifted: the CSV had no confidence, the journal entry had no strategy
    number. Adding "confidence" to one and forgetting the others is
    exactly the kind of silent gap this module removes.

FIELDS (the user's required set, §32)
    strategy      e.g. "XAUUSD_D1_MOMENTUM_BREAKOUT"
    strategy_number  e.g. "V11"  — the version/number a human quotes
    strategy_id   e.g. "EXP-2026-001" style experiment id, may be ""
    symbol        canonical, e.g. "XAUUSD"
    broker_symbol broker's own name, e.g. "GOLD.i#"
    direction     "BUY" | "SELL" | "WAIT" | "BLOCKED"
    entry, stop, target   prices; NaN when not applicable (e.g. WAIT)
    timestamp     ISO-8601 UTC, when the DECISION was made
    bar_timestamp ISO-8601 UTC of the bar the decision is based on
    timeframe     "D1", "H1", ...
    confidence    0.0-1.0 — how strongly the evidence supports the call
    reason        human-readable justification
    reasons       the individual evidence items behind it
    invalidation  what would prove the trade wrong
    risk_pct, lots, risk_usd  sizing, so money is auditable
    magic         broker magic number, so fills can be matched back
    simulated     True for research/paper, False for a broker order

HONESTY RULES (non-negotiable)
    - A WAIT or BLOCKED signal MUST NOT carry entry/stop/target prices.
      Setting them would let a consumer treat a refusal as a trade.
    - confidence is NOT computed here. It is supplied by the caller,
      which is the component that actually holds the evidence. This
      module never invents a number to fill the field.
    - Nothing in this module talks to MT5, the database, or the network.
"""
from __future__ import annotations

import csv
import json
import math
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

# Column order for the CSV the MQL5 chart indicator reads. Keep this in
# sync with mql5/MonthlyBT_Signals.mq5 — the EA selects rows by
# `timeframe` and reads epoch/entry/sl/tp/simulated/exit_* columns.
CSV_COLUMNS = [
    "epoch",             # bar time, epoch seconds — the EA's join key
    "strategy",
    "strategy_number",
    "timeframe",
    "signal",            # BUY / SELL / WAIT / BLOCKED
    "direction",         # alias of signal, kept for readability
    "entry",
    "sl",
    "tp",
    "lots",
    "risk_usd",
    "risk_pct",
    "confidence",
    "reason",
    "simulated",         # 1 = research/paper, 0 = broker order
    "symbol",
    "broker_symbol",
    "magic",
    "decision_ts",       # ISO-8601 UTC
    "bar_ts",            # ISO-8601 UTC
    "invalidation",
    "invalidation_price",
]


def _iso(ts: float | int | None) -> str:
    if ts is None:
        return ""
    return datetime.fromtimestamp(float(ts), tz=timezone.utc).isoformat()


def _num(x) -> float:
    """NaN-safe float: blank/None become NaN so they serialise as ''."""
    if x is None or x == "":
        return float("nan")
    return float(x)


@dataclass
class Signal:
    # --- identity ---------------------------------------------------------
    strategy: str = ""
    strategy_number: str = ""          # "V11", "V12", ...
    strategy_id: str = ""              # experiment/registry id, optional
    symbol: str = "XAUUSD"
    broker_symbol: str = ""
    timeframe: str = ""

    # --- the call ---------------------------------------------------------
    direction: str = "WAIT"            # BUY | SELL | WAIT | BLOCKED
    entry: float = float("nan")
    stop: float = float("nan")
    target: float = float("nan")
    lots: float = 0.0
    risk_pct: float = 0.0
    risk_usd: float = 0.0
    magic: int = 0

    # --- justification ----------------------------------------------------
    confidence: float = 0.0            # 0.0-1.0, supplied by the caller
    reason: str = ""
    reasons: list[str] = field(default_factory=list)
    invalidation: str = ""
    invalidation_price: float = float("nan")

    # --- timing / provenance ---------------------------------------------
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat())
    bar_timestamp: str = ""
    simulated: bool = True

    # ------------------------------------------------------------------
    def __post_init__(self):
        self.direction = (self.direction or "WAIT").upper()
        if self.direction not in ("BUY", "SELL", "WAIT", "BLOCKED"):
            raise ValueError(
                f"direction must be BUY/SELL/WAIT/BLOCKED, got {self.direction!r}")

        # A refusal must never look like a trade. If a caller hands us
        # prices with a WAIT/BLOCKED, drop them rather than propagate a
        # payload a consumer could act on.
        if self.direction in ("WAIT", "BLOCKED"):
            self.entry = float("nan")
            self.stop = float("nan")
            self.target = float("nan")
            self.lots = 0.0
            self.risk_usd = 0.0
            if not self.reason:
                self.reason = "no qualifying evidence — WAIT is a valid decision"

        if not (0.0 <= float(self.confidence) <= 1.0):
            raise ValueError(
                f"confidence must be 0.0-1.0, got {self.confidence!r}")

        if not self.reason:
            self.reason = self.reasons[0] if self.reasons else "unspecified"

    # ------------------------------------------------------------------
    @property
    def is_trade(self) -> bool:
        """True only when this payload describes an actionable order."""
        return self.direction in ("BUY", "SELL")

    @property
    def risk_reward(self) -> float:
        if not self.is_trade or self.direction == "BUY":
            pass
        e, s, t = self.entry, self.stop, self.target
        if not all(math.isfinite(v) for v in (e, s, t)):
            return float("nan")
        risk = abs(e - s)
        reward = (t - e) if self.direction == "BUY" else (e - t)
        if risk <= 0:
            return float("nan")
        return reward / risk

    # ------------------------------------------------------------------
    def to_dict(self) -> dict:
        d = asdict(self)
        d["risk_reward"] = self.risk_reward
        d["is_trade"] = self.is_trade
        return _json_safe(d)

    def to_json(self, **kw) -> str:
        # allow_nan=False is deliberate. Bare NaN is accepted by Python's
        # json module but is NOT valid JSON — JSON.parse in a browser, and
        # most other consumers, throw on it. A refused signal would
        # therefore break the dashboard. NaN becomes null instead.
        return json.dumps(self.to_dict(), default=str, allow_nan=False, **kw)

    def to_csv_row(self, epoch: float | None = None) -> dict:
        """One row in the format mql5/MonthlyBT_Signals.mq5 expects.

        The EA needs the BAR time as its join key. We prefer an explicit
        epoch, then bar_timestamp, then the decision timestamp.
        """
        ep = epoch
        if ep is None and self.bar_timestamp:
            try:
                ep = datetime.fromisoformat(self.bar_timestamp).timestamp()
            except ValueError:
                ep = None
        if ep is None:
            try:
                ep = datetime.fromisoformat(self.timestamp).timestamp()
            except ValueError:
                ep = None
        return {
            "epoch": int(ep) if ep is not None else "",
            "strategy": self.strategy,
            "strategy_number": self.strategy_number,
            "timeframe": self.timeframe,
            "signal": self.direction,
            "direction": self.direction,
            "entry": _fmt(self.entry),
            "sl": _fmt(self.stop),
            "tp": _fmt(self.target),
            "lots": f"{self.lots:.2f}",
            "risk_usd": f"{self.risk_usd:.2f}",
            "risk_pct": f"{self.risk_pct * 100:.3f}",
            "confidence": f"{self.confidence:.2f}",
            "reason": self.reason.replace(",", ";")[:240],
            "simulated": 1 if self.simulated else 0,
            "symbol": self.symbol,
            "broker_symbol": self.broker_symbol,
            "magic": self.magic,
            "decision_ts": self.timestamp,
            "bar_ts": self.bar_timestamp,
            "invalidation": self.invalidation.replace(",", ";")[:160],
            "invalidation_price": _fmt(self.invalidation_price),
        }


def _fmt(x: float) -> str:
    """NaN -> '' so a refused signal has no prices in the CSV."""
    return "" if not math.isfinite(x) else f"{x:.5f}"


def _json_safe(obj):
    """Replace NaN/Infinity with None, recursively.

    NaN is what a missing price means internally, but it is not legal
    JSON. Any consumer that is not Python — a browser, a JS dashboard, a
    strict parser — must receive null, not NaN.
    """
    if isinstance(obj, float):
        return None if not math.isfinite(obj) else obj
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v) for v in obj]
    return obj


# ----------------------------------------------------------------------
# Journal — the append-only record the dashboard and CSV read
# ----------------------------------------------------------------------
SIGNAL_JOURNAL = Path(__file__).resolve().parents[1] / "journal" / "signals.jsonl"


def journal_signal(sig: Signal) -> Path:
    """Append one signal to the journal. One JSON object per line.

    This is the system's own record of what it decided and why — the
    forward evidence that paper_run's historical re-simulation cannot
    provide. It is append-only and is never rewritten.
    """
    SIGNAL_JOURNAL.parent.mkdir(parents=True, exist_ok=True)
    with SIGNAL_JOURNAL.open("a", encoding="utf-8") as fh:
        fh.write(sig.to_json() + "\n")
    return SIGNAL_JOURNAL


# ----------------------------------------------------------------------
# CSV I/O — the bridge to the MT5 chart indicator
# ----------------------------------------------------------------------
def write_signal_csv(signals: list[Signal], path: str | Path,
                     append: bool = False) -> Path:
    """Write signals for the MQL5 indicator to draw on the chart.

    The EA reads <terminal data>\\MQL5\\Files\\signals\\*.csv, so callers
    pass that directory. Creating the directory is our job.
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    exists = p.exists() and p.stat().st_size > 0
    mode = "a" if (append and exists) else "w"
    with p.open(mode, newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=CSV_COLUMNS)
        if mode == "w":
            w.writeheader()
        for s in signals:
            w.writerow(s.to_csv_row())
    return p


def read_signal_csv(path: str | Path) -> list[Signal]:
    """Inverse of write_signal_csv. Used by tests and the dashboard."""
    out: list[Signal] = []
    with Path(path).open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            def f(k, default=float("nan")):
                v = row.get(k, "")
                if v in ("", None):
                    return default
                try:
                    return float(v)
                except ValueError:
                    return default
            s = Signal(
                strategy=row.get("strategy", ""),
                strategy_number=row.get("strategy_number", ""),
                symbol=row.get("symbol", "XAUUSD"),
                broker_symbol=row.get("broker_symbol", ""),
                timeframe=row.get("timeframe", ""),
                direction=row.get("signal") or row.get("direction") or "WAIT",
                entry=f("entry"), stop=f("sl"), target=f("tp"),
                lots=f("lots", 0.0), risk_usd=f("risk_usd", 0.0),
                risk_pct=f("risk_pct", 0.0) / 100.0,
                confidence=f("confidence", 0.0),
                reason=row.get("reason", ""),
                simulated=row.get("simulated", "1") == "1",
                magic=int(f("magic", 0.0)),
                timestamp=row.get("decision_ts") or _iso(f("epoch", None)),
                bar_timestamp=row.get("bar_ts") or _iso(f("epoch", None)),
            )
            out.append(s)
    return out
