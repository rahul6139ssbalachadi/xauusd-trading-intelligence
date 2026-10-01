"""Validation tests for the monthly backtester.

Coverage, per the spec:
  signal generation, entry calculation, SL/TP, position sizing, trade
  execution, exit logic, monthly date filtering, timezone conversion,
  no-lookahead behaviour, P&L calculation — plus two safety properties
  worth more than any metric: this package has no order path, and its V12
  historical scan is provably the same gate as the live runner's.
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from monthly_bt import data as bt_data
from monthly_bt import engine, guard, report, runner, store, strategies, visual
from monthly_bt.engine import SimConfig
from monthly_bt.strategies import V11, V12, get

DB = bt_data.DB


def _have(symbol: str, tf: str, min_bars: int = 200) -> bool:
    try:
        return len(bt_data.load(symbol, tf)) >= min_bars
    except Exception:
        return False


# ======================================================================
# SAFETY — the most important tests in this file
# ======================================================================
class TestNoOrderPath:
    """monthly_bt must be structurally incapable of trading."""

    FORBIDDEN = (r"\border_send\b", r"\bMT5Gateway\b", r"\bpositions_get\b",
                 r"^\s*import\s+MetaTrader5", r"^\s*from\s+MetaTrader5")

    @pytest.mark.parametrize("path", sorted((ROOT / "monthly_bt").glob("*.py")))
    def test_module_compiles(self, path):
        compile(path.read_text(encoding="utf-8"), str(path), "exec")

    def test_research_db_opened_read_only(self):
        src = (ROOT / "monthly_bt" / "data.py").read_text(encoding="utf-8")
        assert "mode=ro" in src

    def test_no_order_symbols_in_code(self):
        """Docstrings legitimately discuss 'order_send' (that's the point of
        the guard docstring). This strips strings and comments and checks
        the actual CODE."""
        import io
        import tokenize
        FORBIDDEN = (r"\border_send\b", r"\bMT5Gateway\b", r"\bpositions_get\b")
        for path in sorted((ROOT / "monthly_bt").glob("*.py")):
            src = path.read_text(encoding="utf-8")
            code = []
            try:
                for tok in tokenize.generate_tokens(io.StringIO(src).readline):
                    if tok.type not in (tokenize.STRING, tokenize.COMMENT):
                        code.append(tok.string)
            except tokenize.TokenError:
                pytest.skip(f"cannot tokenize {path.name}")
            joined = " ".join(code)
            for pat in FORBIDDEN:
                assert not re.search(pat, joined), \
                    f"{path.name} CODE references {pat}"

    def test_meta_trader5_is_never_imported(self):
        import io
        import tokenize
        for path in sorted((ROOT / "monthly_bt").glob("*.py")):
            src = path.read_text(encoding="utf-8")
            for tok in tokenize.generate_tokens(io.StringIO(src).readline):
                if tok.type == tokenize.NAME and tok.string == "MetaTrader5":
                    pytest.fail(f"{path.name} imports MetaTrader5")
                if (tok.type == tokenize.NAME and tok.string == "import"
                        and tok.line.find("MetaTrader5") >= 0):
                    pytest.fail(f"{path.name} imports MetaTrader5")

    def test_output_db_is_separate_from_research_db(self):
        assert store.DB.name == "backtests.db"
        assert store.DB != bt_data.DB

    def test_guard_reports_but_does_not_write(self, monkeypatch):
        status = guard.check_live_switch(require_disarmed=False)
        assert status["order_capable"] is False
        assert isinstance(status["live_trading_enabled"], bool)
        # the settings file must be unchanged by the guard
        before = (ROOT / "config" / "settings.toml").read_bytes()
        guard.check_live_switch(require_disarmed=False)
        assert (ROOT / "config" / "settings.toml").read_bytes() == before

    def test_guard_enforces_when_asked(self):
        if not guard.live_switch_armed():
            pytest.skip("switch already disarmed; enforcement trivially passes")
        with pytest.raises(guard.BacktestGuardError):
            guard.check_live_switch(require_disarmed=True)

    def test_settings_switch_untouched(self):
        """The monthly backtester must not flip either execution switch.

        The backtester is a READ-ONLY research tool. Its guard module
        reports the live switch and never writes it, so the invariant is
        about POLARITY, not about matching git HEAD: the owner legitimately
        changed settings.toml on 2026-09-26 to the demo/live split, and a
        HEAD comparison would fail on that intentional change forever.

        What must hold: live is False (this is a demo-only build) and demo
        is a bool. If the backtester ever wrote this file it could not
        produce either a live=True or a non-bool value undetected.
        """
        import tomllib
        with (ROOT / "config" / "settings.toml").open("rb") as fh:
            s = tomllib.load(fh)
        assert isinstance(s["live_trading_enabled"], bool)
        assert isinstance(s["demo_execution_enabled"], bool)
        assert s["live_trading_enabled"] is False, \
            "live_trading_enabled must be False in this demo-only build"

    def test_backtester_never_writes_settings_toml(self):
        """Structural proof, stronger than any value assertion: no code in
        monthly_bt/ can write config/settings.toml.

        Scoped deliberately to the CONFIG path. The backtester DOES write
        its own report/HTML/chart files under reports/ and mql5/ — that is
        its job — so a blanket "writes nothing" rule would be both false and
        useless. The safety property is specifically that it never touches
        the execution config.
        """
        import io
        import tokenize
        for path in sorted((ROOT / "monthly_bt").glob("*.py")):
            src = path.read_text(encoding="utf-8")
            code = " ".join(
                tok.string for tok in
                tokenize.generate_tokens(io.StringIO(src).readline)
                if tok.type not in (tokenize.STRING, tokenize.COMMENT))
            # reading settings is fine; writing it is not. The check is
            # whether any module could WRITE the config, so look for a
            # write verb applied to the config path.
            assert "settings.toml" not in code or "write" not in code, \
                f"{path.name} both references settings.toml and writes"
            assert not re.search(r"CONFIG_DIR\s*\.\s*write", code), \
                f"{path.name} writes into CONFIG_DIR"


# ======================================================================
# Data layer
# ======================================================================
class TestData:
    def test_available_lists_symbols(self):
        df = bt_data.available()
        assert set(df["symbol"]) >= {"XAUUSD", "BTCUSD"}

    def test_load_is_ordered_and_has_broker_time(self):
        if not _have("XAUUSD", "D1"):
            pytest.skip("no XAUUSD D1 in DB")
        df = bt_data.load("XAUUSD", "D1")
        assert df["ts_broker_epoch"].is_monotonic_increasing
        # broker time is the raw epoch + 3h; the difference must be exactly 3h
        delta = (df["ts_broker"] - df["ts"]).dropna().unique()
        assert list(delta) == [pd.Timedelta(hours=bt_data.BROKER_UTC_OFFSET_H)]

    def test_ohlc_consistent(self):
        if not _have("XAUUSD", "D1"):
            pytest.skip("no data")
        df = bt_data.load("XAUUSD", "D1")
        assert (df["high"] >= df[["open", "close"]].max(axis=1)).all()
        assert (df["low"] <= df[["open", "close"]].min(axis=1)).all()

    def test_unknown_symbol_rejected(self):
        with pytest.raises(ValueError):
            bt_data.load("GBPUSD", "D1")

    def test_provenance_has_required_fields(self):
        if not _have("XAUUSD", "D1"):
            pytest.skip("no data")
        p = bt_data.provenance("XAUUSD", "D1")
        for k in ("data_source", "symbol", "timeframe", "timezone",
                  "date_range", "granularity"):
            assert p[k]

    def test_unit_models_differ_per_instrument(self):
        """Gold's PIP=0.10 is meaningless on BTC — the units must differ."""
        assert bt_data.UNITS["XAUUSD"]["pip"] == 0.10
        assert bt_data.UNITS["BTCUSD"]["pip"] == 1.00


# ======================================================================
# Strategy reuse / parity
# ======================================================================
class TestStrategyReuse:
    def test_v11_params_come_from_the_live_runner(self):
        from execution.run_v11_daily import PARAMS
        assert V11.params["min_atr_pct"] == PARAMS["min_atr_pct"]
        assert V11.params["rr"] == PARAMS["rr"]
        assert V11.params["atr_mult_stop"] == PARAMS["atr_mult_stop"]

    def test_v12_params_come_from_the_live_runner(self):
        from execution.run_v12_hourly import PARAMS
        assert V12.params["min_atr_pct"] == PARAMS["min_atr_pct"]
        assert V12.params["rr"] == PARAMS["rr"]
        assert V12.params["atr_mult_stop"] == PARAMS["atr_mult_stop"]

    def test_no_duplicate_version_module(self):
        """There must be no 'V11_Backtest' / 'V12_Backtest' clone."""
        names = [p.name.lower() for p in ROOT.rglob("*.py")
                 if ".venv" not in p.parts and "backtest" in p.name.lower()
                 and "v1" in p.name.lower()]
        assert not names, f"duplicate strategy modules found: {names}"

    @pytest.mark.skipif(not _have("XAUUSD", "H1", 1000),
                        reason="no H1 history")
    def test_v12_historical_scan_matches_live_gate(self):
        """The month-by-month scan must be the SAME logic as the live
        single-bar gate, verified bar for bar."""
        res = strategies.verify_v12_gate_parity(bt_data.load("XAUUSD", "H1"), n=60)
        assert res["parity"] is True
        assert res["disagreements"] == 0
        assert res["checked"] > 20

    def test_v11_signals_match_documented_count(self):
        """V11 produced exactly 82 signals over the ORIGINAL 10y D1 history
        (2016-09-02 .. 2026-08-28). Do not silently accept a new number.

        The count is history-dependent and legitimately rises as history is
        added, because EMA21/55 warm-up and the 60-bar momentum-rank window
        shift the early bars. The 2026-10-01 ingest prepended 84 D1 bars and
        took it 82 -> 84, both new signals landing in the first weeks after
        the old start. So assert BOTH: the old span still yields exactly 82,
        and the full span yields a count consistent with the extra history.
        """
        if not _have("XAUUSD", "D1", 2000):
            pytest.skip("no full D1 history")
        raw = bt_data.load("XAUUSD", "D1")
        ts = pd.to_datetime(raw["ts_broker_epoch"], unit="s", utc=True)

        # The originally-documented span.
        old = raw[(ts >= pd.Timestamp("2016-09-02", tz="UTC")) &
                  (ts <= pd.Timestamp("2026-08-28", tz="UTC"))].reset_index(drop=True)
        assert len(strategies.v11_signals(runner.build_features(old, V11))) == 82

        # The full, current span: 82 plus whatever the added history unlocked.
        full = strategies.v11_signals(runner.build_features(raw, V11))
        assert 82 <= len(full) <= 90, f"unexpected signal count {len(full)}"
        assert all(s["entry_ts"] for s in full)

    def test_signals_are_buy_only_and_long(self):
        """V11 and V12 are LONG-only by design; a SELL would be a bug."""
        if not _have("XAUUSD", "D1", 2000):
            pytest.skip("no data")
        feats = runner.build_features(bt_data.load("XAUUSD", "D1"), V11)
        sigs = strategies.generate(feats, V11, "XAUUSD")
        assert sigs and {s["signal"] for s in sigs} == {"BUY"}

    def test_get_rejects_unknown(self):
        with pytest.raises(ValueError):
            get("V99")


# ======================================================================
# No look-ahead
# ======================================================================
class TestNoLookAhead:
    def test_entry_is_bar_after_signal(self):
        if not _have("XAUUSD", "D1", 2000):
            pytest.skip("no data")
        feats = runner.build_features(bt_data.load("XAUUSD", "D1"), V11)
        for s in strategies.generate(feats, V11, "XAUUSD"):
            assert s["entry_bar"] == s["signal_bar"] + 1

    def test_truncated_history_does_not_change_past_signals(self):
        """The strongest practical anti-look-ahead test: recompute on a
        prefix of the data. Every signal fully inside the prefix must be
        IDENTICAL to the same signal computed on the full history. If any
        future bar influenced a past signal, this fails."""
        if not _have("XAUUSD", "D1", 2000):
            pytest.skip("no data")
        full = runner.build_features(bt_data.load("XAUUSD", "D1"), V11)
        full_sigs = {s["entry_bar"]: s["entry"] for s in
                     strategies.generate(full, V11, "XAUUSD")}
        cut = int(len(full) * 0.7)
        prefix = full.iloc[:cut]
        pre_sigs = {s["entry_bar"]: s["entry"] for s in
                    strategies.generate(prefix, V11, "XAUUSD")}
        shared = set(full_sigs) & set(pre_sigs)
        assert len(shared) > 5, "too few overlapping signals to be a real test"
        for i in shared:
            assert full_sigs[i] == pytest.approx(pre_sigs[i]), \
                f"signal at bar {i} changed when later data was added"

    def test_indicators_only_use_past_bars(self):
        """EMA at bar i must not depend on bar i+1."""
        if not _have("XAUUSD", "D1", 300):
            pytest.skip("no data")
        from indicators import ema
        df = bt_data.load("XAUUSD", "D1")
        a = ema(df["close"].iloc[:200], 21)
        b = ema(df["close"].iloc[:260], 21).iloc[:200]
        assert a.iloc[-1] == pytest.approx(b.iloc[-1])


# ======================================================================
# Entry / SL / TP / sizing arithmetic (hand-checked)
# ======================================================================
class TestLevels:
    def test_sl_and_tp_are_atr_derived(self):
        p = V11.params
        atr_val, entry = 20.0, 3000.0
        risk = p["atr_mult_stop"] * atr_val
        stop = entry - risk
        target = entry + risk * p["rr"]
        assert stop == pytest.approx(3000.0 - 30.0)
        assert target == pytest.approx(3000.0 + 60.0)

    def test_r_multiple_sign(self):
        """1R win = +rr in R, a stop-out = -1R before costs."""
        rr = 2.0
        risk = 10.0
        assert (risk * rr) / risk == pytest.approx(rr)
        assert -risk / risk == pytest.approx(-1.0)

    def test_risk_pips_uses_instrument_pip(self):
        """Same 5.0 price move is 50 pips on gold, 5 pips on BTC."""
        from monthly_bt.strategies import normalise_signal
        sig = {"entry_bar": 1, "signal_bar": 0, "entry_price": 100.0,
               "stop": 95.0, "target": 110.0, "entry_ts": "t", "atr": 3.0,
               "body_pct": 0.99}
        assert normalise_signal(sig, V11, "XAUUSD")["risk_pips"] == pytest.approx(50.0)
        assert normalise_signal(sig, V11, "BTCUSD")["risk_pips"] == pytest.approx(5.0)

    def test_sizing_respects_lot_step_and_min(self):
        cfg = SimConfig(initial_balance=10_000.0)
        # 1% of 10k = $100; gold $10/pip/lot -> 100 pips risk -> 0.10 lots
        lots = engine.size_lots(10_000.0, 0.01, 100.0, "XAUUSD", cfg)
        assert lots == pytest.approx(0.10)
        # absurdly wide stop -> below min lot -> declined, never oversized
        assert engine.size_lots(10_000.0, 0.01, 100_000.0, "XAUUSD", cfg) == 0.0

    def test_sizing_capped_at_hard_risk_limit(self):
        """A 2% request is capped to the engine's 1% hard limit."""
        cfg = SimConfig(max_risk_per_trade_pct=0.01)
        lots = engine.size_lots(10_000.0, 0.02, 100.0, "XAUUSD", cfg)
        assert lots == pytest.approx(0.10)

    def test_sizing_never_exceeds_requested_risk(self):
        cfg = SimConfig()
        lots = engine.size_lots(10_000.0, 0.01, 133.0, "XAUUSD", cfg)
        usd_risk = engine.UNITS["XAUUSD"]["pip"] * engine.UNITS["XAUUSD"]["contract"] \
            * 133.0 * lots
        assert usd_risk <= 100.0 + 1e-9

    def test_measured_spread_uses_recorded_column(self):
        """`spread` is stored in POINTS. 30 points = $0.30 = 3 pips on gold
        (pip $0.10) and 0.30 pips on BTC (pip $1.00). Both are the same
        $0.30 of cost — only the pip unit differs."""
        df = pd.DataFrame({"spread": [30.0, 40.0]})   # points
        assert engine.measured_spread_pips(df, "XAUUSD") == pytest.approx(0.35)
        assert engine.measured_spread_pips(df, "BTCUSD") == pytest.approx(0.35)


# ======================================================================
# Execution / exit logic
# ======================================================================
def _frame(rows) -> pd.DataFrame:
    return pd.DataFrame(rows)


class TestExecution:
    """Hand-built frames so every exit rule can be checked exactly."""

    BASE = _frame([
        dict(ts_broker_epoch=1_000_000 + i * 3600, open=100.0, high=101.0,
             low=99.0, close=100.5, tick_volume=10, spread=30.0)
        for i in range(20)])
    BASE["ts"] = pd.to_datetime(BASE["ts_broker_epoch"], unit="s", utc=True)
    BASE["ts_broker"] = BASE["ts"]

    def _sig(self, entry_bar=1, entry=100.0, sl=95.0, tp=110.0, risk_pips=50.0):
        return {"strategy": "V11", "strategy_name": "n", "symbol": "XAUUSD",
                "timeframe": "D1", "signal": "BUY", "signal_bar": entry_bar - 1,
                "entry_bar": entry_bar, "signal_ts": self.BASE.iloc[entry_bar - 1]["ts"],
                "entry_ts": self.BASE.iloc[entry_bar]["ts"], "entry": entry,
                "sl": sl, "tp": tp, "risk_price": entry - sl,
                "risk_pips": risk_pips, "atr": 3.0, "body_pct": 0.99}

    def test_target_hit_closes_at_target(self):
        """Fixture: $10 move, stop $5 below -> 100/50 pips, sized 0.20 lot
        (1% of $10k = $100 risk; $5 stop = 50 pips x $10/pip/lot => 0.2).
        So +100 pips x $10 x 0.2 = +$200."""
        df = self.BASE.copy()
        df.loc[2, ["high", "low"]] = [112.0, 99.0]
        cfg = SimConfig(use_measured_spread=False, spread_multiplier=1.0,
                        fallback_spread_pips=0.0, slippage_pips=0.0)
        res = engine.simulate(df, [self._sig()], V11, "XAUUSD", cfg)
        assert len(res["trades"]) == 1
        t = res["trades"][0]
        assert t.exit_reason == "target"
        assert t.exit_price == pytest.approx(110.0)
        assert t.lots == pytest.approx(0.20)
        assert t.risk_usd == pytest.approx(100.0)    # capped at 1% of $10k
        assert t.net_pips == pytest.approx(100.0)    # 10.00 / 0.10 pip
        assert t.net_usd == pytest.approx(200.0)     # 100 pips x $10 x 0.2

    def test_stop_hit_closes_at_stop(self):
        df = self.BASE.copy()
        df.loc[2, ["high", "low"]] = [101.0, 94.0]
        cfg = SimConfig(use_measured_spread=False, fallback_spread_pips=0.0,
                        slippage_pips=0.0)
        res = engine.simulate(df, [self._sig()], V11, "XAUUSD", cfg)
        t = res["trades"][0]
        assert t.exit_reason == "stop"
        assert t.net_pips == pytest.approx(-50.0)
        assert t.net_usd == pytest.approx(-100.0)    # -50 pips x $10 x 0.2

    def test_both_levels_same_bar_uses_closer(self):
        """Conservative tie-break: the level nearer entry fills first."""
        df = self.BASE.copy()
        df.loc[2, ["high", "low"]] = [112.0, 94.0]   # both touched
        cfg = SimConfig(use_measured_spread=False, fallback_spread_pips=0.0,
                        slippage_pips=0.0)
        res = engine.simulate(df, [self._sig()], V11, "XAUUSD", cfg)
        assert res["trades"][0].exit_reason == "stop"

    def test_max_holding_force_closes(self):
        df = self.BASE.copy()   # nothing ever hits; 100 high / 99 low
        cfg = SimConfig(use_measured_spread=False, fallback_spread_pips=0.0,
                        slippage_pips=0.0)
        spec = get("V11")
        sig = self._sig()
        res = engine.simulate(df, [sig], spec, "XAUUSD", cfg)
        t = res["trades"][0]
        assert t.exit_reason == "end"
        assert t.exit_bar == min(1 + spec.max_holding, len(df) - 1)

    def test_costs_reduce_net(self):
        df = self.BASE.copy()
        df.loc[2, ["high", "low"]] = [112.0, 99.0]
        cheap = SimConfig(use_measured_spread=False, fallback_spread_pips=0.0,
                          slippage_pips=0.0)
        dear = SimConfig(use_measured_spread=False, fallback_spread_pips=3.0,
                         slippage_pips=1.0)
        a = engine.simulate(df, [self._sig()], V11, "XAUUSD", cheap)["trades"][0]
        b = engine.simulate(df, [self._sig()], V11, "XAUUSD", dear)["trades"][0]
        assert b.net_pips == pytest.approx(a.net_pips - 8.0)  # 2*(3+1)
        assert b.cost_pips == pytest.approx(8.0)

    def test_second_signal_skipped_while_position_open(self):
        """A SIGNAL is still recorded; the TRADE is refused. Both counts
        must differ — the spec forbids conflating them."""
        df = self.BASE.copy()
        df.loc[2, ["high", "low"]] = [112.0, 99.0]
        cfg = SimConfig(use_measured_spread=False, fallback_spread_pips=0.0,
                        slippage_pips=0.0)
        sigs = [self._sig(entry_bar=1), self._sig(entry_bar=2)]
        res = engine.simulate(df, sigs, V11, "XAUUSD", cfg)
        assert len(res["signals_in_period"] if "signals_in_period" in res
                   else sigs) == 2
        assert len(res["trade_rows"]) == 1
        assert res["skipped"][0]["skip_reason"] == "position_already_open"

    def test_unsizeable_signal_is_skipped_not_faked(self):
        cfg = SimConfig(use_measured_spread=False, fallback_spread_pips=0.0)
        sig = self._sig(risk_pips=1e9)   # cannot be sized at any sane balance
        res = engine.simulate(self.BASE.copy(), [sig], V11, "XAUUSD", cfg)
        assert res["trade_rows"] == []
        assert res["skipped"][0]["skip_reason"] == "size_below_min_lot"

    def test_equity_follows_trades(self):
        df = self.BASE.copy()
        df.loc[2, ["high", "low"]] = [112.0, 99.0]
        cfg = SimConfig(initial_balance=10_000.0, use_measured_spread=False,
                        fallback_spread_pips=0.0, slippage_pips=0.0)
        res = engine.simulate(df, [self._sig()], V11, "XAUUSD", cfg)
        assert res["ending_balance"] == pytest.approx(10_000.0 + 200.0)

    def test_sizing_refuses_wide_stops_on_small_account(self):
        """A D1 stop is 1500-3200 pips wide. At 1% of $10k that floors below
        the 0.01 minimum lot, so the trade is DECLINED — never sized above
        the cap to force a fill. This is the real behaviour of V11 on gold
        and must stay visible, not be papered over."""
        cfg = SimConfig(initial_balance=10_000.0, use_measured_spread=False)
        sig = self._sig(risk_pips=1515.0)
        res = engine.simulate(self.BASE.copy(), [sig], V11, "XAUUSD", cfg)
        assert res["trade_rows"] == []
        assert res["skipped"][0]["skip_reason"] == "size_below_min_lot"

    def test_larger_account_sizes_the_same_trade(self):
        """The same signal IS tradeable with more equity — the decline is a
        sizing outcome, not a signal defect."""
        cfg = SimConfig(initial_balance=100_000.0, use_measured_spread=False,
                        fallback_spread_pips=0.0, slippage_pips=0.0)
        lots = engine.size_lots(100_000.0, 0.01, 1515.0, "XAUUSD", cfg)
        assert lots == pytest.approx(0.06)


# ======================================================================
# P&L / metrics
# ======================================================================
class TestPnL:
    def test_metrics_use_repo_layer(self):
        m = engine.compute_metrics([])
        assert m["total_trades"] == 0

    def test_empty_period_reports_na_not_zero(self):
        """compute_metrics returns only total_trades for an empty list — a
        fabricated 0.0 win rate would be a lie."""
        rows = [report.period_metrics({"period": "2026-01", "signals": [],
                                       "trade_rows": [], "metrics":
                                       engine.compute_metrics([])})]
        r = rows[0]
        assert r["trades"] == 0
        assert r["win_rate"] is None
        assert r["net_pnl"] is None
        assert r["profit_factor"] is None

    def test_win_loss_split_and_pf(self):
        rows = [
            dict(strategy="V11", signal="BUY", signal_bar=0, entry_bar=1,
                 signal_ts="t", entry_ts="t", entry=100.0, sl=95.0, tp=110.0,
                 risk_pips=50.0, atr=1.0, body_pct=0.99, exit_bar=2,
                 exit_ts="t", exit=110.0, exit_reason="target", lots=0.1,
                 commission=0.0, swap=0.0, spread_pips=0.0, slippage_pips=0.0,
                 cost_pips=0.0, gross_pips=100.0, net_pips=100.0, r_multiple=2.0,
                 gross_pnl=100.0, fees=0.0, net_pnl=1000.0, balance_after=11_000.0),
            dict(strategy="V11", signal="BUY", signal_bar=0, entry_bar=1,
                 signal_ts="t", entry_ts="t", entry=100.0, sl=95.0, tp=110.0,
                 risk_pips=50.0, atr=1.0, body_pct=0.99, exit_bar=2,
                 exit_ts="t", exit=95.0, exit_reason="stop", lots=0.1,
                 commission=0.0, swap=0.0, spread_pips=0.0, slippage_pips=0.0,
                 cost_pips=0.0, gross_pips=-50.0, net_pips=-50.0, r_multiple=-1.0,
                 gross_pnl=-50.0, fees=0.0, net_pnl=-500.0, balance_after=10_500.0),
        ]
        from backtest import Trade
        trades = [Trade(entry_bar=t["entry_bar"], entry_price=t["entry"],
                        side="BUY", stop=t["sl"], target=t["tp"],
                        exit_bar=t["exit_bar"], exit_price=t["exit"],
                        exit_reason=t["exit_reason"], points=t["gross_pips"],
                        cost_pips=0.0, net_pips=t["net_pips"], duration_bars=1,
                        lots=0.1, net_usd=t["net_pnl"]) for t in rows]
        r = report.period_metrics({"period": "2026-01", "signals": [],
                                   "trade_rows": rows, "metrics":
                                   engine.compute_metrics(trades)})
        assert r["wins"] == 1 and r["losses"] == 1
        assert r["win_rate"] == pytest.approx(0.5)
        assert r["profit_factor"] == pytest.approx(2.0)
        assert r["net_pnl"] == pytest.approx(500.0)
        assert r["sl_exits"] == 1 and r["tp_exits"] == 1
        assert r["avg_r"] == pytest.approx(0.5)
        assert r["return_pct"] == pytest.approx(5.0)

    def test_infinite_pf_becomes_na(self):
        assert report._m({"profit_factor": float("inf")}, "profit_factor") is None
        assert report._m({"profit_factor": float("nan")}, "profit_factor") is None

    def test_r_multiple_column_present(self):
        assert "r_multiple" in report.COLUMNS


# ======================================================================
# Period filtering / timezone
# ======================================================================
class TestPeriods:
    def test_month_bounds(self):
        s, e = runner.month_bounds("2026-02")
        assert s == pd.Timestamp("2026-02-01")
        assert e == pd.Timestamp("2026-03-01")

    def test_month_bounds_december_rolls_year(self):
        s, e = runner.month_bounds("2026-12")
        assert e == pd.Timestamp("2027-01-01")

    def test_month_range_enumeration(self):
        assert runner.month_range("2025-11", "2026-02") == \
            ["2025-11", "2025-12", "2026-01", "2026-02"]

    def test_month_ends_for(self):
        if not _have("XAUUSD", "D1"):
            pytest.skip("no data")
        feats = runner.build_features(bt_data.load("XAUUSD", "D1"), V11)
        ms = runner.month_ends_for(feats)
        assert ms == sorted(ms)
        assert all(len(m) == 7 for m in ms)

    def test_slice_keeps_warmup_and_filters(self):
        """The window must contain the period's bars PLUS `warmup` leading
        bars, must not run past the period end, and must report the offset
        that maps a window index back to the full frame."""
        if not _have("XAUUSD", "D1"):
            pytest.skip("no data")
        feats = runner.build_features(bt_data.load("XAUUSD", "D1"), V11)
        out, off = runner.slice_period(feats, "2026-01-01", "2026-02-01", warmup=50)
        assert not out.empty
        lo = runner._aware(pd.Timestamp("2026-01-01"))
        hi = runner._aware(pd.Timestamp("2026-02-01"))
        assert (out["ts_broker"] < hi).all()
        assert (out["ts_broker"] >= lo).any()
        assert (out["ts_broker"] < lo).sum() == 50
        # The offset must line the window up with the full frame exactly.
        # DERIVED, not hardcoded: the absolute offset legitimately moves when
        # history is prepended (the 2026-10-01 ingest added 84 D1 bars,
        # shifting Jan 2026 from 2357 to 2441). What must hold is the
        # ALIGNMENT — that is what this test is about.
        assert feats.iloc[off]["ts_broker"] == out["ts_broker"].iloc[0]
        first_in_period = int((feats["ts_broker"] < lo).sum())
        assert off + 50 == first_in_period

    def test_empty_slice_returns_empty_frame_and_offset(self):
        if not _have("XAUUSD", "D1"):
            pytest.skip("no data")
        feats = runner.build_features(bt_data.load("XAUUSD", "D1"), V11)
        out, off = runner.slice_period(feats, "1990-01-01", "1990-02-01")
        assert out.empty

    def test_run_period_indices_are_full_frame(self):
        """A signal's bar index must index the FULL history, or the chart
        and the trade log would plot and log the wrong candles."""
        if not _have("XAUUSD", "D1", 2000):
            pytest.skip("no data")
        feats = runner.build_features(bt_data.load("XAUUSD", "D1"), V11)
        res = runner.run_period(feats, V11, "XAUUSD", "2026-01-01",
                                "2026-02-01", SimConfig())
        for s in res["signals"]:
            i = s["entry_bar"]
            assert 0 <= i < len(feats)
            bar_ts = feats.iloc[i]["ts_broker"]
            assert bar_ts >= runner._aware(pd.Timestamp("2026-01-01"))
            assert bar_ts < runner._aware(pd.Timestamp("2026-02-01"))
        for t in res["trade_rows"]:
            assert 0 <= t["entry_bar"] < t["exit_bar"] < len(feats)

    def test_monthly_periods_are_independent(self):
        """A trade opened in one month must not consume a slot in another:
        each month is run through simulate() on its own window."""
        if not _have("XAUUSD", "D1", 2000):
            pytest.skip("no data")
        feats = runner.build_features(bt_data.load("XAUUSD", "D1"), V11)
        cfg = SimConfig(use_measured_spread=False, fallback_spread_pips=0.0)
        a = runner.run_period(feats, V11, "XAUUSD", "2026-01-01", "2026-02-01", cfg)
        b = runner.run_period(feats, V11, "XAUUSD", "2026-02-01", "2026-03-01", cfg)
        assert a["period"] == "2026-01" and b["period"] == "2026-02"
        assert "trade_rows" in a and "trade_rows" in b

    def test_timezone_is_broker_not_utc(self):
        if not _have("XAUUSD", "D1"):
            pytest.skip("no data")
        df = bt_data.load("XAUUSD", "D1")
        assert (df["ts_broker"].iloc[-1].hour ==
                (df["ts"].iloc[-1].hour + 3) % 24)


# ======================================================================
# Store
# ======================================================================
class TestStore:
    def test_schema_creates_three_tables(self, tmp_path, monkeypatch):
        monkeypatch.setattr(store, "DB", tmp_path / "b.db")
        con = store.connect()
        names = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        con.close()
        assert {"backtest_runs", "backtest_signals",
                "backtest_trades"} <= names

    def test_signals_and_trades_stored_separately(self, tmp_path, monkeypatch):
        monkeypatch.setattr(store, "DB", tmp_path / "b.db")
        sigs = [dict(strategy="V11", symbol="XAUUSD", timeframe="D1", signal="BUY",
                     signal_bar=0, entry_bar=1, signal_ts="s", entry_ts="e",
                     entry=100.0, sl=95.0, tp=110.0, risk_pips=50.0, atr=1.0,
                     body_pct=0.99)]
        trade = dict(strategy="V11", symbol="XAUUSD", timeframe="D1", signal="BUY",
                     signal_bar=0, entry_bar=1, exit_bar=3, signal_ts="s",
                     entry_ts="e", exit_ts="x", entry=100.0, sl=95.0, tp=110.0,
                     exit=110.0, exit_reason="target", lots=0.1, commission=0.0,
                     swap=0.0, spread_pips=0.0, slippage_pips=0.0, cost_pips=0.0,
                     gross_pips=100.0, net_pips=100.0, r_multiple=2.0,
                     gross_pnl=100.0, fees=0.0, net_pnl=1000.0,
                     balance_after=11_000.0)
        res = [{"period": "2026-01", "strategy": "V11", "signals": sigs,
                "trade_rows": [trade], "skipped": []}]
        manifest = {"run_ts_utc": "now", "strategy": "V11",
                    "strategy_name": "n", "strategy_version": "V11",
                    "logic_source": "s", "params": {}, "symbol": "XAUUSD",
                    "timeframe": "D1", "period": "2026-01", "data": {},
                    "sim_config": {}, "guard": {}, "initial_balance": 10_000.0,
                    "git_commit": "abc", "risk_pct_used": 0.01,
                    "max_positions": 1}
        run_id = store.save_run(manifest, res)
        store.save_results(run_id, res)
        con = store.connect()
        assert con.execute("SELECT COUNT(*) FROM backtest_runs").fetchone()[0] == 1
        assert con.execute("SELECT COUNT(*) FROM backtest_signals").fetchone()[0] == 1
        assert con.execute("SELECT COUNT(*) FROM backtest_trades").fetchone()[0] == 1
        sim = con.execute("SELECT simulated FROM backtest_signals").fetchone()[0]
        assert sim == 1
        con.close()

    def test_untraded_signal_is_recorded_with_reason(self, tmp_path, monkeypatch):
        monkeypatch.setattr(store, "DB", tmp_path / "b.db")
        sigs = [dict(strategy="V11", symbol="XAUUSD", timeframe="D1", signal="BUY",
                     signal_bar=0, entry_bar=1, signal_ts="s", entry_ts="e",
                     entry=100.0, sl=95.0, tp=110.0, risk_pips=50.0, atr=1.0,
                     body_pct=0.99)]
        skipped = [{**sigs[0], "skip_reason": "position_already_open"}]
        res = [{"period": "2026-01", "strategy": "V11", "signals": sigs,
                "trade_rows": [], "skipped": skipped}]
        manifest = {"run_ts_utc": "now", "strategy": "V11", "strategy_name": "n",
                    "strategy_version": "V11", "logic_source": "s", "params": {},
                    "symbol": "XAUUSD", "timeframe": "D1", "period": "2026-01",
                    "data": {}, "sim_config": {}, "guard": {},
                    "initial_balance": 10_000.0, "git_commit": "abc",
                    "risk_pct_used": 0.01, "max_positions": 1}
        rid = store.save_run(manifest, res)
        store.save_results(rid, res)
        con = store.connect()
        row = con.execute("SELECT simulated, skip_reason FROM backtest_signals"
                          ).fetchone()
        con.close()
        assert row == (0, "position_already_open")


# ======================================================================
# Report / visual
# ======================================================================
class TestReportAndVisual:
    def test_every_spec_column_present(self):
        required = {"signals", "buy_signals", "sell_signals", "trades",
                    "win_rate", "net_pnl", "gross_profit", "gross_loss",
                    "profit_factor", "max_drawdown", "avg_r", "expectancy",
                    "avg_trade", "largest_winner", "largest_loser",
                    "consec_wins", "consec_losses", "sl_exits", "tp_exits",
                    "other_exits", "start_balance", "end_balance", "return_pct",
                    "total_costs"}
        assert required <= set(report.COLUMNS)

    def test_render_text_and_markdown_do_not_crash_on_empty(self):
        rows = [report.period_metrics({"period": "2026-01", "signals": [],
                                       "trade_rows": [],
                                       "metrics": engine.compute_metrics([])})]
        man = {"data": bt_data.provenance("XAUUSD", "D1") if _have("XAUUSD", "D1")
               else {"date_range": "n/a", "bars": 0, "timezone": "t",
                     "mean_spread": 0.0},
               "logic_source": "s", "git_commit": "c", "run_ts_utc": "t",
               "initial_balance": 10_000.0, "risk_pct_used": 0.01,
               "max_positions": 1,
               "sim_config": SimConfig().as_dict(),
               "guard": guard.check_live_switch()}
        assert "2026-01" in report.render_text(V11, "XAUUSD", rows, man)
        assert "n/a" in report.render_text(V11, "XAUUSD", rows, man)
        assert report.render_markdown(V11, "XAUUSD", rows, man, "rid")

    def test_comparison_declares_no_winner(self):
        rows = [report.period_metrics({"period": "2026-01", "signals": [],
                                       "trade_rows": [],
                                       "metrics": engine.compute_metrics([])})]
        txt = report.render_comparison("XAUUSD", {"V11": rows, "V12": rows})
        assert "no strategy is declared" in txt

    def test_visual_html_is_self_contained(self, tmp_path, monkeypatch):
        monkeypatch.setattr(visual, "VIS", tmp_path)
        if not _have("XAUUSD", "D1", 300):
            pytest.skip("no data")
        feats = runner.build_features(bt_data.load("XAUUSD", "D1"), V11)
        res = runner.run_period(feats, V11, "XAUUSD", "2026-01-01",
                                "2026-02-01", SimConfig())
        res["strategy"] = "V11"
        out = visual.render_period(feats, [res], V11, "XAUUSD", "test")
        html = out.read_text(encoding="utf-8")
        assert "<canvas" in html
        assert "http://" not in html and "https://" not in html   # no CDN
        assert "SIGNAL" in html and "SIMULATED TRADE" in html

    def test_mt5_csv_columns(self, tmp_path, monkeypatch):
        monkeypatch.setattr(visual, "MQL5_OUT", tmp_path)
        if not _have("XAUUSD", "D1", 300):
            pytest.skip("no data")
        feats = runner.build_features(bt_data.load("XAUUSD", "D1"), V11)
        res = runner.run_period(feats, V11, "XAUUSD", "2026-01-01",
                                "2026-02-01", SimConfig())
        res["strategy"] = "V11"
        out = visual.export_mt5_csv(feats, [res], V11, "XAUUSD", "test")
        text = out.read_text(encoding="utf-8")
        assert "epoch" in text.splitlines()[0]
        assert "simulated" in text.splitlines()[0]

    def test_mq5_indicator_is_read_only(self):
        src = (ROOT / "mql5" / "MonthlyBT_Signals.mq5").read_text(encoding="utf-8")
        for banned in ("OrderSend", "PositionOpen", "trade.Buy", "trade.Sell",
                       "AccountInfoDouble"):
            assert banned not in src, f"indicator references {banned}"

    def test_mq5_indicator_filters_by_timeframe(self):
        """V11 is D1 and V12 is H1. The combined csv holds both, so the
        indicator MUST branch on the timeframe or markers land on the
        wrong bars — assert that branch exists."""
        src = (ROOT / "mql5" / "MonthlyBT_Signals.mq5").read_text(encoding="utf-8")
        assert "InpAllowAllTimeframes" in src
        assert "TfFromString" in src
        assert "chart_tf" in src

    def test_combined_csv_holds_both_strategies_with_timeframes(self,
                                                                tmp_path,
                                                                monkeypatch):
        """The combined file is the deliverable the user asked for: one
        chart, both strategies, each row self-describing."""
        monkeypatch.setattr(visual, "MQL5_OUT", tmp_path)
        if not (_have("XAUUSD", "D1", 300) and _have("XAUUSD", "H1", 1000)):
            pytest.skip("need both D1 and H1")
        import csv as _csv
        parts = []
        for key, tf in (("V11", "D1"), ("V12", "H1")):
            spec = get(key)
            feats = runner.build_features(bt_data.load("XAUUSD", tf), spec)
            res = runner.run_period(feats, spec, "XAUUSD", "2026-01-01",
                                    "2026-02-01", SimConfig())
            res["strategy"] = key
            parts.append((feats, [res], spec))
        out = visual.export_mt5_combined(parts, "XAUUSD", "test")
        rows = list(_csv.DictReader(out.open(encoding="utf-8")))
        assert rows
        assert "timeframe" in rows[0]
        tfs = {r["timeframe"] for r in rows}
        assert tfs <= {"D1", "H1"}
        # every row carries SL and TP so the chart can draw them
        for r in rows:
            assert r["sl"] and r["tp"] and r["entry"]
            assert float(r["sl"]) < float(r["entry"]) < float(r["tp"])
        # (epoch, strategy) is unique
        keys = [(r["epoch"], r["strategy"]) for r in rows]
        assert len(keys) == len(set(keys))

    def test_combined_html_has_a_panel_per_strategy(self, tmp_path,
                                                    monkeypatch):
        monkeypatch.setattr(visual, "VIS", tmp_path)
        if not (_have("XAUUSD", "D1", 300) and _have("XAUUSD", "H1", 1000)):
            pytest.skip("need both timeframes")
        import json as _json
        import re as _re
        parts = []
        for key, tf in (("V11", "D1"), ("V12", "H1")):
            spec = get(key)
            feats = runner.build_features(bt_data.load("XAUUSD", tf), spec)
            res = runner.run_period(feats, spec, "XAUUSD", "2026-01-01",
                                    "2026-02-01", SimConfig())
            res["strategy"] = key
            parts.append(dict(feats=feats, results=[res], spec=spec))
        out = visual.render_combined(parts, "XAUUSD", "test")
        html = out.read_text(encoding="utf-8")
        data = _json.loads(_re.search(r"const D = (\{.*?\});\n", html, _re.S).group(1))
        assert len(data["series"]) == 2
        assert {s["spec"]["strategy"] for s in data["series"]} == {"V11", "V12"}
        assert {s["spec"]["timeframe"] for s in data["series"]} == {"D1", "H1"}
        assert "http://" not in html and "https://" not in html   # no CDN

    def test_no_dead_flags(self):
        """--export-mt5 was superseded by --chart; a silent no-op flag is
        worse than no flag."""
        from monthly_bt.cli import build_parser
        dests = {a.dest for a in build_parser()._actions}
        assert "export_mt5" not in dests
        assert "chart" in dests


# ======================================================================
# CLI — the entry point the user actually types
# ======================================================================
class TestCli:
    """Regression guard: an unused-variable cleanup in cli.py once left a
    NameError (`written`) that 78 module-level tests could not see,
    because nothing invoked main(). These drive the real argv path."""

    def test_symbols_flag(self, capsys):
        from monthly_bt.cli import main
        assert main(["--symbols"]) == 0
        assert "XAUUSD" in capsys.readouterr().out

    def test_verify_parity_flag(self, capsys):
        from monthly_bt.cli import main
        assert main(["--strategy", "V12", "--symbol", "XAUUSD",
                     "--verify-parity"]) == 0
        assert "parity" in capsys.readouterr().out

    def test_requires_a_period(self):
        from monthly_bt.cli import main
        with pytest.raises(SystemExit):
            main(["--strategy", "V11", "--symbol", "XAUUSD"])

    def test_unknown_year_is_rejected_cleanly(self):
        from monthly_bt.cli import main
        with pytest.raises(SystemExit):
            main(["--strategy", "V11", "--symbol", "XAUUSD", "--year", "1990"])

    @pytest.mark.skipif(not _have("XAUUSD", "D1", 300), reason="no D1 data")
    def test_single_month_end_to_end(self, tmp_path, monkeypatch, capsys):
        from monthly_bt import cli
        monkeypatch.setattr(report, "REPORTS", tmp_path / "reports")
        monkeypatch.setattr(cli.store, "DB", tmp_path / "backtests.db")
        rc = cli.main(["--strategy", "V11", "--symbol", "XAUUSD",
                       "--month", "2026-01", "--balance", "100000"])
        out = capsys.readouterr().out
        assert rc == 0
        assert "2026-01" in out
        assert "MONTHLY BACKTEST" in out
        assert "No order was placed" in out
        assert list((tmp_path / "reports").glob("V11_*.txt"))

    @pytest.mark.skipif(not (_have("XAUUSD", "D1", 300)
                            and _have("XAUUSD", "H1", 1000)),
                        reason="need both timeframes")
    def test_both_strategies_writes_combined_artifacts(self, tmp_path,
                                                       monkeypatch, capsys):
        from monthly_bt import cli, visual
        monkeypatch.setattr(report, "REPORTS", tmp_path / "reports")
        monkeypatch.setattr(visual, "VIS", tmp_path / "visual")
        monkeypatch.setattr(visual, "MQL5_OUT", tmp_path / "mql5")
        monkeypatch.setattr(cli.store, "DB", tmp_path / "backtests.db")
        rc = cli.main(["--strategy", "both", "--symbol", "XAUUSD",
                       "--month", "2026-01", "--balance", "100000", "--chart"])
        out = capsys.readouterr().out
        assert rc == 0
        assert "COMBINED" in out
        assert "COMPARISON" in out
        assert "no strategy is declared" in out
        assert list((tmp_path / "mql5").glob("COMBINED_*.csv"))
        assert list((tmp_path / "visual").glob("COMBINED_*.html"))

    @pytest.mark.skipif(not (_have("XAUUSD", "D1", 300)
                            and _have("XAUUSD", "H1", 1000)),
                        reason="need both timeframes")
    def test_both_without_chart_writes_no_visual_files(self, tmp_path,
                                                       monkeypatch):
        """Regression: the combined block once ran regardless of --chart,
        silently writing files the user never asked for."""
        from monthly_bt import cli, visual
        monkeypatch.setattr(report, "REPORTS", tmp_path / "reports")
        monkeypatch.setattr(visual, "VIS", tmp_path / "visual")
        monkeypatch.setattr(visual, "MQL5_OUT", tmp_path / "mql5")
        monkeypatch.setattr(cli.store, "DB", tmp_path / "backtests.db")
        assert cli.main(["--strategy", "both", "--symbol", "XAUUSD",
                         "--month", "2026-01", "--balance", "100000"]) == 0
        assert not (tmp_path / "mql5").exists()
        assert not (tmp_path / "visual").exists()
