"""Tests for execution/mt5_reader.py and execution/read_service.py.

The MT5 Python API is stubbed throughout — these tests never touch a real
terminal. What they pin is the behaviour that matters when the broker
disappears at 3am: the loop must report stale data rather than serve it
as if it were live, and it must never touch a forbidden account.
"""
from __future__ import annotations

import json
import sqlite3
import types
from pathlib import Path

import pytest

from execution.mt5_reader import MT5Reader, ReadHealth
from execution.mt5_gateway import FORBIDDEN_LOGINS

SHARK = 900909716957


class _Acc:
    def __init__(self, login, trade_mode=0, balance=1000.0, equity=1000.0):
        self.login = login
        self.trade_mode = trade_mode
        self.balance = balance
        self.equity = equity
        self.margin_free = 500.0
        self.server = "XMGlobal-MT5 10"
        self.company = "XM Global Limited"


class _Pos:
    def __init__(self):
        self.ticket, self.symbol, self.type, self.volume = 1, "GOLD.i#", 0, 0.1
        self.price_open, self.sl, self.tp, self.profit = 4197.0, 4180.0, 4231.0, 12.0
        self.magic, self.comment, self.time = 20260922, "v11", 1760000000


@pytest.fixture
def stub(monkeypatch):
    """Patch the MetaTrader5 module the reader imports."""
    import MetaTrader5 as mt5

    state = {"init_ok": True, "acc": _Acc(345982869), "shutdowns": 0}

    monkeypatch.setattr(mt5, "initialize", lambda path=None: state["init_ok"])
    monkeypatch.setattr(mt5, "shutdown", lambda: state.__setitem__("shutdowns", state["shutdowns"] + 1))
    monkeypatch.setattr(mt5, "account_info", lambda: state["acc"])
    monkeypatch.setattr(mt5, "last_error", lambda: (1, "stub error"))
    monkeypatch.setattr(mt5, "positions_get", lambda *a, **k: [_Pos()])
    monkeypatch.setattr(mt5, "orders_get", lambda *a, **k: [])
    monkeypatch.setattr(mt5, "history_deals_get", lambda *a, **k: None)
    monkeypatch.setattr(mt5, "symbols_get", lambda *a, **k: None)
    monkeypatch.setattr(mt5, "symbol_info", lambda *a, **k: None)
    monkeypatch.setattr(mt5, "POSITION_TYPE_BUY", 0)
    monkeypatch.setattr(mt5, "DEAL_TYPE_BUY", 0)
    monkeypatch.setattr(mt5, "DEAL_ENTRY_IN", 0)
    for name, val in [("TIMEFRAME_M1", 1), ("TIMEFRAME_M5", 5),
                      ("TIMEFRAME_M15", 15), ("TIMEFRAME_M30", 30),
                      ("TIMEFRAME_H1", 16385), ("TIMEFRAME_H4", 16388),
                      ("TIMEFRAME_D1", 16408)]:
        monkeypatch.setattr(mt5, name, val, raising=False)
    return state


def _reader(tmp_path, **kw):
    return MT5Reader(terminal_path="x", allowed_login=345982869,
                     db_path=tmp_path / "t.db", **kw)


# ------------------------------------------------------------- connection
def test_connect_succeeds_on_allowed_login(stub, tmp_path):
    r = _reader(tmp_path)
    assert r.connect() is True
    assert r.health.login == 345982869
    assert r.health.connected


def test_connect_refuses_forbidden_login(stub, tmp_path):
    """The reader must not even READ a forbidden account."""
    stub["acc"] = _Acc(SHARK)
    r = _reader(tmp_path)
    assert r.connect() is False
    assert "forbidden" in r.health.last_error.lower()
    assert stub["shutdowns"] >= 1, "must shut the terminal down"


def test_connect_refuses_a_different_login(stub, tmp_path):
    stub["acc"] = _Acc(999)
    r = _reader(tmp_path)
    assert r.connect() is False
    assert "allowed" in r.health.last_error


def test_connect_returns_false_on_initialize_failure(stub, tmp_path):
    stub["init_ok"] = False
    r = _reader(tmp_path)
    assert r.connect() is False
    assert "initialize failed" in r.health.last_error


def test_forbidden_login_constant_matches_the_owner_ruling():
    assert SHARK in FORBIDDEN_LOGINS


# ------------------------------------------------------------------ cycle
def test_cycle_survives_a_failed_connect(stub, tmp_path):
    """A down broker must not raise. It must record and carry on."""
    stub["init_ok"] = False
    r = _reader(tmp_path)
    h = r.cycle()
    assert h.connected is False
    assert h.errors >= 1
    assert h.last_error


def test_cycle_counts_positions(stub, tmp_path):
    r = _reader(tmp_path)
    h = r.cycle()
    assert h.positions == 1
    assert h.last_ok


def test_cycle_marks_stale_when_nothing_recent(stub, tmp_path):
    r = _reader(tmp_path)
    r.cycle()
    assert r.health.stale_seconds < 60          # fresh after a good cycle


def test_stale_is_true_after_a_long_gap(tmp_path):
    """The failure that caused 33 days of silent WAIT must be visible."""
    from datetime import datetime, timedelta, timezone
    h = ReadHealth(last_ok=(datetime.now(timezone.utc)
                            - timedelta(hours=20)).isoformat())
    d = h.to_dict()
    assert d["stale"] is True
    assert d["stale_seconds"] > 3600


def test_stale_is_infinite_before_the_first_success(tmp_path):
    h = ReadHealth()
    assert h.stale_seconds == float("inf")
    assert h.to_dict()["stale_seconds"] is None


def test_cycle_tolerates_a_broken_ingest(stub, tmp_path, monkeypatch):
    """One bad symbol must not kill the loop for the others."""
    r = _reader(tmp_path)
    monkeypatch.setattr(MT5Reader, "ingest",
                        lambda self, s, t, n=1000: (_ for _ in ()).throw(ValueError("nope")))
    h = r.cycle([("XAUUSD", "D1", 100)])
    assert h.last_ok, "cycle should still complete"
    assert h.connected


# ------------------------------------------------------------------- bars
def test_bars_rejects_an_unknown_timeframe(stub, tmp_path):
    r = _reader(tmp_path)
    r.connect()
    with pytest.raises(ValueError, match="timeframe"):
        r.bars("XAUUSD", "M7")


def test_ingest_writes_new_bars_and_is_idempotent(stub, tmp_path, monkeypatch):
    """Re-ingesting the same bars must not duplicate rows."""
    import numpy as np

    arr = np.array([(1760000000, 4190.0, 4200.0, 4185.0, 4197.0, 123, 25)],
                   dtype=[("time", "<i8"), ("open", "<f8"), ("high", "<f8"),
                          ("low", "<f8"), ("close", "<f8"),
                          ("tick_volume", "<i8"), ("spread", "<i8")])
    monkeypatch.setattr("MetaTrader5.copy_rates_from_pos",
                        lambda *a, **k: arr)

    db = tmp_path / "t.db"
    con = sqlite3.connect(db)
    con.execute("""CREATE TABLE market_data (
        symbol TEXT NOT NULL, timeframe TEXT NOT NULL, source TEXT NOT NULL,
        ts_broker_epoch INTEGER NOT NULL, open REAL NOT NULL, high REAL NOT NULL,
        low REAL NOT NULL, close REAL NOT NULL, tick_volume INTEGER NOT NULL,
        spread INTEGER,
        PRIMARY KEY (symbol,timeframe,source,ts_broker_epoch))""")
    con.commit()
    con.close()

    r = MT5Reader("x", 345982869, db_path=db)
    r.connect()
    assert r.ingest("XAUUSD", "D1", 10) == 1
    assert r.ingest("XAUUSD", "D1", 10) == 0, "second pass must be a no-op"

    con = sqlite3.connect(db)
    row = con.execute("SELECT open,high,low,close,tick_volume,spread "
                      "FROM market_data").fetchone()
    con.close()
    assert row == (4190.0, 4200.0, 4185.0, 4197.0, 123, 25)


def test_ingest_preserves_the_raw_broker_epoch(stub, tmp_path, monkeypatch):
    """The schema says do NOT convert. A conversion here would shift every
    bar by hours and invalidate every backtest in the project."""
    import numpy as np

    arr = np.array([(1760000000, 1.0, 2.0, 0.5, 1.5, 1, 0)],
                   dtype=[("time", "<i8"), ("open", "<f8"), ("high", "<f8"),
                          ("low", "<f8"), ("close", "<f8"),
                          ("tick_volume", "<i8"), ("spread", "<i8")])
    monkeypatch.setattr("MetaTrader5.copy_rates_from_pos", lambda *a, **k: arr)

    db = tmp_path / "t.db"
    con = sqlite3.connect(db)
    con.execute("""CREATE TABLE market_data (
        symbol TEXT NOT NULL, timeframe TEXT NOT NULL, source TEXT NOT NULL,
        ts_broker_epoch INTEGER NOT NULL, open REAL NOT NULL, high REAL NOT NULL,
        low REAL NOT NULL, close REAL NOT NULL, tick_volume INTEGER NOT NULL,
        spread INTEGER,
        PRIMARY KEY (symbol,timeframe,source,ts_broker_epoch))""")
    con.commit()
    con.close()

    r = MT5Reader("x", 345982869, db_path=db)
    r.connect()
    r.ingest("XAUUSD", "D1", 10)
    con = sqlite3.connect(db)
    ep = con.execute("SELECT ts_broker_epoch FROM market_data").fetchone()[0]
    con.close()
    assert ep == 1760000000          # raw, not shifted


# ------------------------------------------------------------- positions
def test_positions_are_normalised_dicts(stub, tmp_path):
    r = _reader(tmp_path)
    r.connect()
    p = r.positions()[0]
    assert p["type"] == "BUY"
    assert p["symbol"] == "GOLD.i#"
    assert isinstance(p["price_open"], float)


def test_no_positions_returns_empty_list(stub, tmp_path, monkeypatch):
    monkeypatch.setattr("MetaTrader5.positions_get", lambda *a, **k: None)
    r = _reader(tmp_path)
    r.connect()
    assert r.positions() == []


# --------------------------------------------------------------- service
def test_service_refuses_to_start_in_live_mode(monkeypatch):
    """Owner ruling: demo only. The service must not start otherwise."""
    from execution import read_service
    from market_data import config as cfg

    monkeypatch.setattr(cfg, "load_settings", lambda: {"trading_mode": "live"})
    rc = read_service.main([])
    assert rc == 2


def test_service_refuses_without_mt5_toml(monkeypatch, tmp_path):
    from execution import read_service
    from market_data import config as cfg

    monkeypatch.setattr(cfg, "load_settings", lambda: {"trading_mode": "demo"})

    def boom():
        raise FileNotFoundError("mt5.toml")
    monkeypatch.setattr(cfg, "load_mt5_config", boom)
    assert read_service.main([]) == 2


def test_service_status_without_health_file(monkeypatch, tmp_path):
    from execution import read_service
    monkeypatch.setattr(read_service, "HEALTH", tmp_path / "nope.json")
    assert read_service.main(["--status"]) == 1


def test_service_writes_a_health_file(stub, monkeypatch, tmp_path):
    from execution import read_service
    from market_data import config as cfg

    monkeypatch.setattr(cfg, "load_settings", lambda: {"trading_mode": "demo"})
    monkeypatch.setattr(cfg, "load_mt5_config", lambda: {
        "terminal_path": "x", "allowed_login": 345982869})
    health = tmp_path / "h.json"
    monkeypatch.setattr(read_service, "HEALTH", health)
    monkeypatch.setattr(read_service, "DEFAULT_JOBS", [])

    rc = read_service.main(["--once"])
    # build_reader() reads cfg via the module reference imported INTO
    # read_service, so patching cfg.load_mt5_config alone is not enough.
    assert rc == 0, "service must reach a connected health file"
    data = json.loads(health.read_text())
    assert data["connected"] is True
    assert data["login"] == 345982869


def test_reader_module_contains_no_order_send():
    """Structural guarantee: the read path can never trade.

    Checks the CODE, not the prose: the docstring legitimately mentions
    order_send while promising not to call it, so a naive substring test
    fails on the comment. Strip comments and docstrings first.
    """
    import ast
    import io
    import tokenize

    for name in ("mt5_reader.py", "read_service.py"):
        path = Path(__file__).resolve().parents[1] / "execution" / name
        src = path.read_text(encoding="utf-8")
        # Remove comments and string literals (which hold the docstrings).
        out = []
        for tok in tokenize.generate_tokens(io.StringIO(src).readline):
            if tok.type in (tokenize.COMMENT, tokenize.STRING):
                continue
            out.append(tok.string)
        code_only = " ".join(out)
        assert "order_send" not in code_only, f"{name} calls order_send"
        # And the module must not import the trade entrypoint at all.
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr == "order_send":
                raise AssertionError(f"{name} references mt5.order_send")
            if isinstance(node, ast.Name) and node.id == "order_send":
                raise AssertionError(f"{name} references order_send")
