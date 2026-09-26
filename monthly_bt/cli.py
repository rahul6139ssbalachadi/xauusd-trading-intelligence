"""CLI: python -m monthly_bt ...

    python -m monthly_bt --strategy V11 --symbol BTCUSD --month 2026-01
    python -m monthly_bt --strategy V12 --symbol XAUUSD --year 2026
    python -m monthly_bt --strategy both --symbol XAUUSD --year 2026
    python -m monthly_bt --strategy V11 --symbol XAUUSD --start 2026-01-01 --end 2026-06-30
    python -m monthly_bt --strategy V11 --symbol XAUUSD --all        # full history
    python -m monthly_bt --strategy V11 --symbol XAUUSD --month 2026-01 --chart
    python -m monthly_bt --symbols                                          # data inventory

--chart writes BOTH the interactive HTML and the MT5 indicator CSV. With
--strategy both it additionally writes one COMBINED csv holding V11 + V12.

READ-ONLY. Places no orders, cannot place orders.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from monthly_bt import data as bt_data
from monthly_bt import guard, report, runner, store, strategies
from monthly_bt.engine import SimConfig


def _add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--strategy", default="V11",
                   help="V11 | V12 | both (default V11)")
    p.add_argument("--symbol", default="XAUUSD", help="XAUUSD | BTCUSD")
    p.add_argument("--month", help="YYYY-MM, e.g. 2026-01")
    p.add_argument("--months", nargs="+", help="several YYYY-MM values")
    p.add_argument("--year", help="YYYY — every month of that year")
    p.add_argument("--start", help="custom range start YYYY-MM-DD")
    p.add_argument("--end", help="custom range end YYYY-MM-DD (exclusive)")
    p.add_argument("--all", action="store_true",
                   help="every month that has data (complete history)")
    p.add_argument("--balance", type=float, default=10_000.0)
    p.add_argument("--slippage", type=float, default=1.0,
                   help="pips per side (assumption, configurable)")
    p.add_argument("--commission", type=float, default=0.0,
                   help="USD per lot round trip (default 0 — verify)")
    p.add_argument("--spread-mult", type=float, default=1.0,
                   help="multiplier on the MEASURED per-bar spread")
    p.add_argument("--max-positions", type=int, default=1)
    p.add_argument("--require-disarmed", action="store_true",
                   help="fail if config live_trading_enabled is true")
    p.add_argument("--no-save", action="store_true",
                   help="do not write to db/backtests.db")
    p.add_argument("--chart", action="store_true",
                   help="also render the visual backtest HTML")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m monthly_bt",
                                description="Month-by-month V11/V12 backtest "
                                            "(research only, no orders)")
    _add_common(p)
    p.add_argument("--symbols", action="store_true",
                   help="print what data the DB actually has, then exit")
    p.add_argument("--verify-parity", action="store_true",
                   help="prove the historical scan matches the live V12 gate")
    return p


def _selected_periods(args, feats) -> list[tuple[str, str, str]]:
    """[(label, start, end), ...] — each becomes an independent period."""
    if args.month:
        s, e = runner.month_bounds(args.month)
        return [(args.month, s.strftime("%Y-%m-%d"), e.strftime("%Y-%m-%d"))]
    if args.months:
        out = []
        for m in args.months:
            s, e = runner.month_bounds(m)
            out.append((m, s.strftime("%Y-%m-%d"), e.strftime("%Y-%m-%d")))
        return out
    if args.year:
        months = [m for m in runner.month_ends_for(feats) if m[:4] == args.year]
        if not months:
            raise SystemExit(f"no {args.symbol} data in year {args.year}")
        out = []
        for m in months:
            s, e = runner.month_bounds(m)
            out.append((m, s.strftime("%Y-%m-%d"), e.strftime("%Y-%m-%d")))
        return out
    if args.start and args.end:
        return [(runner.period_label(args.start, args.end), args.start, args.end)]
    if args.all:
        out = []
        for m in runner.month_ends_for(feats):
            s, e = runner.month_bounds(m)
            out.append((m, s.strftime("%Y-%m-%d"), e.strftime("%Y-%m-%d")))
        return out
    raise SystemExit("specify --month / --months / --year / --start+--end / --all")


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)

    if args.symbols:
        print(bt_data.available().to_string(index=False))
        return 0

    guard_status = guard.check_live_switch(args.require_disarmed)
    if guard_status["live_trading_enabled"]:
        print("NOTE: config live_trading_enabled=true. This backtester has no "
              "order path and does not read or modify that switch; the value "
              "is recorded in the run provenance.\n")

    if args.verify_parity:
        df = bt_data.load(args.symbol, "H1")
        print(strategies.verify_v12_gate_parity(df))
        return 0

    if not (args.month or args.months or args.year or args.all
            or (args.start and args.end)):
        raise SystemExit("specify a period: --month / --months / --year / "
                         "--start+--end / --all")

    keys = ["V11", "V12"] if args.strategy.lower() == "both" \
        else [args.strategy.upper()]
    cfg = SimConfig(initial_balance=args.balance,
                    slippage_pips=args.slippage,
                    commission_per_lot=args.commission,
                    spread_multiplier=args.spread_mult,
                    max_positions=args.max_positions)

    per_strategy: dict[str, list[dict]] = {}
    last: dict[str, tuple[pd.DataFrame, list[dict]]] = {}
    con = None
    if not args.no_save:
        con = store.connect()

    for key in keys:
        spec = strategies.get(key)
        raw = bt_data.load(args.symbol, spec.timeframe)
        feats = runner.build_features(raw, spec)          # FULL history first
        periods = _selected_periods(args, feats)

        print(f"\n### {spec.key} {spec.name} | {args.symbol} | "
              f"{spec.timeframe} | {raw['ts_broker'].iloc[0].date()} .. "
              f"{raw['ts_broker'].iloc[-1].date()} | {len(raw)} bars")

        results = []
        for label, start, end in periods:
            res = runner.run_period(feats, spec, args.symbol, start, end, cfg)
            res["cfg_initial_balance"] = args.balance
            results.append(res)

        rows = [report.period_metrics(r) for r in results]
        for r in results:
            r["strategy"] = spec.key
        per_strategy[spec.key] = rows

        manifest = runner.run_manifest(
            args.symbol, spec.timeframe, spec, cfg, guard_status,
            ",".join(p[0] for p in periods))
        run_id = ""
        if con is not None:
            run_id = store.save_run(manifest, results, con)
            store.save_results(run_id, results, con)
        tag = args.month or args.year or (args.start or "all")
        paths = report.write_all(spec, args.symbol, rows, manifest, run_id,
                                 f"{tag}_{args.symbol}".replace(" ", "_"))
        last[key] = (feats, results)

        print(report.render_text(spec, args.symbol, rows, manifest))
        print(f"  report     : {paths['text'].name} / {paths['markdown'].name}"
              f" / {paths['csv'].name}  ({paths['text'].parent})")
        if run_id:
            print(f"  run_id     : {run_id}  (db: {store.DB})")

        if args.chart:
            from monthly_bt import visual
            out = visual.render_period(feats, results, spec, args.symbol,
                                       label=tag)
            print(f"  visual     : {out}")
            mql = visual.export_mt5_csv(feats, results, spec, args.symbol,
                                        label=tag)
            print(f"  MT5 import : {mql}")

    if con is not None:
        con.close()

    if len(per_strategy) > 1:
        print()
        print(report.render_comparison(args.symbol, per_strategy))

    if args.chart and len(last) > 1:
        from monthly_bt import visual
        label = args.month or args.year or "all"
        csv_path = visual.export_mt5_combined(
            [(f, r, strategies.get(k)) for k, (f, r) in last.items()],
            args.symbol, label=label)
        html_path = visual.render_combined(
            [dict(feats=f, results=r, spec=strategies.get(k))
             for k, (f, r) in last.items()], args.symbol, label=label)
        print(f"\n  COMBINED chart : {html_path}")
        print(f"  COMBINED MT5   : {csv_path}")
        print("    V11 rows are D1, V12 rows are H1. The indicator filters by")
        print("    chart timeframe: D1 chart shows V11, H1 chart shows V12.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
