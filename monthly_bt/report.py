"""Monthly / yearly reports — text, Markdown and CSV.

Every metric requested by the spec is emitted, and a metric that cannot be
computed is reported as `n/a` with the reason rather than being filled
with a zero (which would be a fabrication).

`compute_metrics` returns {"total_trades": 0} and nothing else for an empty
window, so all accessors go through _m() which normalises missing / None /
inf to an explicit n/a.
"""
from __future__ import annotations

import csv
import math
from pathlib import Path

from monthly_bt.strategies import StrategySpec

ROOT = Path(__file__).resolve().parents[1]
REPORTS = ROOT / "reports" / "monthly"


def _m(metrics: dict, key: str):
    """Safe metric fetch: None/NaN/inf -> None (rendered as n/a)."""
    v = metrics.get(key)
    if v is None:
        return None
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
        return None
    return v


def _f(v, spec="", na="n/a") -> str:
    if v is None:
        return na
    try:
        return format(v, spec)
    except (TypeError, ValueError):
        return na


def period_metrics(res: dict) -> dict:
    """The full spec-11 metric set for ONE period, normalised."""
    m = res.get("metrics", {})
    rows = res.get("trade_rows", [])
    signals = res.get("signals", [])
    wins = [t for t in rows if t["net_pnl"] > 0]
    losses = [t for t in rows if t["net_pnl"] <= 0]
    sl_exits = sum(1 for t in rows if t["exit_reason"] == "stop")
    tp_exits = sum(1 for t in rows if t["exit_reason"] == "target")
    other = sum(1 for t in rows if t["exit_reason"] not in ("stop", "target"))
    rs = [t["r_multiple"] for t in rows if t["r_multiple"] is not None]
    nets = [t["net_pnl"] for t in rows]

    start_bal = res.get("initial_balance")
    if start_bal is None and rows:
        # balance before the first trade = after minus its own net
        start_bal = rows[0]["balance_after"] - rows[0]["net_pnl"]
    if start_bal is None:
        start_bal = res.get("cfg_initial_balance")
    end_bal = rows[-1]["balance_after"] if rows else start_bal

    return {
        "period": res.get("period", "?"),
        "signals": len(signals),
        "buy_signals": sum(1 for s in signals if s["signal"] == "BUY"),
        "sell_signals": sum(1 for s in signals if s["signal"] == "SELL"),
        "trades": len(rows),
        "r_multiple": (sum(rs) / len(rs)) if rs else None,
        "wins": len(wins), "losses": len(losses),
        "win_rate": _m(m, "win_rate"),
        "net_pnl": sum(nets) if nets else None,
        "gross_profit": sum(t["gross_pnl"] for t in wins) if wins else None,
        "gross_loss": -sum(t["gross_pnl"] for t in losses) if losses else None,
        "profit_factor": _m(m, "profit_factor"),
        "max_drawdown": _m(m, "max_drawdown_pips"),
        "avg_r": (sum(rs) / len(rs)) if rs else None,
        "expectancy": _m(m, "expectancy_pips"),
        "avg_trade": (sum(nets) / len(nets)) if nets else None,
        "largest_winner": max(nets) if nets else None,
        "largest_loser": min(nets) if nets else None,
        "consec_wins": _m(m, "longest_win_streak"),
        "consec_losses": _m(m, "longest_loss_streak"),
        "sl_exits": sl_exits, "tp_exits": tp_exits, "other_exits": other,
        "start_balance": start_bal, "end_balance": end_bal,
        "return_pct": ((end_bal - start_bal) / start_bal * 100)
                      if start_bal and end_bal is not None else None,
        "total_costs": sum(t["fees"] for t in rows) if rows else None,
        "skipped": len(res.get("skipped", [])),
    }


COLUMNS = ["period", "signals", "buy_signals", "sell_signals", "trades",
           "wins", "losses", "win_rate", "net_pnl", "gross_profit",
           "gross_loss", "profit_factor", "max_drawdown", "avg_r", "r_multiple",
           "expectancy", "avg_trade", "largest_winner", "largest_loser",
           "consec_wins", "consec_losses", "sl_exits", "tp_exits",
           "other_exits", "start_balance", "end_balance", "return_pct",
           "total_costs", "skipped"]


def render_text(spec: StrategySpec, symbol: str, rows: list[dict],
                manifest: dict | None = None) -> str:
    """Terminal-readable monthly table."""
    out = []
    out.append("=" * 108)
    out.append(f"MONTHLY BACKTEST  {spec.key} ({spec.name})  symbol={symbol}"
               f"  tf={spec.timeframe}")
    out.append("=" * 108)
    if manifest:
        d = manifest["data"]
        out.append(f"  data      : {d['date_range']}  ({d['bars']} bars)  "
                   f"mean spread {d['mean_spread']:.2f} {symbol} price units")
        out.append(f"  timezone  : {d['timezone']}")
        out.append(f"  logic     : {manifest['logic_source']}")
        out.append(f"  commit    : {manifest['git_commit']}   run {manifest['run_ts_utc']}")
        out.append(f"  balance   : ${manifest['initial_balance']:,.2f}   "
                   f"risk/trade {manifest['risk_pct_used']*100:.1f}%   "
                   f"max positions {manifest['max_positions']}")
        out.append(f"  costs     : spread measured, slippage "
                   f"{manifest['sim_config']['slippage_pips']} pip/side, "
                   f"commission ${manifest['sim_config']['commission_per_lot']}/lot, "
                   f"swap $0 (NOT modelled)")
        out.append(f"  guard     : order_capable=False, live_switch="
                   f"{manifest['guard']['live_trading_enabled']} (not modified)")
    out.append("-" * 108)
    out.append(f"{'Month':<9}{'Sig':>5}{'BUY':>5}{'SELL':>5}{'Trd':>5}{'Win%':>7}"
               f"{'PF':>7}{'Net$':>11}{'MaxDD$':>10}{'AvgR':>7}{'SL':>4}"
               f"{'TP':>4}{'End':>4}{'Costs$':>9}{'Skip':>6}")
    out.append("-" * 108)
    for r in rows:
        out.append(
            f"{r['period']:<9}{r['signals']:>5}{r['buy_signals']:>5}"
            f"{r['sell_signals']:>5}{r['trades']:>5}"
            f"{_f((r['win_rate'] or 0)*100 if r['win_rate'] is not None else None, '.1f'):>7}"
            f"{_f(r['profit_factor'], '.2f'):>7}"
            f"{_f(r['net_pnl'], ',.2f'):>11}"
            f"{_f(r['max_drawdown'], ',.1f'):>10}"
            f"{_f(r['avg_r'], '.2f'):>7}"
            f"{r['sl_exits']:>4}{r['tp_exits']:>4}{r['other_exits']:>4}"
            f"{_f(r['total_costs'], ',.2f'):>9}{r['skipped']:>6}")
    out.append("-" * 108)

    # totals across periods
    tot_sig = sum(r["signals"] for r in rows)
    tot_trd = sum(r["trades"] for r in rows)
    tot_net = sum(r["net_pnl"] or 0 for r in rows)
    tot_win = sum(r["wins"] for r in rows)
    tot_cost = sum(r["total_costs"] or 0 for r in rows)
    tot_skip = sum(r["skipped"] for r in rows)
    out.append(f"{'TOTAL':<9}{tot_sig:>5}{'':>5}{'':>5}{tot_trd:>5}"
               f"{_f(tot_win/tot_trd*100 if tot_trd else None, '.1f'):>7}"
               f"{'':>7}{_f(tot_net, ',.2f'):>11}{'':>10}{'':>7}"
               f"{'':>4}{'':>4}{'':>4}{_f(tot_cost, ',.2f'):>9}{tot_skip:>6}")
    out.append("=" * 108)
    if tot_sig and not tot_trd:
        out.append("NOTE: every signal was declined by position sizing. The D1 "
                   "stop is 1500-3200 pips wide; at the strategy's "
                   f"{manifest['risk_pct_used']*100 if manifest else 1.0:.0f}% "
                   "risk on a small balance that floors below the 0.01 minimum "
                   "lot. Raise --balance (e.g. --balance 100000) or the engine "
                   "will never force a trade it cannot size.")
        out.append("=" * 108)
    out.append("Simulated research only. No order was placed; no live/demo "
               "trade exists from this run.")
    return "\n".join(out)


def render_comparison(symbol: str, per_strategy: dict[str, list[dict]]) -> str:
    """V11 vs V12 on the same periods — statistics side by side, no verdict."""
    out = ["=" * 108,
           f"COMPARISON  symbol={symbol}  (measured statistics only — "
           f"no strategy is declared 'better' on one metric)", "=" * 108]
    out.append(f"{'Month':<9}{'V11 sig':>8}{'V11 trd':>8}{'V11 net$':>11}"
               f"{'V12 sig':>8}{'V12 trd':>8}{'V12 net$':>11}{'diff$':>11}")
    out.append("-" * 108)
    keys = sorted({r["period"] for rows in per_strategy.values() for r in rows})
    v11 = {r["period"]: r for r in per_strategy.get("V11", [])}
    v12 = {r["period"]: r for r in per_strategy.get("V12", [])}
    for k in keys:
        a, b = v11.get(k, {}), v12.get(k, {})
        an = a.get("net_pnl") or 0.0
        bn = b.get("net_pnl") or 0.0
        out.append(f"{k:<9}{a.get('signals', 0):>8}{a.get('trades', 0):>8}"
                   f"{_f(an, ',.2f'):>11}{b.get('signals', 0):>8}"
                   f"{b.get('trades', 0):>8}{_f(bn, ',.2f'):>11}"
                   f"{_f(an - bn, ',.2f'):>11}")
    out.append("-" * 108)
    for key, rows in sorted(per_strategy.items()):
        t = sum(r["trades"] for r in rows)
        w = sum(r["wins"] for r in rows)
        n = sum(r["net_pnl"] or 0 for r in rows)
        months_with = sum(1 for r in rows if r["trades"] > 0)
        out.append(f"  {key}: {sum(r['signals'] for r in rows)} signals, "
                   f"{t} trades, win% {_f(w/t*100 if t else None, '.1f')}, "
                   f"net ${_f(n, ',.2f')}, active in {months_with}/{len(rows)} periods")
    out.append("=" * 108)
    return "\n".join(out)


def render_markdown(spec: StrategySpec, symbol: str, rows: list[dict],
                    manifest: dict | None, run_id: str = "") -> str:
    out = [f"# {spec.key} monthly backtest — {symbol} ({spec.timeframe})", ""]
    if manifest:
        d = manifest["data"]
        out += [
            f"- run_id: `{run_id or 'not saved'}`",
            f"- logic: {manifest['logic_source']}",
            f"- commit: `{manifest['git_commit']}` at {manifest['run_ts_utc']}",
            f"- data: {d['date_range']} ({d['bars']} {spec.timeframe} bars)",
            f"- timezone: {d['timezone']}",
            f"- granularity: {d['granularity']}",
            f"- start balance: ${manifest['initial_balance']:,.2f}, "
            f"risk {manifest['risk_pct_used']*100:.1f}%/trade, "
            f"max {manifest['max_positions']} position",
            f"- costs: measured spread, "
            f"{manifest['sim_config']['slippage_pips']} pip/side slippage, "
            f"commission ${manifest['sim_config']['commission_per_lot']}/lot, "
            f"swap $0 (NOT modelled)",
            f"- safety: order_capable=False, "
            f"live_trading_enabled={manifest['guard']['live_trading_enabled']} "
            f"(reported, never modified)",
            "",
        ]
    out += ["| " + " | ".join(COLUMNS) + " |",
            "|" + "---|" * len(COLUMNS)]
    for r in rows:
        cells = []
        for c in COLUMNS:
            v = r[c]
            if v is None:
                cells.append("n/a")
            elif c in ("win_rate", "return_pct"):
                cells.append(f"{v*100:.1f}%" if c == "win_rate" else f"{v:.2f}%")
            elif isinstance(v, float):
                cells.append(f"{v:,.2f}")
            else:
                cells.append(str(v))
        out.append("| " + " | ".join(cells) + " |")
    out += ["", "Simulated research only. No orders were placed.", ""]
    return "\n".join(out)


def write_csv(rows: list[dict], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=COLUMNS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    return path


def write_all(spec: StrategySpec, symbol: str, rows: list[dict],
              manifest: dict | None, run_id: str, tag: str) -> dict:
    """Write .txt / .md / .csv side by side. Returns the paths."""
    stamp = tag or "run"
    base = REPORTS / f"{spec.key}_{symbol}_{stamp}"
    paths = {}
    txt = base.with_suffix(".txt")
    txt.parent.mkdir(parents=True, exist_ok=True)
    txt.write_text(render_text(spec, symbol, rows, manifest), encoding="utf-8")
    paths["text"] = txt
    md = base.with_suffix(".md")
    md.write_text(render_markdown(spec, symbol, rows, manifest, run_id),
                  encoding="utf-8")
    paths["markdown"] = md
    paths["csv"] = write_csv(rows, base.with_suffix(".csv"))
    return paths
