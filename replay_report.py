"""
Turn a replay snapshot dump into a self-contained, filterable HTML report.

WHAT IT IS
  replay.py walks a past session through the real engine and writes
  logs/replay/frames_<session>.json — every stock's Long/Short score and RVOL for
  every minute of that session. This module renders that file into one HTML page
  with no external assets, so the "First seen" rule can be backtested:

    move the RVOL box or a table's ≤ cap  ->  every first-seen time recomputes
    in the browser, exactly as the app's own tables would have shown it.

  Same predicate the app uses (static/index.html, renderSide):
      Long table   : Long ≥ 50  and  Long > Short  and  Short ≤ cap
      Short table  : Short ≥ 50 and  Short > Long  and  Long  ≤ cap
      both         : RVOL ≥ min (blank = no constraint) — one shared box

RUN
  python3 replay.py --session 2026-10-16            # writes the snapshots
  python3 replay_report.py                          # writes the report
  python3 replay_report.py --frames logs/replay/frames_2026-10-16.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime

from upstox_client import IST

BASE = os.path.dirname(os.path.abspath(__file__))
LOGS = os.environ.get("LOGS_DIR") or os.path.join(BASE, "logs")
REPLAY_DIR = os.path.join(LOGS, "replay")

CSS = """
:root{
  --bg:#070a10; --bg2:#0b1019; --panel:#0e141f; --panel2:#131b28;
  --line:rgba(148,170,210,.10); --line2:rgba(148,170,210,.18);
  --txt:#e9eefb; --dim:#8a97b4; --dim2:#5a6785;
  --up:#0ecb81; --dn:#f6465d; --warn:#f5b544; --acc:#4c8dff;
  --upbg:rgba(14,203,129,.13); --dnbg:rgba(246,70,93,.13);
  --r:12px; --nums:ui-monospace,"SF Mono",SFMono-Regular,Menlo,Consolas,"Roboto Mono",monospace;
}
*{box-sizing:border-box}
html,body{margin:0;padding:0;background:var(--bg);color:var(--txt);
  font:14px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
  -webkit-font-smoothing:antialiased}
td,th,.mono,input,button{font-variant-numeric:tabular-nums}
.wrap{max-width:1420px;margin:0 auto;padding:0 16px 70px}
.mono{font-family:var(--nums)}
.dim{color:var(--dim)} .dim2{color:var(--dim2)}
.up{color:var(--up)} .dn{color:var(--dn)} .wn{color:var(--warn)}
a{color:var(--acc);text-decoration:none} a:hover{text-decoration:underline}

header{display:flex;align-items:center;gap:12px;flex-wrap:wrap;
  padding:16px 0 12px;margin-bottom:14px;border-bottom:1px solid var(--line)}
.brand{display:flex;align-items:center;gap:10px;font-weight:700;font-size:16.5px}
.logo{width:30px;height:30px;border-radius:9px;display:grid;place-items:center;font-size:15px;
  background:linear-gradient(135deg,#4c8dff,#8b5cf6);box-shadow:0 3px 14px rgba(76,141,255,.35)}
.spacer{flex:1}
.pill{display:inline-flex;align-items:center;gap:6px;padding:4px 10px;border-radius:8px;
  font-size:11.5px;font-weight:700;letter-spacing:.35px;border:1px solid var(--line);
  background:var(--panel);white-space:nowrap}
.pill.warn{border-color:rgba(245,181,68,.42);background:rgba(245,181,68,.12);color:#f7c76b}
.pill.ok{border-color:rgba(14,203,129,.42);background:var(--upbg);color:#4fe0a5}

.notice{margin:0 0 16px;padding:11px 14px;border-radius:var(--r);font-size:12.5px;
  border:1px solid rgba(245,181,68,.35);background:rgba(245,181,68,.08);color:#f0cd8a}
.notice b{color:#f7c76b}

.card{background:var(--panel);border:1px solid var(--line);border-radius:var(--r);
  padding:14px;margin-bottom:18px}
.card>h2{margin:0 0 4px;font-size:15px;display:flex;align-items:center;gap:8px}
.card>h2 .sub{font-weight:400;font-size:12px;color:var(--dim)}
.rule{margin:0 0 12px;font-size:12.5px;color:var(--dim)}
.rule code{font-family:var(--nums);color:#c3cede;background:var(--panel2);
  border:1px solid var(--line);border-radius:6px;padding:1px 5px}

/* ---- controls ---- */
.controls{display:flex;align-items:center;gap:14px;flex-wrap:wrap;
  background:var(--panel);border:1px solid var(--line);border-radius:var(--r);
  padding:11px 14px;margin-bottom:16px}
.field{display:flex;align-items:center;gap:7px;font-size:12.5px}
.field label{color:var(--dim)}
.field input{width:74px;background:var(--bg2);color:var(--txt);border:1px solid var(--line2);
  border-radius:8px;padding:6px 8px;font-family:var(--nums);font-size:13px}
.field input:focus{outline:none;border-color:var(--acc);box-shadow:0 0 0 3px rgba(76,141,255,.15)}
.field.long label b{color:var(--up)} .field.short label b{color:var(--dn)}
.presets{display:flex;gap:6px;flex-wrap:wrap}
.btn{background:var(--panel2);color:var(--dim);border:1px solid var(--line2);border-radius:8px;
  padding:6px 10px;font-size:12px;cursor:pointer}
.btn:hover{color:var(--txt);border-color:var(--acc)}
.btn.on{color:#cfe0ff;border-color:rgba(76,141,255,.55);background:rgba(76,141,255,.14)}
.who{font-size:12px;color:var(--dim2);margin-left:auto}

/* ---- kpis ---- */
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(168px,1fr));gap:10px;margin-bottom:18px}
.kpi{background:var(--panel);border:1px solid var(--line);border-radius:var(--r);padding:11px 13px}
.kpi .k{font-size:11px;letter-spacing:.4px;text-transform:uppercase;color:var(--dim2)}
.kpi .v{font-family:var(--nums);font-size:23px;font-weight:700;margin-top:3px}
.kpi .s{font-size:11.5px;color:var(--dim);margin-top:2px}

/* ---- tables ---- */
table{width:100%;border-collapse:collapse}
th,td{padding:6px 8px;text-align:left;border-bottom:1px solid var(--line);white-space:nowrap}
th{font-size:11px;letter-spacing:.35px;text-transform:uppercase;color:var(--dim2);font-weight:600;
  position:sticky;top:0;background:var(--panel);z-index:2}
tbody tr:hover{background:rgba(76,141,255,.06)}
td.num{text-align:right;font-family:var(--nums)}
.sym{font-weight:650}
.tag{font-size:10.5px;font-weight:700;border-radius:5px;padding:1px 5px;border:1px solid var(--line2)}
.tag.long{color:#4fe0a5;background:var(--upbg);border-color:rgba(14,203,129,.42)}
.tag.short{color:#ff8b9c;background:var(--dnbg);border-color:rgba(246,70,93,.42)}
.tag.new{color:#c4b5fd;background:rgba(139,92,246,.16);border-color:rgba(139,92,246,.5)}
.tag.pick{color:#f7c76b;background:rgba(245,181,68,.12);border-color:rgba(245,181,68,.42)}

/* timeline strip: 09:15 -> 15:30 */
.lane{position:relative;height:16px;min-width:230px;border-radius:5px;
  background:
    linear-gradient(90deg,rgba(148,170,210,.10) 1px,transparent 1px) 0 0/25% 100%,
    repeating-linear-gradient(90deg,rgba(148,170,210,.06) 0 1px,transparent 1px 100%),
    var(--bg2);
  border:1px solid var(--line);overflow:hidden}
.lane i{position:absolute;top:2px;bottom:2px;border-radius:3px;display:block}
.lane i.long{background:linear-gradient(180deg,#2ee6a8,#12a06a);box-shadow:0 0 6px rgba(14,203,129,.35)}
.lane i.short{background:linear-gradient(180deg,#ff7d90,#c9384f);box-shadow:0 0 6px rgba(246,70,93,.35)}
.lane i.pick{outline:1px solid rgba(245,181,68,.85)}
.lane u{position:absolute;top:0;bottom:0;width:2px;background:#e9eefb;opacity:.85;
  border-radius:1px;text-decoration:none}
.axis{position:relative;height:15px;min-width:230px;margin-top:3px}
.axis s{position:absolute;transform:translateX(-50%);font-size:10px;color:var(--dim2);text-decoration:none}
.empty{padding:16px;text-align:center;color:var(--dim2);font-size:13px}

/* membership chart */
.chart{width:100%;height:150px;display:block}
.legend{display:flex;gap:16px;font-size:12px;color:var(--dim);margin-top:6px;flex-wrap:wrap}
.legend i{display:inline-block;width:10px;height:10px;border-radius:3px;margin-right:5px}

/* sensitivity grid */
.gridwrap{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:14px}
.grid table{border-collapse:separate;border-spacing:3px}
.grid th{position:static;background:none;text-transform:none;font-size:11px;color:var(--dim);padding:2px 5px}
.grid td{padding:7px 8px;border:1px solid var(--line);border-radius:8px;text-align:center;
  font-family:var(--nums);font-size:12.5px;cursor:default}
.grid td.hd{color:var(--dim2);font-family:inherit;font-size:11.5px;text-align:right;border:none}
.grid .big{display:block;font-size:14px;font-weight:700}
.grid .sm{display:block;font-size:10.5px;color:var(--dim)}
footer{margin-top:22px;padding-top:14px;border-top:1px solid var(--line);
  font-size:12px;color:var(--dim2)}
""".strip()

JS = r"""
const D = JSON.parse(document.getElementById('rdata').textContent);
const FLOOR = 50;
const S = D.stamps_iso.map(s => s.slice(11, 16));          // "09:15"
const N = S.length;
const syms = D.symbols;
const IDX = {}; syms.forEach((s, i) => IDX[s] = i);
const frames = D.frames;
const picks = new Set(Object.keys(D.recorded || {}));

/* per-minute lookup: score[side][symIdx][stamp] using sparse maps we fill once */
const SL = syms.map(() => new Array(N).fill(null));
const SS = syms.map(() => new Array(N).fill(null));
const RV = syms.map(() => new Array(N).fill(null));
for(let t = 0; t < N; t++)
  for(const [si, sl, ss, rv] of frames[t]){ SL[si][t] = sl; SS[si][t] = ss; RV[si][t] = rv; }

const num = id => { const v = document.getElementById(id).value.trim(); return v === '' ? null : Number(v); };
const isNum = v => v !== null && v !== undefined && !Number.isNaN(v);
const pad2 = n => String(n).padStart(2, '0');
const ist12 = hhmm => {                                   // same 12-hour rule the app uses
  const [h, m] = hhmm.split(':').map(Number);
  const p = h >= 12 ? 'PM' : 'AM'; const h12 = h % 12 === 0 ? 12 : h % 12;
  return `${h12}:${pad2(m)} ${p}`;
};
const hm = t => S[t];
const hms = t => S[t] + ':00';

/* one predicate, mirroring static/index.html renderSide() */
function qualifies(si, t, side, rvMin, cap){
  const a = side === 'long' ? SL[si][t] : SS[si][t];
  const b = side === 'long' ? SS[si][t] : SL[si][t];
  if(a === null || b === null) return false;
  if(a < FLOOR) return false;
  if(!(a > b)) return false;
  const rv = RV[si][t];
  if(isNum(rvMin) && !(isNum(rv) && rv >= rvMin)) return false;
  if(isNum(cap) && !(b <= cap)) return false;
  return true;
}

/* full history for one side under one filter (hoisted arrays: this is the hot loop) */
function scan(side, rvMin, cap){
  const useRv = isNum(rvMin), useCap = isNum(cap);
  const A = side === 'long' ? SL : SS, B = side === 'long' ? SS : SL;
  const out = [];
  for(let si = 0; si < syms.length; si++){
    const a = A[si], b = B[si], rv = RV[si];
    let first = -1, last = -1, n = 0, best = null, inside = false;
    const runs = [];
    for(let t = 0; t < N; t++){
      const av = a[t];
      if(av !== null && av >= FLOOR){
        const bv = b[t];
        if(bv !== null && av > bv && (!useCap || bv <= cap) &&
           (!useRv || (rv[t] !== null && rv[t] >= rvMin))){
          if(first < 0) first = t;
          last = t; n++;
          if(best === null || av > best) best = av;
          if(inside) runs[runs.length - 1][1] = t;
          else { runs.push([t, t]); inside = true; }
          continue;
        }
      }
      inside = false;
    }
    if(first < 0) continue;
    out.push({si, sym: syms[si], first, last, mins: n, score: best, runs,
              rvol: rv[first], other: b[first]});
  }
  out.sort((x, y) => x.first - y.first || (y.score - x.score));
  return out;
}

function laneHTML(r, side){
  const w = 100 / N;
  let h = '';
  for(const [a, b] of r.runs)
    h += `<i class="${side}${picks.has(r.sym) ? ' pick' : ''}" style="left:${a * w}%;width:${Math.max(0.35,(b - a + 1) * w)}%" title="${
      side.toUpperCase()} ${r.sym} · ${hm(a)} → ${hm(b)}"></i>`;
  h += `<u style="left:${r.first * w}%" title="first seen ${hms(r.first)}"></u>`;
  return `<div class="lane">${h}</div>`;
}


function med(arr){ if(!arr.length) return null; const s = arr.slice().sort((a, b) => a - b);
  const m = s.length >> 1; return s.length % 2 ? s[m] : Math.round((s[m - 1] + s[m]) / 2); }

function rowsHTML(rows, side){
  if(!rows.length) return `<tr><td colspan="7" class="empty">No ${side} stock passed this filter.</td></tr>`;
  return rows.map(r => `<tr>
    <td><span class="sym">${r.sym}</span>${picks.has(r.sym) ? ' <span class="tag pick" title="the engine logged this as a pick that day">pick</span>' : ''}${
      r.first < 15 ? ' <span class="tag new" title="entered in the first 15 minutes">early</span>' : ''}</td>
    <td class="num">${hms(r.first)}<div class="dim2" style="font-size:10.5px">${ist12(hm(r.first))}</div></td>
    <td>${laneHTML(r, side)}</td>
    <td class="num">${r.mins}</td>
    <td class="num">${r.score === null ? '—' : r.score.toFixed(1)}</td>
    <td class="num">${isNum(r.other) ? r.other.toFixed(1) : '—'}</td>
    <td class="num">${isNum(r.rvol) ? r.rvol.toFixed(2) + '×' : '—'}</td>
  </tr>`).join('');
}

/* ---- membership curve ---- */
function curve(L, Sh){
  const longN = new Array(N).fill(0), shortN = new Array(N).fill(0);
  for(const r of L) for(const [a, b] of r.runs) for(let t = a; t <= b; t++) longN[t]++;
  for(const r of Sh) for(const [a, b] of r.runs) for(let t = a; t <= b; t++) shortN[t]++;
  const W = 1000, H = 150, px = i => (i + 0.5) * W / N;
  const max = Math.max(4, ...longN, ...shortN);
  const py = v => H - 18 - (v / max) * (H - 34);
  const line = arr => arr.map((v, i) => `${px(i).toFixed(1)},${py(v).toFixed(1)}`).join(' ');
  const area = arr => `0,${py(0)} ${line(arr)} ${W.toFixed(1)},${py(0)}`;
  let ticks = '';
  for(let i = 0; i < N; i += 45){
    ticks += `<line x1="${px(i)}" y1="0" x2="${px(i)}" y2="${H - 18}" stroke="rgba(148,170,210,.10)"/>`;
    ticks += `<text x="${px(i)}" y="${H - 4}" fill="#5a6785" font-size="10" text-anchor="middle">${hm(i)}</text>`;
  }
  let gl = '';
  for(let g = 0; g <= 4; g++){
    const v = max * g / 4, y = py(v);
    gl += `<line x1="0" y1="${y}" x2="${W}" y2="${y}" stroke="rgba(148,170,210,.08)"/>`;
    gl += `<text x="2" y="${y - 2}" fill="#5a6785" font-size="10">${Math.round(v)}</text>`;
  }
  document.getElementById('chart').innerHTML =
    `<svg class="chart" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">${gl}${ticks}
      <polygon points="${area(shortN)}" fill="rgba(246,70,93,.16)"/>
      <polygon points="${area(longN)}" fill="rgba(14,203,129,.16)"/>
      <polyline points="${line(longN)}" fill="none" stroke="#2ee6a8" stroke-width="2"/>
      <polyline points="${line(shortN)}" fill="none" stroke="#ff7d90" stroke-width="2"/>
    </svg>`;
  document.getElementById('chartcap').textContent =
    `peak ${Math.max(...longN)} long / ${Math.max(...shortN)} short at one minute`;
}

/* ---- filter-sensitivity matrix (computed once) ---- */
const RV_GRID = [null, 1.0, 1.5, 2.0, 2.5, 3.0];
const CP_GRID = [null, 60, 50, 40, 35, 30];
function matrix(){
  const cells = {long: [], short: []};
  for(const side of ['long', 'short']){
    cells[side] = RV_GRID.map(rv => CP_GRID.map(cap => {
      const rows = scan(side, rv, cap);
      return {n: rows.length, med: med(rows.map(r => r.first)), rows};
    }));
  }
  const colour = n => {
    const a = Math.min(1, n / 60);
    return `background:rgba(76,141,255,${(a * 0.30).toFixed(2)})`;
  };
  const label = v => v === null ? 'any' : (v >= 60 ? String(v) : v.toFixed(1));
  for(const side of ['long', 'short']){
    const g = cells[side];
    const maxN = Math.max(...g.flat().map(c => c.n), 1);
    let h = '<table><tr><th></th>' + CP_GRID.map(c => `<th>cap ${label(c)}</th>`).join('') + '</tr>';
    RV_GRID.forEach((rv, i) => {
      h += `<tr><td class="hd">RVOL ≥ ${label(rv)}</td>`;
      CP_GRID.forEach((cap, j) => {
        const c = g[i][j];
        const op = 0.05 + 0.35 * (c.n / maxN);
        h += `<td style="background:rgba(${side === 'long' ? '14,203,129' : '246,70,93'},${op.toFixed(2)})"
          title="${c.n} stocks · median first seen ${c.med === null ? '—' : hms(c.med)}">` +
          `<span class="big">${c.n}</span><span class="sm">${c.med === null ? '—' : 'med ' + hm(c.med)}</span></td>`;
      });
      h += '</tr>';
    });
    document.getElementById(side + 'matrix').innerHTML = h + '</table>';
  }
}

/* ---- render ---- */
function render(){
  const rvMin = num('fRvol'), cl = num('fShortMax'), cs = num('fLongMax');
  const q = document.getElementById('q').value.trim().toLowerCase();
  const keep = rows => rows.filter(r => !q || r.sym.toLowerCase().includes(q));
  const L = keep(scan('long', rvMin, cl)), Sh = keep(scan('short', rvMin, cs));
  document.getElementById('tbodyLong').innerHTML = rowsHTML(L, 'long');
  document.getElementById('tbodyShort').innerHTML = rowsHTML(Sh, 'short');

  document.getElementById('nLong').textContent = L.length;
  document.getElementById('nShort').textContent = Sh.length;

  const medL = med(L.map(r => r.first)), medS = med(Sh.map(r => r.first));
  document.getElementById('kLong').textContent = L.length;
  document.getElementById('kLongS').textContent = medL === null ? 'none' : 'median first seen ' + hms(medL);
  document.getElementById('kShort').textContent = Sh.length;
  document.getElementById('kShortS').textContent = medS === null ? 'none' : 'median first seen ' + hms(medS);

  const early = [...L, ...Sh].filter(r => r.first < 15).length;
  document.getElementById('kEarly').textContent = early;
  document.getElementById('kEarlyS').textContent = 'entered in the first 15 minutes';

  const all = [...L, ...Sh];
  const late = all.filter(r => r.last >= N - 1).length;
  document.getElementById('kLate').textContent = late;
  document.getElementById('kLateS').textContent = 'still in a table at ' + hm(N - 1);
  const longest = all.slice().sort((a, b) => b.mins - a.mins)[0];
  document.getElementById('kLongest').textContent = longest ? longest.sym : '—';
  document.getElementById('kLongestS').textContent = longest ? longest.mins + ' minutes in the table' : 'nothing qualified';
  document.getElementById('kWindow').textContent = hm(0) + '→' + hm(N - 1);
  document.getElementById('kWindowS').textContent = N + ' minutes replayed at ' + D.step_min + '-minute steps';

  document.getElementById('sig').textContent =
    `RVOL ≥ ${isNum(rvMin) ? rvMin : 'any'} · Short ≤ ${isNum(cl) ? cl : 'any'} · Long ≤ ${isNum(cs) ? cs : 'any'}`;
  curve(L, Sh);
}

const PRESETS = [
  ['none', null, null, null],
  ['RVOL ≥ 1.5', 1.5, null, null],
  ['RVOL ≥ 1.5 · cap 40', 1.5, 40, 40],
  ['RVOL ≥ 2 · cap 35', 2, 35, 35],
  ['tight: RVOL ≥ 2 · cap 30', 2, 30, 30],
];
function applyPreset(p){
  document.getElementById('fRvol').value = p[1] === null ? '' : p[1];
  document.getElementById('fShortMax').value = p[2] === null ? '' : p[2];
  document.getElementById('fLongMax').value = p[3] === null ? '' : p[3];
  render();
}
function markPreset(){
  const rv = num('fRvol'), cl = num('fShortMax'), cs = num('fLongMax');
  [...document.querySelectorAll('#presets .btn')].forEach((b, i) => {
    const p = PRESETS[i];
    b.classList.toggle('on', p[1] === rv && p[2] === cl && p[3] === cs);
  });
}
function initControls(){
  const box = document.getElementById('presets');
  PRESETS.forEach(p => {
    const b = document.createElement('button');
    b.className = 'btn'; b.textContent = p[0];
    b.onclick = () => { applyPreset(p); markPreset(); };
    box.appendChild(b);
  });
  ['fRvol', 'fShortMax', 'fLongMax', 'q'].forEach(id =>
    document.getElementById(id).addEventListener('input', () => { markPreset(); render(); }));
  matrix();
  markPreset();
  render();
}
document.addEventListener('DOMContentLoaded', initControls);
"""

HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>__TITLE__</title>
<style>
__CSS__
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div class="brand"><span class="logo">▤</span> Session replay</div>
    <span class="pill ok">__SESSION__</span>
    <span class="pill">__SOURCE_PILL__</span>
    <span class="pill">__STAMPS__ minutes</span>
    <span class="spacer"></span>
    <span class="dim" style="font-size:12px">built __BUILT__ IST · <a href="/">back to the live screen</a></span>
  </header>

  __NOTICE__

  <div class="card">
    <h2>How a stock gets into a table <span class="sub">the exact rule the live screen uses</span></h2>
    <p class="rule">
      <code>Long</code> — Long score ≥ 50 <b>and</b> Long &gt; Short <b>and</b> Short ≤ cap &nbsp;·&nbsp;
      <code>Short</code> — Short score ≥ 50 <b>and</b> Short &gt; Long <b>and</b> Long ≤ cap &nbsp;·&nbsp;
      <code>RVOL</code> — one shared minimum, blank means no constraint.<br/>
      "First seen" is the first minute in this session that a stock satisfied the filter that is set below.
      Change the boxes and every timestamp recomputes — that is the backtest.
    </p>
    <div class="controls" style="margin:0">
      <div class="field"><label for="fRvol">RVOL ≥</label>
        <input id="fRvol" type="number" min="0" step="0.1" placeholder="any" value="1.5"></div>
      <div class="field long"><label for="fShortMax"><b>Short</b> ≤</label>
        <input id="fShortMax" type="number" min="0" step="1" placeholder="any"></div>
      <div class="field short"><label for="fLongMax"><b>Long</b> ≤</label>
        <input id="fLongMax" type="number" min="0" step="1" placeholder="any"></div>
      <div class="field"><label for="q">symbol</label>
        <input id="q" type="text" placeholder="all" style="width:110px"></div>
      <div class="presets" id="presets"></div>
      <span class="who" id="sig"></span>
    </div>
  </div>

  <div class="kpis">
    <div class="kpi"><div class="k">Long table</div><div class="v up" id="kLong">0</div><div class="s" id="kLongS"></div></div>
    <div class="kpi"><div class="k">Short table</div><div class="v dn" id="kShort">0</div><div class="s" id="kShortS"></div></div>
    <div class="kpi"><div class="k">Early entries</div><div class="v" id="kEarly">0</div><div class="s" id="kEarlyS"></div></div>
    <div class="kpi"><div class="k">Held to the close</div><div class="v" id="kLate">0</div><div class="s" id="kLateS"></div></div>
    <div class="kpi"><div class="k">Longest lived</div><div class="v" id="kLongest">—</div><div class="s" id="kLongestS"></div></div>
    <div class="kpi"><div class="k">Session</div><div class="v" id="kWindow">—</div><div class="s" id="kWindowS"></div></div>
  </div>

  <div class="card">
    <h2>▲ Long setups <span class="sub"><span id="nLong">0</span> stocks under the current filter</span></h2>
    <p class="rule">The strip is the session, 09:15 → 15:30. A bar means the stock was in the Long table during those minutes; the white tick is the first time it qualified.</p>
    <table>
      <thead><tr><th>Symbol</th><th>First seen</th><th>Session timeline__AXIS__</th>
        <th style="text-align:right">Minutes in</th><th style="text-align:right">Long</th>
        <th style="text-align:right">Short</th><th style="text-align:right">RVOL</th></tr></thead>
      <tbody id="tbodyLong"></tbody>
    </table>
  </div>

  <div class="card">
    <h2>▼ Short setups <span class="sub"><span id="nShort">0</span> stocks under the current filter</span></h2>
    <p class="rule">Same rule, mirrored: Short must be the stronger side and Long must stay at or below the cap.</p>
    <table>
      <thead><tr><th>Symbol</th><th>First seen</th><th>Session timeline__AXIS__</th>
        <th style="text-align:right">Minutes in</th><th style="text-align:right">Short</th>
        <th style="text-align:right">Long</th><th style="text-align:right">RVOL</th></tr></thead>
      <tbody id="tbodyShort"></tbody>
    </table>
  </div>

  <div class="card">
    <h2>How many stocks were in each table, minute by minute</h2>
    <p class="rule">Both curves follow the filter boxes above.</p>
    <div id="chart"></div>
    <div class="legend">
      <span><i style="background:#2ee6a8"></i>Long table</span>
      <span><i style="background:#ff7d90"></i>Short table</span>
      <span class="dim2" id="chartcap"></span>
    </div>
  </div>

  <div class="card">
    <h2>Every filter pair, at a glance</h2>
    <p class="rule">Number of stocks that would have entered each table, and the median time they first did. Hover a cell for detail. This grid is the whole backtest in one picture — read the top-left corner as "loosest filter", the bottom-right as "strictest".</p>
    <div class="gridwrap">
      <div class="grid"><h3 class="dim" style="font-size:12.5px;margin:0 0 6px">▲ Long table</h3><div id="longmatrix"></div></div>
      <div class="grid"><h3 class="dim" style="font-size:12.5px;margin:0 0 6px">▼ Short table</h3><div id="shortmatrix"></div></div>
    </div>
  </div>

  <footer>
    __FOOTER__
  </footer>
</div>
<script id="rdata" type="application/json">__DATA__</script>
<script>
__JS__
</script>
</body>
</html>
"""


def axis_html(stamps: list, every: int = 45) -> str:
    """A 09:15 → 15:30 ruler that lines up with the timeline strips."""
    n = len(stamps) or 1
    out = []
    for i in range(0, len(stamps), every):
        out.append('<s style="left:%.3f%%">%s</s>' % ((i + 0.5) * 100.0 / n, str(stamps[i])[11:16]))
    return '<div class="axis">' + "".join(out) + '</div>'


def load_frames(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        d = json.load(f)
    if not d.get("frames"):
        raise SystemExit(f"{path}: no frames in this dump — run replay.py without --no-export")
    return d


def render(d: dict, title: str | None = None) -> str:
    session = d.get("session") or "session"
    src = (d.get("source") or "").lower()
    real = src == "upstox"
    notice = ""
    if not real:
        notice = (
            '<div class="notice"><b>Simulated data.</b> These candles came from the local stand-in '
            'API, not from Upstox — the mechanism (when a stock crosses your filter) is what this page '
            'demonstrates; the prices are synthetic. Run <span class="mono">python3 replay.py</span> on a '
            'host that can reach Upstox for the same report on real session data.</div>')
    data = json.dumps(d, separators=(",", ":")).replace("</", "<\\/")
    axis = axis_html(d.get("stamps_iso") or [])
    footer = (
        "Replay of the NSE session <b>%s</b> through the real scoring engine (<span class=\"mono\">engine.py</span>), "
        "one engine clock per minute. %d minutes, %d symbols. First-seen times are when the filter was "
        "satisfied, not when the live screen happened to refresh. Engine picks logged that day are marked "
        "<span class=\"tag pick\">pick</span>." % (session, d.get("stamps") or 0, len(d.get("symbols") or []))
    )
    src_pill = ("Upstox data" if real else
                ("demo generator" if src == "demo" else "stand-in API — simulated"))
    out = (HTML
           .replace("__TITLE__", title or f"Session replay · {session} · F&O screener")
           .replace("__CSS__", CSS)
           .replace("__JS__", JS)
           .replace("__SESSION__", session)
           .replace("__SOURCE_PILL__", src_pill)
           .replace("__STAMPS__", str(d.get("stamps") or 0))
           .replace("__BUILT__", datetime.now(IST).strftime("%d %b %H:%M"))
           .replace("__AXIS__", axis)
           .replace("__NOTICE__", notice)
           .replace("__FOOTER__", footer)
           .replace("__DATA__", data))
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    ap.add_argument("--frames", default=None, help="frames_<session>.json written by replay.py")
    ap.add_argument("--out", default=None, help="where to write the HTML (default: logs/replay/report_<session>.html)")
    ap.add_argument("--title", default=None)
    args = ap.parse_args(argv)

    path = args.frames
    if not path:
        cands = sorted(f for f in os.listdir(REPLAY_DIR) if f.startswith("frames_") and f.endswith(".json")) \
            if os.path.isdir(REPLAY_DIR) else []
        if not cands:
            print("No frames_*.json found — run: python3 replay.py --session <YYYY-MM-DD>")
            return 1
        path = os.path.join(REPLAY_DIR, cands[-1])
    if not os.path.exists(path):
        print(f"{path} not found")
        return 1

    d = load_frames(path)
    out = args.out or os.path.join(REPLAY_DIR, f"report_{d.get('session') or 'session'}.html")
    html = render(d, args.title)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"report: {out}  ({len(html) // 1024} KB, {len(d.get('symbols') or [])} symbols, "
          f"{d.get('stamps')} minutes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
