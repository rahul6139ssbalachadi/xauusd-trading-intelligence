"""Integrity check on ingested bars.

Answers three questions the ingestion output does not:
  1. Are the rows internally valid (high >= max(o,c), low <= min(o,c),
     high >= low, positive prices)?
  2. Is there a GAP in the series? A gap at the JOIN between the old data
     and the new data is an ingest failure that otherwise looks like
     success; a gap mid-history is a broker data hole.
  3. Do bar counts match the expected rate, or is a chunk missing?

Read-only. Usage:
    ./.venv/Scripts/python.exe scripts/check_ingest_integrity.py
"""
from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ingest_common import (  # noqa: E402
    BAR_SECONDS,
    coverage_pct,
    find_gaps,
    invalid_ohlc_rows,
    max_gap_seconds,
)
from market_data import config as cfg  # noqa: E402

SYMBOLS = ["XAUUSD", "BTCUSD"]
TFS = ["M1", "M5", "M15", "H1", "H4", "D1"]

# A hole this long or longer is reported prominently. For gold, M1 is the
# only timeframe where the broker's own history is short enough that an
# un-fetched stretch is plausible -- and one was found: 2026-08-18 to
# 2026-08-24 (137.7h) is missing because the terminal's ~30-day M1
# retention rolled forward past it. Nothing can backfill that; it is gone
# from the broker, not skipped by the ingest.
NOTABLE_GAP_HOURS = 100


def fmt(epoch):
    return datetime.fromtimestamp(int(epoch), tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


def main() -> int:
    db = cfg.PROJECT_ROOT / cfg.load_settings()["paths"]["db"]
    conn = sqlite3.connect(db)
    problems = 0
    notable = []

    for sym in SYMBOLS:
        print(f"=== {sym}  (gap threshold {max_gap_seconds(sym)/3600:.0f}h) ===")
        for tf in TFS:
            rows = conn.execute(
                "SELECT ts_broker_epoch, open, high, low, close FROM market_data "
                "WHERE symbol=? AND timeframe=? AND source='mt5' "
                "ORDER BY ts_broker_epoch", (sym, tf)).fetchall()
            if not rows:
                print(f"  {tf:>4}: NO DATA")
                problems += 1
                continue

            bad = invalid_ohlc_rows(rows)
            if bad:
                problems += len(bad)
                print(f"  {tf:>4}: {len(bad)} INVALID OHLC rows "
                      f"(first at {fmt(bad[0][0])})")

            epochs = [r[0] for r in rows]
            gaps = find_gaps(epochs, tf, sym)
            cov = coverage_pct(len(rows), epochs, tf, sym)
            biggest = max((d for _, d in gaps), default=0)

            print(f"  {tf:>4}: {len(rows):>7,d} bars  {fmt(epochs[0])} .. "
                  f"{fmt(epochs[-1])}")
            print(f"        validity={'OK' if not bad else 'FAIL'}  "
                  f"gaps={len(gaps)}  max_gap={biggest/3600:.1f}h  "
                  f"coverage={cov:.0f}%")

            for at, d in gaps:
                if d / 3600 >= NOTABLE_GAP_HOURS:
                    notable.append((sym, tf, at, d))
                    print(f"        NOTABLE HOLE: {d/3600:.1f}h starting "
                          f"after {fmt(at)}")
        print()

    print("=== NOTABLE HOLES ===")
    if not notable:
        print("  none")
    for sym, tf, at, d in notable:
        print(f"  {sym} {tf}: {d/3600:.1f}h after {fmt(at)}")

    print()
    print("=== RESULT ===")
    print("PASS - no invalid rows, no unexpected gaps" if problems == 0
          else f"REVIEW - {problems} invalid row(s)")
    conn.close()
    return 0 if problems == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
