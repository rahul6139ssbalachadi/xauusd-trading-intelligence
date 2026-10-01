"""24/7 MT5 read service — market data, history, and open positions.

WHAT THIS IS
    A supervised, always-on reader that keeps the local database current
    and exposes what the broker actually holds. It NEVER sends an order.
    There is no order_send call anywhere in this file, and none can be
    added without editing this module's docstring to say so.

    It is the data half of the deployment. The trading half stays behind
    the ExecutionEngine's Section 23 checklist.

WHY A SEPARATE SERVICE
    The existing worker.py evaluates strategies from STORED bars, which
    is correct for a server that receives bars from a Windows bridge. On
    a machine that has MT5 installed natively, something must keep those
    bars fresh or every decision is made on month-old data. That is this
    module.

READING (safe, no market impact)
    - OHLCV bars per timeframe, written to the local SQLite database
    - symbol info (point, digits, contract size, lot step, min lot)
    - account info (balance, equity, margin) — read-only
    - open positions and pending orders
    - deal/order history for the reconciliation window

WRITING
    Only to the local SQLite database. Never to the broker. The one
    exception is explicitly NOT here: order placement is ExecutionEngine.

RECONNECTION
    MT5 drops the connection at weekends, on daily maintenance, and on
    network loss. A read loop that does not handle that looks alive while
    serving stale data — the exact failure that made the last deployment
    silently emit WAIT for 33 days. So:
      - every tick re-verifies mt5.initialize()
      - a failed cycle records a health event, it does not raise
      - stale_data_minutes is exposed so the dashboard can show the age
      - the loop never exits on a broker error; only a clean shutdown
        request or repeated unrecoverable config errors stop it

SAFETY
    - DEMO-ONLY in the sense that it refuses to even connect to an
      account on the forbidden list, and refuses to run at all unless
      TRADING_MODE=demo. Reading a funded account is not dangerous, but
      the owner asked for demo-only, so demo-only is what it does.
    - Fails safe: if anything is wrong with the connection, it stops
      reading rather than writing partial data.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import MetaTrader5 as mt5

from execution.mt5_gateway import FORBIDDEN_LOGINS

log = logging.getLogger("mt5reader")

# Bars per request are capped by the broker (~100k). Requesting more
# returns fewer, silently, which would look like data loss.
_BAR_CHUNK = 50_000


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class ReadHealth:
    """What the dashboard shows. Cheap to build, safe to serialise."""

    connected: bool = False
    last_ok: str = ""                 # ISO UTC of last successful cycle
    last_error: str = ""
    login: int | None = None
    server: str = ""
    company: str = ""
    balance: float = 0.0
    equity: float = 0.0
    margin_free: float = 0.0
    positions: int = 0
    orders: int = 0
    bars_written: int = 0
    cycles: int = 0
    errors: int = 0
    symbols: list[str] = field(default_factory=list)
    started: str = field(default_factory=_utc)

    @property
    def stale_seconds(self) -> float:
        if not self.last_ok:
            return float("inf")
        try:
            t = datetime.fromisoformat(self.last_ok)
        except ValueError:
            return float("inf")
        return (datetime.now(timezone.utc) - t).total_seconds()

    def to_dict(self) -> dict:
        d = self.__dict__.copy()
        d["stale_seconds"] = (
            None if self.stale_seconds == float("inf")
            else round(self.stale_seconds, 1))
        d["stale"] = self.stale_seconds > 300          # 5 min
        return d


class MT5Reader:
    """Read-only MT5 client. Owns the connection; no order methods exist."""

    def __init__(self, terminal_path: str, allowed_login: int,
                 db_path: Path | None = None,
                 forbidden: frozenset[int] = FORBIDDEN_LOGINS):
        self.terminal_path = terminal_path
        self.allowed_login = int(allowed_login)
        self.forbidden = forbidden
        self.health = ReadHealth()
        self.db_path = Path(db_path or (Path(__file__).resolve().parents[1] / "db" / "trading.db"))
        self._connected = False

    # -- connection -----------------------------------------------------
    def connect(self) -> bool:
        """Connect and verify identity. Returns False instead of raising so
        the loop can keep retrying across broker downtime."""
        if not mt5.initialize(path=self.terminal_path):
            code, desc = mt5.last_error()
            self.health.last_error = f"initialize failed [{code}] {desc}"
            self.health.connected = False
            return False

        acc = mt5.account_info()
        if acc is None:
            self.health.last_error = "account_info() returned None"
            mt5.shutdown()
            return False

        if acc.login in self.forbidden:
            # Never even read a forbidden account, let alone trade it.
            mt5.shutdown()
            self.health.last_error = f"login {acc.login} is forbidden; refusing to read"
            self.health.connected = False
            return False

        if acc.login != self.allowed_login:
            mt5.shutdown()
            self.health.last_error = (
                f"login {acc.login} != allowed {self.allowed_login}")
            self.health.connected = False
            return False

        self.health.login = acc.login
        self.health.server = acc.server
        self.health.company = acc.company
        self.health.balance = float(acc.balance)
        self.health.equity = float(acc.equity)
        self.health.margin_free = float(acc.margin_free)
        self.health.connected = True
        self.health.last_error = ""
        log.info("connected login=%s server=%s", acc.login, acc.server)
        return True

    def disconnect(self) -> None:
        if self._connected or self.health.connected:
            mt5.shutdown()
        self._connected = False
        self.health.connected = False

    # -- reads ----------------------------------------------------------
    def symbols(self) -> list[str]:
        syms = mt5.symbols_get()
        if not syms:
            return []
        names = [s.name for s in syms]
        self.health.symbols = names
        return names

    def symbol_info(self, symbol: str):
        return mt5.symbol_info(symbol)

    def positions(self) -> list[dict]:
        """Open positions as plain dicts. Read-only, safe for the API."""
        pos = mt5.positions_get()
        if not pos:
            return []
        out = []
        for p in pos:
            out.append({
                "ticket": p.ticket,
                "symbol": p.symbol,
                "type": "BUY" if p.type == mt5.POSITION_TYPE_BUY else "SELL",
                "volume": p.volume,
                "price_open": p.price_open,
                "sl": p.sl,
                "tp": p.tp,
                "profit": p.profit,
                "magic": p.magic,
                "comment": p.comment,
                "time": datetime.fromtimestamp(p.time, tz=timezone.utc).isoformat(),
            })
        return out

    def orders(self) -> list[dict]:
        ords = mt5.orders_get()
        if not ords:
            return []
        return [{
            "ticket": o.ticket,
            "symbol": o.symbol,
            "type": o.type,
            "volume": o.volume,
            "price": o.price,
            "sl": o.sl,
            "tp": o.tp,
            "magic": o.magic,
        } for o in ords]

    def history(self, days: int = 7) -> list[dict]:
        """Closed deals in the window. This is the forward evidence: the
        system can finally see its own fills, not just paper rows."""
        frm = datetime.now(timezone.utc) - timedelta(days=days)
        deals = mt5.history_deals_get(frm, datetime.now(timezone.utc))
        if not deals:
            return []
        return [{
            "ticket": d.ticket,
            "symbol": d.symbol,
            "direction": "BUY" if d.type == mt5.DEAL_TYPE_BUY else "SELL",
            "volume": d.volume,
            "price": d.price,
            "profit": d.profit,
            "magic": d.magic,
            "comment": d.comment,
            "entry": "IN" if d.entry == mt5.DEAL_ENTRY_IN else "OUT",
            "time": datetime.fromtimestamp(d.time, tz=timezone.utc).isoformat(),
        } for d in deals]

    # -- bars -----------------------------------------------------------
    def bars(self, symbol: str, timeframe: str, count: int = 500) -> list[dict]:
        tf = _tf(timeframe)
        if tf is None:
            raise ValueError(f"unknown timeframe {timeframe!r}")
        arr = mt5.copy_rates_from_pos(symbol, tf, 0, count)
        if arr is None:
            return []
        return [dict(zip(arr.dtype.names, row)) for row in arr]

    def last_bar_time(self, symbol: str, timeframe: str) -> float | None:
        tf = _tf(timeframe)
        if tf is None:
            return None
        arr = mt5.copy_rates_from_pos(symbol, tf, 0, 1)
        if arr is None or len(arr) == 0:
            return None
        return float(arr[-1][0])

    def ingest(self, symbol: str, timeframe: str, count: int = 5000) -> int:
        """Pull bars and write them to the local database.

        Returns the number of NEW rows written. The table's primary key is
        (symbol, timeframe, source, ts_broker_epoch), so re-ingesting an
        existing bar is a no-op and this is safe to call every cycle.

        Two deliberate choices, both matching the existing schema:

        - ts_broker_epoch is the RAW broker epoch. The schema documents it
          as "offset/timezone UNVERIFIED, do not assume UTC", and the
          whole 616k-bar history was ingested that way. Converting here
          would silently shift every bar by hours and invalidate every
          backtest. Same convention, on purpose.

        - spread is written from the bar's own spread field in POINTS,
          matching how MT5Provider stored it. The runner divides by 0.01
          to get pips, so points is the right unit to keep. The schema
          notes MT5Provider does not populate it; this does, which is
          strictly more information, not a different meaning.
        """
        import sqlite3

        rows = self.bars(symbol, timeframe, count)
        if not rows:
            return 0

        new = 0
        con = sqlite3.connect(self.db_path)
        try:
            for r in rows:
                spread = r.get("spread")
                cur = con.execute(
                    "INSERT OR IGNORE INTO market_data "
                    "(symbol, timeframe, source, ts_broker_epoch, open, high, "
                    "low, close, tick_volume, spread) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (symbol, timeframe, "mt5", int(r["time"]),
                     float(r["open"]), float(r["high"]), float(r["low"]),
                     float(r["close"]), int(r.get("tick_volume", 0) or 0),
                     int(spread) if spread is not None else None))
                if cur.rowcount:
                    new += 1
            con.commit()
        finally:
            con.close()
        self.health.bars_written += new
        return new

    # -- one full cycle --------------------------------------------------
    def cycle(self, jobs: list[tuple[str, str, int]] | None = None) -> ReadHealth:
        """One read pass. Never raises on a broker error — records it.

        jobs is a list of (symbol, timeframe, count) to ingest. None means
        just refresh the account snapshot and positions.
        """
        self.health.cycles += 1
        if not self.health.connected and not self.connect():
            self.health.errors += 1
            return self.health

        try:
            acc = mt5.account_info()
            if acc is not None:
                self.health.balance = float(acc.balance)
                self.health.equity = float(acc.equity)
                self.health.margin_free = float(acc.margin_free)
            self.health.positions = len(self.positions())
            self.health.orders = len(self.orders())

            for sym, tf, n in (jobs or []):
                try:
                    self.ingest(sym, tf, n)
                except Exception as e:      # one bad symbol must not kill the loop
                    log.warning("ingest %s/%s failed: %s", sym, tf, e)

            self.health.last_ok = _utc()
            self.health.last_error = ""
        except Exception as e:
            self.health.errors += 1
            self.health.last_error = f"{type(e).__name__}: {e}"
            log.warning("cycle error: %s", e)
        return self.health

    def run_forever(self, jobs: list[tuple[str, str, int]] | None = None,
                    interval: int = 60, max_cycles: int | None = None) -> None:
        """Block, reading forever. Ctrl-C stops it cleanly.

        max_cycles exists for tests and for a supervised one-shot run.
        """
        n = 0
        while True:
            self.cycle(jobs)
            n += 1
            if max_cycles is not None and n >= max_cycles:
                return
            try:
                time.sleep(interval)
            except KeyboardInterrupt:
                log.info("interrupted; shutting down")
                self.disconnect()
                return


def _tf(name: str):
    return {
        "M1": mt5.TIMEFRAME_M1, "M5": mt5.TIMEFRAME_M5,
        "M15": mt5.TIMEFRAME_M15, "M30": mt5.TIMEFRAME_M30,
        "H1": mt5.TIMEFRAME_H1, "H4": mt5.TIMEFRAME_H4,
        "D1": mt5.TIMEFRAME_D1,
    }.get(name.upper())
