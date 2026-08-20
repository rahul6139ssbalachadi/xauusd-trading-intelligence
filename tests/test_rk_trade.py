"""Tests for RK Trade multi-user live analysis platform."""
from __future__ import annotations

import sys
import tempfile
import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rk_trade.users import UserManager
from rk_trade.strategies import StrategyRegistry
from rk_trade.journal import DecisionJournal
from rk_trade.signal_engine import SignalEngine, SignalDecision, _compute_confidence
from montecarlo import run_monte_carlo, MCConfig
from candles import analyze_patterns, render_pattern_report
from strategy import Strategy


def _tmp_um():
    tmp = tempfile.mkdtemp()
    return UserManager(base_dir=Path(tmp) / "users")


def test_register_creates_user():
    um = _tmp_um()
    user = um.register("alice")
    assert user.username == "alice"
    assert len(user.api_key) == 32
    assert (um.base_dir / "alice" / "profile.json").exists()
    assert (um.base_dir / "alice" / "journal.jsonl").exists()
    assert (um.base_dir / "alice" / "strategies").is_dir()


def test_register_duplicate_fails():
    um = _tmp_um()
    um.register("alice")
    with pytest.raises(FileExistsError):
        um.register("alice")


def test_register_invalid_username_fails():
    um = _tmp_um()
    with pytest.raises(ValueError):
        um.register("alice/../../etc")


def test_get_and_list_users():
    um = _tmp_um()
    um.register("alice")
    um.register("bob")
    users = um.list_users()
    assert len(users) == 2
    assert {u.username for u in users} == {"alice", "bob"}
    assert um.get("alice") is not None
    assert um.get("nonexistent") is None


def test_delete_user():
    um = _tmp_um()
    um.register("alice")
    assert um.delete("alice") is True
    assert um.delete("alice") is False
    assert um.get("alice") is None


def _tmp_sr():
    um = _tmp_um()
    return um, StrategyRegistry(um)


def test_strategy_registration():
    um, sr = _tmp_sr()
    um.register("alice")
    v1_path = Path(__file__).resolve().parents[1] / "strategy" / "defs" / "XAUUSD_STRUCTURE_BREAK_V1.json"
    dest = sr.register("alice", "my_strat", v1_path)
    assert dest.exists()
    strats = sr.list("alice")
    assert len(strats) == 1
    assert strats[0]["active"] is True


def test_strategy_activation_toggle():
    um, sr = _tmp_sr()
    um.register("alice")
    v1 = Path(__file__).resolve().parents[1] / "strategy" / "defs" / "XAUUSD_STRUCTURE_BREAK_V2.json"
    v2 = Path(__file__).resolve().parents[1] / "strategy" / "defs" / "XAUUSD_STRUCTURE_BREAK_V1.json"
    sr.register("alice", "s1", v1, is_active=True)
    sr.register("alice", "s2", v2, is_active=False)
    assert len(sr.get_active("alice")) == 1
    sr.deactivate("alice", "s1")
    assert len(sr.get_active("alice")) == 0
    sr.activate("alice", "s1")
    assert len(sr.get_active("alice")) == 1


def test_strategy_deactivate_nonexistent_fails():
    um, sr = _tmp_sr()
    um.register("alice")
    with pytest.raises(KeyError):
        sr.deactivate("alice", "nonexistent")


def test_journal_append_and_read():
    tmp = tempfile.mkdtemp()
    journal = DecisionJournal(Path(tmp) / "j.jsonl")
    d = SignalDecision(
        timestamp="2026-01-01T00:00:00Z", user="alice",
        strategy_name="STRAT", strategy_version="V1", symbol="XAUUSD",
        signal="BUY", confidence=75.0, bias="up",
        reasons=["test reason"], stop=100.0, target=105.0,
        lots=0.1, risk_usd=10.0, risk_pct=0.25, regime="normal",
    )
    journal.append(d)
    entries = journal.read()
    assert len(entries) == 1
    assert entries[0]["signal"] == "BUY"
    assert entries[0]["strategy"] == "STRAT"


def test_journal_summarize():
    tmp = tempfile.mkdtemp()
    journal = DecisionJournal(Path(tmp) / "j.jsonl")
    for i in range(10):
        journal.append(SignalDecision(
            timestamp=f"2026-01-01T00:0{i}:00Z", user="alice",
            strategy_name="S", strategy_version="V1", symbol="XAUUSD",
            signal="BUY" if i < 3 else "WAIT", confidence=70.0 if i < 3 else 0.0,
            bias="up", reasons=["r"], risk_usd=5.0 if i < 3 else 0.0,
        ))
    summary = journal.summarize()
    assert summary["total"] == 10
    assert summary["buys"] == 3
    assert summary["waits"] == 7
    assert summary["planned_risk_usd"] == 15.0


def test_journal_empty():
    tmp = tempfile.mkdtemp()
    journal = DecisionJournal(Path(tmp) / "nonexistent.jsonl")
    assert journal.read() == []
    assert journal.summarize() == {"total": 0}


def test_confidence_computation():
    m5 = pd.Series({"adx": 30.0, "rsi": 50.0})
    strat = Strategy("T", "V1", "XAUUSD", "M15", "M5",
                     {"bias_trend": "up"}, {"type": "atr", "atr_multiple": 1.5},
                     {"type": "rr", "risk_reward": 2.0}, 0.0025, 1, ["london"])
    c1 = _compute_confidence(strat, m5, ["bias up confirmed", "adx 30 >= 20", "BOS up"])
    c2 = _compute_confidence(strat, m5, [])
    assert c1 > c2
    assert 0 <= c1 <= 95


def test_signal_decision_to_dict_nan():
    d = SignalDecision(
        timestamp="2026-01-01", user="alice", strategy_name="T",
        strategy_version="V1", symbol="XAUUSD",
        signal="WAIT", confidence=0.0, bias="flat",
    )
    result = d.to_dict()
    assert result["stop"] is None
    assert result["target"] is None
    assert result["signal"] == "WAIT"


@dataclass
class FakeTrade:
    net_pips: float
    entry_price: float = 0.0
    stop: float = 0.0
    side: str = "BUY"


def test_mc_with_trade_objects():
    trades = [FakeTrade(net_pips=10.0) for _ in range(50)] + \
             [FakeTrade(net_pips=-5.0) for _ in range(50)]
    stats = run_monte_carlo(trades, MCConfig(n_iterations=50, seed=42))
    assert stats.n_iterations == 50
    assert stats.is_robust is True


def test_mc_negative_edge():
    trades = [FakeTrade(net_pips=-10.0) for _ in range(50)] + \
             [FakeTrade(net_pips=5.0) for _ in range(10)]
    stats = run_monte_carlo(trades, MCConfig(n_iterations=50, seed=42))
    assert stats.is_robust is False
    assert stats.ruin_prob > 0.0


def test_candle_patterns_on_synthetic_data():
    rng = np.random.default_rng(42)
    n = 500
    close = np.cumsum(rng.standard_normal(n) * 0.5 + 100)
    open_ = close + rng.standard_normal(n) * 0.3
    high = np.maximum(open_, close) + np.abs(rng.standard_normal(n) * 0.2)
    low = np.minimum(open_, close) - np.abs(rng.standard_normal(n) * 0.2)
    # inject a known doji at index 10 (open == close within threshold)
    open_[10] = 100.0
    close[10] = 100.0
    high[10] = 100.5
    low[10] = 99.5
    df = pd.DataFrame({"open": open_, "high": high, "low": low, "close": close})
    stats = analyze_patterns(df, min_samples=1, horizon=3)
    assert set(stats.keys()) == {"doji", "hammer", "shooting_star",
                                  "engulfing_bullish", "engulfing_bearish",
                                  "inside_bar", "pin_bar"}
    total_occ = sum(s.occurrences for s in stats.values())
    assert total_occ > 0
    report = render_pattern_report(stats)
    assert "Pattern" in report


# ---------------------------------------------------------------------------
# Atomic write + locking tests (concurrency safety)
# ---------------------------------------------------------------------------
def test_atomic_write_json_roundtrip():
    from rk_trade.utils import atomic_write_json, safe_read_json
    tmp = tempfile.mkdtemp()
    path = Path(tmp) / "test.json"
    data = {"key": "value", "nested": {"a": 1}}
    atomic_write_json(path, data)
    assert path.exists()
    loaded = safe_read_json(path)
    assert loaded == data


def test_atomic_write_text_no_partial():
    """atomic_write_text should not leave temp files after success."""
    from rk_trade.utils import atomic_write_text
    tmp = tempfile.mkdtemp()
    path = Path(tmp) / "test.txt"
    atomic_write_text(path, "hello world")
    assert path.read_text() == "hello world"
    # no leftover temp files
    temps = list(Path(tmp).glob("*.tmp"))
    assert len(temps) == 0


def test_safe_read_json_missing_file():
    from rk_trade.utils import safe_read_json
    assert safe_read_json(Path("/nonexistent/file.json"), default={"x": 1}) == {"x": 1}


def test_safe_read_json_corrupt_returns_default():
    from rk_trade.utils import safe_read_json, atomic_write_text
    tmp = tempfile.mkdtemp()
    path = Path(tmp) / "bad.json"
    path.write_text("{not valid json")
    assert safe_read_json(path) is None
    assert safe_read_json(path, default={}) == {}


def test_strategy_validation_rejects_non_strategy_file():
    """Registering a non-strategy JSON should fail with ValueError."""
    um, sr = _tmp_sr()
    um.register("alice")
    tmp = tempfile.mkdtemp()
    bad_path = Path(tmp) / "not_a_strategy.json"
    bad_path.write_text(json.dumps({"foo": "bar"}))
    with pytest.raises(ValueError, match="not a valid Strategy"):
        sr.register("alice", "bad", bad_path)


def test_strategy_validation_accepts_valid():
    um, sr = _tmp_sr()
    um.register("alice")
    # The V1 def is valid
    v1 = Path(__file__).resolve().parents[1] / "strategy" / "defs" / "XAUUSD_STRUCTURE_BREAK_V1.json"
    dest = sr.register("alice", "good", v1)
    assert dest.exists()


def test_concurrent_journal_appends():
    """Multiple appends from 'concurrent' calls should not lose entries."""
    from rk_trade.journal import DecisionJournal
    tmp = tempfile.mkdtemp()
    journal = DecisionJournal(Path(tmp) / "j.jsonl")
    for i in range(50):
        journal.append(SignalDecision(
            timestamp=f"2026-01-01T00:0{i:02d}:00Z", user="alice",
            strategy_name="S", strategy_version="V1", symbol="XAUUSD",
            signal="BUY", confidence=70.0, bias="up",
            risk_usd=5.0,
        ))
    entries = journal.read()
    assert len(entries) == 50
    assert all(e["signal"] == "BUY" for e in entries)


def test_strategy_registry_atomic_save():
    """Registry should be readable JSON after multiple updates."""
    um, sr = _tmp_sr()
    um.register("alice")
    v1 = Path(__file__).resolve().parents[1] / "strategy" / "defs" / "XAUUSD_STRUCTURE_BREAK_V1.json"
    sr.register("alice", "s1", v1)
    sr.register("alice", "s2", v1)
    # registry should be valid JSON
    reg_path = sr._registry_path("alice")
    reg = json.loads(reg_path.read_text())
    assert "s1" in reg and "s2" in reg
