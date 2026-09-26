"""Historical data access — READ-ONLY.

Source: db/trading.db, the same SQLite store every other research script
in this repo reads. Opened with `mode=ro` so a bug here cannot write.

Provenance (documented per the spec, re-derived from the DB at run time
rather than hardcoded):

  DATA SOURCE     : MT5 demo terminal (XMGlobal-MT5, login 345982869)
                    via market_data/providers/mt5_provider.py -> SQLite
  SYMBOL          : canonical XAUUSD / BTCUSD
  TIMEFRAME       : D1 for V11, H1 for V12
  TIMEZONE        : broker server time = UTC+3 (EEST). The stored epoch is
                    BROKER time, not UTC. `ts` keeps the raw epoch; every
                    month bucket uses `ts_broker`.
  GRANULARITY     : OHLC bars + recorded tick_volume + recorded per-bar
                    spread (in POINTS). No tick-level replay is available,
                    so intra-bar sequence is unknown — see LIMITATIONS.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "db" / "trading.db"

#: Broker server offset from UTC in hours. The DB stores broker-server
#: epochs (verified live against symbol_info_tick in Aug 2026). Summer
#: = UTC+3 (EEST), winter = UTC+2 (EET); we use the summer value
#: consistently, which is a documented assumption, not a fact.
BROKER_UTC_OFFSET_H = 3

#: Per-instrument unit model. `pip` is the price move that "one pip" means
#: for that instrument; `contract` is USD per 1.00 of price move per 1.0
#: lot; `usd_per_pip_per_lot` is USD P&L per pip per lot. Gold's row is the
#: repo-wide convention (risk.CONTRACT_MULTIPLIER = 100, PIP = 0.10); BTC's
#: is the convention established in research/btc_v11_v12_study.py.
UNITS = {
    "XAUUSD": {"pip": 0.10, "point": 0.01, "contract": 100.0,
               "usd_per_pip_per_lot": 10.0, "digits": 2},
    "BTCUSD": {"pip": 1.00, "point": 0.01, "contract": 1.0,
               "usd_per_pip_per_lot": 1.0, "digits": 2},
}


def _connect() -> sqlite3.Connection:
    """Read-only connection. `mode=ro` makes any write raise."""
    con = sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True)
    return con


def available(symbols=("XAUUSD", "BTCUSD")) -> pd.DataFrame:
    """What the DB actually holds — drives every 'no data' message."""
    with _connect() as con:
        return pd.read_sql_query(
            "SELECT symbol, timeframe, COUNT(*) bars, "
            "MIN(ts_broker_epoch) lo, MAX(ts_broker_epoch) hi "
            "FROM market_data WHERE source='mt5' GROUP BY symbol, timeframe",
            con)


def load(symbol: str, timeframe: str) -> pd.DataFrame:
    """Load full OHLCV history for one symbol/timeframe, oldest first.

    Returns a frame with `ts` (UTC-labelled raw epoch, as stored),
    `ts_broker` (the same instant + 3h, used for month/year bucketing),
    open/high/low/close, tick_volume and spread (points).
    """
    if symbol not in UNITS:
        raise ValueError(f"unknown symbol {symbol!r}; have {sorted(UNITS)}")
    with _connect() as con:
        df = pd.read_sql_query(
            "SELECT ts_broker_epoch, open, high, low, close, tick_volume, spread "
            "FROM market_data WHERE symbol=? AND timeframe=? AND source='mt5' "
            "ORDER BY ts_broker_epoch",
            con, params=(symbol, timeframe))
    if df.empty:
        raise ValueError(f"no {timeframe} rows for {symbol} in {DB.name}")
    df["ts"] = pd.to_datetime(df["ts_broker_epoch"], unit="s", utc=True)
    df["ts_broker"] = df["ts"] + pd.Timedelta(hours=BROKER_UTC_OFFSET_H)
    return df.reset_index(drop=True)


def provenance(symbol: str, timeframe: str) -> dict:
    """Data-provenance record embedded in every backtest run (spec 4/14)."""
    df = load(symbol, timeframe)
    u = UNITS[symbol]
    return {
        "data_source": "db/trading.db (SQLite) <- MT5 demo, XMGlobal-MT5 "
                       "login 345982869, via market_data MT5Provider",
        "symbol": symbol,
        "timeframe": timeframe,
        "timezone": f"broker server time = UTC+{BROKER_UTC_OFFSET_H} "
                    "(assumption: summer EEST applied year-round)",
        "date_range": f"{df['ts_broker'].iloc[0]} .. {df['ts_broker'].iloc[-1]}",
        "bars": int(len(df)),
        "granularity": "OHLC bars; tick_volume and per-bar spread (points) "
                       "recorded. No tick-level replay available.",
        "mean_spread": float((df["spread"] * u["point"]).mean()),
        "pip": u["pip"],
        "db_md5_note": "data is not modified by this module (opened mode=ro)",
    }
