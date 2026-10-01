"""Catch-up ingestion of XAUUSD (GOLD.i#) across every supported timeframe.

Pulls the maximum history the XM Global demo terminal exposes:
  M1  ~30 days (session hours only)   M5  ~180 days    M15 ~2 years
  H1  ~10 years                       H4  ~10 years    D1  ~10 years

Writes into db/trading.db via market_data.insert_mt5_bars().
READ-ONLY with respect to the broker: reads bars, never trades.
Idempotent: re-running skips rows already present for
(symbol, timeframe, source='mt5', epoch), so it is safe to run on a schedule.

Window sizes are asserted against MT5's ~100k bar cap at startup -- an
oversized window is silently truncated from the END, which looks exactly
like a stale feed. See scripts/ingest_common.py.

Usage:
    ./.venv/Scripts/python.exe scripts/ingest_gold_all_tf.py
    ./.venv/Scripts/python.exe scripts/ingest_gold_all_tf.py --timeframes M1 M5
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import MetaTrader5 as mt5

from ingest_common import assert_windows_safe, ensure_db, fetch_with_shrink
from market_data import config as cfg
from market_data.base import Timeframe
from market_data.providers.mt5_provider import MT5AccountGuardError, MT5Provider
from market_data.storage import insert_mt5_bars

SYMBOL = "XAUUSD"

# (timeframe, days) -- deliberately generous; fetch_with_shrink handles the
# broker's real retention limit, so the fetch reveals the true depth instead
# of us assuming it.
WINDOWS = [
    (Timeframe.M1, 40),
    (Timeframe.M5, 200),
    (Timeframe.M15, 800),
    (Timeframe.H1, 3800),
    (Timeframe.H4, 3800),
    (Timeframe.D1, 3800),
]

_MTF = {
    Timeframe.M1: mt5.TIMEFRAME_M1,
    Timeframe.M5: mt5.TIMEFRAME_M5,
    Timeframe.M15: mt5.TIMEFRAME_M15,
    Timeframe.M30: mt5.TIMEFRAME_M30,
    Timeframe.H1: mt5.TIMEFRAME_H1,
    Timeframe.H4: mt5.TIMEFRAME_H4,
    Timeframe.D1: mt5.TIMEFRAME_D1,
}

SCHEMA_PATH = cfg.PROJECT_ROOT / "db" / "schema" / "market_data.sql"


def db_state(conn, tf: Timeframe):
    return conn.execute(
        "SELECT COUNT(*), MIN(ts_broker_epoch), MAX(ts_broker_epoch) "
        "FROM market_data WHERE symbol=? AND timeframe=? AND source='mt5'",
        (SYMBOL, tf.value),
    ).fetchone()


def fmt(epoch):
    if epoch is None:
        return "n/a"
    return datetime.fromtimestamp(int(epoch), tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--timeframes", nargs="*", default=None,
                    help="subset, e.g. --timeframes M1 M5 (default: all)")
    args = ap.parse_args()

    assert_windows_safe(WINDOWS, symbol=SYMBOL)
    wanted = {t.upper() for t in args.timeframes} if args.timeframes else None

    conn = ensure_db(cfg.PROJECT_ROOT / cfg.load_settings()["paths"]["db"],
                     SCHEMA_PATH)
    provider = MT5Provider.from_config()
    try:
        provider.connect()
    except MT5AccountGuardError as exc:
        print(f"MT5 guard refused connection: {exc}", file=sys.stderr)
        return 1

    rc = 0
    try:
        sym = provider.symbol_map.get(SYMBOL)
        if sym is None:
            print(f"{SYMBOL} is not mapped in config/symbols.toml", file=sys.stderr)
            return 1

        acc = mt5.account_info()
        print(f"canonical {SYMBOL} -> broker symbol {sym!r}")
        print(f"account {acc.login} on {acc.server} "
              f"(trade_mode={acc.trade_mode}, balance={acc.balance})\n")

        before = {tf: db_state(conn, tf) for tf, _ in WINDOWS}
        now = datetime.now(timezone.utc)

        for tf, days in WINDOWS:
            if wanted is not None and tf.value not in wanted:
                continue
            rates, used = fetch_with_shrink(
                lambda d: mt5.copy_rates_range(sym, _MTF[tf],
                                               now - timedelta(days=d), now),
                days)
            if rates is None:
                print(f"{SYMBOL} {tf.value:>3}: NO DATA at any window size")
                rc = 1
                continue

            first = datetime.utcfromtimestamp(int(rates["time"][0])).date()
            last = datetime.utcfromtimestamp(int(rates["time"][-1])).date()
            res = insert_mt5_bars(conn, SYMBOL, tf, rates)
            conn.commit()

            b_count, b_min, b_max = before[tf]
            a_count, a_min, a_max = db_state(conn, tf)
            print(f"{SYMBOL} {tf.value:>3}: requested {days:>5}d, used {used:>5}d "
                  f"| fetched {len(rates):>6d} bars  {first} .. {last}")
            print(f"           inserted {res.inserted:>6d}, skipped {res.skipped:>6d}")
            print(f"           DB before: {b_count:>7d} bars  {fmt(b_min)} .. {fmt(b_max)}")
            print(f"           DB after : {a_count:>7d} bars  {fmt(a_min)} .. {fmt(a_max)}")
            if res.inserted == 0 and b_max is not None and a_max <= b_max:
                print("           (already up to date)")
            print()
        return rc
    finally:
        provider.close()
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
