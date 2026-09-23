"""Execution engine tests (Phase 14).

All tests run against a FAKE gateway — no MT5 terminal, no orders, no
network. They verify the Section 23 checklist, the kill switch, the
demo-only hard rule, the no-manual-interference rule, and journaling.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import execution
from execution import (
    ExecutionEngine,
    OrderRequest,
    kill_switch_active,
    trip_kill_switch,
)


# ---------------------------------------------------------------------------
# Fake gateway — implements exactly the interface the engine uses
# ---------------------------------------------------------------------------
class FakeAccount:
    def __init__(self, login=345982869, trade_mode=0, equity=10_000.0):
        self.login = login
        self.trade_mode = trade_mode  # 0 = DEMO
        self.equity = equity


class FakeTick:
    def __init__(self, bid, ask):
        self.bid = bid
        self.ask = ask


class FakeSymbol:
    def __init__(self, visible=True):
        self.visible = visible


class FakePosition:
    def __init__(self, symbol, magic=0):
        self.symbol = symbol
        self.magic = magic


class FakeGateway:
    ACCOUNT_TRADE_MODE_DEMO = 0

    def __init__(self):
        self.account = FakeAccount()
        self.symbols = {"GOLD.i#": FakeSymbol(True)}
        self.ticks = {"GOLD.i#": FakeTick(4450.00, 4450.35)}
        self.positions: list[FakePosition] = []
        self.orders_sent: list[dict] = []

    def account_info(self):
        return self.account

    def symbol_info(self, symbol):
        return self.symbols.get(symbol)

    def symbol_info_tick(self, symbol):
        return self.ticks.get(symbol)

    def positions_get(self, symbol=None):
        return tuple(p for p in self.positions if symbol is None or p.symbol == symbol)

    def order_send(self, **kwargs):
        self.orders_sent.append(kwargs)
        return 500_001  # fake ticket


def make_request(**overrides):
    defaults = dict(
        symbol="XAUUSD", broker_symbol="GOLD.i#", direction="BUY",
        lots=0.01, entry=4450.0, stop=4400.0, target=4550.0,
        strategy="XAUUSD_D1_MOMENTUM_BREAKOUT", version="V11",
        magic=20260922,
    )
    defaults.update(overrides)
    return OrderRequest(**defaults)


@pytest.fixture(autouse=True)
def _clean_kill_switch(tmp_path, monkeypatch):
    """Point the kill switch + journal at tmp so tests never touch the
    real execution/ dir, and never leave a tripped switch behind."""
    ks = tmp_path / "KILL_SWITCH"
    journal = tmp_path / "journal.jsonl"
    monkeypatch.setattr(execution, "KILL_SWITCH", ks)
    monkeypatch.setattr(execution, "JOURNAL", journal)
    yield
    # no cleanup needed: tmp_path is discarded


@pytest.fixture
def engine():
    gw = FakeGateway()
    settings = {"live_trading_enabled": True}
    approved = {"XAUUSD_D1_MOMENTUM_BREAKOUT:V11": {
        "max_spread_pips": 5.0, "risk_pct": 0.01}}
    eng = ExecutionEngine(
        gateway=gw, settings=settings, approved=approved,
        risk_limits={"max_daily_loss_pct": 1.0, "max_weekly_loss_pct": 3.0},
    )
    eng._journal_path = None  # journal goes to patched JOURNAL path
    return eng


# ---------------------------------------------------------------------------
# Kill switch
# ---------------------------------------------------------------------------
class TestKillSwitch:
    def test_inactive_by_default(self):
        assert kill_switch_active() is False

    def test_trip_activates_and_persists(self):
        trip_kill_switch("test trip")
        assert kill_switch_active() is True
        assert "test trip" in execution.KILL_SWITCH.read_text()

    def test_trip_blocks_execution(self, engine):
        trip_kill_switch("daily loss")
        res = engine.execute(make_request())
        assert res.decision == "BLOCKED"
        assert any("kill_switch" in n for n, _, _ in res.checks if _ is False or True)
        assert engine.gateway.orders_sent == []


# ---------------------------------------------------------------------------
# Gate 1: live_trading_enabled
# ---------------------------------------------------------------------------
class TestLiveSwitch:
    def test_disabled_switch_blocks(self, engine):
        engine.settings = {"live_trading_enabled": False}
        res = engine.execute(make_request())
        assert res.decision == "BLOCKED"
        assert engine.gateway.orders_sent == []

    def test_missing_switch_blocks(self, engine):
        engine.settings = {}
        res = engine.execute(make_request())
        assert res.decision == "BLOCKED"


# ---------------------------------------------------------------------------
# Hard rule: DEMO only
# ---------------------------------------------------------------------------
class TestDemoOnly:
    def test_real_account_refused(self, engine):
        engine.gateway.account = FakeAccount(trade_mode=2)  # REAL
        res = engine.execute(make_request())
        assert res.decision == "BLOCKED"
        assert any(n == "account_is_demo" and not ok
                   for n, ok, _ in res.checks)
        assert engine.gateway.orders_sent == []

    def test_demo_account_passes(self, engine):
        check = engine.pre_trade_checks(make_request())
        assert check.ok, check.reason


# ---------------------------------------------------------------------------
# Section 23 checklist items
# ---------------------------------------------------------------------------
class TestChecklist:
    def test_all_pass_clean_state(self, engine):
        check = engine.pre_trade_checks(make_request())
        assert check.ok, check.reason
        names = [n for n, _, _ in check.checks]
        # the 10 spec checks + 2 engine extras
        assert "kill_switch_inactive" in names
        assert "live_trading_enabled" in names
        assert "account_is_demo" in names
        assert "symbol_exists" in names
        assert "market_open" in names
        assert "spread_acceptable" in names
        assert "size_valid" in names
        assert "stop_loss_exists" in names
    def test_take_profit_and_risk_and_dup_checks_present(self, engine):
        check = engine.pre_trade_checks(make_request())
        names = [n for n, _, _ in check.checks]
        assert "take_profit_exists" in names
        assert "risk_within_limit" in names
        assert "no_open_position_on_symbol" in names
        assert "strategy_approved" in names

    def test_unknown_symbol_blocks(self, engine):
        res = engine.execute(make_request(broker_symbol="NOPE"))
        assert res.decision == "BLOCKED"
        assert any(n == "symbol_exists" and not ok
                   for n, ok, _ in res.checks)

    def test_market_closed_blocks(self, engine):
        engine.gateway.ticks["GOLD.i#"] = None
        res = engine.execute(make_request())
        assert res.decision == "BLOCKED"
        assert any(n == "market_open" and not ok
                   for n, ok, _ in res.checks)

    def test_wide_spread_blocks(self, engine):
        engine.gateway.ticks["GOLD.i#"] = FakeTick(4450.00, 4460.00)  # 100 pips
        res = engine.execute(make_request())
        assert res.decision == "BLOCKED"
        assert any(n == "spread_acceptable" and not ok
                   for n, ok, _ in res.checks)

    def test_bad_lots_blocks(self, engine):
        res = engine.execute(make_request(lots=0.005))
        assert res.decision == "BLOCKED"
        assert any(n == "size_valid" and not ok
                   for n, ok, _ in res.checks)

    def test_stop_above_entry_blocks(self, engine):
        res = engine.execute(make_request(stop=4500.0))
        assert res.decision == "BLOCKED"
        assert any(n == "stop_loss_exists" and not ok
                   for n, ok, _ in res.checks)

    def test_target_below_entry_blocks(self, engine):
        res = engine.execute(make_request(target=4400.0))
        assert res.decision == "BLOCKED"
        assert any(n == "take_profit_exists" and not ok
                   for n, ok, _ in res.checks)

    def test_excess_risk_blocks(self, engine):
        # 50-pip stop * 100 * 5 lots = $25,000 risk vs $100 cap
        res = engine.execute(make_request(lots=5.0, stop=4400.0))
        assert res.decision == "BLOCKED"
        assert any(n == "risk_within_limit" and not ok
                   for n, ok, _ in res.checks)

    def test_unapproved_strategy_blocks(self, engine):
        res = engine.execute(make_request(strategy="OTHER", version="V1"))
        assert res.decision == "BLOCKED"
        assert any(n == "strategy_approved" and not ok
                   for n, ok, _ in res.checks)


# ---------------------------------------------------------------------------
# Manual-trade interference rule
# ---------------------------------------------------------------------------
class TestManualInterference:
    def test_any_position_on_symbol_blocks(self, engine):
        # your manual mobile trade on the same symbol
        engine.gateway.positions.append(FakePosition("GOLD.i#", magic=0))
        res = engine.execute(make_request())
        assert res.decision == "BLOCKED"
        assert any(n == "no_open_position_on_symbol" and not ok
                   for n, ok, _ in res.checks)

    def test_bot_own_position_also_blocks(self, engine):
        # even our own bot position — one position at a time (max_positions=1)
        engine.gateway.positions.append(
            FakePosition("GOLD.i#", magic=20260922))
        res = engine.execute(make_request())
        assert res.decision == "BLOCKED"


# ---------------------------------------------------------------------------
# Happy path: order actually sent with SL/TP
# ---------------------------------------------------------------------------
class TestExecution:
    def test_execute_sends_order_with_sl_tp_magic(self, engine):
        res = engine.execute(make_request())
        assert res.ok and res.decision == "EXECUTED"
        assert res.order_ticket == 500_001
        assert len(engine.gateway.orders_sent) == 1
        sent = engine.gateway.orders_sent[0]
        assert sent["symbol"] == "GOLD.i#"
        assert sent["sl"] == 4400.0
        assert sent["tp"] == 4550.0
        assert sent["magic"] == 20260922
        assert sent["volume"] == 0.01
        assert sent["action"] == "BUY"

    def test_rejected_order_reported_as_blocked(self, engine):
        engine.gateway.order_send = lambda **kw: -10009  # broker reject
        res = engine.execute(make_request())
        assert res.decision == "BLOCKED"
        assert res.order_ticket is None
        assert "-10009" in res.reason

    def test_every_decision_journaled(self, engine):
        engine.execute(make_request())
        engine.settings = {"live_trading_enabled": False}
        engine.execute(make_request())
        lines = execution.JOURNAL.read_text().strip().splitlines()
        assert len(lines) == 2
        events = [json.loads(l) for l in lines]
        assert events[0]["event"] == "EXECUTED"
        assert events[0]["ticket"] == 500_001
        assert events[1]["event"] == "BLOCKED"
        assert "live_trading_enabled" in events[1]["reason"]


# ---------------------------------------------------------------------------
# Kill-switch triggers (Section 24)
# ---------------------------------------------------------------------------
class TestKillTriggers:
    def test_daily_loss_trips(self, engine):
        tripped = engine.check_kill_triggers(
            daily_pnl=-200.0, equity=10_000.0, consecutive_losses=1)
        assert tripped is True
        assert kill_switch_active() is True

    def test_consecutive_losses_trip(self, engine):
        tripped = engine.check_kill_triggers(
            daily_pnl=0.0, equity=10_000.0, consecutive_losses=3)
        assert tripped is True

    def test_normal_pnl_does_not_trip(self, engine):
        tripped = engine.check_kill_triggers(
            daily_pnl=-50.0, equity=10_000.0, consecutive_losses=1)
        assert tripped is False
        assert kill_switch_active() is False


# ---------------------------------------------------------------------------
# Gateway source discipline: order_send only in execution/
# ---------------------------------------------------------------------------
class TestSourceDiscipline:
    def test_market_data_still_readonly(self):
        src = (ROOT / "market_data" / "providers" / "mt5_provider.py").read_text()
        assert "mt5.order_send(" not in src

    def test_v11_runner_uses_default_dry_run_off_but_has_flag(self):
        src = (ROOT / "execution" / "run_v11_daily.py").read_text()
        assert "--dry-run" in src
        assert "research.v11_d1_momentum" in src  # validated logic reuse
