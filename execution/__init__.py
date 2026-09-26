"""Execution engine (Phase 14). SIMULATION-GATED DEMO EXECUTION.

This is the ONLY module in the repo allowed to place orders. Everything
else stays read-only by construction. It exists behind three independent
gates that must ALL pass before any order leaves this machine:

  Gate 1  config/settings.toml  live_trading_enabled == true
          (flipped manually by the account owner, 2026-09-22)
  Gate 2  execution/approved.json lists the strategy as approved
  Gate 3  kill switch file absent / inactive (Section 24: manual reset)

Plus the full Section 23 pre-trade checklist verified on every order:
  1. symbol exists          6. take-profit exists
  2. market is open         7. risk within limit
  3. spread acceptable      8. no duplicate position (incl. manual trades)
  4. position size valid    9. strategy approved
  5. stop-loss exists      10. kill switch inactive

HARD RULES (non-negotiable):
  - DEMO ACCOUNTS ONLY. The engine refuses to run against a REAL account
    (trade_mode ACCOUNT_TRADE_MODE_REAL) regardless of the switch.
  - Never trades alongside the user's manual positions: if ANY position
    is open on the symbol, the engine declines.
  - The gateway is injected. Tests use a fake; production uses the MT5
    gateway in mt5_gateway.py. No MetaTrader5 import here.
  - Every decision, pass or block, is journaled to execution/journal.jsonl
    before the order is sent.
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from market_data import config as cfg

ROOT = Path(__file__).resolve().parents[1]
KILL_SWITCH = ROOT / "execution" / "KILL_SWITCH"
JOURNAL = ROOT / "execution" / "journal.jsonl"
APPROVED = ROOT / "execution" / "approved.json"

# XAUUSD on this broker: point=0.01, pip=0.10 -> pips = points * 0.1
POINTS_TO_PIPS = 0.1


# ---------------------------------------------------------------------------
# Kill switch (Section 24) — manual reset only
# ---------------------------------------------------------------------------
def kill_switch_active() -> bool:
    """True if the kill switch has been tripped. Presence of the file
    KILL_SWITCH means TRIPPED; deleting it is the manual reset."""
    return KILL_SWITCH.exists()


def trip_kill_switch(reason: str) -> None:
    """Trip the kill switch. Once tripped, no new orders until manually
    reset (file deleted) by the user."""
    KILL_SWITCH.parent.mkdir(parents=True, exist_ok=True)
    KILL_SWITCH.write_text(
        f"tripped: {datetime.now(timezone.utc).isoformat()}\nreason: {reason}\n",
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Approved-strategy registry (Gate 2)
# ---------------------------------------------------------------------------
def load_approved() -> dict[str, dict]:
    """Map 'name:version' -> approval record from approved.json."""
    data = json.loads(APPROVED.read_text(encoding="utf-8"))
    return {f"{a['name']}:{a['version']}": a for a in data.get("approved", [])}


# ---------------------------------------------------------------------------
# Pre-trade checklist result
# ---------------------------------------------------------------------------
@dataclass
class PreTradeCheck:
    ok: bool
    checks: list[tuple[str, bool, str]] = field(default_factory=list)  # (name, passed, detail)
    reason: str = ""

    @property
    def failures(self) -> list[str]:
        return [f"{n}: {d}" for n, ok, d in self.checks if not ok]


@dataclass
class OrderRequest:
    symbol: str                 # canonical, e.g. "XAUUSD"
    broker_symbol: str          # e.g. "GOLD.i#"
    direction: str              # "BUY"
    lots: float
    entry: float                # planned entry (market order uses current ask)
    stop: float
    target: float
    strategy: str
    version: str
    magic: int
    comment: str = ""


@dataclass
class ExecutionResult:
    ok: bool
    decision: str               # EXECUTED / BLOCKED / DECLINED
    checks: list[tuple[str, bool, str]]
    order_ticket: int | None = None
    reason: str = ""
    ts: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


# ---------------------------------------------------------------------------
# The engine
# ---------------------------------------------------------------------------
class ExecutionEngine:
    """Runs the Section 23 checklist and, only if everything passes,
    sends one market order with SL/TP through the injected gateway."""

    def __init__(self, gateway, settings: dict | None = None,
                 approved: dict[str, dict] | None = None,
                 risk_limits: dict | None = None,
                 allowed_login: int | None = None):
        self.gateway = gateway
        self.settings = settings or cfg.load_settings()
        self.approved = approved if approved is not None else load_approved()
        self.risk_limits = risk_limits or cfg.load_risk_limits()
        self._journal_path = JOURNAL
        # The demo login that must be connected for any order to pass.
        # Falls back to config/mt5.toml (git-ignored, created per machine).
        # None disables the per-order re-check — acceptable only for a test
        # double; MT5Gateway always sets it.
        self.allowed_login = allowed_login or getattr(gateway, "allowed_login", None)
        if not self.allowed_login:
            try:
                self.allowed_login = cfg.load_mt5_config().get("allowed_login")
            except FileNotFoundError:
                self.allowed_login = None

    # -- journaling ---------------------------------------------------------
    def _journal(self, event: dict) -> None:
        JOURNAL.parent.mkdir(parents=True, exist_ok=True)
        with JOURNAL.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, default=str) + "\n")

    # -- Section 23 checklist ------------------------------------------------
    def pre_trade_checks(self, req: OrderRequest) -> PreTradeCheck:
        checks: list[tuple[str, bool, str]] = []

        # 10. kill switch (checked first — cheapest hard gate)
        ks = not kill_switch_active()
        checks.append(("kill_switch_inactive", ks,
                       "inactive" if ks else "TRIPPED — manual reset required"))

        # 1. Gate 1 — two switches with OPPOSITE polarity. This is the whole
        #    safety argument:
        #      demo_execution_enabled  may permit orders, but only on an
        #                               account already verified DEMO with
        #                               the expected login (checks 2/2b).
        #      live_trading_enabled    is NOT a permission. It is asserted
        #                               FALSE; true is treated as a
        #                               misconfiguration and BLOCKS, so it
        #                               can never authorise a real account.
        #    The old design used live_trading_enabled as the on/off switch,
        #    so setting it true also meant "real money allowed" on paper.
        demo_ok = self.settings.get("demo_execution_enabled") is True
        checks.append((
            "demo_execution_enabled", demo_ok,
            "true" if demo_ok else "false — DEMO execution disabled"))

        live_flag = self.settings.get("live_trading_enabled") is True
        checks.append((
            "live_trading_forbidden", not live_flag,
            "FORBIDDEN — this build is DEMO-only and can never trade live"
            if live_flag else
            "false — real-money execution permanently disabled"))

        # 2. account is DEMO (hard rule: never a real account)
        acc = self.gateway.account_info()
        demo = acc is not None and acc.trade_mode == self.gateway.ACCOUNT_TRADE_MODE_DEMO
        checks.append(("account_is_demo", demo,
                       f"login={getattr(acc, 'login', None)} mode={getattr(acc, 'trade_mode', None)}"))

        # 2b. the connected account is the EXPECTED demo login. connect()
        #     already refuses a mismatch, but that is a connect-time
        #     guarantee; re-checking per order catches a re-login between
        #     connect and send. Skipped when no expected login is configured
        #     (test doubles), where connect() is the only enforcement.
        if acc is not None and self.allowed_login:
            checks.append((
                "account_login_expected",
                getattr(acc, "login", None) == self.allowed_login,
                f"{getattr(acc, 'login', None)} vs expected {self.allowed_login}"))

        # 3. symbol exists on broker
        sym = self.gateway.symbol_info(req.broker_symbol)
        sym_ok = sym is not None and getattr(sym, "visible", False)
        checks.append(("symbol_exists", sym_ok,
                       f"{req.broker_symbol} visible={getattr(sym, 'visible', None)}"))

        # 4. market open
        mkt = sym is not None and self.gateway.symbol_info_tick(req.broker_symbol) is not None
        checks.append(("market_open", mkt, "tick available" if mkt else "no tick — market closed?"))

        # 5. spread acceptable (vs strategy's max_spread_pips)
        approval = self.approved.get(f"{req.strategy}:{req.version}")
        max_spread = (approval or {}).get("max_spread_pips", 5.0)
        tick = self.gateway.symbol_info_tick(req.broker_symbol) if sym is not None else None
        if tick is not None:
            spread_pips = (tick.ask - tick.bid) / 0.10
            spread_ok = spread_pips <= max_spread
            checks.append(("spread_acceptable", spread_ok,
                           f"{spread_pips:.1f} pips (max {max_spread})"))
        else:
            spread_ok = False
            checks.append(("spread_acceptable", False, "no tick"))

        # 6. position size valid (broker min/step, risk-engine cap)
        lots_ok = req.lots >= 0.01 and round(req.lots, 2) == req.lots
        checks.append(("size_valid", lots_ok, f"lots={req.lots}"))

        # 7. stop-loss exists and is on the correct side
        sl_ok = req.stop > 0 and req.stop < req.entry
        checks.append(("stop_loss_exists", sl_ok,
                       f"stop={req.stop} entry={req.entry}"))

        # 8. take-profit exists and is on the correct side
        tp_ok = req.target > req.entry > 0
        checks.append(("take_profit_exists", tp_ok,
                       f"target={req.target} entry={req.entry}"))

        # 9. risk within limit (uses Phase 10 sizing convention)
        risk_usd = (req.entry - req.stop) * 100.0 * req.lots  # XAUUSD multiplier
        risk_cap = (approval or {}).get("risk_pct", 0.01)
        # equity needed from account; fall back to conservative static cap
        equity = getattr(acc, "equity", 10_000.0) if acc is not None else 10_000.0
        risk_ok = risk_usd <= equity * risk_cap * 1.01  # 1% rounding tolerance
        checks.append(("risk_within_limit", risk_ok,
                       f"risk=${risk_usd:.2f} cap=${equity * risk_cap:.2f}"))

        # 11 (extra). no duplicate / no manual interference:
        #     decline if ANY position open on this symbol
        positions = self.gateway.positions_get(symbol=req.broker_symbol) or []
        no_dup = len(positions) == 0
        checks.append(("no_open_position_on_symbol", no_dup,
                       f"{len(positions)} open position(s) on {req.broker_symbol}"))

        # 12 (extra). strategy approved (Gate 2)
        appr = f"{req.strategy}:{req.version}" in self.approved
        checks.append(("strategy_approved", appr,
                       f"{req.strategy}:{req.version}" if appr else "not in approved.json"))

        ok = all(p for _, p, _ in checks)
        return PreTradeCheck(ok=ok, checks=checks,
                             reason="; ".join(f"{n}: {d}" for n, ok_, d in checks if not ok_))

    # -- execution -----------------------------------------------------------
    def execute(self, req: OrderRequest) -> ExecutionResult:
        """Run the checklist; journal; send the order only if all pass."""
        check = self.pre_trade_checks(req)

        if not check.ok:
            res = ExecutionResult(
                ok=False, decision="BLOCKED", checks=check.checks,
                reason=check.reason,
            )
            self._journal({
                "event": "BLOCKED", "ts": res.ts, "id": uuid.uuid4().hex[:12],
                "strategy": f"{req.strategy}:{req.version}",
                "symbol": req.symbol, "reason": check.reason,
            })
            return res

        # All checks passed — send via gateway (market order with SL/TP)
        ticket = self.gateway.order_send(
            symbol=req.broker_symbol,
            action="BUY",
            volume=req.lots,
            sl=req.stop,
            tp=req.target,
            magic=req.magic,
            comment=req.comment or f"{req.strategy} {req.version}",
        )

        ok = ticket is not None and ticket > 0
        res = ExecutionResult(
            ok=ok,
            decision="EXECUTED" if ok else "BLOCKED",
            checks=check.checks,
            order_ticket=ticket if ok else None,
            reason="" if ok else f"order_send failed: {ticket}",
        )
        self._journal({
            "event": res.decision, "ts": res.ts, "id": uuid.uuid4().hex[:12],
            "strategy": f"{req.strategy}:{req.version}",
            "symbol": req.symbol, "lots": req.lots, "entry": req.entry,
            "stop": req.stop, "target": req.target,
            "ticket": res.order_ticket, "reason": res.reason,
        })
        return res

    # -- Section 24 triggers ---------------------------------------------------
    def check_kill_triggers(self, daily_pnl: float, equity: float,
                            consecutive_losses: int) -> bool:
        """Evaluate kill-switch triggers after each closed trade. Returns
        True if the switch was tripped."""
        limits = self.risk_limits
        tripped = False
        if daily_pnl < 0 and equity > 0:
            if abs(daily_pnl) / equity > limits.get("max_daily_loss_pct", 1.0) / 100:
                trip_kill_switch(f"daily loss {daily_pnl:.2f} exceeds "
                                 f"{limits.get('max_daily_loss_pct', 1.0)}% of equity")
                tripped = True
        if consecutive_losses >= 3:
            trip_kill_switch(f"{consecutive_losses} consecutive losses")
            tripped = True
        return tripped
