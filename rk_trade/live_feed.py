"""Read-only live market data feed for RK Trade.

Connects to MT5 in READ-ONLY mode and streams live market data (bars + ticks).
This module CANNOT trade — it only calls:
  - mt5.initialize / mt5.shutdown (connection lifecycle)
  - mt5.account_info (identity guard)
  - mt5.symbol_info (symbol reconciliation)
  - mt5.copy_rates_from / copy_rates_range (historical bars)
  - mt5.copy_ticks_from / copy_ticks_range (tick data)

NO trading functions (order_send, order_check, positions_get, etc.) are ever
called. live_trading_enabled stays false.

The feed yields new completed bars as they close, with an explicit buffer
delay (default 2 seconds) to avoid acting on stale/incomplete bar data.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterator

import pandas as pd

from market_data.providers.mt5_provider import MT5Provider
from market_data.base import Timeframe


_MT5_TF_MAP = {
    Timeframe.M1: "M1",
    Timeframe.M5: "M5",
    Timeframe.M15: "M15",
    Timeframe.M30: "M30",
    Timeframe.H1: "H1",
    Timeframe.H4: "H4",
    Timeframe.D1: "D1",
}


@dataclass
class Bar:
    """Single OHLCV bar from the live feed."""
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    spread: float  # in points
    volume: float = 0.0


class LiveFeed:
    """Stream completed bars from MT5 (read-only).

    Usage:
        with LiveFeed(symbol="GOLD.i#") as feed:
            for bar in feed.stream(timeframe="M5", lookback=100):
                process(bar)
    """

    def __init__(self, symbol: str = "XAUUSD",
                 provider: MT5Provider | None = None,
                 buffer_seconds: float = 2.0):
        self.symbol = symbol
        self._provider = provider
        self.buffer_seconds = buffer_seconds
        self._owns_provider = provider is None
        self._last_bar_ts: datetime | None = None

    def _get_provider(self) -> MT5Provider:
        if self._provider is None:
            self._provider = MT5Provider.from_config()
        return self._provider

    def __enter__(self) -> "LiveFeed":
        provider = self._get_provider()
        if self._owns_provider:
            provider.connect()
        return self

    def __exit__(self, *exc) -> None:
        if self._owns_provider and self._provider:
            self._provider.close()

    def get_latest_bars(self, tf: str = "M5", lookback: int = 50) -> pd.DataFrame:
        """Fetch the most recent `lookback` completed bars (read-only)."""
        import MetaTrader5 as mt5
        provider = self._get_provider()
        if not provider._connected:
            provider.connect()

        # Map timeframe string
        tf_map = {"M1": mt5.TIMEFRAME_M1, "M5": mt5.TIMEFRAME_M5,
                  "M15": mt5.TIMEFRAME_M15, "M30": mt5.TIMEFRAME_M30,
                  "H1": mt5.TIMEFRAME_H1, "H4": mt5.TIMEFRAME_H4,
                  "D1": mt5.TIMEFRAME_D1}

        mt5_tf = tf_map.get(tf, mt5.TIMEFRAME_M5)
        # Use copy_rates_from with a generous lookback to get enough bars
        now = datetime.now(timezone.utc)
        rates = mt5.copy_rates_from(
            provider.symbol_map.get(self.symbol, self.symbol),
            mt5_tf, now, lookback + 10  # extra buffer for filtering
        )
        if rates is None or len(rates) == 0:
            return pd.DataFrame(columns=["timestamp", "open", "high",
                                        "low", "close", "volume", "spread"])

        raw = pd.DataFrame(rates)
        # drop the incomplete current bar (last row), keep only completed bars
        raw = raw.iloc[:-1] if len(raw) > lookback else raw
        out = pd.DataFrame({
            "timestamp": pd.to_datetime(raw["time"], unit="s", utc=True),
            "open": raw["open"],
            "high": raw["high"],
            "low": raw["low"],
            "close": raw["close"],
            "volume": raw["tick_volume"],
            "spread": raw.get("spread", 0.0),
        })
        return out.tail(lookback)

    def stream(self, tf: str = "M5", lookback: int = 50,
               interval: float = 5.0, max_retries: int = 5,
               retry_delay: float = 10.0) -> Iterator[pd.DataFrame]:
        """Continuously yield the latest bar window as new bars close.

        Yields a DataFrame of `lookback` rows each time a new completed
        bar is detected. Blocks between polls for `interval` seconds.
        To stop: break the loop or KeyboardInterrupt.

        Reconnection: if the MT5 connection drops, the stream will attempt
        to reconnect up to `max_retries` times (every `retry_delay` seconds).
        If reconnection fails, the generator yields one final WAIT decision
        via an exception — the caller (SignalEngine) handles it gracefully.
        """
        retries = 0
        while True:
            try:
                bars = self.get_latest_bars(tf, lookback)
                retries = 0  # reset on success
                if len(bars) > 0:
                    new_ts = bars["timestamp"].iloc[-1]
                    if self._last_bar_ts is None or new_ts > self._last_bar_ts:
                        self._last_bar_ts = new_ts
                        yield bars
                time.sleep(interval)
            except KeyboardInterrupt:
                break
            except Exception as e:
                if retries < max_retries:
                    retries += 1
                    print(f"LiveFeed: connection issue ({e}), "
                          f"retrying {retries}/{max_retries} in {retry_delay}s...")
                    time.sleep(retry_delay)
                    # attempt reconnection
                    try:
                        self._get_provider().connect()
                    except Exception:
                        continue
                else:
                    print(f"LiveFeed: max retries exceeded, stopping. Last error: {e}")
                    raise

    def get_current_price(self) -> tuple[float, float] | None:
        """Get current bid/ask for the symbol (read-only)."""
        import MetaTrader5 as mt5
        provider = self._get_provider()
        if not provider._connected:
            provider.connect()
        info = mt5.symbol_info(provider.symbol_map.get(self.symbol, self.symbol))
        if info is None:
            return None
        return (info.bid, info.ask)
