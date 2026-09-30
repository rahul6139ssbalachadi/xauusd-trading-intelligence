"""The forbidden-account denylist must hold under every configuration.

Owner ruling 2026-09-30: account 900909716957 (SharkFunded-live, a prop
firm account that was logged into the terminal) must never receive an
order from this system.

The ruling is encoded as a hardcoded constant in execution/mt5_gateway.py
rather than in config, specifically so that editing config/mt5.toml
cannot authorise it. These tests pin that property: even if someone edits
the config so that the forbidden login is BOTH the allowed_login AND a
demo account, connect() must still refuse.
"""
from pathlib import Path

import pytest

from execution.mt5_gateway import FORBIDDEN_LOGINS, MT5Gateway, MT5GatewayError

ROOT = Path(__file__).resolve().parents[1]

# The login called out in the ruling. If this ever changes, the constant
# above must be updated in the same commit — that is the point of the test.
SHARK_LOGIN = 900909716957


def test_forbidden_login_is_on_the_denylist():
    assert SHARK_LOGIN in FORBIDDEN_LOGINS


def test_denylist_is_a_frozenset():
    """Immutable, so no runtime code can quietly add/remove entries."""
    assert isinstance(FORBIDDEN_LOGINS, frozenset)


class _Acc:
    def __init__(self, login, trade_mode=0):
        self.login = login
        self.trade_mode = trade_mode


def _patch_mt5(monkeypatch, acc):
    """Stub the MetaTrader5 module the gateway imports."""
    import MetaTrader5 as mt5

    monkeypatch.setattr(mt5, "initialize", lambda path=None: True)
    monkeypatch.setattr(mt5, "shutdown", lambda: None)
    monkeypatch.setattr(mt5, "account_info", lambda: acc)
    # Demo constant so trade_mode=0 reads as DEMO below.
    monkeypatch.setattr(mt5, "ACCOUNT_TRADE_MODE_DEMO", 0, raising=False)


def test_connect_refuses_forbidden_login_even_when_it_is_allowed(monkeypatch):
    """The key property: config cannot authorise the forbidden account.

    Here allowed_login IS the forbidden login and the account reports
    DEMO, so the demo gate and the allowed_login gate would both pass.
    The denylist must still refuse.
    """
    _patch_mt5(monkeypatch, _Acc(SHARK_LOGIN, trade_mode=0))
    gw = MT5Gateway(terminal_path="x", allowed_login=SHARK_LOGIN)
    with pytest.raises(MT5GatewayError) as e:
        gw.connect()
    assert "denylist" in str(e.value).lower()


def test_connect_shuts_terminal_down_on_denylist_hit(monkeypatch):
    """A refused connect must not leave the terminal attached."""
    import MetaTrader5 as mt5

    calls = []
    monkeypatch.setattr(mt5, "initialize", lambda path=None: True)
    monkeypatch.setattr(mt5, "shutdown", lambda: calls.append(1))
    monkeypatch.setattr(mt5, "account_info", lambda: _Acc(SHARK_LOGIN, 0))

    gw = MT5Gateway(terminal_path="x", allowed_login=SHARK_LOGIN)
    with pytest.raises(MT5GatewayError):
        gw.connect()
    assert calls, "terminal must be shut down when refusing a forbidden login"


def test_connect_still_refuses_when_login_merely_differs(monkeypatch):
    """The pre-existing login-mismatch guard must not regress."""
    _patch_mt5(monkeypatch, _Acc(111111111, trade_mode=0))
    gw = MT5Gateway(terminal_path="x", allowed_login=222222222)
    with pytest.raises(MT5GatewayError) as e:
        gw.connect()
    assert "allowed_login" in str(e.value)


def test_denylist_is_not_configurable_via_toml():
    """The constant must not be mirrored into a config file that can be
    edited to empty it. Guards against a future 'move it to config' refactor
    quietly reintroducing the risk."""
    toml = (ROOT / "config" / "mt5.toml").read_text(encoding="utf-8")
    assert "FORBIDDEN" not in toml.upper()
    assert "900909716957" not in toml
