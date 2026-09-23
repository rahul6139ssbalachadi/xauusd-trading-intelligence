"""Paper-trading harness (Phase 11).

CRITICAL SAFETY PROPERTY
-----------------------
This module is SIMULATION-ONLY. It NEVER places, modifies, or closes a real
order and never connects to MT5 in trade mode. It exists to:
  - generate BUY/SELL/WAIT decisions from a strategy (reusing the backtest
    engine's signal logic),
  - record every decision + its reasoning in an append-only trade journal,
  - track paper (virtual) P&L and risk sizing.

The journal is the audit trail required by CLAUDE.md (complete trading
journal, every decision explained). It writes to a JSONL file by default and
can also be mirrored into the SQLite DB's `journal` table if present, but it
does NOT touch any live account.

Per the user's standing instruction: the live demo MT5 account (with trades
opened from mobile) must never be affected. live_trading_enabled is and stays
False. This module enforces that by simply having no execution path at all.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

import pandas as pd

from strategy import Strategy, build_features, evaluate
from backtest import run_backtest, Trade
from risk import apply_risk_to_backtest, RiskConfig

JOURNAL_COLS = [
    "ts", "strategy", "version", "decision", "bias", "reasons",
    "entry_plan", "stop", "target", "lots", "risk_usd", "equity",
]


@dataclass
class JournalEntry:
    ts: str
    strategy: str
    version: str
    decision: str            # BUY / SELL / WAIT
    bias: str
    reasons: list[str]
    entry_plan: float = float("nan")
    stop: float = float("nan")
    target: float = float("nan")
    lots: float = 0.0
    risk_usd: float = 0.0
    equity: float = 0.0

    def to_row(self) -> dict:
        d = asdict(self)
        d["reasons"] = json.dumps(self.reasons)
        return d


class JournalStore:
    """Append-only trade journal. Writes JSONL; optionally mirrors to SQLite.

    No execution. No account access. Pure record-keeping.
    """

    def __init__(self, path: str | Path = "journal.jsonl", db_path: str | Path | None = None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db_path = Path(db_path) if db_path else None

    def append(self, entry: JournalEntry) -> None:
        row = entry.to_row()
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")
        # mirror to SQLite if a db_path was configured (connect creates it)
        self._mirror_sqlite(row)

    def _mirror_sqlite(self, row: dict) -> None:
        if not self.db_path:
            return
        try:
            con = sqlite3.connect(self.db_path)
            con.execute(
                "CREATE TABLE IF NOT EXISTS journal ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, strategy TEXT, "
                "version TEXT, decision TEXT, bias TEXT, reasons TEXT, "
                "entry_plan REAL, stop REAL, target REAL, lots REAL, "
                "risk_usd REAL, equity REAL)"
            )
            con.execute(
                "INSERT INTO journal (ts, strategy, version, decision, bias, "
                "reasons, entry_plan, stop, target, lots, risk_usd, equity) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                tuple(row.get(c) for c in JOURNAL_COLS),
            )
            con.commit()
            con.close()
        except Exception:
            # journal must never break the run; mirror failure is non-fatal
            pass

    def read(self) -> list[JournalEntry]:
        if not self.path.exists():
            return []
        out = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            d = json.loads(line)
            d["reasons"] = json.loads(d["reasons"])
            out.append(JournalEntry(**d))
        return out


# --------------------------------------------------------------------------
# Paper run: generate decisions over a historical (or live-priced) window
# --------------------------------------------------------------------------
def paper_run(
    strat: Strategy,
    bias_df: pd.DataFrame,
    trigger_df: pd.DataFrame,
    journal: JournalStore,
    equity: float = 10_000.0,
    slippage_pips: float = 0.5,
    cfg: RiskConfig | None = None,
    max_entries: int | None = None,
) -> pd.DataFrame:
    """Generate and journal decisions for every trigger bar.

    For each non-WAIT decision we also run the (already validated) backtest
    sizing so the journal records the planned entry/stop/target/lots/risk.
    This is purely a decision logger — it reuses run_backtest's per-trade
    output to populate the journal, but does NOT execute anything.

    Returns the decision DataFrame (same shape as evaluate output).
    """
    dec = evaluate(strat, build_features(bias_df), build_features(trigger_df))
    # reuse backtest to get sized trades (entry/stop/target/lots/risk) in
    # chronological order; map by exit/entry bar back to the decision frame
    res = run_backtest(strat, bias_df, trigger_df, slippage_pips=slippage_pips)
    sized = apply_risk_to_backtest(res.trades, equity_start=equity, cfg=cfg,
                                    risk_pct=strat.risk_pct)
    # index trades by their entry bar (iloc in trigger_df)
    trade_by_entry = {t.entry_bar: t for t in sized}

    count = 0
    for i in range(len(dec)):
        row = dec.iloc[i]
        sig = row["signal"]
        if sig == "WAIT":
            entry = JournalEntry(
                ts=str(trigger_df.iloc[i]["ts"]),
                strategy=strat.name, version=strat.version,
                decision="WAIT", bias=row["bias"], reasons=row["reasons"],
                equity=equity,
            )
        else:
            t = trade_by_entry.get(i)
            lots = t.lots if t else 0.0
            risk = t.risk_usd if t else 0.0
            entry = JournalEntry(
                ts=str(trigger_df.iloc[i]["ts"]),
                strategy=strat.name, version=strat.version,
                decision=sig, bias=row["bias"], reasons=row["reasons"],
                entry_plan=float(row.get("close", "nan")),
                stop=float(row.get("stop", "nan")),
                target=float(row.get("target", "nan")),
                lots=lots, risk_usd=risk, equity=equity,
            )
        journal.append(entry)
        count += 1
        if max_entries and count >= max_entries:
            break
    return dec


def summarize_journal(journal: JournalStore) -> dict:
    """Aggregate the journal into decision stats (audit summary)."""
    entries = journal.read()
    if not entries:
        return {"total": 0}
    decisions = [e.decision for e in entries]
    buys = decisions.count("BUY")
    sells = decisions.count("SELL")
    waits = decisions.count("WAIT")
    total_risk = sum(e.risk_usd for e in entries if e.decision != "WAIT")
    return {
        "total": len(entries),
        "buys": buys,
        "sells": sells,
        "waits": waits,
        "planned_risk_usd": round(total_risk, 2),
    }
