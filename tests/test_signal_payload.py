"""Tests for execution/signal_payload.py — the canonical signal schema.

The properties tested here are the ones that keep a refusal from being
mistaken for a trade, and that keep the chart CSV readable by the MQL5
indicator.
"""
from __future__ import annotations

import math
from pathlib import Path

import pytest

from execution.signal_payload import (
    CSV_COLUMNS,
    Signal,
    read_signal_csv,
    write_signal_csv,
)


def _buy(**kw) -> Signal:
    base = dict(
        strategy="XAUUSD_D1_MOMENTUM_BREAKOUT", strategy_number="V11",
        symbol="XAUUSD", broker_symbol="GOLD.i#", timeframe="D1",
        direction="BUY", entry=4197.0, stop=4180.0, target=4231.0,
        lots=0.12, risk_pct=0.01, risk_usd=20.40, confidence=0.72,
        reason="D1 momentum + EMA21>EMA55", magic=20260922,
    )
    base.update(kw)
    return Signal(**base)


# ---------------------------------------------------------------- identity
def test_carries_every_required_field():
    """The user's required set: name, number, symbol, direction, entry,
    SL, TP, timestamp, confidence, reason."""
    s = _buy()
    assert s.strategy == "XAUUSD_D1_MOMENTUM_BREAKOUT"
    assert s.strategy_number == "V11"
    assert s.symbol == "XAUUSD"
    assert s.direction == "BUY"
    assert s.entry == 4197.0
    assert s.stop == 4180.0
    assert s.target == 4231.0
    assert s.timestamp
    assert s.confidence == 0.72
    assert "momentum" in s.reason


def test_timestamp_is_iso_utc():
    import datetime as dt
    s = _buy()
    parsed = dt.datetime.fromisoformat(s.timestamp)
    assert parsed.tzinfo is not None


# ---------------------------------------------------------------- refusals
def test_wait_clears_all_prices():
    """A WAIT must not carry prices, or a consumer could act on it."""
    s = Signal(strategy="x", direction="WAIT", entry=4197.0, stop=4180.0,
               target=4231.0, lots=5.0, risk_usd=500.0)
    assert math.isnan(s.entry) and math.isnan(s.stop) and math.isnan(s.target)
    assert s.lots == 0.0 and s.risk_usd == 0.0
    assert not s.is_trade


def test_blocked_clears_all_prices():
    s = Signal(strategy="x", direction="BLOCKED", entry=1.0, stop=2.0,
               target=3.0, reason="spread too wide")
    assert math.isnan(s.entry)
    assert not s.is_trade
    assert s.reason == "spread too wide"


def test_wait_gets_a_default_reason_not_empty_string():
    s = Signal(strategy="x", direction="WAIT")
    assert s.reason  # never blank — a blank reason is an unexplained refusal


def test_buy_keeps_its_prices():
    s = _buy()
    assert s.is_trade
    assert s.entry == 4197.0


# ------------------------------------------------------------- validation
def test_rejects_unknown_direction():
    with pytest.raises(ValueError, match="direction"):
        Signal(strategy="x", direction="MAYBE")


def test_rejects_out_of_range_confidence():
    with pytest.raises(ValueError, match="confidence"):
        Signal(strategy="x", direction="WAIT", confidence=1.5)
    with pytest.raises(ValueError, match="confidence"):
        Signal(strategy="x", direction="WAIT", confidence=-0.1)


def test_direction_is_normalised_to_uppercase():
    s = Signal(strategy="x", direction="buy", entry=1.0, stop=0.5, target=2.0)
    assert s.direction == "BUY"


# ------------------------------------------------------------ risk/reward
def test_risk_reward_for_buy():
    # entry 4197, stop 4180 -> risk 17; target 4231 -> reward 34 -> 2.0
    assert _buy().risk_reward == pytest.approx(2.0)


def test_risk_reward_for_sell():
    s = Signal(strategy="x", direction="SELL", entry=4197.0,
               stop=4214.0, target=4146.0)
    # risk 17, reward 51 -> 3.0
    assert s.risk_reward == pytest.approx(3.0)


def test_risk_reward_is_nan_without_prices():
    assert math.isnan(Signal(strategy="x", direction="WAIT").risk_reward)


# --------------------------------------------------------------------- csv
def test_csv_has_the_ea_join_key_first():
    """mql5/MonthlyBT_Signals.mq5 selects rows by the `epoch` column."""
    assert CSV_COLUMNS[0] == "epoch"


def test_csv_includes_confidence_and_reason():
    """These are the fields that were missing from the old CSV — the whole
    reason this module exists."""
    assert "confidence" in CSV_COLUMNS
    assert "reason" in CSV_COLUMNS
    assert "strategy_number" in CSV_COLUMNS


def test_write_and_read_round_trip(tmp_path: Path):
    p = tmp_path / "sig.csv"
    write_signal_csv([_buy(), Signal(strategy="y", direction="WAIT")], p)
    back = read_signal_csv(p)
    assert len(back) == 2
    assert back[0].direction == "BUY"
    assert back[0].entry == pytest.approx(4197.0)
    assert back[0].stop == pytest.approx(4180.0)
    assert back[0].confidence == pytest.approx(0.72)
    assert back[0].strategy_number == "V11"
    assert back[1].direction == "WAIT"
    assert math.isnan(back[1].entry)


def test_wait_row_has_no_prices_in_the_csv(tmp_path: Path):
    """The EA must not draw entry/SL/TP lines for a refusal."""
    p = tmp_path / "sig.csv"
    write_signal_csv([Signal(strategy="x", direction="WAIT")], p)
    text = p.read_text(encoding="utf-8")
    row = text.splitlines()[1]
    assert ",BUY," not in row
    # entry/sl/tp columns must be empty for a WAIT
    parts = row.split(",")
    assert parts[CSV_COLUMNS.index("entry")] == ""


def test_creates_parent_directory(tmp_path: Path):
    p = tmp_path / "deep" / "nested" / "sig.csv"
    write_signal_csv([_buy()], p)
    assert p.exists()


def test_append_does_not_duplicate_the_header(tmp_path: Path):
    p = tmp_path / "sig.csv"
    write_signal_csv([_buy()], p)
    write_signal_csv([_buy()], p, append=True)
    lines = p.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 3          # header + 2 rows
    assert lines[0] == ",".join(CSV_COLUMNS)
    assert lines.count(lines[0]) == 1


def test_overwrite_replaces_previous_contents(tmp_path: Path):
    p = tmp_path / "sig.csv"
    write_signal_csv([_buy()], p)
    write_signal_csv([_buy()], p)      # append=False
    lines = p.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2


def test_reason_commas_do_not_break_the_csv(tmp_path: Path):
    """A reason like 'D1 up, EMA21>55' must not shift every later column."""
    p = tmp_path / "sig.csv"
    write_signal_csv([_buy(reason="D1 up, EMA21>55, ADX 27")], p)
    back = read_signal_csv(p)
    assert len(back) == 1
    assert back[0].entry == pytest.approx(4197.0)   # column did not shift
    assert back[0].confidence == pytest.approx(0.72)


# ------------------------------------------------------------------- json
def test_to_dict_exposes_derived_fields():
    d = _buy().to_dict()
    assert d["is_trade"] is True
    assert d["risk_reward"] == pytest.approx(2.0)


def test_to_json_is_serialisable():
    import json
    d = json.loads(_buy().to_json())
    assert d["strategy_number"] == "V11"


def test_refused_signal_json_is_strictly_valid():
    """A WAIT must not emit bare NaN.

    Python's json module writes NaN happily, but it is not valid JSON and
    JSON.parse in a browser throws on it — which would break the dashboard
    exactly when there is nothing to trade.
    """
    import json
    raw = Signal(strategy="x", direction="WAIT").to_json()
    assert "NaN" not in raw
    assert "Infinity" not in raw
    d = json.loads(raw)          # strict by default: rejects NaN
    assert d["entry"] is None
    assert d["target"] is None
    assert d["risk_reward"] is None


def test_trade_signal_json_keeps_real_numbers():
    import json
    d = json.loads(_buy().to_json())
    assert d["entry"] == pytest.approx(4197.0)
    assert d["risk_reward"] == pytest.approx(2.0)
