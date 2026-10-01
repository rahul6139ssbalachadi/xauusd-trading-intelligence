"""Shared helpers for MT5 history ingestion scripts.

Pure logic lives here (no MT5 import, no network) so it can be unit-tested;
the scripts supply the MT5-specific fetch callable.

The load-bearing idea is REQUEST SIZING. `mt5.copy_rates_range` fills from
the START of the window forward and refuses requests above ~100k bars, so an
oversized window silently truncates the END -- the newest bars never arrive
and a live feed looks stale. That is a request-size artifact, not a data
gap, and it is the bug that left BTCUSD M1/M5/M15 parked at 2026-09-28
while M30/H1/H4/D1 on the same symbol were current.

Two defences, both here:
  * `fetch_with_shrink` halves the window and retries instead of storing a
    silent gap when a request comes back empty.
  * `window_is_safe` / `assert_windows_safe` refuse a window whose estimated
    bar count approaches the cap, so the truncation is caught at build time.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Callable, Sequence

BAR_CAP = 100_000
# Refuse a window estimated above this, leaving a 30k margin for the broker
# returning more bars than the nominal rate.
BAR_CAP_SAFE = 70_000

# Nominal bars per calendar day, assuming a 24/7 market.
BARS_PER_DAY = {
    "M1": 1440,
    "M5": 288,
    "M15": 96,
    "M30": 48,
    "H1": 24,
    "H4": 6,
    "D1": 1,
}

# Fraction of the calendar week the symbol actually trades. Metals and FX
# close for the weekend, so a 24/7 estimate overshoots by ~1.5x on gold:
# XAUUSD H1 over 3,800d is 61,854 real bars, not 91,200. Crypto trades
# through, so it needs the full 1.0 -- and that is exactly why BTCUSD M1
# at 68d really does hit the cap while gold M1 at 68d does not.
SESSION_FACTOR = {
    "XAUUSD": 5 / 7,
    "BTCUSD": 1.0,
}
DEFAULT_SESSION_FACTOR = 5 / 7


def _factor(symbol: str | None) -> float:
    if symbol is None:
        return DEFAULT_SESSION_FACTOR
    return SESSION_FACTOR.get(symbol.upper(), DEFAULT_SESSION_FACTOR)


def estimate_bars(tf: str, days: int, symbol: str | None = None) -> int:
    """Estimated bar count for `days` of `tf`, session-adjusted."""
    return int(days * BARS_PER_DAY[tf] * _factor(symbol))


def window_is_safe(tf: str, days: int, symbol: str | None = None) -> bool:
    """True if `days` of `tf` is estimated to fit under the MT5 bar cap."""
    return estimate_bars(tf, days, symbol) <= BAR_CAP_SAFE


def assert_windows_safe(windows: Sequence[tuple], symbol: str | None = None) -> None:
    """Fail loudly on a window that would be silently truncated.

    `windows` is a sequence of (timeframe, days) where timeframe has a
    `.value` (market_data.base.Timeframe) or is a plain string.
    """
    bad = []
    for tf, days in windows:
        name = getattr(tf, "value", tf)
        if not window_is_safe(name, days, symbol):
            bad.append(f"{name} {days}d "
                       f"(~{estimate_bars(name, days, symbol):,} bars "
                       f"> {BAR_CAP_SAFE:,})")
    if bad:
        raise ValueError(
            "window too large for the MT5 bar cap -- the newest bars would "
            "be silently truncated. Shorten the window or add a recent-window "
            "top-up pass:\n  " + "\n  ".join(bad)
        )


def fetch_with_shrink(
    fetch: Callable[[int], object],
    days: int,
    min_days: int = 1,
) -> tuple[object | None, int]:
    """Call `fetch(days)`, halving until it returns a non-empty result.

    `fetch` takes a window size in days and returns whatever MT5 returned
    (or None/empty). Returns (rates, days_actually_used); (None, 0) if every
    size down to `min_days` came back empty. Never returns a silent gap.
    """
    d = days
    while d >= min_days:
        rates = fetch(d)
        if rates is not None and len(rates) > 0:
            return rates, d
        d //= 2
    return None, 0


def ensure_db(db_path: Path, schema_path: Path) -> sqlite3.Connection:
    """Open the DB, applying the schema when creating it fresh."""
    is_new = not db_path.exists()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    if is_new:
        conn.executescript(schema_path.read_text())
    return conn


# Seconds between bars, per timeframe.
BAR_SECONDS = {
    "M1": 60, "M5": 300, "M15": 900, "M30": 1800,
    "H1": 3600, "H4": 14400, "D1": 86400,
}

# A gap longer than this is a real hole rather than an expected closure.
# Session-traded gold closes every weekend (~50h) and for Christmas/New Year
# (a 2020-12-24 gap measured 79-80h; 2016-12-23 measured 96h), so the
# threshold must clear a long holiday. Crypto trades 24/7, so anything past
# half a day is a genuine hole -- and the broker's early BTC history really
# does have them (a 113-day hole in 2016, a 33-day hole in 2017).
MAX_GAP_SECONDS = {
    "XAUUSD": 120 * 3600,
    "BTCUSD": 12 * 3600,
}
DEFAULT_MAX_GAP_SECONDS = 120 * 3600


def max_gap_seconds(symbol: str | None = None) -> int:
    if symbol is None:
        return DEFAULT_MAX_GAP_SECONDS
    return MAX_GAP_SECONDS.get(symbol.upper(), DEFAULT_MAX_GAP_SECONDS)


def find_gaps(epochs: Sequence[int], tf: str,
              symbol: str | None = None) -> list[tuple[int, int]]:
    """Return [(epoch_before_gap, gap_seconds)] for gaps above the threshold.

    `epochs` must be sorted ascending. Expects a regular bar spacing of
    BAR_SECONDS[tf], so a gap is anything beyond that.
    """
    limit = max_gap_seconds(symbol)
    out = []
    for a, b in zip(epochs, epochs[1:]):
        d = b - a
        if d > limit:
            out.append((a, d))
    return out


def coverage_pct(n_bars: int, epochs: Sequence[int], tf: str,
                 symbol: str | None = None) -> float:
    """Bars present as a percentage of the bars the span should contain.

    Session-adjusted, so 100% means complete. Without the factor a
    session-traded symbol can never exceed ~71% (5/7) and a healthy gold
    series reads as if a third of it were missing.
    """
    if len(epochs) < 2:
        return 0.0
    span = epochs[-1] - epochs[0]
    expected = (span / BAR_SECONDS[tf]) * _factor(symbol)
    return (n_bars / expected * 100) if expected else 0.0


def invalid_ohlc_rows(rows: Sequence[Sequence]) -> list[Sequence]:
    """Rows violating OHLC sanity: high < low, high below the body, low
    above the body, or a non-positive price. `rows` are
    (epoch, open, high, low, close)."""
    return [r for r in rows
            if r[2] < r[3]
            or r[2] < max(r[1], r[4])
            or r[3] > min(r[1], r[4])
            or min(r[1], r[4]) <= 0]
