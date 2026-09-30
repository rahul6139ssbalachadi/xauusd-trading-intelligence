"""Production MT5 gateway for the execution engine.

The ONLY file besides execution/__init__.py that may call order_send.
It wraps the raw MetaTrader5 calls behind the same interface the tests
fake, so the engine logic is provably identical in test and production.

SAFETY: hard-guards DEMO ONLY at the gateway level too (belt and
braces) — a REAL account is refused before any order method exists.
"""
from __future__ import annotations

from typing import Any

import MetaTrader5 as mt5

from market_data import config as cfg


class MT5GatewayError(RuntimeError):
    pass


# Accounts that must NEVER receive an order from this system, under any
# configuration. Owner ruling 2026-09-30. This is deliberately a hardcoded
# constant rather than config, so that editing config/mt5.toml cannot
# authorise it: connect() checks this list before the demo and allowed_login
# gates and shuts the terminal down on a match.
FORBIDDEN_LOGINS: frozenset[int] = frozenset({
    900909716957,  # SharkFunded-live (Shark Funded Ltd.) — prop firm, not ours
})


class MT5Gateway:
    """Thin, explicit wrapper. Every method maps 1:1 to an MT5 call."""

    ACCOUNT_TRADE_MODE_DEMO = mt5.ACCOUNT_TRADE_MODE_DEMO

    def __init__(self, terminal_path: str, allowed_login: int):
        self.terminal_path = terminal_path
        self.allowed_login = allowed_login
        self._connected = False

    def connect(self) -> None:
        if self._connected:
            return
        if not mt5.initialize(path=self.terminal_path):
            code, desc = mt5.last_error()
            raise MT5GatewayError(f"mt5.initialize failed: [{code}] {desc}")

        acc = mt5.account_info()
        if acc is None:
            mt5.shutdown()
            raise MT5GatewayError("account_info() returned None")

        # Hard denylist, checked BEFORE the demo/login gates so that a
        # forbidden account can never reach an order even if
        # allowed_login in config/mt5.toml is later mis-edited to it.
        # Owner ruling 2026-09-30: never place an order on this login.
        if acc.login in FORBIDDEN_LOGINS:
            mt5.shutdown()
            raise MT5GatewayError(
                f"REFUSED: login {acc.login} is on the permanent denylist "
                f"and must never be traded. See FORBIDDEN_LOGINS.")

        if acc.trade_mode != mt5.ACCOUNT_TRADE_MODE_DEMO:
            mt5.shutdown()
            raise MT5GatewayError(
                f"REFUSED: account {acc.login} is not a DEMO account "
                f"(trade_mode={acc.trade_mode}). Live execution is "
                f"demo-only by hard rule.")
        if acc.login != self.allowed_login:
            mt5.shutdown()
            raise MT5GatewayError(
                f"REFUSED: login {acc.login} != allowed_login {self.allowed_login}")
        self._connected = True

    def disconnect(self) -> None:
        if self._connected:
            mt5.shutdown()
            self._connected = False

    # -- read side (used by the checklist) ----------------------------------
    def account_info(self) -> Any:
        return mt5.account_info()

    def symbol_info(self, symbol: str) -> Any:
        return mt5.symbol_info(symbol)

    def symbol_info_tick(self, symbol: str) -> Any:
        return mt5.symbol_info_tick(symbol)

    def positions_get(self, symbol: str | None = None) -> tuple | None:
        return mt5.positions_get(symbol=symbol)

    def copy_rates(self, symbol: str, timeframe: int, count: int):
        """Read-only: last `count` bars for symbol, oldest -> newest.
        Mirrors mt5.copy_rates_from_pos 1:1."""
        if not self._connected:
            raise MT5GatewayError("gateway not connected")
        return mt5.copy_rates_from_pos(symbol, timeframe, 0, count)

    # -- write side (the only order path in the repo) ------------------------
    def order_send(self, symbol: str, action: str, volume: float,
                   sl: float, tp: float, magic: int, comment: str = "") -> int | None:
        """Send ONE market order with SL/TP. Returns the ticket, or None
        on failure (rejection details are logged by the engine)."""
        if not self._connected:
            raise MT5GatewayError("gateway not connected")

        direction = {"BUY": mt5.ORDER_TYPE_BUY, "SELL": mt5.ORDER_TYPE_SELL}[action]
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": volume,
            "type": direction,
            "price": 0.0,  # market — broker fills at current
            "sl": round(sl, 2),
            "tp": round(tp, 2),
            "magic": magic,
            "comment": comment[:31],
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        result = mt5.order_send(request)
        if result is None:
            return None
        if result.retcode != mt5.TRADE_RETCODE_DONE:
            return -result.retcode  # negative = rejected, engine blocks
        return result.order
