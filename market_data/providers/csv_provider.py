from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd

from market_data import config as cfg
from market_data.base import OHLCV_COLUMNS, MarketDataProvider, Timeframe
from market_data.providers.source_formats import REGISTRY, SourceFormat


class CSVProvider(MarketDataProvider):
    """MarketDataProvider backed by vendor CSV exports on disk.

    Every registered source (dukascopy, histdata, and later xm_export)
    goes through this same class -- only the SourceFormat differs --
    so callers always get back the identical OHLCV_COLUMNS schema
    regardless of which vendor's raw file layout was underneath.
    """

    def __init__(self, fmt: SourceFormat, raw_dir: Path, symbol_map: dict[str, str]):
        self.fmt = fmt
        self.source_name = fmt.name
        self.raw_dir = Path(raw_dir)
        # canonical symbol -> this source's name for it
        self.symbol_map = symbol_map

    @classmethod
    def from_config(cls, source_name: str, raw_dir: Path | None = None) -> "CSVProvider":
        """Build a CSVProvider for `source_name` using config/symbols.toml
        and config/settings.toml. `raw_dir` overrides the default
        data/raw/<source_name>/ location (mainly for tests)."""
        fmt = REGISTRY[source_name]
        symbols = cfg.load_symbols()["symbols"]
        symbol_map = {
            canonical: entry[source_name]
            for canonical, entry in symbols.items()
            if source_name in entry
        }
        if raw_dir is None:
            settings = cfg.load_settings()
            raw_dir = cfg.PROJECT_ROOT / settings["paths"]["data_raw"] / source_name
        return cls(fmt=fmt, raw_dir=Path(raw_dir), symbol_map=symbol_map)

    def available_symbols(self) -> list[str]:
        return sorted(self.symbol_map.keys())

    def get_ohlcv(
        self,
        symbol: str,
        timeframe: Timeframe,
        start: date | None = None,
        end: date | None = None,
    ) -> pd.DataFrame:
        source_symbol = self.symbol_map.get(symbol)
        if source_symbol is None:
            raise ValueError(
                f"{symbol!r} is not mapped for source {self.fmt.name!r} "
                f"(check config/symbols.toml)"
            )

        tf_token = self.fmt.timeframe_tokens.get(timeframe.value, timeframe.value)
        pattern = self.fmt.filename_glob.format(symbol=source_symbol, timeframe=tf_token)
        files = sorted(self.raw_dir.glob(pattern))
        if not files:
            raise FileNotFoundError(
                f"No files matching {pattern!r} in {self.raw_dir} "
                f"(source={self.fmt.name}, symbol={symbol}, timeframe={timeframe.value})"
            )

        frames = [self._read_file(f) for f in files]
        df = pd.concat(frames, ignore_index=True)
        df = df.drop_duplicates(subset="timestamp", keep="first")
        df = df.sort_values("timestamp").reset_index(drop=True)

        if start is not None:
            df = df[df["timestamp"] >= pd.Timestamp(start, tz="UTC")]
        if end is not None:
            df = df[df["timestamp"] <= pd.Timestamp(end, tz="UTC")]

        return df.reset_index(drop=True)

    def _read_file(self, path: Path) -> pd.DataFrame:
        fmt = self.fmt
        header_row = 0 if fmt.has_header else None
        raw = pd.read_csv(path, delimiter=fmt.delimiter, header=header_row)

        out = pd.DataFrame(index=raw.index)
        for field, col in fmt.column_map.items():
            out[field] = raw[col] if fmt.has_header else raw.iloc[:, col]

        if fmt.timestamp_format:
            ts = pd.to_datetime(out["timestamp"], format=fmt.timestamp_format)
        else:
            ts = pd.to_datetime(out["timestamp"])
        if ts.dt.tz is None:
            ts = ts.dt.tz_localize(fmt.timestamp_tz)
        out["timestamp"] = ts.dt.tz_convert("UTC")

        for c in ("open", "high", "low", "close", "volume"):
            out[c] = pd.to_numeric(out[c], errors="coerce")

        return out[OHLCV_COLUMNS]
