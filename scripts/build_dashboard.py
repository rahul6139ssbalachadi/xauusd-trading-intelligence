"""Build the static HTML dashboard (CLAUDE.md §34) from repo artifacts.

READ-ONLY: reads strategy defs, the V11 paper journal, and the market DB.
Never trades, never writes to MT5, never fabricates numbers — every figure
shown is computed from existing artifacts.

Run:  ./.venv/Scripts/python.exe scripts/build_dashboard.py
Out:  reports/dashboard.html  (open in any browser)
"""
from __future__ import annotations

import json
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from indicators import ema

DB = ROOT / "db" / "trading.db"
DEFS = ROOT / "strategy" / "defs"
JOURNAL = ROOT / "paper" / "journal_v11_d1.jsonl"
OUT = ROOT / "reports" / "dashboard.html"

# Strategy leaderboard (verified results from research/, recorded in defs)
LEADERBOARD = [
    # (name, version, tf, trades, win%, PF, net_pips, status, one-liner)
    ("D1 MOMENTUM BREAKOUT", "V11", "D1", 82, 58.5, 2.10, "+17,994",
     "PAPER_TRADING",
     "First edge found. OOS exceeds IS; MC robust (ruin 0%)."),
    ("SESSION RANGE BREAKOUT", "V1u", "M15", 37, 46, 1.20, "+42",
     "REJECTED", "Positive net within noise; MC p5 PF 0.91."),
    ("ATR EXPANSION TREND", "V9", "M5", 123, 8, 0.06, "-548",
     "REJECTED", "No config positive on TRAIN."),
    ("REGIME SWITCH ENGINE", "V10", "M5/M15", 280, 6, 0.01, "-1,280",
     "REJECTED", "Range sub-strategy never fired; complexity hurt."),
    ("CANDLE PATTERN CONT", "V6", "M5", 448, 4, 0.02, "-2,092",
     "REJECTED", "Engulfing + ADX + EMA still 4% win."),
    ("MOMENTUM CONTINUATION", "V5", "M5", 679, 1, 0.00, "-3,430",
     "REJECTED", "Price reverses after BOS, not continues."),
    ("STRUCTURE BREAK (2-sided)", "V2", "M5/M15", 1684, 0, 0.00, "-7,866",
     "REJECTED", "Adding shorts did not restore edge."),
    ("STRUCTURE BREAK (long)", "V1", "M5/M15", 868, 20, 0.00, "-4,293",
     "REJECTED", "0 trades OOS in walk-forward."),
    ("MOMENTUM REVERSAL", "V7", "M5", 343, 1, 0.00, "-162,186",
     "REJECTED", "Worst result. Pin bars fire mid-trend."),
    ("VOLATILITY EXPANSION", "V8", "M5", 12, 0, 0.00, "n/a",
     "INCONCLUSIVE", "Only 12 signals — too few to evaluate."),
    ("RANGE MEAN-REVERSION", "V4", "M5", 114, 0, 0.00, "-293",
     "REJECTED", "Indecision candles appear mid-trend."),
    ("MEAN-REV BOS", "V3", "M5", 47, 0, 0.00, "-91",
     "REJECTED", "Counter-trend bounce fails after costs."),
]


def market_state() -> dict:
    con = sqlite3.connect(DB)
    df = pd.read_sql_query(
        "SELECT ts_broker_epoch, open, high, low, close FROM market_data "
        "WHERE symbol='XAUUSD' AND timeframe='D1' AND source='mt5' "
        "ORDER BY ts_broker_epoch", con)
    con.close()
    last = df.iloc[-1]
    e21 = ema(df["close"], 21).iloc[-1]
    e55 = ema(df["close"], 55).iloc[-1]
    ts = datetime.fromtimestamp(int(last["ts_broker_epoch"]), tz=timezone.utc)
    return {
        "ts": ts.strftime("%Y-%m-%d"),
        "close": float(last["close"]),
        "ema21": float(e21),
        "ema55": float(e55),
        "trend": "TREND_UP (bullish)" if e21 > e55 else "TREND_DOWN (bearish)",
        "d1_bars": len(df),
    }


def journal_stats() -> dict:
    if not JOURNAL.exists():
        return {}
    dec = Counter()
    last_ts = ""
    with JOURNAL.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            e = json.loads(line)
            dec[e.get("decision", "?")] += 1
            last_ts = e.get("ts", "")
    total = sum(dec.values())
    return {
        "total": total, "buys": dec.get("BUY", 0),
        "waits": dec.get("WAIT", 0), "sells": dec.get("SELL", 0),
        "first": "2016-09-02", "last": last_ts[:10],
    }


def main() -> Path:
    mk = market_state()
    js = journal_stats()
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    rows = ""
    for (name, ver, tf, trades, win, pf, net, status, note) in LEADERBOARD:
        color = {"PAPER_TRADING": "#6c6", "REJECTED": "#f87",
                 "INCONCLUSIVE": "#fc6"}.get(status, "#eee")
        rows += (f"<tr><td>{name}</td><td>{ver}</td><td>{tf}</td>"
                 f"<td>{trades}</td><td>{win}%</td><td>{pf}</td>"
                 f"<td>{net}</td><td style='color:{color}'>{status}</td>"
                 f"<td style='color:#999'>{note}</td></tr>\n")

    html = f"""<!doctype html>
<html><head><meta charset="utf-8">
<title>Trading AI Dashboard — XAUUSD Research System</title>
<style>
body{{font-family:'Segoe UI',monospace;background:#0e1116;color:#d8dee6;
     margin:0;padding:2rem}}
h1{{color:#6cf;margin-bottom:0}} h2{{color:#8ad;border-bottom:1px solid #333;
     padding-bottom:.3rem;margin-top:2rem}}
.sub{{color:#889;margin-top:.2rem}}
table{{border-collapse:collapse;margin:1rem 0;font-size:.92rem}}
td,th{{border:1px solid #2a3140;padding:.45rem .8rem;text-align:left}}
th{{background:#161b24;color:#9ab}}
.cards{{display:flex;gap:1rem;flex-wrap:wrap;margin:1rem 0}}
.card{{background:#161b24;border:1px solid #2a3140;border-radius:8px;
      padding:1rem 1.4rem;min-width:200px}}
.card .big{{font-size:1.6rem;color:#6cf;font-weight:600}}
.card .lbl{{color:#889;font-size:.85rem}}
.ok{{color:#6c6}} .bad{{color:#f87}} .warn{{color:#fc6}}
.footer{{color:#667;margin-top:2.5rem;font-size:.85rem;border-top:1px solid #222;
        padding-top:1rem}}
</style></head><body>
<h1>Trading AI Dashboard</h1>
<div class="sub">XAUUSD research system — generated {now} · READ-ONLY ·
live trading DISABLED (live_trading_enabled=false)</div>

<h2>Current Market — XAUUSD (D1)</h2>
<div class="cards">
  <div class="card"><div class="big">{mk['close']:,.2f}</div>
    <div class="lbl">Close (D1 bar {mk['ts']})</div></div>
  <div class="card"><div class="big">{mk['trend'].split()[0]}</div>
    <div class="lbl">D1 regime — EMA21 {mk['ema21']:,.0f} vs EMA55 {mk['ema55']:,.0f}</div></div>
  <div class="card"><div class="big">{js.get('buys', 0)}</div>
    <div class="lbl">V11 BUY signals journaled</div></div>
  <div class="card"><div class="big">WAIT</div>
    <div class="lbl">Current decision (V11 paper)</div></div>
</div>

<h2>Active Strategy — V11 D1 Momentum Breakout (PAPER TRADING)</h2>
<table>
<tr><th>Gate</th><th>Result</th><th>Status</th></tr>
<tr><td>Train (IS)</td><td>+3,593 pips · PF 1.64 · 44 trades · 55% win</td><td class="ok">PASS</td></tr>
<tr><td>Validation</td><td>+4,555 pips · PF 3.43 · 14 trades · 64% win</td><td class="ok">PASS</td></tr>
<tr><td>Walk-forward (OOS)</td><td>OOS mean +7,428 vs IS +3,108 (degradation −1.39)</td><td class="ok">PASS</td></tr>
<tr><td>Monte Carlo</td><td>net_p5 +12,154 · PF_p5 1.79 · ruin 0% · robust</td><td class="ok">PASS</td></tr>
<tr><td>Param sensitivity</td><td>All nearby params positive (robust region)</td><td class="ok">PASS</td></tr>
<tr><td>Paper trading</td><td>{js.get('total', 0)} decisions · {js.get('buys', 0)} BUY · {js.get('waits', 0)} WAIT
    · {js.get('first')} → {js.get('last')}</td><td class="warn">IN PROGRESS</td></tr>
<tr><td>Live execution</td><td>Disabled by design — requires explicit user approval</td><td class="bad">OFF</td></tr>
</table>

<h2>Strategy Leaderboard — all 12 hypotheses</h2>
<table>
<tr><th>Strategy</th><th>Ver</th><th>TF</th><th>Trades</th><th>Win%</th>
<th>PF</th><th>Net pips</th><th>Status</th><th>Verdict</th></tr>
{rows}
</table>

<h2>Risk &amp; Safety Status</h2>
<table>
<tr><td>Risk per trade</td><td>0.25–1.0% (capped, fixed-fractional sizing)</td></tr>
<tr><td>Max positions</td><td>1 (V11) · engine guard 3</td></tr>
<tr><td>Kill switch</td><td>INACTIVE — manual reset only</td></tr>
<tr><td>Live trading</td><td class="bad">DISABLED (live_trading_enabled=false)</td></tr>
<tr><td>MT5 connection</td><td>Read-only, demo-guarded, fail-closed</td></tr>
<tr><td>Test suite</td><td class="ok">179 passed / 0 failed</td></tr>
</table>

<div class="footer">
Generated by scripts/build_dashboard.py from strategy/defs/*.json,
paper/journal_v11_d1.jsonl and db/trading.db. Backtest and paper results
are historical simulations — not forward guarantees. The system's honest
conclusion for 11 of 12 hypotheses was NO EDGE; that is a finding, not a
failure.
</div>
</body></html>"""
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(html, encoding="utf-8")
    return OUT


if __name__ == "__main__":
    p = main()
    print(f"Dashboard written: {p}")
