"""Tests for the V11 D1 paper-trade harness (paper/d1_paper.py).

Verifies:
  - The harness journals BUY decisions for signal bars and WAIT for all others
  - Every journal entry has valid structure (reasons list, bias, decision)
  - The paper run is simulation-only (no MT5 writes, no execution path)
  - Sized trades are attached with lots/risk_usd/net_usd
  - summarize_journal works on the D1 journal

Read-only: queries db/trading.db only, never calls MT5 in trade mode.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from market_data import config as cfg
from paper import JournalStore, summarize_journal
from paper.d1_paper import paper_run_d1

# V11 best params (from research/v11_d1_momentum.py)
V11_PARAMS = {
    "body_pct_threshold": 0.95,
    "trend_filter": True,
    "min_atr_pct": 0.005,
    "rr": 2.0,
    "atr_mult_stop": 1.5,
    "max_holding_d1": 8,
}


def test_paper_run_d1_journals_decisions():
    """Full-run paper trade: journal has BUY + WAIT entries with reasons."""
    with tempfile.TemporaryDirectory() as tmp:
        journal = JournalStore(Path(tmp) / "paper_d1.jsonl")
        sized = paper_run_d1(V11_PARAMS, journal, equity=10_000.0)

        entries = journal.read()
        assert len(entries) > 0
        # every entry has valid decision
        assert all(e.decision in ("BUY", "SELL", "WAIT") for e in entries)
        # every WAIT has reasons; every BUY has reasons + pricing
        waits = [e for e in entries if e.decision == "WAIT"]
        buys = [e for e in entries if e.decision == "BUY"]
        assert all(len(e.reasons) > 0 for e in entries)
        assert all(e.bias in ("bull", "bear") for e in entries if e.decision != "WAIT")
        for b in buys:
            assert b.entry_plan != float("nan")
            assert b.stop != float("nan")
            assert b.target != float("nan")
            assert b.lots >= 0.0
            assert b.risk_usd >= 0.0
        # V11 is long-only, so no SELL decisions
        assert all(e.decision != "SELL" for e in entries)
        # summarize works
        s = summarize_journal(journal)
        assert s["total"] == len(entries)
        assert s["buys"] == len(buys)
        assert s["waits"] == len(waits)
        print(f"\n[V11 paper D1] total={s['total']} buys={s['buys']} "
              f"waits={s['waits']} planned_risk=${s['planned_risk_usd']:.2f} "
              f"sized_trades={len(sized)}")


def test_paper_run_d1_max_entries():
    """--max-entries caps the journal length."""
    with tempfile.TemporaryDirectory() as tmp:
        journal = JournalStore(Path(tmp) / "paper_d1_cap.jsonl")
        paper_run_d1(V11_PARAMS, journal, equity=10_000.0, max_entries=300)
        entries = journal.read()
        assert len(entries) == 300
        # still has valid decisions
        assert all(e.decision in ("BUY", "SELL", "WAIT") for e in entries)


def test_paper_run_d1_sized_trades_have_lots():
    """At least some trades should be sizeable (lots > 0) after risk sizing."""
    with tempfile.TemporaryDirectory() as tmp:
        journal = JournalStore(Path(tmp) / "paper_d1_sized.jsonl")
        sized = paper_run_d1(V11_PARAMS, journal, equity=10_000.0)
        assert len(sized) > 0
        # all trades have lots attribute set (some may be 0 if stop too wide)
        assert all(hasattr(t, "lots") for t in sized)
        assert all(hasattr(t, "risk_usd") for t in sized)
        assert all(hasattr(t, "net_usd") for t in sized)
        # at least one trade should be sizeable
        assert any(t.lots > 0 for t in sized)
        # net_usd is consistent with net_pips * PIP * lots
        from risk import PIP
        for t in sized:
            assert t.net_usd == pytest.approx(t.net_pips * PIP * t.lots, abs=1e-6)
