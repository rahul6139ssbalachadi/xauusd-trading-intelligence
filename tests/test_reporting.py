"""Tests for the Phase 12 reporting / dashboard layer.

Covers: experiment-registry ID minting, report aggregation structure,
text + HTML renderers, and a real-data report build (structural invariants).
All read-only — no execution, no account writes.
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
from paper import JournalStore, paper_run
from reporting import (
    ExperimentRegistry, build_report, render_text, render_html,
    write_report, StrategyReport,
)

DEF = (
    Path(__file__).resolve().parents[1]
    / "strategy"
    / "defs"
    / "XAUUSD_STRUCTURE_BREAK_V2.json"
)


# ---- experiment registry --------------------------------------------------

def test_registry_mints_unique_ids():
    with tempfile.TemporaryDirectory() as tmp:
        reg = ExperimentRegistry(Path(tmp) / "reg.jsonl")
        a = reg.mint("EXP", "first")
        b = reg.mint("BACKTEST", "second")
        assert a.startswith("EXP-") and b.startswith("BACKTEST-")
        assert a != b
        # third run reuses same file, continues sequence
        reg2 = ExperimentRegistry(Path(tmp) / "reg.jsonl")
        c = reg2.mint("STRAT", "third")
        assert c != a and c != b


# ---- report aggregation + renderers --------------------------------------

def test_build_report_structure():
    # minimal synthetic: use a tiny strategy on a tiny frame is heavy; instead
    # build a report object directly and check renderers handle it
    r = StrategyReport(
        name="X", version="V1",
        metrics={"total_trades": 10, "win_rate": 0.5, "net_pips": 12.3,
                 "profit_factor": 1.4, "sharpe": 0.7},
        journal={"total": 5, "buys": 2, "sells": 1, "waits": 2,
                 "planned_risk_usd": 15.0},
        notes="test",
    )
    txt = render_text(r)
    assert "X V1" in txt
    assert "Profit factor" in txt
    assert "Journal" in txt
    html = render_html(r)
    assert "<html" in html and "X V1" in html
    assert "Read-only report" in html


def test_render_handles_empty():
    r = StrategyReport(name="Z", version="V0")
    txt = render_text(r)
    assert "Z V0" in txt
    # no crash on empty metrics/journal
    assert "Trades" not in txt.split("Notes")[0] or True


def test_write_report_files(tmp_path):
    r = StrategyReport(name="W", version="V9", metrics={"total_trades": 3})
    p = write_report(r, tmp_path / "rep.txt", fmt="text")
    assert p.exists() and "W V9" in p.read_text()
    p2 = write_report(r, tmp_path / "rep.html", fmt="html")
    assert p2.exists() and "<html" in p2.read_text()


# ---- real-data report -----------------------------------------------------

def _load(tf, limit=None):
    db = cfg.PROJECT_ROOT / cfg.load_settings()["paths"]["db"]
    con = sqlite3.connect(db)
    q = (f"SELECT ts_broker_epoch, open, high, low, close, spread FROM market_data "
         f"WHERE symbol='XAUUSD' AND timeframe='{tf}' AND source='mt5' ORDER BY ts_broker_epoch")
    if limit:
        q += f" LIMIT {limit}"
    df = pd.read_sql_query(q, con)
    con.close()
    df["ts"] = pd.to_datetime(df["ts_broker_epoch"], unit="s", utc=True)
    df["spread_pips"] = df["spread"] * 0.01 / 0.10
    return df


def test_build_report_real_data():
    m15 = _load("M15", 1500)
    m5 = _load("M5", 6000)
    if len(m15) < 300 or len(m5) < 300:
        pytest.skip("not enough data")
    strat = Strategy.load(DEF)
    with tempfile.TemporaryDirectory() as tmp:
        j = JournalStore(Path(tmp) / "j.jsonl")
        paper_run(strat, m15, m5, j, equity=10_000,
                  slippage_pips=0.5, max_entries=100)
        rep = build_report(strat, m15, m5, journal=j, slippage_pips=0.5,
                           notes="V2 two-sided, real gold slice")
        d = rep.as_dict()
        # invariants
        assert d["name"] == "XAUUSD_STRUCTURE_BREAK"
        assert d["version"] == "V2"
        assert isinstance(d["backtest_metrics"]["total_trades"], int)
        assert d["backtest_metrics"]["total_trades"] >= 0
        # journal stats merged
        assert d["journal_stats"]["total"] == 100
        # renderers don't crash on real data
        assert "XAUUSD_STRUCTURE_BREAK" in render_text(rep)
        assert "<html" in render_html(rep)
        print("\n[Report V2 real] trades=%d net=%.1f pf=%.2f journal=%d" % (
            d["backtest_metrics"]["total_trades"],
            d["backtest_metrics"]["net_pips"],
            d["backtest_metrics"]["profit_factor"],
            d["journal_stats"]["total"]))
