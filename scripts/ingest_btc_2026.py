"""Ingest BTCUSD 2026 bars from the MT5 terminal into db/trading.db.

STRICTLY READ-ONLY WITH RESPECT TO TRADING: this only calls copy_rates*,
never order_send / positions_get. It writes rows to the research DB, which
is the same read-only store every other research script uses.

Symbol: BTCUSD# on XMGlobal-MT5 (confirmed via symbol_info; the broker also
offers BTCGBP#, BTCEUR#, ETHBTC# — those quote 0.00 and have no data, so
BTCUSD# is the only viable BTC instrument on this account).

History caveat observed on this account: copy_rates_from_pos / copy_rates
with count=0 return None ("Terminal: Call failed") until a bounded
copy_rates_range call has forced the terminal to download the history.
This script therefore issues one range request per timeframe FIRST, then
reads. Do not "optimise" that away — it was the only way to get bars.

Idempotent: re-running replaces the same symbol/timeframe rows.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from market_data import config as cfg

SYMBOL = "BTCUSD#"          # broker name
CANON = "BTCUSD"            # canonical name stored in the DB
TF = {"D1": "TIMEFRAME_D1", "H1": "TIMEFRAME_H1",
      "M15": "TIMEFRAME_M15", "H4": "TIMEFRAME_H4"}


def _mt5():
    import MetaTrader5 as mt5
    c = cfg.load_mt5_config()
    if not mt5.initialize(path=c["terminal_path"]):
        raise SystemExit(f"MT5 init failed: {mt5.last_error()}")
    if not mt5.symbol_select(SYMBOL, True):
        raise SystemExit(f"symbol_select failed: {mt5.last_error()}")
    return mt5


def fetch(mt5, label: str, start: datetime, end: datetime) -> pd.DataFrame:
    """Range request (forces history download) + a positional read."""
    tf = getattr(mt5, TF[label])
    fr, to = int(start.timestamp()), int(end.timestamp())

    rates = mt5.copy_rates_range(SYMBOL, tf, fr, to)
    if rates is not None and len(rates):
        df = pd.DataFrame(rates)
        return _finish(df)

    # Fall back to a positional read once the range primed the cache.
    pos = mt5.copy_rates_from_pos(SYMBOL, tf, 0, 20000)
    if pos is None or not len(pos):
        raise SystemExit(f"{label}: no rates returned "
                         f"(err={mt5.last_error()})")
    df = pd.DataFrame(pos)
    return _finish(df[df["time"] >= fr])


def _finish(df: pd.DataFrame) -> pd.DataFrame:
    df = df.rename(columns={"time": "ts_broker_epoch"})
    df["ts"] = pd.to_datetime(df["ts_broker_epoch"], unit="s", utc=True)
    return df[["ts_broker_epoch", "ts", "open", "high", "low", "close",
               "tick_volume", "spread"]].sort_values(
        "ts_broker_epoch").reset_index(drop=True)


def quality(df: pd.DataFrame, label: str) -> dict:
    """Data-quality report. Does NOT repair anything — it reports."""
    dts = df["ts_broker_epoch"].diff().dropna()
    mode = dts.mode().iloc[0] if len(dts) else 0
    ohlc_bad = int(((df["high"] < df[["open", "close", "low"]].max(axis=1)) |
                    (df["low"] > df[["open", "close", "high"]].min(axis=1))
                    ).sum())
    return {
        "timeframe": label,
        "bars": len(df),
        "start": str(df["ts"].iloc[0]),
        "end": str(df["ts"].iloc[-1]),
        "duplicates": int(df["ts_broker_epoch"].duplicated().sum()),
        "irregular_gaps": int((dts != mode).sum()) if mode else 0,
        "gaps_gt_2x_mode": int((dts > 2 * mode).sum()) if mode else 0,
        "ohlc_violations": ohlc_bad,
        "null_close": int(df["close"].isna().sum()),
        "min_close": float(df["close"].min()),
        "max_close": float(df["close"].max()),
        "mean_spread_pts": float(df["spread"].mean()) if "spread" in df else 0.0,
    }


def ingest(db_path: Path, start: datetime, end: datetime,
           timeframes: list[str]) -> list[dict]:
    mt5 = _mt5()
    reports = []
    con = sqlite3.connect(db_path)
    for label in timeframes:
        df = fetch(mt5, label, start, end)
        rep = quality(df, label)
        reports.append(rep)
        con.execute("DELETE FROM market_data WHERE symbol=? AND timeframe=? "
                    "AND source='mt5'", (CANON, label))
        # Column names must match the table exactly: tick_volume, NOT volume.
        # The table carries strict CHECK constraints (ohlc ordering, > 0).
        con.executemany(
            "INSERT INTO market_data (symbol, timeframe, source, "
            "ts_broker_epoch, open, high, low, close, spread, tick_volume) "
            "VALUES (?,?,'mt5',?,?,?,?,?,?,?)",
            [(CANON, label, int(r.ts_broker_epoch), float(r.open),
              float(r.high), float(r.low), float(r.close), int(r.spread),
              int(getattr(r, "tick_volume", 0) or 0))
             for r in df.itertuples()])
        con.commit()
        print(f"  {label}: {len(df)} bars "
              f"{df['ts'].iloc[0].date()} -> {df['ts'].iloc[-1].date()}")
        time.sleep(2)
    con.close()
    mt5.shutdown()
    return reports


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2026-01-01")
    ap.add_argument("--end", default="")
    ap.add_argument("--tf", default="D1,H1,M15")
    ap.add_argument("--db", default="")
    args = ap.parse_args()

    start = datetime.fromisoformat(args.start).replace(tzinfo=timezone.utc)
    end = (datetime.fromisoformat(args.end).replace(tzinfo=timezone.utc)
           if args.end else datetime.now(timezone.utc))
    db = Path(args.db) if args.db else ROOT / cfg.load_settings()["paths"]["db"]
    tfs = [t.strip() for t in args.tf.split(",") if t.strip()]

    print(f"Importing {CANON} ({SYMBOL}) {start.date()} -> {end.date()}")
    print(f"  timeframes: {', '.join(tfs)}   db: {db}")
    reports = ingest(db, start, end, tfs)

    print("\nDATA QUALITY REPORT (raw as received; nothing repaired)")
    for r in reports:
        print(f"\n  [{r['timeframe']}] {r['bars']} bars  "
              f"{r['start'][:10]} -> {r['end'][:10]}")
        print(f"    duplicates={r['duplicates']}  "
              f"irregular_gaps={r['irregular_gaps']}  "
              f"gaps>2x={r['gaps_gt_2x_mode']}")
        print(f"    ohlc_violations={r['ohlc_violations']}  "
              f"null_close={r['null_close']}")
        print(f"    close range: {r['min_close']:.2f} .. {r['max_close']:.2f}  "
              f"mean_spread={r['mean_spread_pts']:.1f} pts")
    return 0


if __name__ == "__main__":
    sys.exit(main())
