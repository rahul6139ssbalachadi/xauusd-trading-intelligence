"""Tests for the Phase 11 paper-trading harness.

Verifies the journal records decisions (append-only, round-trips), the
summarize aggregator works, and a real-data paper run produces a valid
journal — all WITHOUT any execution path (simulation only).
"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from market_data import config as cfg
from strategy import Strategy
from paper import JournalStore, JournalEntry, paper_run, summarize_journal
from risk import RiskConfig

DEF = (
    Path(__file__).resolve().parents[1]
    / "strategy"
    / "defs"
    / "XAUUSD_STRUCTURE_BREAK_V2.json"
)


# ---- journal store --------------------------------------------------------

def test_journal_append_and_read_roundtrip():
    with tempfile.TemporaryDirectory() as tmp:
        j = JournalStore(Path(tmp) / "j.jsonl")
        e1 = JournalEntry(ts="2026-01-05T14:05:00+00:00", strategy="X",
                          version="V1", decision="BUY", bias="up",
                          reasons=["bias up confirmed", "BOS up confirmed"],
                          entry_plan=2010.5, stop=2005.0, target=2021.5,
                          lots=0.12, risk_usd=24.0, equity=10000.0)
        e2 = JournalEntry(ts="2026-01-05T14:10:00+00:00", strategy="X",
                          version="V1", decision="WAIT", bias="down",
                          reasons=["bias down != required up"])
        j.append(e1)
        j.append(e2)
        entries = j.read()
        assert len(entries) == 2
        assert entries[0].decision == "BUY"
        assert entries[0].reasons == ["bias up confirmed", "BOS up confirmed"]
        assert entries[1].decision == "WAIT"


def test_journal_mirror_sqlite_nonfatal():
    import os
    tmp = tempfile.mkdtemp()
    try:
        db = Path(tmp) / "db.sqlite"
        j = JournalStore(Path(tmp) / "j.jsonl", db_path=db)
        j.append(JournalEntry(ts="t", strategy="X", version="V1",
                              decision="SELL", bias="down", reasons=["x"],
                              lots=0.1, risk_usd=5.0))
        # sqlite mirror should have created the row
        con = sqlite3.connect(db)
        n = con.execute("SELECT COUNT(*) FROM journal").fetchone()[0]
        con.close()
        assert n == 1
        # journal jsonl still has it
        assert len(j.read()) == 1
    finally:
        # close any lingering handles before removing
        for p in Path(tmp).glob("*"):
            try:
                os.remove(p)
            except OSError:
                pass
        try:
            os.rmdir(tmp)
        except OSError:
            pass


def test_summarize_journal():
    with tempfile.TemporaryDirectory() as tmp:
        j = JournalStore(Path(tmp) / "j.jsonl")
        j.append(JournalEntry(ts="t1", strategy="X", version="V1",
                              decision="BUY", bias="up", reasons=["r"],
                              lots=0.1, risk_usd=10.0))
        j.append(JournalEntry(ts="t2", strategy="X", version="V1",
                              decision="SELL", bias="down", reasons=["r"],
                              lots=0.2, risk_usd=20.0))
        j.append(JournalEntry(ts="t3", strategy="X", version="V1",
                              decision="WAIT", bias="flat", reasons=["r"]))
        s = summarize_journal(j)
        assert s["total"] == 3
        assert s["buys"] == 1 and s["sells"] == 1 and s["waits"] == 1
        assert s["planned_risk_usd"] == 30.0


# ---- paper run on real data ----------------------------------------------

def _load(tf, limit=None):
    db = cfg.PROJECT_ROOT / cfg.load_settings()["paths"]["db"]
    con = sqlite3.connect(db)
    q = (f"SELECT ts_broker_epoch, open, high, low, close, spread FROM market_data "
         f"WHERE symbol='XAUUSD' AND timeframe='{tf}' AND source='mt5' "
         f"ORDER BY ts_broker_epoch")
    if limit:
        q += f" LIMIT {limit}"
    df = pd.read_sql_query(q, con)
    con.close()
    df["ts"] = pd.to_datetime(df["ts_broker_epoch"], unit="s", utc=True)
    df["spread_pips"] = df["spread"] * 0.01 / 0.10
    return df


def test_paper_run_real_data():
    m15 = _load("M15", 1500)
    m5 = _load("M5", 6000)
    if len(m15) < 300 or len(m5) < 300:
        pytest.skip("not enough data")
    strat = Strategy.load(DEF)
    with tempfile.TemporaryDirectory() as tmp:
        j = JournalStore(Path(tmp) / "paper.jsonl")
        dec = paper_run(strat, m15, m5, j, equity=10_000,
                        slippage_pips=0.5, max_entries=200)
        entries = j.read()
        # journal must have exactly max_entries rows
        assert len(entries) == 200
        # every decision is a valid kind
        assert all(e.decision in ("BUY", "SELL", "WAIT") for e in entries)
        # BUY/SELL entries carry a planned stop/target and lots
        acts = [e for e in entries if e.decision != "WAIT"]
        if acts:
            assert all(e.stop == e.stop and e.target == e.target for e in acts)  # not NaN-only check
            assert all(e.lots >= 0 for e in acts)
        s = summarize_journal(j)
        assert s["total"] == 200
        # report
        print("\n[Paper V2 real] entries=%d buys=%d sells=%d waits=%d "
              "planned_risk=$%.2f" % (
                  s["total"], s["buys"], s["sells"], s["waits"],
                  s["planned_risk_usd"]))
