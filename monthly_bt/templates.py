"""Combined V11+V12 visual template (HTML/CSS/JS as a string).

Kept in its own module because it is a large opaque blob: visual.py stays
readable and the renderer logic stays reviewable.

Zero dependencies on purpose — matplotlib/plotly are not installed in this
project and adding them was rejected. This is a self-contained canvas
candlestick chart: no CDN, no network, opens straight from disk.
"""
from __future__ import annotations

COMBINED_TEMPLATE = r"""<!doctype html>
<html><head><meta charset="utf-8"><title>Visual Backtest - V11 + V12</title>
<style>
 body{margin:0;background:#0e1117;color:#c9d1d9;font:13px ui-monospace,Consolas,monospace}
 header{padding:10px 14px;background:#161b22;border-bottom:1px solid #30363d}
 h1{margin:0 0 4px;font-size:15px}
 .sub{color:#8b949e;font-size:12px}
 .panel{border-bottom:1px solid #21262d}
 .ph{padding:6px 14px;background:#0d1117;font-size:12px}
 .ph b{color:#58a6ff}
 .ph .tf{color:#d29922}
 canvas{display:block;cursor:crosshair}
 aside{padding:8px 14px}
 .sig{border-left:3px solid #58a6ff;padding:5px 8px;margin:4px 0;background:#161b22}
 .sim{border-left-color:#3fb950}
 .skip{border-left-color:#d29922}
 .t{color:#58a6ff;font-weight:bold}
 .pos{color:#3fb950}.neg{color:#f85149}.mut{color:#8b949e}
 #tip{position:fixed;background:#161b22ee;border:1px solid #30363d;padding:6px 8px;
      pointer-events:none;display:none;font-size:12px;z-index:9;white-space:pre}
 .lg span{margin-right:14px}
</style></head><body>
<header>
 <h1 id="title"></h1>
 <div class="sub" id="sub"></div>
 <div class="sub lg" style="margin-top:6px">
  <span style="color:#3fb950">&#9679; filled = SIMULATED TRADE</span>
  <span style="color:#58a6ff">&#9675; hollow = SIGNAL (no trade)</span>
  <span style="color:#f85149">&#9632; entry</span>
  <span style="color:#d29922">&#9632; SL</span>
  <span style="color:#a371f7">&#9632; TP</span>
  <span class="mut">drag = pan, wheel = zoom, hover = details</span>
 </div>
</header>
<div id="panels"></div>
<div id="tip"></div>
<script>
const D = /*__DATA__*/;
const tip = document.getElementById('tip');
document.getElementById('title').textContent =
  'Visual backtest - V11 + V12 on ' + D.meta.symbol + (D.meta.label ? ' - ' + D.meta.label : '');
document.getElementById('sub').textContent = D.meta.note;

D.series.forEach(function(S){
  const C = S.candles, M = S.markers, spec = S.spec;
  const wrap = document.createElement('div'); wrap.className='panel';
  wrap.innerHTML = '<div class="ph"><b>'+spec.strategy+'</b> '
    + '<span class="mut">'+spec.strategy_name+'</span> | timeframe <span class="tf">'
    + spec.timeframe+'</span> | params '+JSON.stringify(spec.params)+'</div>';
  const cv = document.createElement('canvas'); cv.style.height='400px';
  wrap.appendChild(cv);
  const aside = document.createElement('aside');
  aside.innerHTML = '<b>SIGNALS</b> <span class="mut">('+M.signals.length+' signal(s), '
    + M.signals.filter(function(s){return s.simulated;}).length+' trade(s), '
    + M.skipped.length+' skipped)</span>';
  M.signals.forEach(function(s){
    const d=document.createElement('div');
    d.className='sig '+(s.simulated?'sim':'skip');
    let pnl='';
    if(s.simulated){
      pnl = s.net_pnl>=0 ? '<span class=pos>+'+Number(s.net_pnl).toFixed(2)+'</span>'
                         : '<span class=neg>'+Number(s.net_pnl).toFixed(2)+'</span>';
    }
    d.innerHTML = '<span class=t>'+s.signal+' '+s.strategy+'</span> <span class=mut>'
      + s.ts.slice(0,16) + '</span><br>entry '+s.entry+' | SL '+s.sl+' | TP '+s.tp
      + (s.atr!=null ? '<br>ATR '+Number(s.atr).toFixed(2) : '')
      + (s.simulated ? '<br>'+s.exit_reason+' @ '+s.exit+'  R='+s.r+'  '+pnl
                     : '<br><span class=mut>signal only - no simulated trade</span>');
    aside.appendChild(d);
  });
  M.skipped.forEach(function(s){
    const d=document.createElement('div'); d.className='sig skip';
    d.innerHTML='<span class=t>'+s.strategy+'</span> <span class=mut>'+s.ts.slice(0,16)
      +'</span><br>'+s.reason;
    aside.appendChild(d);
  });
  wrap.appendChild(aside);
  document.getElementById('panels').appendChild(wrap);

  const idxOf={}; C.t.forEach(function(t,i){idxOf[t]=i;});
  const SIG = M.signals.map(function(s){
    return Object.assign({},s,{
      i: idxOf[s.ts]!==undefined ? idxOf[s.ts] : null,
      j: s.exit_ts && idxOf[s.exit_ts]!==undefined ? idxOf[s.exit_ts] : null});
  }).filter(function(s){return s.i!==null;});

  const cx = cv.getContext('2d');
  let lo=0, hi=C.c.length-1, drag=null;
  const padL=8, padR=78, padT=12, padB=24;

  function fit(){
    cv.width=cv.clientWidth*devicePixelRatio;
    cv.height=cv.clientHeight*devicePixelRatio;
    cx.setTransform(devicePixelRatio,0,0,devicePixelRatio,0,0);
    draw();
  }
  function pr(){
    let a=1e18,b=-1e18;
    for(let k=lo;k<=hi;k++){ a=Math.min(a,C.l[k]); b=Math.max(b,C.h[k]); }
    for(const s of SIG) if(s.i>=lo&&s.i<=hi){ a=Math.min(a,s.sl); b=Math.max(b,s.tp); }
    return [a,b];
  }
  function x(i){ return padL+(i-lo)/Math.max(1,hi-lo+1)*(cv.clientWidth-padL-padR); }
  function y(p,a,b){ return padT+(b-p)/(b-a||1)*(cv.clientHeight-padT-padB); }

  function draw(){
    const W=cv.clientWidth,H=cv.clientHeight;
    cx.clearRect(0,0,W,H);
    const a=pr()[0], b=pr()[1];
    cx.strokeStyle='#21262d'; cx.fillStyle='#8b949e'; cx.font='11px monospace'; cx.lineWidth=1;
    for(let g=0;g<=5;g++){
      const p=a+(b-a)*g/5, yy=y(p,a,b);
      cx.beginPath(); cx.moveTo(padL,yy); cx.lineTo(W-padR,yy); cx.stroke();
      cx.fillText(p.toFixed(2),W-padR+6,yy+3);
    }
    const step=Math.max(1,(hi-lo+1)/(W-padL-padR)), bw=Math.max(1,step*0.62);
    for(let k=Math.max(0,lo);k<=hi;k++){
      const up=C.c[k]>=C.o[k], xx=x(k);
      cx.strokeStyle=up?'#3fb950':'#f85149';
      cx.fillStyle=up?'rgba(63,185,80,.7)':'rgba(248,81,73,.7)';
      cx.beginPath(); cx.moveTo(xx,y(C.h[k],a,b)); cx.lineTo(xx,y(C.l[k],a,b)); cx.stroke();
      cx.fillRect(xx-bw/2,y(Math.max(C.o[k],C.c[k]),a,b),bw,
                  Math.max(1,Math.abs(y(C.o[k],a,b)-y(C.c[k],a,b))));
    }
    for(const s of SIG){
      if(s.i<lo||s.i>hi) continue;
      const xx=x(s.i);
      cx.setLineDash([3,3]); cx.lineWidth=1;
      cx.strokeStyle='#f85149';
      cx.beginPath(); cx.moveTo(xx,y(s.entry,a,b)); cx.lineTo(xx,y(C.c[s.i],a,b)); cx.stroke();
      cx.strokeStyle='#d29922';
      cx.beginPath(); cx.moveTo(xx-6,y(s.sl,a,b)); cx.lineTo(xx+6,y(s.sl,a,b)); cx.stroke();
      cx.strokeStyle='#a371f7';
      cx.beginPath(); cx.moveTo(xx-6,y(s.tp,a,b)); cx.lineTo(xx+6,y(s.tp,a,b)); cx.stroke();
      if(s.j!==null && s.j!==undefined){
        cx.strokeStyle='#8b949e';
        cx.beginPath(); cx.moveTo(xx,y(s.entry,a,b)); cx.lineTo(x(s.j),y(s.exit,a,b)); cx.stroke();
      }
      cx.setLineDash([]);
      const my = s.simulated ? y(s.sl,a,b)+16 : y(C.h[s.i],a,b)-8;
      cx.beginPath(); cx.arc(xx,my,5,0,7);
      if(s.simulated){ cx.fillStyle='#3fb950'; cx.fill(); }
      else { cx.strokeStyle='#58a6ff'; cx.lineWidth=2; cx.stroke(); }
      cx.fillStyle = s.simulated ? '#3fb950' : '#58a6ff';
      cx.font='bold 10px monospace';
      cx.fillText(s.strategy,xx-11,my-8);
    }
    cx.fillStyle='#8b949e';
    for(let k=lo;k<=hi;k+=Math.max(1,Math.floor((hi-lo+1)/8))){
      cx.fillText(C.t[k].slice(0,10),x(k)-24,H-7);
    }
  }

  cv.addEventListener('mousemove',function(e){
    const r=cv.getBoundingClientRect(), mx=e.clientX-r.left;
    let best=null, bd=1e9;
    for(const s of SIG){
      const dx=Math.abs(x(s.i)-mx);
      if(dx<bd && s.i>=lo && s.i<=hi){ bd=dx; best=s; }
    }
    if(best && bd<8){
      tip.style.display='block';
      tip.style.left=(e.clientX+16)+'px';
      tip.style.top=(e.clientY+12)+'px';
      let txt = best.signal+'  '+best.strategy+'  ('+spec.timeframe+' bar)\n'
        + (best.simulated ? 'SIMULATED TRADE' : 'SIGNAL (no trade)') + '\n'
        + 'entry  '+best.entry+'\nSL      '+best.sl+'\nTP      '+best.tp;
      if(best.exit) txt += '\nexit    '+best.exit+'  ('+best.exit_reason+')'
                         + '\nnet     '+best.net_pnl+'\nR       '+best.r;
      if(best.body_pct!=null) txt += '\nbody%   '+best.body_pct;
      tip.textContent = txt;
    } else { tip.style.display='none'; }
  });

  cv.addEventListener('mousedown',function(e){ drag={x:e.clientX,lo:lo,hi:hi}; });
  window.addEventListener('mouseup',function(){ drag=null; });
  window.addEventListener('mousemove',function(e){
    if(!drag) return;
    const dx=e.clientX-drag.x, per=(drag.hi-drag.lo)/(cv.clientWidth-padL-padR);
    let nl=Math.round(drag.lo-dx*per), nh=Math.round(drag.hi-dx*per);
    if(nh-nl<5) return;
    lo=Math.max(0,nl); hi=Math.min(C.c.length-1,nh); draw();
  });
  cv.addEventListener('wheel',function(e){
    e.preventDefault();
    const k=e.deltaY>0?1.15:0.87, mid=(lo+hi)/2, span=(hi-lo)*k;
    lo=Math.max(0,Math.round(mid-span/2));
    hi=Math.min(C.c.length-1,Math.round(mid+span/2));
    draw();
  },{passive:false});

  fit(); setTimeout(fit,80);
});
</script></body></html>"""
