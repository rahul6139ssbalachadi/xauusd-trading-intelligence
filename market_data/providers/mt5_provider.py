from __future__ import annotations

from datetime import date, datetime, timezone

import MetaTrader5 as mt5
import pandas as pd

from market_data import config as cfg
from market_data.base import OHLCV_COLUMNS, MarketDataProvider, Timeframe

# This module is READ-ONLY by hard rule. The only MetaTrader5 module
# functions it may call are:
#   initialize, shutdown, last_error   -- connection lifecycle
#   account_info                       -- identity guard (connect())
#   symbol_info                        -- symbol reconciliation / data
#   copy_rates_range                   -- OHLCV data
# order_send, order_check, positions_get, positions_total, and every
# other trading/execution function must NEVER be called from this
# module. There is no live-execution path here at all -- that is
# Phase 14, explicitly out of scope and disabled by default.

_MT5_TIMEFRAME = {
    Timeframe.M1: mt5.TIMEFRAME_M1,
    Timeframe.M5: mt5.TIMEFRAME_M5,
    Timeframe.M15: mt5.TIMEFRAME_M15,
    Timeframe.M30: mt5.TIMEFRAME_M30,
    Timeframe.H1: mt5.TIMEFRAME_H1,
    Timeframe.H4: mt5.TIMEFRAME_H4,
    Timeframe.D1: mt5.TIMEFRAME_D1,
}


class MT5AccountGuardError(RuntimeError):
    """Raised when connecting fails, or the connected MT5 account fails
    a hard safety guard (wrong login, not a demo account). Never caught
    silently -- callers must not proceed past this."""


class MT5Provider(MarketDataProvider):
    """Read-only MarketDataProvider backed by a live MT5 terminal.

    Hard guards, non-negotiable:
    - connect() always passes an explicit terminal_path to
      mt5.initialize(). Never auto-detected -- this machine has
      multiple MT5 terminal installs and auto-detection could silently
      attach to the wrong one.
    - connect() refuses to proceed unless the connected account is a
      DEMO account (trade_mode == ACCOUNT_TRADE_MODE_DEMO) AND its
      login exactly matches allowed_login. Either mismatch raises
      MT5AccountGuardError and shuts the connection back down.
    - No data call will run before connect() has succeeded.
    """

    source_name = "mt5"

    def __init__(
        self,
        terminal_path: str,
        allowed_login: int,
        symbol_map: dict[str, str] | None = None,
    ):
        self.terminal_path = terminal_path
        self.allowed_login = allowed_login
        self.symbol_map = symbol_map or {}
        self._connected = False

    @classmethod
    def from_config(cls) -> "MT5Provider":
        """Build an MT5Provider from config/mt5.toml (terminal_path,
        allowed_login) and config/symbols.toml (xm symbol mappings)."""
        mt5_cfg = cfg.load_mt5_config()
        symbols = cfg.load_symbols()["symbols"]
        symbol_map = {
            canonical: entry["xm"]
            for canonical, entry in symbols.items()
            if "xm" in entry
        }
        return cls(
            terminal_path=mt5_cfg["terminal_path"],
            allowed_login=mt5_cfg["allowed_login"],
            symbol_map=symbol_map,
        )

    def connect(self) -> None:
        """Initialize the MT5 connection and verify hard guards. Must
        succeed before any data call. Raises MT5AccountGuardError (and
        shuts the connection back down) if any guard fails."""
        if self._connected:
            return

        if not mt5.initialize(path=self.terminal_path):
            code, desc = mt5.last_error()
            raise MT5AccountGuardError(
                f"mt5.initialize(path={self.terminal_path!r}) failed: [{code}] {desc}"
            )

        account = mt5.account_info()
        if account is None:
            mt5.shutdown()
            raise MT5AccountGuardError(
                "mt5.account_info() returned None after initialize() -- "
                "no account is logged into this terminal"
            )

        if account.trade_mode != mt5.ACCOUNT_TRADE_MODE_DEMO:
            mt5.shutdown()
            raise MT5AccountGuardError(
                f"connected account trade_mode={account.trade_mode} is not "
                f"ACCOUNT_TRADE_MODE_DEMO ({mt5.ACCOUNT_TRADE_MODE_DEMO}) -- "
                f"refusing to proceed against a non-demo account"
            )

        if account.login != self.allowed_login:
            mt5.shutdown()
            raise MT5AccountGuardError(
                f"connected account login={account.login} does not match "
                f"allowed_login={self.allowed_login} from config/mt5.toml -- "
                f"refusing to proceed"
            )

        self._connected = True

    def close(self) -> None:
        if self._connected:
            mt5.shutdown()
            self._connected = False

    def __enter__(self) -> "MT5Provider":
        self.connect()
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def _require_connected(self) -> None:
        if not self._connected:
            raise MT5AccountGuardError(
                "MT5Provider is not connected -- call connect() (or use "
                "it as a context manager) before any data call"
            )

    def available_symbols(self) -> list[str]:
        return sorted(self.symbol_map.keys())

    def reconcile_symbols(self) -> dict[str, dict]:
        """Check every canonical symbol's configured MT5 name against
        the real terminal symbol list. Returns, per canonical symbol,
        whether the configured name actually exists and is visible.

        This is the Section-31 evidence step: configured xm names in
        config/symbols.toml (only GOLD.i is confirmed; others,
        including the "GOLD.i#" in config/mt5.toml, are unverified)
        must never be trusted without checking them against the real
        terminal first.
        """
        self._require_connected()
        report = {}
        for canonical, mt5_name in self.symbol_map.items():
            info = mt5.symbol_info(mt5_name)
            report[canonical] = {
                "configured_name": mt5_name,
                "exists": info is not None,
                "visible": bool(info.visible) if info is not None else False,
            }
        return report

    def get_ohlcv(
        self,
        symbol: str,
        timeframe: Timeframe,
        start: date | None = None,
        end: date | None = None,
    ) -> pd.DataFrame:
        self._require_connected()

        mt5_symbol = self.symbol_map.get(symbol)
        if mt5_symbol is None:
            raise ValueError(
                f"{symbol!r} is not mapped for source 'mt5' (check config/symbols.toml)"
            )

        info = mt5.symbol_info(mt5_symbol)
        if info is None:
            raise ValueError(
                f"MT5 has no symbol named {mt5_symbol!r} (configured for "
                f"canonical {symbol!r}) -- run reconcile_symbols() first"
            )

        mt5_tf = _MT5_TIMEFRAME[timeframe]
        start_dt = (
            datetime.combine(start, datetime.min.time(), tzinfo=timezone.utc)
            if start is not None
            else datetime(2000, 1, 1, tzinfo=timezone.utc)
        )
        end_dt = (
            datetime.combine(end, datetime.min.time(), tzinfo=timezone.utc)
            if end is not None
            else datetime.now(timezone.utc)
        )

        rates = mt5.copy_rates_range(mt5_symbol, mt5_tf, start_dt, end_dt)
        if rates is None or len(rates) == 0:
            return pd.DataFrame(columns=OHLCV_COLUMNS)

        raw = pd.DataFrame(rates)
        out = pd.DataFrame(
            {
                "timestamp": pd.to_datetime(raw["time"], unit="s", utc=True),
                "open": raw["open"],
                "high": raw["high"],
                "low": raw["low"],
                "close": raw["close"],
                "volume": raw["tick_volume"],
            }
        )
        return out[OHLCV_COLUMNS]
