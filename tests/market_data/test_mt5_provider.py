from __future__ import annotations

import inspect

import pandas as pd
import pytest

from market_data.base import Timeframe
from market_data.providers import mt5_provider as mp
from market_data.providers.mt5_provider import MT5AccountGuardError, MT5Provider


class FakeAccount:
    def __init__(self, login: int, trade_mode: int):
        self.login = login
        self.trade_mode = trade_mode


class FakeSymbolInfo:
    def __init__(self, visible: bool = True):
        self.visible = visible


class FakeMT5:
    """Stand-in for the MetaTrader5 module -- no real terminal needed.
    Only implements the calls MT5Provider is allowed to make."""

    ACCOUNT_TRADE_MODE_DEMO = 0
    ACCOUNT_TRADE_MODE_REAL = 2

    def __init__(self):
        self.initialize_result = True
        self.initialize_calls: list[str | None] = []
        self.shutdown_called = False
        self.account = FakeAccount(login=555, trade_mode=self.ACCOUNT_TRADE_MODE_DEMO)
        self.symbols: dict[str, FakeSymbolInfo] = {}
        self.rates: dict[str, list[dict] | None] = {}

    def initialize(self, path=None):
        self.initialize_calls.append(path)
        return self.initialize_result

    def last_error(self):
        return (1, "fake init failure")

    def shutdown(self):
        self.shutdown_called = True

    def account_info(self):
        return self.account

    def symbol_info(self, name):
        return self.symbols.get(name)

    def copy_rates_range(self, symbol, timeframe, start, end):
        return self.rates.get(symbol)


@pytest.fixture
def fake_mt5(monkeypatch):
    fake = FakeMT5()
    monkeypatch.setattr(mp, "mt5", fake)
    return fake


@pytest.fixture
def provider(fake_mt5):
    return MT5Provider(
        terminal_path=r"C:\Program Files\XM Global MT5\terminal64.exe",
        allowed_login=555,
        symbol_map={"XAUUSD": "GOLD.i", "EURUSD": "EURUSD.i"},
    )


# ---------------------------------------------------------------------------
# connect() guards
# ---------------------------------------------------------------------------


def test_connect_passes_explicit_terminal_path(provider, fake_mt5):
    provider.connect()
    assert fake_mt5.initialize_calls == [r"C:\Program Files\XM Global MT5\terminal64.exe"]
    assert provider._connected


def test_connect_raises_when_initialize_fails(provider, fake_mt5):
    fake_mt5.initialize_result = False
    with pytest.raises(MT5AccountGuardError, match="initialize"):
        provider.connect()
    assert not provider._connected


def test_connect_raises_and_shuts_down_when_no_account(provider, fake_mt5):
    fake_mt5.account = None
    with pytest.raises(MT5AccountGuardError, match="account_info"):
        provider.connect()
    assert fake_mt5.shutdown_called
    assert not provider._connected


def test_connect_raises_and_shuts_down_when_not_demo(provider, fake_mt5):
    fake_mt5.account = FakeAccount(login=555, trade_mode=fake_mt5.ACCOUNT_TRADE_MODE_REAL)
    with pytest.raises(MT5AccountGuardError, match="not.*DEMO|ACCOUNT_TRADE_MODE_DEMO"):
        provider.connect()
    assert fake_mt5.shutdown_called
    assert not provider._connected


def test_connect_raises_and_shuts_down_when_login_mismatch(provider, fake_mt5):
    fake_mt5.account = FakeAccount(login=999999, trade_mode=fake_mt5.ACCOUNT_TRADE_MODE_DEMO)
    with pytest.raises(MT5AccountGuardError, match="does not match"):
        provider.connect()
    assert fake_mt5.shutdown_called
    assert not provider._connected


def test_connect_is_idempotent(provider, fake_mt5):
    provider.connect()
    provider.connect()
    assert len(fake_mt5.initialize_calls) == 1


def test_context_manager_connects_and_closes(provider, fake_mt5):
    with provider as p:
        assert p._connected
    assert not provider._connected
    assert fake_mt5.shutdown_called


def test_close_without_connect_does_not_call_shutdown(provider, fake_mt5):
    provider.close()
    assert not fake_mt5.shutdown_called


# ---------------------------------------------------------------------------
# data calls require connect() first
# ---------------------------------------------------------------------------


def test_get_ohlcv_before_connect_raises(provider):
    with pytest.raises(MT5AccountGuardError, match="not connected"):
        provider.get_ohlcv("XAUUSD", Timeframe.H1)


def test_reconcile_symbols_before_connect_raises(provider):
    with pytest.raises(MT5AccountGuardError, match="not connected"):
        provider.reconcile_symbols()


# ---------------------------------------------------------------------------
# reconcile_symbols
# ---------------------------------------------------------------------------


def test_reconcile_symbols_reports_existing_and_missing(provider, fake_mt5):
    fake_mt5.symbols["GOLD.i"] = FakeSymbolInfo(visible=True)
    # EURUSD.i deliberately left absent -- unconfirmed mapping per symbols.toml.
    provider.connect()
    report = provider.reconcile_symbols()
    assert report["XAUUSD"] == {"configured_name": "GOLD.i", "exists": True, "visible": True}
    assert report["EURUSD"] == {"configured_name": "EURUSD.i", "exists": False, "visible": False}


# ---------------------------------------------------------------------------
# get_ohlcv
# ---------------------------------------------------------------------------


def test_get_ohlcv_unmapped_symbol_raises(provider, fake_mt5):
    provider.connect()
    with pytest.raises(ValueError, match="not mapped"):
        provider.get_ohlcv("GBPUSD", Timeframe.H1)


def test_get_ohlcv_unknown_mt5_symbol_raises(provider, fake_mt5):
    provider.connect()
    # symbol_info() returns None for GOLD.i -- not registered on this fake terminal.
    with pytest.raises(ValueError, match="reconcile_symbols"):
        provider.get_ohlcv("XAUUSD", Timeframe.H1)


def test_get_ohlcv_maps_rates_to_standard_schema(provider, fake_mt5):
    fake_mt5.symbols["GOLD.i"] = FakeSymbolInfo(visible=True)
    fake_mt5.rates["GOLD.i"] = [
        {"time": 1704067200, "open": 2050.0, "high": 2051.0, "low": 2049.5, "close": 2050.5, "tick_volume": 120},
        {"time": 1704070800, "open": 2050.5, "high": 2052.0, "low": 2050.0, "close": 2051.8, "tick_volume": 131},
    ]
    provider.connect()
    df = provider.get_ohlcv("XAUUSD", Timeframe.H1)
    assert list(df.columns) == ["timestamp", "open", "high", "low", "close", "volume"]
    assert len(df) == 2
    assert df.iloc[0]["timestamp"] == pd.Timestamp("2024-01-01 00:00:00", tz="UTC")
    assert df.iloc[0]["close"] == pytest.approx(2050.5)
    assert df.iloc[0]["volume"] == 120


def test_get_ohlcv_no_rates_returns_empty_standard_schema(provider, fake_mt5):
    fake_mt5.symbols["GOLD.i"] = FakeSymbolInfo(visible=True)
    fake_mt5.rates["GOLD.i"] = None
    provider.connect()
    df = provider.get_ohlcv("XAUUSD", Timeframe.H1)
    assert list(df.columns) == ["timestamp", "open", "high", "low", "close", "volume"]
    assert df.empty


# ---------------------------------------------------------------------------
# hard guard: forbidden trading/execution calls must never appear in this module
# ---------------------------------------------------------------------------


def test_module_source_never_calls_trading_functions():
    # Check actual call sites (mt5.<fn>(), not prose in comments/docstrings
    # that document the guard itself).
    source = inspect.getsource(mp)
    forbidden_calls = [
        "mt5.order_send(",
        "mt5.order_check(",
        "mt5.order_calc_margin(",
        "mt5.order_calc_profit(",
        "mt5.positions_get(",
        "mt5.positions_total(",
        "mt5.position_get(",
        "mt5.trade_send(",
    ]
    for call in forbidden_calls:
        assert call not in source, f"forbidden trading call {call!r} found in mt5_provider.py"
