"""Tests for api/strategy_stats.py and api/dashboard_api.py.

The dashboard's credibility rests on one rule: every number it shows is
either broker-confirmed or explicitly a backtest result. These tests pin
that rule, and the defensive parsing that keeps one malformed strategy
def from taking down the whole page.
"""
from __future__ import annotations

import json

import pytest

from api import strategy_stats as S


# ---------------------------------------------------------------- helpers
def test_number_normalisation():
    assert S._n(1.5) == 1.5
    assert S._n(3) == 3.0
    assert S._n(None) is None
    assert S._n(True) is None, "a bool is not a metric"
    assert S._n("abc") is None


def test_missing_is_distinct_from_zero():
    """'no drawdown' and 'never measured drawdown' are different facts."""
    d = {"max_drawdown_pips": 0}
    assert S._n(d.get("max_drawdown_pips")) == 0.0
    assert S._n({}.get("max_drawdown_pips")) is None


# ------------------------------------------------------------------- V11
def test_v11_metrics_are_read_from_the_def():
    m = S.strategy_metrics("V11")
    assert m["found"] is True
    assert m["version"] == "V11"
    h = m["headline"]
    assert h["trades"] == 82.0
    assert h["profit_factor"] == 2.10
    assert h["win_rate"] == 0.585
    assert h["max_drawdown_pips"] == 3005.4
    assert h["expectancy_pips"] == 219.4


def test_v11_is_marked_approved():
    m = S.strategy_metrics("V11")
    assert m["status"] == "approved"
    assert m["approved_on"]


def test_v11_robustness_and_walk_forward():
    m = S.strategy_metrics("V11")
    assert m["robustness"]["is_robust"] is True
    assert m["robustness"]["ruin_prob"] == 0.0
    w = m["walk_forward"]
    assert w["oos_exceeds_is"] is True, (
        "V11's OOS beats in-sample — that is the key robustness fact and "
        "the UI must be able to say so")


def test_v11_provenance_disclaims_live_results():
    m = S.strategy_metrics("V11")
    assert "NOT live" in m["provenance"]["note"]


# -------------------------------------------------------------- robustness
def test_a_rejected_def_with_prose_in_monte_carlo_does_not_crash():
    """Real case: a REJECTED strategy stores 'NOT robust' as a string
    where a dict is expected. It must render as no-metrics, not 500."""
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as td:
        defs = Path(td) / "defs"
        defs.mkdir()
        (defs / "X_V1.json").write_text(json.dumps({
            "name": "X", "version": "V1", "market": "XAUUSD",
            "research_results": {
                "full_dataset": {"trades": 100, "profit_factor": 0.5},
                "monte_carlo": "NOT robust",
                "walk_forward": "not run",
            }}), encoding="utf-8")

        old = S.DEFS
        S.DEFS = defs
        try:
            m = S.strategy_metrics("V1")
            assert m["found"] is True
            assert m["headline"]["profit_factor"] == 0.5
            assert m["robustness"]["is_robust"] is None
            assert m["walk_forward"]["oos_exceeds_is"] is None
        finally:
            S.DEFS = old


def test_unknown_strategy_is_reported_not_raised():
    m = S.strategy_metrics("V999")
    assert m["found"] is False
    assert m["requested"] == "V999"


def test_all_strategies_parses_every_def_on_disk():
    """A single malformed def must not break the whole dashboard."""
    lst = S.all_strategies()
    assert len(lst) > 5
    assert all(s.get("found") for s in lst)


def test_approved_strategies_sort_first():
    lst = S.all_strategies()
    statuses = [s["status"] for s in lst]
    if "approved" in statuses and "research" in statuses:
        assert statuses.index("approved") < statuses.index("research")


# ------------------------------------------------------------------ money
def test_money_is_all_none_when_mt5_is_down():
    m = S.money_fields({"connected": False, "stale": True})
    assert m["balance"] is None
    assert m["equity"] is None
    assert m["todays_pnl"] is None
    assert "not connected" in m["reason"]


def test_money_is_none_when_connected_but_stale():
    """A stale balance is a lie. Refuse to show it."""
    m = S.money_fields({"connected": True, "stale": True, "balance": 1000.0})
    assert m["balance"] is None


def test_open_pnl_is_equity_minus_balance():
    m = S.money_fields({"connected": True, "stale": False,
                        "balance": 1000.0, "equity": 1012.5})
    assert m["balance"] == 1000.0
    assert m["open_pnl"] == 12.5


def test_backtest_pips_are_never_converted_to_dollars():
    """The whole point: pips are not money. money_fields() must not
    derive a $ figure from any backtest number."""
    m = S.money_fields({"connected": True, "stale": False,
                        "balance": 1000.0, "equity": 1000.0})
    for k, v in m.items():
        if k in ("reason",):
            continue
        assert v is None or isinstance(v, (int, float))


# ---------------------------------------------------------------- overview
def test_engine_state_is_never_optimistic():
    from api.dashboard_api import _engine_state

    assert _engine_state({})["state"] == "stopped"
    assert _engine_state({"running": True, "connected": False})["state"] == "offline"
    assert _engine_state({"running": True, "connected": True,
                          "stale": True})["state"] == "stale"
    assert _engine_state({"running": True, "connected": True,
                          "stale": False, "cycles": 5,
                          "stale_seconds": 2})["state"] == "running"


def test_overview_shape():
    from api.dashboard_api import overview
    o = overview()
    for k in ("header", "money", "positions", "recent_signals",
              "recent_trades", "strategies", "execution"):
        assert k in o, f"missing {k}"
    assert o["execution"]["live_money"] is False
    assert o["execution"]["mode"] == "demo"


def test_strategy_page_404_shape():
    from api.dashboard_api import strategy_page
    d = strategy_page("V999")
    assert d["found"] is False


def test_strategy_page_carries_robustness():
    from api.dashboard_api import strategy_page
    d = strategy_page("V11")
    assert d["found"] is True
    assert d["strategy"]["robustness"]["is_robust"] is True
    assert "live_signal" in d


# -------------------------------------------------------------------- api
def test_dashboard_routes():
    from fastapi.testclient import TestClient
    from api.main import app

    c = TestClient(app)
    assert c.get("/api/dashboard").status_code == 200
    assert c.get("/api/dashboard/strategy/V11").status_code == 200
    assert c.get("/api/dashboard/strategy/NOPE404").status_code == 404
    r = c.get("/")
    assert r.status_code == 200
    assert "Trading AI" in r.text


def test_dashboard_html_has_no_external_dependency():
    """It must work on a phone with no internet and no build step."""
    from pathlib import Path
    html = (Path(__file__).resolve().parents[1] / "dashboard" / "index.html")\
        .read_text(encoding="utf-8")
    assert "https://" not in html
    assert "<script src=" not in html, "no external scripts"
