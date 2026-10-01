"""Tests for the trade-by-trade execution log.

The load-bearing claim of research/trade_log.py is its SIZING AUDIT: it
recomputes every lot size independently of the engine and asserts they
match. If that audit silently passed on wrong numbers, the report would
state a lot size the engine never used. These tests pin that down, plus
the CSV/report shape the user actually reads.
"""
from __future__ import annotations

import re
import sqlite3
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from monthly_bt import data as bt_data
from monthly_bt import strategies as S
from monthly_bt.engine import SimConfig, size_lots
from monthly_bt.data import UNITS
from research import trade_log as TL


def _have(tf="D1", n=300):
    try:
        return len(bt_data.load("XAUUSD", tf)) >= n
    except Exception:
        return False


# ======================================================================
# Sizing audit — the report's central claim
# ======================================================================
class TestSizingAudit:
    def test_audit_passes_on_a_hand_checked_trade(self):
        """A single trade whose lot size is known exactly.

        1% of $100,000 = $1,000 budget. A 215.2-pip stop on gold at
        $10/pip/lot needs 1000 / (215.2 * 10) = 0.4647, floored to the
        0.01 lot step = 0.46.
        """
        risk_pips = 215.2
        lots = size_lots(100_000.0, 0.02, risk_pips, "XAUUSD", SimConfig())
        assert lots == pytest.approx(0.46)
        trade = {
            "strategy": "V12", "entry_bar": 5, "entry": 4256.74,
            "sl": 4235.22, "tp": 4321.29, "risk_pips": risk_pips,
            "lots": lots, "net_pnl": 2958.04,
            "balance_after": 100_000.0 + 2958.04,
        }
        assert TL.audit_sizing([trade], S.V12, "XAUUSD") == []

    def test_audit_catches_a_wrong_lot_size(self):
        """The audit must FAIL, not pass quietly, when the executed lot
        size disagrees with the recomputed one."""
        trade = {
            "strategy": "V12", "entry_bar": 5, "entry": 4256.74,
            "sl": 4235.22, "tp": 4321.29, "risk_pips": 215.2,
            "lots": 0.99,                       # wrong on purpose
            "net_pnl": 0.0, "balance_after": 100_000.0,
        }
        problems = TL.audit_sizing([trade], S.V12, "XAUUSD")
        assert len(problems) == 1
        assert "V12" in problems[0] and "lots" in problems[0]

    def test_audit_uses_equity_before_the_trade_not_after(self):
        """Sizing compounds on the balance BEFORE the trade closed. An
        audit that used balance_after would drift on every trade."""
        lots = size_lots(110_000.0, 0.02, 100.0, "XAUUSD", SimConfig())
        trade = {
            "strategy": "V12", "entry_bar": 1, "entry": 1000.0,
            "sl": 990.0, "tp": 1030.0, "risk_pips": 100.0, "lots": lots,
            "net_pnl": 0.0, "balance_after": 110_000.0,
        }
        assert TL.audit_sizing([trade], S.V12, "XAUUSD") == []

    @pytest.mark.skipif(not _have("H1", 1000), reason="no H1 data")
    def test_audit_passes_on_real_trades(self):
        """End-to-end on the real engine output for both strategies."""
        problems = []
        for key, tf in (("V11", "D1"), ("V12", "H1")):
            if not _have(tf, 300):
                continue
            spec = S.get(key)
            periods = [("2026-08", "2026-08-01", "2026-09-01")]
            trades, _, _ = TL.collect(key, "XAUUSD", periods, 100_000.0)
            problems += TL.audit_sizing(trades, spec, "XAUUSD")
        assert problems == []


# ======================================================================
# Frame + renderers
# ======================================================================
class TestFrameAndRender:
    TRADES = [{
        "period": "2026-08", "strategy": "V12", "signal": "BUY",
        "timeframe": "H1", "signal_ts": "2026-08-05 05:00:00+00:00",
        "entry_ts": "2026-08-05 06:00:00+00:00", "entry": 4129.19,
        "sl": 4115.90, "tp": 4169.06, "exit_ts": "2026-08-05 11:00:00+00:00",
        "exit": 4169.06, "exit_reason": "target", "lots": 0.75,
        "risk_pips": 132.9, "spread_pips": 0.14, "cost_pips": 2.28,
        "fees": 1.71, "net_pips": 398.7, "r_multiple": 3.0,
        "net_pnl": 2971.91, "balance_after": 102_971.91,
        "atr": 166.1, "body_pct": 1.0,
    }]

    def test_to_frame_has_every_requested_column(self):
        df = TL.to_frame(self.TRADES, "XAUUSD")
        for col in ("period", "strategy", "signal", "timeframe",
                    "signal_time", "entry_time", "entry", "sl", "tp",
                    "exit_time", "exit", "exit_reason", "lots", "stop_pips",
                    "risk_usd", "spread_pips", "cost_pips", "costs_usd",
                    "net_pips", "r_multiple", "net_pnl", "balance_after"):
            assert col in df.columns, f"missing {col}"

    def test_risk_usd_uses_the_instrument_pip_value(self):
        df = TL.to_frame(self.TRADES, "XAUUSD")
        # 132.9 pips * $10/pip/lot * 0.75 lots
        assert df["risk_usd"].iloc[0] == pytest.approx(132.9 * 10 * 0.75, abs=0.02)

    def test_risk_usd_differs_for_btc(self):
        """BTC's pip is $1.00 and contract 1.0, so the same pip count is a
        completely different dollar risk. Reusing gold's math would be a
        10x error."""
        gold = TL.to_frame(self.TRADES, "XAUUSD")["risk_usd"].iloc[0]
        btc = TL.to_frame(self.TRADES, "BTCUSD")["risk_usd"].iloc[0]
        assert btc != gold
        assert btc == pytest.approx(gold / 10, rel=0.01)

    def test_txt_lists_time_side_entry_sl_tp_and_lots(self):
        df = TL.to_frame(self.TRADES, "XAUUSD")
        txt = TL.render_txt(df, "XAUUSD", 100_000.0)
        assert "2026-08-05 06:00" in txt     # entry time
        assert "BUY" in txt
        assert "4129.19" in txt             # entry
        assert "4115.90" in txt             # SL
        assert "4169.06" in txt             # TP
        assert "0.75" in txt                # lots
        assert "No order was placed" in txt

    def test_md_reports_a_passed_audit(self):
        df = TL.to_frame(self.TRADES, "XAUUSD")
        md = TL.render_md(df, "XAUUSD", 100_000.0, [], [])
        assert "Sizing audit — PASSED" in md
        assert "**0.75**" in md

    def test_md_reports_a_failed_audit(self):
        df = TL.to_frame(self.TRADES, "XAUUSD")
        md = TL.render_md(df, "XAUUSD", 100_000.0, [],
                          ["  V12 bogus: lots 9.99"])
        assert "Sizing audit — FAILED" in md

    def test_md_separates_signals_from_trades(self):
        df = TL.to_frame(self.TRADES, "XAUUSD")
        declined = [{**self.TRADES[0], "skip_reason": "position_already_open"}]
        md = TL.render_md(df, "XAUUSD", 100_000.0, declined, [])
        assert "did NOT become trades" in md
        assert "position_already_open" in md

    def test_empty_frame_renders_headers_without_crashing(self):
        """A month with no trades must still produce a readable table with
        headers, not a traceback — the user asks for windows that may be
        empty."""
        cols = list(TL.to_frame(self.TRADES, "XAUUSD").columns)
        empty = pd.DataFrame(columns=cols)
        txt = TL.render_txt(empty, "XAUUSD", 100_000.0)
        assert "TRADE-BY-TRADE" in txt
        assert "entry time" in txt          # header row still printed
        assert "No order was placed" in txt
        assert len(TL.render_md(empty, "XAUUSD", 100_000.0, [], [])) > 0


# ======================================================================
# Periods
# ======================================================================
class TestPeriods:
    def test_months_become_half_open_ranges(self):
        class A:
            months = ["2026-05", "2026-06"]
            all = False
            symbol = "XAUUSD"
        out = TL.periods_for(A, S.V11)
        assert out == [("2026-05", "2026-05-01", "2026-06-01"),
                       ("2026-06", "2026-06-01", "2026-07-01")]

    @pytest.mark.skipif(not _have("D1", 300), reason="no D1 data")
    def test_all_covers_every_month_with_data(self):
        class A:
            months = []
            all = True
            symbol = "XAUUSD"
        out = TL.periods_for(A, S.V11)
        # DERIVED, not hardcoded: the span grows as history is ingested.
        # It was 120 months (2016-09..2026-08); the 2026-10-01 ingest widened
        # it to 2016-05..2026-10, i.e. 126. Assert the enumeration covers the
        # data's actual span rather than a frozen number.
        con = sqlite3.connect(bt_data.DB)
        lo, hi = con.execute(
            "SELECT MIN(ts_broker_epoch), MAX(ts_broker_epoch) FROM market_data "
            "WHERE symbol='XAUUSD' AND timeframe='D1' AND source='mt5'").fetchone()
        con.close()
        want = pd.period_range(pd.Timestamp(int(lo), unit="s"),
                               pd.Timestamp(int(hi), unit="s"), freq="M")
        assert len(out) == len(want)
        assert out[0][0] == str(want[0])
        assert out[-1][0] == str(want[-1])
        assert out[0][0] < out[-1][0]   # chronological
        for _, s, e in out:
            assert s < e


# ======================================================================
# The report must never claim a SELL that does not exist
# ======================================================================
class TestLongOnlyHonesty:
    @pytest.mark.skipif(not _have("H1", 1000), reason="no H1 data")
    def test_every_real_trade_is_buy(self):
        """V11/V12 are LONG-only by design. If this ever produces a SELL the
        strategy logic changed, and the report's side column is load-bearing
        for the user's reading of it."""
        trades, _, _ = TL.collect("V12", "XAUUSD",
                                  [("2026-08", "2026-08-01", "2026-09-01")],
                                  100_000.0)
        assert trades
        assert {t["signal"] for t in trades} == {"BUY"}


# ======================================================================
# Read-only
# ======================================================================
class TestReadOnly:
    def test_no_order_path(self):
        import io
        import tokenize
        src = (ROOT / "research" / "trade_log.py").read_text(encoding="utf-8")
        code = " ".join(tok.string for tok in
                        tokenize.generate_tokens(io.StringIO(src).readline)
                        if tok.type not in (tokenize.STRING, tokenize.COMMENT))
        for pat in (r"\border_send\b", r"\bMT5Gateway\b", r"\bpositions_get\b"):
            assert not re.search(pat, code, re.M), f"references {pat}"

    def test_writes_only_to_its_own_output_dir(self):
        assert TL.OUT.name == "trades"
        assert "reports" in TL.OUT.parts
        # the research market-data DB is never the write target
        assert TL.OUT.suffix != ".db"
        assert not str(TL.OUT).endswith(bt_data.DB.name)
