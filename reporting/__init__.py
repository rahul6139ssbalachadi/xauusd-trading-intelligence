"""Reporting / dashboard (Phase 12).

READ-ONLY monitoring and reporting layer. It aggregates:
  - backtest metrics (from backtest.compute_metrics / run_backtest)
  - paper-trade journal stats (from paper.JournalStore)
into a single summary, and renders it as plain text or HTML.

This is the observation/monitoring surface described in CLAUDE.md. It never
executes trades, never writes to any account, and never mutates strategy
state. Every number it shows is computed from already-existing artifacts
(backtest results, journal files) — nothing is invented.

Also provides a tiny experiment-registry helper that mints the
EXP-/STRAT-/BACKTEST- IDs CLAUDE.md requires, so future runs are traceable.
"""
from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from backtest import run_backtest, compute_metrics
from strategy import Strategy
from paper import JournalStore, summarize_journal


# --------------------------------------------------------------------------
# Experiment registry (CLAUDE.md §4: every experiment gets a unique ID)
# --------------------------------------------------------------------------
class ExperimentRegistry:
    """Mints and persists experiment IDs. Append-only JSONL under .hermes or
    a caller-supplied dir. Pure bookkeeping — no trading, no model calls."""

    def __init__(self, path: str | Path = ".experiment_registry.jsonl"):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._seq = self._next_seq()

    def _next_seq(self) -> int:
        if not self.path.exists():
            return 1
        n = 0
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                n += 1
        return n + 1

    def mint(self, kind: str, label: str) -> str:
        """kind in {EXP, STRAT, BACKTEST, PAPER}. Returns e.g. 'EXP-2026-001'."""
        kind = kind.upper()
        year = _dt.date.today().year
        seq = self._seq
        eid = f"{kind}-{year}-{seq:03d}"
        rec = {
            "id": eid, "kind": kind, "label": label,
            "created": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        }
        with self.path.open("a", encoding="utf-8") as f:
            f.write(__import__("json").dumps(rec) + "\n")
        self._seq += 1
        return eid


# --------------------------------------------------------------------------
# Report aggregation
# --------------------------------------------------------------------------
@dataclass
class StrategyReport:
    name: str
    version: str
    metrics: dict = field(default_factory=dict)
    journal: dict = field(default_factory=dict)
    notes: str = ""

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "version": self.version,
            "backtest_metrics": self.metrics,
            "journal_stats": self.journal,
            "notes": self.notes,
        }


def build_report(
    strat: Strategy,
    bias_df,
    trigger_df,
    journal: JournalStore | None = None,
    slippage_pips: float = 0.5,
    notes: str = "",
) -> StrategyReport:
    """Run a backtest (read-only) and merge journal stats into a report."""
    res = run_backtest(strat, bias_df, trigger_df, slippage_pips=slippage_pips)
    journal_stats = summarize_journal(journal) if journal else {}
    return StrategyReport(
        name=strat.name, version=strat.version,
        metrics=res.metrics, journal=journal_stats, notes=notes,
    )


# --------------------------------------------------------------------------
# Renderers (text + HTML) — pure formatting, no side effects
# --------------------------------------------------------------------------
def _fmt(v, nd=2):
    if v is None:
        return "n/a"
    if isinstance(v, float):
        if v != v:  # nan
            return "n/a"
        return f"{v:.{nd}f}"
    return str(v)


def render_text(report: StrategyReport) -> str:
    m = report.metrics
    j = report.journal
    lines = []
    lines.append("=" * 60)
    lines.append(f"  STRATEGY REPORT — {report.name} {report.version}")
    lines.append("=" * 60)
    if m:
        lines.append(f"  Trades         : {_fmt(m.get('total_trades'))}")
        lines.append(f"  Win rate       : {_fmt(m.get('win_rate', 0) * 100, 1)}%")
        lines.append(f"  Net pips       : {_fmt(m.get('net_pips'))}")
        lines.append(f"  Profit factor  : {_fmt(m.get('profit_factor'))}")
        lines.append(f"  Expectancy     : {_fmt(m.get('expectancy_pips'))} pips")
        lines.append(f"  Max drawdown   : {_fmt(m.get('max_drawdown_pips'))} pips")
        lines.append(f"  Sharpe         : {_fmt(m.get('sharpe'))}")
        lines.append(f"  Sortino        : {_fmt(m.get('sortino'))}")
        lines.append(f"  Avg win / loss : {_fmt(m.get('avg_win_pips'))} / {_fmt(m.get('avg_loss_pips'))}")
        lines.append(f"  Win/Loss streak: {_fmt(m.get('longest_win_streak'))} / {_fmt(m.get('longest_loss_streak'))}")
    if j:
        lines.append("-" * 60)
        lines.append(f"  Journal        : {_fmt(j.get('total'))} decisions")
        lines.append(f"    BUY/SELL/WAIT: {_fmt(j.get('buys'))}/{_fmt(j.get('sells'))}/{_fmt(j.get('waits'))}")
        lines.append(f"    Planned risk : ${_fmt(j.get('planned_risk_usd', 0.0))}")
    if report.notes:
        lines.append("-" * 60)
        lines.append(f"  Notes          : {report.notes}")
    lines.append("=" * 60)
    return "\n".join(lines)


def render_html(report: StrategyReport) -> str:
    m = report.metrics
    j = report.journal
    rows = ""
    if m:
        for k in ["total_trades", "win_rate", "net_pips", "profit_factor",
                  "expectancy_pips", "max_drawdown_pips", "sharpe", "sortino",
                  "avg_win_pips", "avg_loss_pips", "longest_win_streak",
                  "longest_loss_streak"]:
            val = m.get(k)
            if k == "win_rate" and val is not None:
                val = f"{val * 100:.1f}%"
            rows += f"<tr><td>{k}</td><td>{_fmt(val)}</td></tr>\n"
    jrows = ""
    if j:
        for k, v in j.items():
            jrows += f"<tr><td>{k}</td><td>{_fmt(v)}</td></tr>\n"
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>{report.name} {report.version}</title>
<style>body{{font-family:monospace;background:#111;color:#eee;padding:2rem}}
table{{border-collapse:collapse}}td{{border:1px solid #444;padding:.4rem .8rem}}
h1{{color:#6cf}}.warn{{color:#f88}}</style></head>
<body>
<h1>{report.name} {report.version}</h1>
<h3>Backtest metrics</h3><table>{rows}</table>
<h3>Journal stats</h3><table>{jrows}</table>
<p class="warn">Read-only report. No live trading. Numbers are backtest/paper
results, not forward guarantees.</p>
</body></html>"""


def write_report(report: StrategyReport, out_path: str | Path, fmt: str = "text") -> Path:
    """Render a report to disk (text or html). Read-only artifact."""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    content = render_html(report) if fmt == "html" else render_text(report)
    out_path.write_text(content, encoding="utf-8")
    return out_path
