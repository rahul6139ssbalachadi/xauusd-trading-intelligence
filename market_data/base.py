from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date
from enum import Enum

import pandas as pd


class Timeframe(str, Enum):
    M1 = "M1"
    M5 = "M5"
    M15 = "M15"
    M30 = "M30"
    H1 = "H1"
    H4 = "H4"
    D1 = "D1"


# Standardized OHLCV schema every provider must return, regardless of
# source. Timestamp is UTC, tz-aware, ascending, deduplicated.
OHLCV_COLUMNS = ["timestamp", "open", "high", "low", "close", "volume"]


class MarketDataProvider(ABC):
    """Common interface for all market data sources (Section 5).

    Every implementation must return the same schema (OHLCV_COLUMNS)
    regardless of how the underlying source is structured, so
    downstream code never needs to know which provider produced a
    given DataFrame.
    """

    source_name: str

    @abstractmethod
    def get_ohlcv(
        self,
        symbol: str,
        timeframe: Timeframe,
        start: date | None = None,
        end: date | None = None,
    ) -> pd.DataFrame:
        """Return OHLCV data for `symbol` at `timeframe`.

        `symbol` is always the canonical name from config/symbols.yaml;
        the provider is responsible for translating it to its own
        naming convention internally.
        """
        raise NotImplementedError

    @abstractmethod
    def available_symbols(self) -> list[str]:
        """Canonical symbol names this provider instance can serve."""
        raise NotImplementedError
