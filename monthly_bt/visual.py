"""Visual backtest — two delivery paths, no third-party plotting library.

matplotlib and plotly are NOT installed and requirements.txt is a minimal
core set; adding a dependency was rejected. Instead:

  A. INTERACTIVE HTML (zero dependency, always works)
     A self-contained candlestick chart with BUY/SELL markers, entry/SL/TP
     lines, exit markers and a hover tooltip per signal. Pan with drag,
     zoom with the wheel, and it is servable by the existing FastAPI
     backend or opened straight from disk. This is the primary "move
     through the historical period" view.

  B. INSIDE METATRADER 5 (the platform this project already uses)
     export_mt5_csv() writes the signals in a layout that the MQL5
     indicator `mql5/MonthlyBT_Signals.mq5` reads, so the markers appear
     on a real MT5 chart on the real broker symbol (GOLD.i# / BTCUSD#)
     with the broker's own candles. That is the same platform the live
     runners use; nothing about the account is touched, the indicator
     only reads a file.

Both render the SAME signal dicts the backtest used, so the chart cannot
disagree with the report.

SIGNAL vs SIMULATED TRADE are drawn differently and labelled differently:
  - hollow marker, "SIGNAL"  = the strategy generated the condition
  - filled marker, "TRADE"   = the simulator actually opened and closed it
  - a signal with no marker pair is listed in the skipped panel with the
    reason (position already open / size below min lot / no forward bars)
"""
from __future__ import annotations

import csv
import json
from pathlib import Path

import pandas as pd

from monthly_bt.templates import COMBINED_TEMPLATE as _COMBINED_TEMPLATE

ROOT = Path(__file__).resolve().parents[1]
VIS = ROOT / "reports" / "monthly" / "visual"
MQL5_OUT = ROOT / "mql5" / "signals"


def _ts(feats: pd.DataFrame, i: int) -> str:
    return str(feats.iloc[i]["ts_broker"])


def collect_markers(feats: pd.DataFrame, results: list[dict]) -> dict:
    """Flatten every period's signals + trades into chart-ready markers."""
    markers = {"signals": [], "trades": [], "skipped": []}
    for res in results:
        strategy = res.get("strategy")
        traded = {t["entry_bar"]: t for t in res.get("trade_rows", [])}
        for s in res.get("signals", []):
            i = s["entry_bar"]
            if i >= len(feats):
                continue
            t = traded.get(i)
            m = {
                "strategy": s["strategy"] or strategy,
                "signal": s["signal"],
                "ts": _ts(feats, i),
                "signal_ts": str(s.get("signal_ts")),
                "entry": s["entry"], "sl": s["sl"], "tp": s["tp"],
                "atr": s.get("atr"), "body_pct": s.get("body_pct"),
                "simulated": t is not None,
            }
            if t is not None:
                j = t["exit_bar"]
                m.update({
                    "exit_ts": str(t["exit_ts"]), "exit": t["exit"],
                    "exit_reason": t["exit_reason"], "lots": t["lots"],
                    "net_pnl": t["net_pnl"], "r": t["r_multiple"],
                    "exit_index": j,
                })
            else:
                m["exit"] = None
            markers["signals"].append(m)
        for sk in res.get("skipped", []):
            i = sk["entry_bar"]
            if i < len(feats):
                markers["skipped"].append({
                    "strategy": sk.get("strategy") or strategy,
                    "ts": _ts(feats, i), "reason": sk["skip_reason"],
                    "entry": sk["entry"], "sl": sk["sl"], "tp": sk["tp"]})
    markers["signals"].sort(key=lambda m: m["ts"])
    return markers


def candles(feats: pd.DataFrame) -> dict:
    return {
        "t": [str(x) for x in feats["ts_broker"]],
        "o": [float(x) for x in feats["open"]],
        "h": [float(x) for x in feats["high"]],
        "l": [float(x) for x in feats["low"]],
        "c": [float(x) for x in feats["close"]],
    }


def chart_window(feats: pd.DataFrame, results: list[dict],
                 before: int = 120, after: int = 60) -> pd.DataFrame:
    """Narrow the plotted candles to a context window around the signals.

    Rendering 59,313 H1 bars produced a 3.8 MB HTML that browsers crawl on.
    The backtest itself is unaffected — this only chooses what to DRAW. If
    there are signals, the window spans from `before` bars ahead of the
    earliest signal to `after` bars past the latest exit. With no signals,
    the last 400 bars are shown so the chart is not empty.
    """
    idx = []
    for res in results:
        for s in res.get("signals", []):
            idx.append(s["entry_bar"])
        for t in res.get("trade_rows", []):
            idx.append(t["exit_bar"])
    n = len(feats)
    if not idx:
        return feats.iloc[max(0, n - 400):]
    lo = max(0, min(idx) - before)
    hi = min(n, max(idx) + after)
    return feats.iloc[lo:hi].reset_index(drop=True)


def render_period(feats: pd.DataFrame, results: list[dict], spec,
                  symbol: str, label: str = "") -> Path:
    """Write the interactive HTML for one period (or a set of periods).

    The plotted frame is the context window; marker bar indices are
    re-based onto it so the JS index lookup stays correct.
    """
    VIS.mkdir(parents=True, exist_ok=True)
    plotted = chart_window(feats, results)
    offset = int(feats.index[feats["ts_broker"].eq(
        plotted["ts_broker"].iloc[0])][0]) if len(plotted) else 0
    rebased = _rebase(results, offset, len(plotted))
    data = {
        "candles": candles(plotted),
        "markers": collect_markers(plotted, rebased),
        "meta": {
            "strategy": spec.key, "strategy_name": spec.name,
            "symbol": symbol, "timeframe": spec.timeframe,
            "label": label, "params": spec.params,
            "bars_plotted": len(plotted), "bars_available": len(feats),
            "note": "Research backtest. No order was placed. Signals are "
                    "generated by the same V11/V12 code the live runners use. "
                    "Chart shows a context window around the signals; the "
                    "trade log has every bar.",
        },
    }
    out = VIS / f"{spec.key}_{symbol}_{label or 'period'}.html"
    out.write_text(_TEMPLATE.replace("/*__DATA__*/",
                                     json.dumps(data, default=str)),
                   encoding="utf-8")
    return out


def export_mt5_csv(feats: pd.DataFrame, results: list[dict], spec,
                   symbol: str, label: str = "period") -> Path:
    """CSV the MQL5 indicator reads: marker records, broker-epoch stamped.

    Timestamps are BROKER epoch seconds so the indicator can match them
    directly to bar times on the broker chart without any timezone guess.
    """
    MQL5_OUT.mkdir(parents=True, exist_ok=True)
    out = MQL5_OUT / f"{spec.key}_{symbol}_{label}.csv"
    rows = mt5_rows(feats, results, spec)
    _write_csv(out, rows)
    return out


def mt5_rows(feats: pd.DataFrame, results: list[dict], spec) -> list[dict]:
    """Flatten signals + simulated trades into the indicator's CSV schema."""
    rows = []
    traded = {t["entry_bar"]: t for res in results
              for t in res.get("trade_rows", [])}
    for res in results:
        for s in res.get("signals", []):
            i = s["entry_bar"]
            if i >= len(feats):
                continue
            t = traded.get(i)
            rows.append({
                "epoch": int(feats.iloc[i]["ts_broker_epoch"]),
                "strategy": s.get("strategy") or res.get("strategy") or spec.key,
                "timeframe": s.get("timeframe") or spec.timeframe,
                "signal": s["signal"],
                "entry": round(float(s["entry"]), 5),
                "sl": round(float(s["sl"]), 5),
                "tp": round(float(s["tp"]), 5),
                "simulated": 1 if t else 0,
                "exit_epoch": int(feats.iloc[t["exit_bar"]]["ts_broker_epoch"]) if t else "",
                "exit": round(float(t["exit"]), 5) if t else "",
                "exit_reason": t["exit_reason"] if t else "",
                "net_pnl": round(float(t["net_pnl"]), 2) if t else "",
                "body_pct": round(float(s["body_pct"]), 4)
                            if s.get("body_pct") == s.get("body_pct") else "",
                "r": round(float(t["r_multiple"]), 3)
                     if t and t["r_multiple"] is not None else "",
            })
    rows.sort(key=lambda r: r["epoch"])
    return rows


MT5_COLUMNS = ["epoch", "strategy", "timeframe", "signal", "entry", "sl", "tp",
               "simulated", "exit_epoch", "exit", "exit_reason", "net_pnl",
               "body_pct", "r"]


def _write_csv(out: Path, rows: list[dict]) -> Path:
    with out.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=MT5_COLUMNS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    return out


def export_mt5_combined(parts: list[tuple], symbol: str,
                        label: str = "both") -> Path:
    """ONE CSV containing several strategies, for a single MT5 chart.

    `parts` is [(feats, results, spec), ...]. Every row keeps its own
    strategy AND timeframe, so the indicator can show V11's D1 markers on a
    D1 chart and V12's H1 markers on an H1 chart from the same file.
    """
    MQL5_OUT.mkdir(parents=True, exist_ok=True)
    rows: list[dict] = []
    for feats, results, spec in parts:
        rows.extend(mt5_rows(feats, results, spec))
    # dedupe on (epoch, strategy) in case two runs overlapped
    seen, uniq = set(), []
    for r in sorted(rows, key=lambda r: (r["epoch"], r["strategy"])):
        key = (r["epoch"], r["strategy"])
        if key in seen:
            continue
        seen.add(key)
        uniq.append(r)
    return _write_csv(MQL5_OUT / f"COMBINED_{symbol}_{label}.csv", uniq)


def render_combined(parts: list[dict], symbol: str,
                    label: str = "both") -> Path:
    """ONE HTML page with BOTH strategies.

    V11 is D1 and V12 is H1, so the two series are NOT on a shared bar
    index. The page therefore renders a stacked layout: one candlestick
    panel per strategy on its own native timeframe, each with its own
    markers, and a shared summary table. Plotting D1 signals on an H1 chart
    (or resampling) would misplace every marker.
    """
    VIS.mkdir(parents=True, exist_ok=True)
    series = []
    for part in parts:
        spec = part["spec"]
        feats, results = part["feats"], part["results"]
        plotted = chart_window(feats, results)
        offset = int(feats.index[feats["ts_broker"].eq(
            plotted["ts_broker"].iloc[0])][0])
        rebased = _rebase(results, offset, len(plotted))
        series.append({
            "spec": {"strategy": spec.key, "strategy_name": spec.name,
                     "symbol": symbol, "timeframe": spec.timeframe,
                     "params": spec.params},
            "candles": candles(plotted),
            "markers": collect_markers(plotted, rebased),
        })
    data = {
        "series": series,
        "meta": {"symbol": symbol, "label": label,
                 "note": "Research backtest. No order was placed. V11 is D1, "
                         "V12 is H1 — each is plotted on its own native "
                         "timeframe in its own panel."},
    }
    out = VIS / f"COMBINED_{symbol}_{label}.html"
    out.write_text(_COMBINED_TEMPLATE.replace(
        "/*__DATA__*/", json.dumps(data, default=str)), encoding="utf-8")
    return out

def _rebase(results: list[dict], offset: int, n: int) -> list[dict]:
    """Shift window-relative bar indices back onto the plotted frame."""
    out = []
    for res in results:
        out.append({
            **res,
            "signals": [{**s, "entry_bar": s["entry_bar"] - offset}
                        for s in res.get("signals", [])
                        if 0 <= s["entry_bar"] - offset < n],
            "trade_rows": [{**t, "entry_bar": t["entry_bar"] - offset,
                            "exit_bar": t["exit_bar"] - offset}
                           for t in res.get("trade_rows", [])
                           if 0 <= t["entry_bar"] - offset < n],
            "skipped": [{**s, "entry_bar": s["entry_bar"] - offset}
                        for s in res.get("skipped", [])
                        if 0 <= s["entry_bar"] - offset < n],
        })
    return out


# The HTML is a single self-contained document: inline CSS + vanilla JS
# canvas candlestick renderer. No CDN, no network, no dependency.
_TEMPLATE = r"""<!doctype html>
<html><head><meta charset="utf-8"><title>Visual Backtest</title>
<style>
 body{margin:0;background:#0e1117;color:#c9d1d9;font:13px ui-monospace,Consolas,monospace}
 header{padding:10px 14px;background:#161b22;border-bottom:1px solid #30363d}
 h1{margin:0 0 4px;font-size:15px}
 .sub{color:#8b949e;font-size:12px}
 .wrap{display:flex}
 #chartWrap{flex:1;position:relative}
 canvas{display:block;cursor:crosshair}
 aside{width:380px;border-left:1px solid #30363d;padding:10px;overflow:auto;height:78vh}
 .sig{border-bottom:1px solid #21262d;padding:7px 0}
 .sim{background:#161b22;border-left:3px solid #3fb950;padding:7px 8px}
 .skip{border-left:3px solid #d29922;padding:7px 8px;background:#161b22}
 .t{color:#58a6ff;font-weight:bold}
 .pos{color:#3fb950}.neg{color:#f85149}.mut{color:#8b949e}
 table{border-collapse:collapse;margin:6px 0 12px;font-size:12px}
 td{padding:1px 8px 1px 0}
 #tip{position:absolute;background:#161b22ee;border:1px solid #30363d;padding:6px 8px;
      pointer-events:none;display:none;font-size:12px;z-index:5;white-space:pre}
 .legend span{margin-right:14px}
</style></head><body>
<header>
 <h1 id="title"></h1>
 <div class="sub" id="sub"></div>
 <div class="sub legend" style="margin-top:6px">
  <span style="color:#3fb950">&#9679; filled = SIMULATED TRADE</span>
  <span style="color:#58a6ff">&#9675; hollow = SIGNAL (no trade)</span>
  <span style="color:#f85149">&#9632; entry line</span>
  <span style="color:#d29922">&#9632; SL</span>
  <span style="color:#a371f7">&#9632; TP</span>
  <span class="mut">drag = pan, wheel = zoom, click = zoom in</span>
 </div>
</header>
<div class="wrap">
 <div id="chartWrap"><canvas id="cv"></canvas><div id="tip"></div></div>
 <aside><b>SIGNALS</b> <span class="mut" id="cnt"></span><div id="list"></div></aside>
</div>
<script>
const D = /*__DATA__*/;
const C = D.candles, M = D.markers, meta = D.meta;
document.getElementById('title').textContent =
  'Visual backtest — ' + meta.strategy + ' ' + meta.symbol + ' ' + meta.timeframe
  + (meta.label ? ' — ' + meta.label : '');
document.getElementById('sub').textContent =
  'params: ' + JSON.stringify(meta.params) + ' | ' + meta.note;
document.getElementById('cnt').textContent = M.signals.length + ' signal(s), '
  + M.signals.filter(s=>s.simulated).length + ' simulated trade(s), '
  + M.skipped.length + ' skipped';

/* index lookup: entry_ts -> bar index */
const idxOf = {}; C.t.forEach((t,i)=>idxOf[t]=i);
const SIG = M.signals.map(s=>Object.assign({}, s, {
  i: idxOf[s.ts]!==undefined?idxOf[s.ts]:null,
  j: s.exit_ts && idxOf[s.exit_ts]!==undefined?idxOf[s.exit_ts]:null
})).filter(s=>s.i!==null);

const cv=document.getElementById('cv'), cx=cv.getContext('2d'), tip=document.getElementById('tip');
let lo=0, hi=C.c.length-1, padL=8, padR=72, padT=14, padB=26;

function fit(){cv.width=cv.clientWidth*devicePixelRatio; cv.height=cv.clientHeight*devicePixelRatio;
  cx.setTransform(devicePixelRatio,0,0,devicePixelRatio,0,0); draw();}
function priceRange(){let a=1e18,b=-1e18;for(let k=lo;k<=hi;k++){a=Math.min(a,C.l[k]);b=Math.max(b,C.h[k]);}
  for(const s of SIG) if(s.i>=lo&&s.i<=hi){a=Math.min(a,s.sl);b=Math.max(b,s.tp);} return [a,b];}
function x(i){return padL+(i-lo)/Math.max(1,hi-lo+1)*(cv.clientWidth-padL-padR);}
function y(p,a,b){const t=padT,h=cv.clientHeight-padT-padB;return t+(b-p)/(b-a||1)*h;}

function draw(){
  const W=cv.clientWidth,H=cv.clientHeight; cx.clearRect(0,0,W,H);
  const [a,b]=priceRange();
  // grid + price axis
  cx.strokeStyle='#21262d'; cx.fillStyle='#8b949e'; cx.font='11px monospace'; cx.lineWidth=1;
  for(let g=0;g<=5;g++){const p=a+(b-a)*g/5,yy=y(p,a,b);
    cx.beginPath();cx.moveTo(padL,yy);cx.lineTo(W-padR,yy);cx.stroke();
    cx.fillText(p.toFixed(2),W-padR+6,yy+3);}
  // candles
  const step=Math.max(1,(hi-lo+1)/(W-padL-padR)), bw=Math.max(1,step*0.62);
  for(let k=Math.max(0,lo);k<=hi;k++){
    const up=C.c[k]>=C.o[k], xx=x(k);
    cx.strokeStyle=up?'#3fb950':'#f85149'; cx.fillStyle=up?'rgba(63,185,80,.7)':'rgba(248,81,73,.7)';
    cx.beginPath();cx.moveTo(xx,y(C.h[k],a,b));cx.lineTo(xx,y(C.l[k],a,b));cx.stroke();
    cx.fillRect(xx-bw/2,y(Math.max(C.o[k],C.c[k]),a,b),bw,Math.max(1,Math.abs(y(C.o[k],a,b)-y(C.c[k],a,b))));}
  // signals
  for(const s of SIG){ if(s.i<lo||s.i>hi) continue; const xx=x(s.i);
    cx.setLineDash([3,3]); cx.lineWidth=1;
    cx.strokeStyle='#f85149'; cx.beginPath();cx.moveTo(xx,y(s.entry,a,b));cx.lineTo(xx,y(C.c[s.i],a,b));cx.stroke();
    cx.strokeStyle='#d29922'; cx.beginPath();cx.moveTo(xx-6,y(s.sl,a,b));cx.lineTo(xx+6,y(s.sl,a,b));cx.stroke();
    cx.strokeStyle='#a371f7'; cx.beginPath();cx.moveTo(xx-6,y(s.tp,a,b));cx.lineTo(xx+6,y(s.tp,a,b));cx.stroke();
    if(s.j!==null&&s.j!==undefined){cx.strokeStyle='#8b949e';cx.beginPath();
      cx.moveTo(xx,y(s.entry,a,b));cx.lineTo(x(s.j),y(s.exit,a,b));cx.stroke();}
    cx.setLineDash([]);
    // marker: filled = simulated trade, hollow = signal only
    const my=s.simulated?y(s.sl,a,b)+16:y(C.h[s.i],a,b)-8;
    cx.beginPath();cx.arc(xx,my,5,0,7);
    if(s.simulated){cx.fillStyle='#3fb950';cx.fill();}
    else{cx.strokeStyle='#58a6ff';cx.lineWidth=2;cx.stroke();}
    cx.fillStyle=s.simulated?'#3fb950':'#58a6ff';cx.font='bold 10px monospace';
    cx.fillText(s.strategy,xx-11,my-8);
  }
  // time axis
  cx.fillStyle='#8b949e';
  for(let k=lo;k<=hi;k+=Math.max(1,Math.floor((hi-lo+1)/8))){cx.fillText(C.t[k].slice(0,10),x(k)-24,H-8);}
  // hover
  const mv=cv.onmousemove;
}

cv.addEventListener('mousemove',e=>{const r=cv.getBoundingClientRect(),mx=e.clientX-r.left,my=e.clientY-r.top;
  let best=null,bd=1e9;for(const s of SIG){const dx=Math.abs(x(s.i)-mx);if(dx<bd&&s.i>=lo&&s.i<=hi){bd=dx;best=s;}}
  if(best&&bd<8){tip.style.display='block';tip.style.left=(mx+14)+'px';tip.style.top=(my+10)+'px';
    tip.textContent=(best.signal+'  '+best.strategy+'\n'+
      (best.simulated?'SIMULATED TRADE':'SIGNAL (no trade)\n')+
      'entry  '+best.entry+'\nSL      '+best.sl+'\nTP      '+best.tp+
      (best.exit?'\nexit    '+best.exit+'  ('+best.exit_reason+')\nnet     '+best.net_pnl+'\nR       '+best.r:'')+
      (best.body_pct!=null?'\nbody%   '+best.body_pct:''));}else tip.style.display='none';});
cv.addEventListener('mousedown',e=>{drag={x:e.clientX,lo:lo,hi:hi};});
window.addEventListener('mouseup',()=>drag=null);
window.addEventListener('mousemove',e=>{if(!drag)return;
  const dx=e.clientX-drag.x,per=(drag.hi-drag.lo)/(cv.clientWidth-padL-padR);
  let nl=Math.round(drag.lo-dx*per),nh=Math.round(drag.hi-dx*per);if(nh-nl<5)return;
  lo=Math.max(0,nl);hi=Math.min(C.c.length-1,nh);draw();});
cv.addEventListener('wheel',e=>{e.preventDefault();const k=e.deltaY>0?1.15:0.87;
  const mid=(lo+hi)/2,span=(hi-lo)*k;lo=Math.max(0,Math.round(mid-span/2));
  hi=Math.min(C.c.length-1,Math.round(mid+span/2));draw();},{passive:false});
window.addEventListener('resize',fit);

/* side panel */
const L=document.getElementById('list');
for(const s of SIG){const d=document.createElement('div');
  d.className='sig '+(s.simulated?'sim':'skip');
  const pnl=s.simulated?(s.net_pnl>=0?'<span class=pos>+'+s.net_pnl.toFixed(2)+'</span>':'<span class=neg>'+s.net_pnl.toFixed(2)+'</span>'):'';
  d.innerHTML='<span class=t>'+s.signal+' '+s.strategy+'</span> '
   +'<span class=mut>'+s.ts.slice(0,16)+'</span><br>'
   +'entry '+s.entry+' | SL '+s.sl+' | TP '+s.tp
   +(s.atr!=null?'<br>ATR '+Number(s.atr).toFixed(2)+' body% '+(s.body_pct!=null?Number(s.body_pct).toFixed(3):'n/a'):'')
   +(s.simulated?'<br>'+s.exit_reason+' @ '+s.exit+'  R='+s.r+'  '+pnl:'<br><span class=mut>signal only — no simulated trade</span>');
  L.appendChild(d);}
if(M.skipped.length){const h=document.createElement('div');h.innerHTML='<br><b>SKIPPED SIGNALS</b>';L.appendChild(h);
  for(const s of M.skipped){const d=document.createElement('div');d.className='skip';
    d.innerHTML='<span class=t>'+s.strategy+'</span> <span class=mut>'+s.ts.slice(0,16)+'</span><br>'+s.reason;L.appendChild(d);}}
fit();
</script></body></html>"""
