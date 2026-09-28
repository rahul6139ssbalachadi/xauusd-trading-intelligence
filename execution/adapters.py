"""Execution adapters — the boundary between the engine and a broker.

WHY THIS EXISTS
    MT5 is Windows-only. Putting the whole system on a Linux server used to
    mean losing the ability to place orders at all. This module makes that
    failure mode explicit and safe instead of implicit:

        the server has an adapter that REFUSES, rather than no adapter at all

The interface is deliberately tiny. Anything the research engine needs to
know about "placing a trade" is expressed here, so a new venue means a new
class, not a new branch in the strategy code.

SAFETY CONTRACT (inherited, not re-implemented)
    Every adapter here is subordinate to the existing controls. None of them
    can widen what the ExecutionEngine permits:
      - config/settings.toml  live_trading_enabled  (true BLOCKS)
      - execution/approved.json                     (strategy must be approved)
      - the kill-switch file                        (manual reset only)
      - MT5Gateway's own DEMO + login verification  (Windows path)
    The Null adapter is the default precisely so that a misconfigured or
    containerised deployment degrades to "does nothing" instead of
    "does something irreversible".
"""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from appconfig import AppConfig, get_config

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class OrderIntent:
    """What a strategy wants done. Produced by strategy code, never sent
    by it. Contains no credentials and nothing broker-specific beyond the
    broker symbol name."""

    symbol: str
    broker_symbol: str
    direction: str                 # BUY | SELL
    lots: float
    entry: float
    stop: float
    target: float
    strategy: str
    version: str
    magic: int
    comment: str = ""
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def to_wire(self) -> dict:
        """Plain dict for JSON transport to a remote bridge."""
        return {
            "symbol": self.symbol,
            "broker_symbol": self.broker_symbol,
            "direction": self.direction,
            "lots": round(float(self.lots), 4),
            "entry": float(self.entry),
            "stop": float(self.stop),
            "target": float(self.target),
            "strategy": self.strategy,
            "version": self.version,
            "magic": int(self.magic),
            "comment": self.comment[:31],
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class ExecutionResult:
    """Uniform outcome. `status` is the field callers must branch on.

    status values:
      EXECUTED   the order reached the venue
      RECORDED   intent stored, no order placed (Demo adapter)
      REFUSED    refused by this adapter, deliberately and safely
      UNAVAILABLE the venue is not reachable/configured
      FAILED     the venue was reachable and rejected the order
    """

    status: str
    reason: str = ""
    ticket: int | None = None
    adapter: str = ""
    detail: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status == "EXECUTED"

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "reason": self.reason,
            "ticket": self.ticket,
            "adapter": self.adapter,
            "detail": self.detail,
        }


# ---------------------------------------------------------------------------
# Interface
# ---------------------------------------------------------------------------
class ExecutionAdapter(ABC):
    """Venue-agnostic order interface.

    Implementations must be constructible on any OS and must not import a
    platform-specific broker SDK at module level. That is what allows this
    whole package to be imported inside a Linux container.
    """

    name: str = "abstract"

    def __init__(self, cfg: AppConfig | None = None):
        self.cfg = cfg or get_config()

    @abstractmethod
    def available(self) -> bool:
        """True only if this adapter could actually place an order now."""

    @abstractmethod
    def place(self, intent: OrderIntent) -> ExecutionResult:
        """Attempt to place the order. Must never raise for a refused order —
        a refusal is a RESULT, not an exception, so callers cannot
        accidentally treat it as a crash and retry harder."""

    def status(self) -> dict:
        """Human/phone-readable state. No secrets."""
        return {"adapter": self.name, "available": self.available()}

    def describe(self) -> str:
        return f"{self.name}(available={self.available()})"


# ---------------------------------------------------------------------------
# 1. Null — the safe default
# ---------------------------------------------------------------------------
class NullExecutionAdapter(ExecutionAdapter):
    """Places nothing. Ever.

    This is the default in every environment, including production. It
    exists so that a deployment which forgets to configure execution still
    behaves correctly: strategies still generate signals, the dashboard
    still shows them, and nothing reaches a broker.
    """

    name = "null"

    def available(self) -> bool:
        return False

    def place(self, intent: OrderIntent) -> ExecutionResult:
        log.info(
            "order REFUSED (no adapter configured): %s %s %.2f lots @ %.2f",
            intent.direction, intent.symbol, intent.lots, intent.entry,
        )
        return ExecutionResult(
            status="REFUSED",
            reason=(
                "EXECUTION_ADAPTER is 'null' — this deployment records and "
                "displays signals but places no orders. Set EXECUTION_ADAPTER "
                "on a machine that owns the broker terminal."
            ),
            adapter=self.name,
        )


# ---------------------------------------------------------------------------
# 2. Demo — forward-test the pipeline, place nothing
# ---------------------------------------------------------------------------
class DemoExecutionAdapter(ExecutionAdapter):
    """Writes the order intent to the journal and returns RECORDED.

    Useful on a headless server: the full strategy -> risk -> engine path is
    exercised and auditable, and the phone dashboard can show a complete
    decision trail, without any broker connection existing at all.

    It is NOT a simulation of fills. It records intent. Do not read RECORDED
    as "a trade happened".
    """

    name = "demo"

    def available(self) -> bool:
        return True

    def place(self, intent: OrderIntent) -> ExecutionResult:
        return self._record(intent)

    def _record(self, intent: OrderIntent) -> ExecutionResult:
        from execution import JOURNAL
        try:
            JOURNAL.parent.mkdir(parents=True, exist_ok=True)
            event = {
                "ts": datetime.now(timezone.utc).isoformat(),
                "adapter": self.name,
                "event": "ORDER_INTENT_RECORDED",
                "note": "no broker order was placed",
                **intent.to_wire(),
            }
            with JOURNAL.open("a", encoding="utf-8") as f:
                f.write(json.dumps(event, default=str) + "\n")
        except OSError as exc:
            log.error("demo adapter could not write journal: %s", exc)
            return ExecutionResult(
                status="FAILED", reason=f"journal write failed: {exc}",
                adapter=self.name,
            )
        log.info(
            "order RECORDED (demo adapter, nothing sent): %s %s %.2f lots @ %.2f",
            intent.direction, intent.symbol, intent.lots, intent.entry,
        )
        return ExecutionResult(
            status="RECORDED",
            reason="intent journaled; no order placed (DEMO adapter)",
            adapter=self.name,
            detail={"journal": str(JOURNAL)},
        )


# ---------------------------------------------------------------------------
# 3. MT5 — Windows only, lazily imported
# ---------------------------------------------------------------------------
class MT5ExecutionAdapter(ExecutionAdapter):
    """Talks to a LOCAL MetaTrader5 terminal. Windows only.

    The import is inside connect() on purpose. `import MetaTrader5` at module
    level is exactly what makes this package uninstallable on Linux, so it
    must never appear at the top of a file that a server imports.

    Even here, the adapter is subordinate: it constructs the existing
    MT5Gateway, which independently refuses any non-DEMO account and any
    login that is not the configured one. This class adds a configuration
    gate, it does not replace the security gate.
    """

    name = "mt5"

    def _mt5_config(self) -> dict:
        """Resolve connection settings from BOTH sources.

        Env vars are the deployment path (a container has no config/mt5.toml,
        which is git-ignored). config/mt5.toml is the established local path
        on Windows. The adapter must honour both, or it reports "unavailable"
        on a Windows box that is fully and correctly configured.

        Precedence: explicit env var, then config/mt5.toml, then empty.
        An unresolvable config yields {} and available() returns False, which
        is the correct fail-closed outcome.
        """
        cfg: dict = {}
        try:
            from market_data import config as mdcfg
            cfg = mdcfg.load_mt5_config()
        except FileNotFoundError:
            # No TOML and no env terminal path. Not an error: the adapter is
            # simply not configured, and must stay disabled.
            pass
        except Exception:  # noqa: BLE001
            # A malformed TOML must disable the adapter, not crash startup.
            pass
        return {
            "terminal_path": self.cfg.mt5_terminal_path or cfg.get("terminal_path", ""),
            "allowed_login": self.cfg.mt5_login or cfg.get("allowed_login", ""),
        }

    def available(self) -> bool:
        conf = self._mt5_config()
        if not conf.get("terminal_path"):
            return False
        try:
            import MetaTrader5  # noqa: F401
        except ImportError:
            return False
        return True

    def _connect(self):
        from execution.mt5_gateway import MT5Gateway

        conf = self._mt5_config()
        if not conf.get("terminal_path"):
            raise RuntimeError("MT5_TERMINAL_PATH is not set and "
                               "config/mt5.toml is absent")
        raw_login = conf.get("allowed_login") or 0
        try:
            login = int(raw_login)
        except (TypeError, ValueError):
            raise RuntimeError(f"allowed_login is not a number: {raw_login!r}")
        if not login:
            raise RuntimeError("allowed_login is not configured; refusing to "
                               "connect without an expected account")
        gw = MT5Gateway(
            terminal_path=conf["terminal_path"],
            allowed_login=login,
        )
        gw.connect()
        return gw

    def place(self, intent: OrderIntent) -> ExecutionResult:
        if not self._mt5_config().get("terminal_path"):
            return ExecutionResult(
                status="UNAVAILABLE",
                reason="MT5_TERMINAL_PATH is not set and config/mt5.toml is "
                       "absent; the MT5 adapter is disabled by configuration.",
                adapter=self.name,
            )
        try:
            import MetaTrader5  # noqa: F401
        except ImportError as exc:
            return ExecutionResult(
                status="UNAVAILABLE",
                reason=f"MetaTrader5 package unavailable on this platform ({exc}). "
                       f"MT5 execution is Windows-only.",
                adapter=self.name,
            )
        try:
            gw = self._connect()
        except Exception as exc:  # noqa: BLE001
            return ExecutionResult(
                status="UNAVAILABLE",
                reason=f"MT5 connect failed: {type(exc).__name__}: {exc}",
                adapter=self.name,
            )
        try:
            from execution import ExecutionEngine, OrderRequest

            req = OrderRequest(
                symbol=intent.symbol, broker_symbol=intent.broker_symbol,
                direction=intent.direction, lots=intent.lots, entry=intent.entry,
                stop=intent.stop, target=intent.target, strategy=intent.strategy,
                version=intent.version, magic=intent.magic,
                comment=intent.comment,
            )
            res = ExecutionEngine(gw).execute(req)
            return ExecutionResult(
                status="EXECUTED" if res.ok else "REFUSED",
                reason=res.reason or res.decision,
                ticket=res.order_ticket,
                adapter=self.name,
                detail={"checks": [[n, ok, d] for n, ok, d in res.checks]},
            )
        finally:
            gw.disconnect()

    def status(self) -> dict:
        conf = self._mt5_config()
        return {
            "adapter": self.name,
            "available": self.available(),
            "terminal_configured": bool(conf.get("terminal_path")),
            "login_configured": bool(conf.get("allowed_login")),
            # Never the value itself: the account number is not a secret, but
            # it is identifying and has no business in a public health payload.
            "source": "env" if self.cfg.mt5_terminal_path else "config/mt5.toml",
        }


# ---------------------------------------------------------------------------
# 4. Bridge — server forwards to a Windows machine
# ---------------------------------------------------------------------------
class BridgeExecutionAdapter(ExecutionAdapter):
    """Forwards the order intent to a Windows bridge over HTTPS.

    This is the adapter that lets the SERVER propose trades while the MT5
    terminal stays on Windows. It is NOT a bypass: the bridge is the party
    that runs the ExecutionEngine and the MT5 gateway, so all ten pre-trade
    checks still run on the Windows side before anything reaches the broker.

    If BRIDGE_URL or BRIDGE_TOKEN is missing, this adapter is UNAVAILABLE.
    An unauthenticated order bridge must never be reachable, so there is no
    anonymous fallback.
    """

    name = "bridge"

    def available(self) -> bool:
        return bool(self.cfg.bridge_url and self.cfg.bridge_token)

    def place(self, intent: OrderIntent) -> ExecutionResult:
        if not self.cfg.bridge_url:
            return ExecutionResult(
                status="UNAVAILABLE",
                reason="BRIDGE_URL is not set; the bridge adapter is disabled.",
                adapter=self.name,
            )
        if not self.cfg.bridge_token:
            # Fail closed, loudly, and never send anything unauthenticated.
            log.error("BRIDGE_TOKEN is unset — refusing to send an order over an "
                      "unauthenticated channel")
            return ExecutionResult(
                status="REFUSED",
                reason="BRIDGE_TOKEN is not set. An unauthenticated order bridge "
                       "is never contacted.",
                adapter=self.name,
            )
        payload = json.dumps(intent.to_wire()).encode("utf-8")
        req = urllib.request.Request(
            self.cfg.bridge_url.rstrip("/") + "/bridge/orders",
            data=payload,
            method="POST",
            headers={
                "Content-Type": "application/json",
                # Sent as a header, never logged. See the logging filter in
                # observability.py.
                "X-Bridge-Token": self.cfg.bridge_token,
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.cfg.bridge_timeout) as r:
                body = json.loads(r.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as exc:
            return ExecutionResult(
                status="REFUSED",
                reason=f"bridge returned HTTP {exc.code}",
                adapter=self.name,
            )
        except (urllib.error.URLError, TimeoutError) as exc:
            return ExecutionResult(
                status="UNAVAILABLE",
                reason=f"bridge unreachable: {type(exc).__name__}: {exc}",
                adapter=self.name,
            )
        except (ValueError, json.JSONDecodeError) as exc:
            return ExecutionResult(
                status="FAILED",
                reason=f"bridge sent an unreadable response: {exc}",
                adapter=self.name,
            )
        status = str(body.get("status", "FAILED")).upper()
        return ExecutionResult(
            status=status if status in {
                "EXECUTED", "RECORDED", "REFUSED", "UNAVAILABLE", "FAILED",
            } else "FAILED",
            reason=str(body.get("reason", "")),
            ticket=body.get("ticket"),
            adapter=self.name,
            detail={k: v for k, v in body.items() if k not in {"status", "reason", "ticket"}},
        )

    def status(self) -> dict:
        return {
            "adapter": self.name,
            "available": self.available(),
            "url_configured": bool(self.cfg.bridge_url),
            "token_configured": bool(self.cfg.bridge_token),
        }


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------
ADAPTERS: dict[str, type[ExecutionAdapter]] = {
    "null": NullExecutionAdapter,
    "demo": DemoExecutionAdapter,
    "mt5": MT5ExecutionAdapter,
    "bridge": BridgeExecutionAdapter,
}


def get_adapter(cfg: AppConfig | None = None,
                name: str | None = None) -> ExecutionAdapter:
    """Build the configured adapter.

    An unknown adapter name falls back to Null rather than raising: an
    unrecognised setting must never escalate into something more powerful
    than what was asked for.
    """
    cfg = cfg or get_config()
    key = (name or cfg.execution_adapter or "null").lower()
    cls = ADAPTERS.get(key)
    if cls is None:
        log.warning(
            "unknown EXECUTION_ADAPTER %r — falling back to 'null' "
            "(no orders will be placed). Valid: %s",
            key, ", ".join(sorted(ADAPTERS)),
        )
        return NullExecutionAdapter(cfg)
    return cls(cfg)


__all__ = [
    "ADAPTERS", "BridgeExecutionAdapter", "DemoExecutionAdapter",
    "ExecutionAdapter", "ExecutionResult", "MT5ExecutionAdapter",
    "NullExecutionAdapter", "OrderIntent", "get_adapter",
]
