"""Self-contained HTML for the Package Designer UI."""

from __future__ import annotations

import json
import re

from ros2inspector.design.models import known_interfaces_json

_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ROS2 Inspector — Package Designer</title>
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Segoe UI',system-ui,sans-serif;background:#0a0a18;color:#c9d1d9;
  display:flex;flex-direction:column;height:100vh;overflow:hidden;user-select:none}

/* toolbar */
#tb{height:52px;background:#0e0e1c;border-bottom:1px solid rgba(139,92,246,.22);
  display:flex;align-items:center;padding:0 16px;gap:12px;flex-shrink:0}
.brand{font-weight:700;font-size:14px;color:#a78bfa;white-space:nowrap}
.sep{width:1px;height:20px;background:rgba(255,255,255,.08);flex-shrink:0}
.tb-lbl{color:#475569;font-size:12px;white-space:nowrap}
.tb-inp{background:rgba(255,255,255,.04);border:1px solid rgba(255,255,255,.09);
  border-radius:6px;color:#e2e8f0;padding:5px 9px;
  font-family:monospace;font-size:12px;outline:none;width:160px}
.tb-inp:focus{border-color:rgba(139,92,246,.5)}
.btn{background:rgba(139,92,246,.12);border:1px solid rgba(139,92,246,.28);
  border-radius:8px;color:#a78bfa;padding:6px 14px;font-size:12px;
  font-weight:600;cursor:pointer;white-space:nowrap;transition:background .15s}
.btn:hover{background:rgba(139,92,246,.24)}
.btn:disabled{opacity:.4;cursor:not-allowed}
.btn-save{background:rgba(52,211,153,.1);border:1px solid rgba(52,211,153,.28);
  border-radius:8px;color:#34d399;padding:6px 14px;font-size:12px;
  font-weight:600;cursor:pointer;white-space:nowrap;transition:background .15s}
.btn-save:hover{background:rgba(52,211,153,.2)}
.btn-save:disabled{opacity:.4;cursor:not-allowed}
.btn-p{background:linear-gradient(135deg,#8b5cf6,#6d28d9);border:none;
  border-radius:8px;color:#fff;padding:6px 14px;font-size:12px;
  font-weight:600;cursor:pointer;white-space:nowrap;transition:opacity .15s}
.btn-p:hover{opacity:.88}
.btn-p:disabled{opacity:.4;cursor:not-allowed}
.tb-hint{color:#1e2d40;font-size:11px;margin-left:auto;font-style:italic;white-space:nowrap}

/* package tabs bar */
#pt{height:40px;background:#0b0b19;border-bottom:1px solid rgba(139,92,246,.15);
  display:flex;align-items:flex-end;padding:0 12px;gap:2px;flex-shrink:0;overflow-x:auto}
#pt::-webkit-scrollbar{height:3px}
#pt::-webkit-scrollbar-track{background:transparent}
#pt::-webkit-scrollbar-thumb{background:rgba(139,92,246,.3);border-radius:2px}
.ptab{height:32px;display:flex;align-items:center;gap:6px;
  padding:0 10px 0 12px;
  background:rgba(255,255,255,.03);
  border:1px solid rgba(255,255,255,.07);border-bottom:none;
  border-radius:6px 6px 0 0;
  font-size:12px;font-family:monospace;color:#475569;
  cursor:pointer;white-space:nowrap;transition:background .15s,color .15s}
.ptab.active{background:#12121e;border-color:rgba(139,92,246,.4);color:#e2e8f0}
.ptab:hover:not(.active){background:rgba(255,255,255,.06);color:#94a3b8}
.ptab-del{background:none;border:none;color:#2d3748;cursor:pointer;
  font-size:14px;padding:1px 3px;line-height:1;border-radius:3px;
  transition:color .15s;flex-shrink:0}
.ptab-del:hover{color:#f87171}
.ptab-add{height:26px;align-self:center;margin-left:4px;
  background:rgba(139,92,246,.08);border:1px solid rgba(139,92,246,.18);
  border-radius:6px;color:#8b5cf6;padding:0 10px;font-size:11px;
  font-weight:600;cursor:pointer;white-space:nowrap;flex-shrink:0}
.ptab-add:hover{background:rgba(139,92,246,.16)}

/* layout */
#main{display:flex;flex:1;overflow:hidden}

/* canvas */
#cv{flex:1;position:relative;overflow:hidden;
  background-color:#0a0a18;
  background-image:radial-gradient(rgba(139,92,246,.12) 1px,transparent 1px);
  background-size:28px 28px;cursor:default}
#svo{position:absolute;top:0;left:0;width:100%;height:100%;
  pointer-events:none;overflow:visible}

/* nodes */
.node{position:absolute;width:280px;background:#12121e;
  border:1px solid rgba(139,92,246,.35);border-radius:12px;overflow:hidden;
  box-shadow:0 4px 24px rgba(0,0,0,.4);transition:border-color .2s,box-shadow .2s}
.node.sel{border-color:#8b5cf6;
  box-shadow:0 0 0 2px rgba(139,92,246,.3),0 8px 32px rgba(0,0,0,.5)}
.nh{height:44px;display:flex;align-items:center;padding:0 12px;gap:8px;
  background:linear-gradient(135deg,rgba(139,92,246,.18),rgba(34,211,238,.08));
  border-bottom:1px solid rgba(139,92,246,.12);cursor:move}
.ndot{width:8px;height:8px;border-radius:50%;
  background:linear-gradient(135deg,#8b5cf6,#22d3ee);flex-shrink:0}
.nlbl{font-weight:700;font-size:13px;color:#e2e8f0;flex:1;
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-family:monospace}
.ndel{background:none;border:none;color:#475569;cursor:pointer;
  font-size:16px;padding:2px 4px;line-height:1;border-radius:4px;flex-shrink:0;
  transition:color .15s}
.ndel:hover{color:#f87171}
.nch{display:grid;grid-template-columns:1fr 1fr;
  border-bottom:1px solid rgba(255,255,255,.04);padding:3px 0}
.clbl{font-size:9px;color:#2d3748;text-transform:uppercase;letter-spacing:.08em;padding:0 10px}
.clbl.r{text-align:right}
.ncols{display:grid;grid-template-columns:1fr 1fr}
.sc{border-right:1px solid rgba(255,255,255,.04)}
.tr{height:40px;display:flex;align-items:center;padding:0 10px}
.tr.sub{padding-left:18px}
.tr.pub{padding-right:18px;flex-direction:row-reverse}
.ti{display:flex;flex-direction:column;gap:1px}
.tn{font-size:12px;color:#e2e8f0;font-family:monospace;line-height:1.2}
.tt{font-size:10px;color:#475569;line-height:1.2}
.tr.pub .ti{text-align:right}
.ec{height:40px;display:flex;align-items:center;padding:0 10px;
  color:#1a2535;font-size:11px;font-style:italic}
.ec.r{justify-content:flex-end}

/* sidebar */
#sb{width:300px;flex-shrink:0;background:#0e0e1c;
  border-left:1px solid rgba(139,92,246,.18);
  display:flex;flex-direction:column;overflow-y:auto;
  padding:20px 16px;gap:20px;transition:width .2s,padding .2s}
#sb.h{width:0;padding:0;overflow:hidden;border:none}
.stit{font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.08em;color:#8b5cf6}
.slbl{display:block;font-size:10px;font-weight:700;text-transform:uppercase;
  letter-spacing:.08em;color:#475569;margin-bottom:6px}
.sinp{background:rgba(255,255,255,.04);border:1px solid rgba(255,255,255,.09);
  border-radius:6px;color:#e2e8f0;padding:6px 10px;
  font-family:monospace;font-size:12px;outline:none;width:100%}
.sinp:focus{border-color:rgba(139,92,246,.5)}
.srow{display:flex;align-items:center;justify-content:space-between;margin-bottom:8px}
.addb{background:rgba(139,92,246,.1);border:1px solid rgba(139,92,246,.22);
  border-radius:6px;color:#a78bfa;padding:3px 8px;font-size:11px;font-weight:600;cursor:pointer}
.tc{background:rgba(255,255,255,.02);border:1px solid rgba(255,255,255,.06);
  border-radius:8px;padding:10px;margin-bottom:8px}
.tcr{display:flex;align-items:center;gap:6px;margin-bottom:6px}
.tdot{width:6px;height:6px;border-radius:50%;flex-shrink:0}
.trem{background:none;border:none;color:#475569;cursor:pointer;
  font-size:14px;padding:2px 4px;line-height:1;transition:color .15s}
.trem:hover{color:#f87171}
.ssel{background:#0a0a18;border:1px solid rgba(255,255,255,.09);
  border-radius:6px;color:#e2e8f0;padding:4px 8px;
  font-family:monospace;font-size:11px;width:100%;outline:none;cursor:pointer}
.shnote{margin-top:auto;padding:10px 12px;
  background:rgba(139,92,246,.06);border-radius:8px;
  border:1px solid rgba(139,92,246,.1);
  font-size:11px;color:#475569;line-height:1.6}

/* empty state */
#es{position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);
  text-align:center;color:#1a2535;pointer-events:none}
#es.h{display:none}
#es .big{font-size:48px;margin-bottom:10px}
#es p{font-size:13px}

/* toast */
#toast{position:fixed;bottom:24px;right:24px;
  background:#12121e;border:1px solid rgba(139,92,246,.3);border-radius:12px;
  padding:14px 18px;font-size:13px;box-shadow:0 8px 32px rgba(0,0,0,.5);
  max-width:420px;line-height:1.5;z-index:9999;
  opacity:0;transform:translateY(16px);
  transition:opacity .25s ease,transform .25s ease;pointer-events:none}
#toast.show{opacity:1;transform:translateY(0)}
#toast.ok{border-color:rgba(52,211,153,.4);color:#34d399}
#toast.err{border-color:rgba(248,113,113,.4);color:#f87171}
</style>
</head>
<body>

<div id="tb">
  <span class="brand">🤖 Package Designer</span>
  <div class="sep"></div>
  <span class="tb-lbl">pkg:</span>
  <input class="tb-inp" id="pn" value="my_package" spellcheck="false">
  <div class="sep"></div>
  <button class="btn" id="an">+ Add Node</button>
  <button class="btn-save" id="sv">💾 Save</button>
  <button class="btn-p" id="gb" disabled>↓ Generate All</button>
  <span class="tb-hint">Click node to configure · Drag cyan handles to connect · Ctrl+S to save</span>
</div>

<div id="pt"></div>

<div id="main">
  <div id="cv">
    <svg id="svo" xmlns="http://www.w3.org/2000/svg">
      <defs>
        <marker id="arr" markerWidth="8" markerHeight="8" refX="7" refY="3" orient="auto">
          <path d="M0,0 L0,6 L8,3 z" fill="#22d3ee"/>
        </marker>
      </defs>
      <g id="eg"></g>
      <g id="tg"></g>
      <g id="hg"></g>
    </svg>
    <div id="es">
      <div class="big">⬡</div>
      <p>Click <strong style="color:#a78bfa">+ Add Node</strong> to start designing</p>
    </div>
  </div>

  <div id="sb" class="h"></div>
</div>

<div id="toast"></div>
<datalist id="dl-msg"></datalist>
<datalist id="dl-srv"></datalist>
<datalist id="dl-action"></datalist>

<script>
// ── constants ──────────────────────────────────────────────────────────────
const NW=280, NH=44, CH=26, RH=40;

// Suggested interfaces; any other pkg/Type can be typed into the field.
const IFACES=__INTERFACES_SLOT__;
const MSGS=IFACES.msg, SRVS=IFACES.srv, ACTIONS=IFACES.action;

// ── state ──────────────────────────────────────────────────────────────────
// packages: [{id, pkg_name, nodes, edges}]
const S={packages:[],activePkg:null,sel:null,nid:1,tid:1,pid:1};
const SESSION_TOKEN=__SESSION_TOKEN_SLOT__;

function pkg(){ return S.packages.find(p=>p.id===S.activePkg)||S.packages[0]||null; }

// ── dom ────────────────────────────────────────────────────────────────────
const CV  = document.getElementById('cv');
const SVO = document.getElementById('svo');
const EG  = document.getElementById('eg');
const TG  = document.getElementById('tg');
const HG  = document.getElementById('hg');
const SB  = document.getElementById('sb');
const ES  = document.getElementById('es');
const GB  = document.getElementById('gb');
const PT  = document.getElementById('pt');
const PN  = document.getElementById('pn');
const TOAST = document.getElementById('toast');

// ── geometry ───────────────────────────────────────────────────────────────
function hp(node,side,i){
  return {
    x: side==='pub' ? node.x+NW : node.x,
    y: node.y + NH + CH + i*RH + RH/2,
  };
}

function bez(sx,sy,tx,ty){
  const cp=Math.max(80,Math.abs(tx-sx)*.5);
  return `M${sx},${sy} C${sx+cp},${sy} ${tx-cp},${ty} ${tx},${ty}`;
}

function esc(s){
  return String(s)
    .replace(/&/g,'&amp;').replace(/</g,'&lt;')
    .replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;');
}
// A value embedded as a JS string literal inside an HTML event-handler attribute.
function jsa(v){ return esc(JSON.stringify(String(v))); }

function edgeEnds(p,e){
  const fn=p.nodes.find(n=>n.id===e.fn), tn=p.nodes.find(n=>n.id===e.tn);
  if(!fn||!tn) return null;
  const pub=fn.publishers.find(x=>x.id===e.ft), sub=tn.subscribers.find(x=>x.id===e.tt);
  if(!pub||!sub) return null;
  return {fn,tn,pub,sub,ok:pub.name===sub.name&&pub.msgType===sub.msgType};
}

// Drop wires whose endpoints were deleted; return wires that no longer match.
function badEdges(p){
  p.edges=p.edges.filter(e=>edgeEnds(p,e));
  return p.edges.filter(e=>!edgeEnds(p,e).ok);
}

// ── package tabs ──────────────────────────────────────────────────────────
function renderTabs(){
  PT.innerHTML='';
  for(const p of S.packages){
    const tab=document.createElement('div');
    tab.className='ptab'+(p.id===S.activePkg?' active':'');
    tab.innerHTML=`<span>${esc(p.pkg_name)}</span>`
      +`<button class="ptab-del" title="Delete package">×</button>`;
    tab.addEventListener('click', e=>{
      if(e.target.classList.contains('ptab-del')) return;
      switchPkg(p.id);
    });
    tab.querySelector('.ptab-del').addEventListener('click', e=>{
      e.stopPropagation(); delPkg(p.id);
    });
    PT.appendChild(tab);
  }
  const addBtn=document.createElement('button');
  addBtn.className='ptab-add';
  addBtn.textContent='+ Package';
  addBtn.addEventListener('click', addPkg);
  PT.appendChild(addBtn);
}

// ── render ─────────────────────────────────────────────────────────────────
function renderAll(){
  renderEdges();
  renderHandles();
  renderNodes();
  const p=pkg();
  ES.classList.toggle('h', !!(p&&p.nodes.length>0));
  GB.disabled = !S.packages.some(p=>p.nodes.length>0);
  renderTabs();
}

function renderEdges(){
  EG.innerHTML='';
  const p=pkg(); if(!p) return;
  for(const e of p.edges){
    const fn=p.nodes.find(n=>n.id===e.fn);
    const tn=p.nodes.find(n=>n.id===e.tn);
    if(!fn||!tn) continue;
    const fi=fn.publishers.findIndex(x=>x.id===e.ft);
    const ti=tn.subscribers.findIndex(x=>x.id===e.tt);
    if(fi<0||ti<0) continue;
    const sp=hp(fn,'pub',fi), tp2=hp(tn,'sub',ti);
    const ends=edgeEnds(p,e);
    const path=makeSVG('path');
    path.setAttribute('d',bez(sp.x,sp.y,tp2.x,tp2.y));
    path.setAttribute('stroke',ends&&ends.ok?'#22d3ee':'#f87171');
    if(ends&&!ends.ok) path.setAttribute('stroke-dasharray','6 4');
    path.setAttribute('stroke-width','2');
    path.setAttribute('fill','none');
    path.setAttribute('marker-end','url(#arr)');
    path.setAttribute('opacity','.75');
    path.style.pointerEvents='stroke';
    path.style.cursor='pointer';
    const tip=makeSVG('title');
    tip.textContent=ends&&!ends.ok
      ? `${fn.label} → ${tn.label}: topic name or type no longer match (click to remove)`
      : `${fn.label} → ${tn.label} (click to remove)`;
    path.appendChild(tip);
    path.addEventListener('click',()=>{ p.edges=p.edges.filter(x=>x.id!==e.id); renderAll(); });
    EG.appendChild(path);
  }
}

function renderHandles(){
  HG.innerHTML='';
  const p=pkg(); if(!p) return;
  for(const node of p.nodes){
    node.publishers.forEach((pub,i)=>{
      const {x,y}=hp(node,'pub',i);
      const c=makeHandle(x,y,'#22d3ee','#67e8f9');
      c.dataset.nid=node.id; c.dataset.tid=pub.id; c.dataset.side='pub';
      c.style.cursor='crosshair';
      c.addEventListener('mousedown',onHDown);
      HG.appendChild(c);
    });
    node.subscribers.forEach((sub,i)=>{
      const {x,y}=hp(node,'sub',i);
      const c=makeHandle(x,y,'#8b5cf6','#a78bfa');
      c.dataset.nid=node.id; c.dataset.tid=sub.id; c.dataset.side='sub';
      HG.appendChild(c);
    });
  }
}

function makeHandle(x,y,fill,stroke){
  const c=makeSVG('circle');
  c.setAttribute('cx',x); c.setAttribute('cy',y);
  c.setAttribute('r','6');
  c.setAttribute('fill',fill);
  c.setAttribute('stroke',stroke);
  c.setAttribute('stroke-width','1.5');
  c.style.pointerEvents='all';
  return c;
}

function makeSVG(tag){
  return document.createElementNS('http://www.w3.org/2000/svg',tag);
}

function renderNodes(){
  const p=pkg();
  const validIds=new Set((p?p.nodes:[]).map(n=>'nd'+n.id));
  CV.querySelectorAll('.node').forEach(el=>{ if(!validIds.has(el.id)) el.remove(); });
  if(!p) return;
  for(const node of p.nodes){
    let el=document.getElementById('nd'+node.id);
    if(!el){
      el=document.createElement('div');
      el.className='node'; el.id='nd'+node.id;
      CV.appendChild(el);
    }
    el.style.left=node.x+'px'; el.style.top=node.y+'px';
    el.classList.toggle('sel',node.id===S.sel);
    el.innerHTML=nodeHTML(node);
    el.querySelector('.nh').addEventListener('mousedown',e=>onNDown(e,node.id));
    el.querySelector('.ndel').addEventListener('click',e=>{ e.stopPropagation(); delNode(node.id); });
    el.addEventListener('click',()=>selNode(node.id));
  }
}

function nodeHTML(nd){
  const subs=nd.subscribers.map(s=>`
    <div class="tr sub"><div class="ti">
      <div class="tn">${esc(s.name)}</div>
      <div class="tt">${esc(s.msgType)}</div>
    </div></div>`).join('')||'<div class="ec">—</div>';
  const pubs=nd.publishers.map(p2=>`
    <div class="tr pub"><div class="ti">
      <div class="tn">${esc(p2.name)}</div>
      <div class="tt">${esc(p2.msgType)}</div>
    </div></div>`).join('')||'<div class="ec r">—</div>';
  const serviceCount=(nd.services||[]).length;
  const clientCount=(nd.clients||[]).length;
  const actionServerCount=(nd.action_servers||[]).length;
  const actionClientCount=(nd.action_clients||[]).length;
  return `
    <div class="nh">
      <span class="ndot"></span>
      <span class="nlbl">${esc(nd.label)}</span>
      <button class="ndel" title="Delete node">×</button>
    </div>
    <div class="nch">
      <span class="clbl">Subscribers</span>
      <span class="clbl r">Publishers</span>
    </div>
    <div class="ncols">
      <div class="sc">${subs}</div>
      <div>${pubs}</div>
    </div>
    <div style="padding:7px 10px;border-top:1px solid #1e293b;font-size:10px;color:#64748b">
      Services ${serviceCount}/${clientCount} provider/client · Actions ${actionServerCount}/${actionClientCount} server/client
    </div>`;
}

// ── sidebar ────────────────────────────────────────────────────────────────
function renderSB(){
  if(!S.sel){ SB.classList.add('h'); return; }
  SB.classList.remove('h');
  const p=pkg(); if(!p) return;
  const nd=p.nodes.find(n=>n.id===S.sel);
  if(!nd) return;
  SB.innerHTML=`
    <div style="display:flex;align-items:center;justify-content:space-between">
      <span class="stit">Node Config</span>
      <button onclick="closeSB()" style="background:none;border:none;color:#64748b;cursor:pointer;font-size:18px;line-height:1">×</button>
    </div>

    <div>
      <label class="slbl">Node Name</label>
      <input class="sinp" id="snm" value="${esc(nd.label)}" spellcheck="false">
    </div>

    <div>
      <div class="srow">
        <label class="slbl" style="margin:0">
          <span style="display:inline-block;width:7px;height:7px;border-radius:50%;background:#22d3ee;margin-right:5px;vertical-align:middle"></span>
          Publishers
        </label>
        <button class="addb" onclick="addT('pub')">+ Add</button>
      </div>
      <div id="spubs">${tHTML(nd.publishers,'pub')}</div>
    </div>

    <div>
      <div class="srow">
        <label class="slbl" style="margin:0">
          <span style="display:inline-block;width:7px;height:7px;border-radius:50%;background:#a78bfa;margin-right:5px;vertical-align:middle"></span>
          Subscribers
        </label>
        <button class="addb" onclick="addT('sub')">+ Add</button>
      </div>
      <div id="ssubs">${tHTML(nd.subscribers,'sub')}</div>
    </div>

    ${endpointSection('services','Service Providers',nd.services||[],SRVS,'#34d399')}
    ${endpointSection('clients','Service Clients',nd.clients||[],SRVS,'#10b981')}
    ${endpointSection('action_servers','Action Servers',nd.action_servers||[],ACTIONS,'#f472b6')}
    ${endpointSection('action_clients','Action Clients',nd.action_clients||[],ACTIONS,'#ec4899')}

    <div class="shnote">
      Drag from a <span style="color:#22d3ee">cyan handle ●</span> (publisher) to a
      <span style="color:#a78bfa">violet handle ●</span> (subscriber) to wire topics.
      Click an edge to remove it.
    </div>`;

  document.getElementById('snm').addEventListener('input',e=>{
    const n=pkg()&&pkg().nodes.find(x=>x.id===S.sel);
    if(n){ n.label=e.target.value; renderAll(); }
  });
}

function tHTML(topics,side){
  const col=side==='pub'?'#22d3ee':'#a78bfa';
  if(!topics.length) return '<p style="color:#1e293b;font-size:12px;font-style:italic">None yet</p>';
  return topics.map(t=>`
    <div class="tc">
      <div class="tcr">
        <span class="tdot" style="background:${col}"></span>
        <input class="sinp" style="flex:1;padding:4px 8px;font-size:11px"
          value="${esc(t.name)}" spellcheck="false"
          onchange="updT(${jsa(side)},${jsa(t.id)},'name',this.value)" placeholder="/topic">
        <button class="trem" onclick="remT(${jsa(side)},${jsa(t.id)})">×</button>
      </div>
      <input class="ssel" list="dl-msg" value="${esc(t.msgType)}" spellcheck="false"
        placeholder="pkg/Type" title="Pick a type or type any pkg/Type"
        onchange="updT(${jsa(side)},${jsa(t.id)},'msgType',this.value)">
    </div>`).join('');
}

function endpointSection(kind,label,endpoints,typeList,color){
  return `<div>
    <div class="srow">
      <label class="slbl" style="margin:0">
        <span style="display:inline-block;width:7px;height:7px;border-radius:50%;background:${color};margin-right:5px;vertical-align:middle"></span>
        ${label}
      </label>
      <button class="addb" onclick="addEndpoint(${jsa(kind)})">+ Add</button>
    </div>
    <div>${endpointHTML(kind,endpoints,typeList,color)}</div>
  </div>`;
}

function endpointHTML(kind,endpoints,typeList,color){
  if(!endpoints.length) return '<p style="color:#1e293b;font-size:12px;font-style:italic">None yet</p>';
  return endpoints.map(t=>`
    <div class="tc">
      <div class="tcr">
        <span class="tdot" style="background:${color}"></span>
        <input class="sinp" style="flex:1;padding:4px 8px;font-size:11px"
          value="${esc(t.name)}" spellcheck="false"
          onchange="updEndpoint(${jsa(kind)},${jsa(t.id)},'name',this.value)" placeholder="/name">
        <button class="trem" onclick="remEndpoint(${jsa(kind)},${jsa(t.id)})">×</button>
      </div>
      <input class="ssel" list="${typeList===ACTIONS?'dl-action':'dl-srv'}" value="${esc(t.msgType)}"
        spellcheck="false" placeholder="pkg/Type" title="Pick a type or type any pkg/Type"
        onchange="updEndpoint(${jsa(kind)},${jsa(t.id)},'msgType',this.value)">
    </div>`).join('');
}

// ── package actions ────────────────────────────────────────────────────────
function addPkg(){
  let n=S.pid++;
  while(S.packages.some(p=>p.id==='p'+n||p.pkg_name==='package_'+n)) n=S.pid++;
  const id='p'+n;
  S.packages.push({id, pkg_name:'package_'+n, nodes:[], edges:[]});
  switchPkg(id);
}

function delPkg(id){
  if(S.packages.length<=1){ showToast('Cannot delete the last package.','err'); return; }
  S.packages=S.packages.filter(p=>p.id!==id);
  if(S.activePkg===id){
    S.activePkg=S.packages[0].id;
    S.sel=null;
    SB.classList.add('h');
    CV.querySelectorAll('.node').forEach(el=>el.remove());
    PN.value=S.packages[0].pkg_name;
  }
  renderAll();
}

function switchPkg(id){
  if(S.activePkg===id) return;
  S.activePkg=id;
  S.sel=null;
  SB.classList.add('h');
  CV.querySelectorAll('.node').forEach(el=>el.remove());
  const p=pkg();
  if(p) PN.value=p.pkg_name;
  renderAll();
}

// ── node actions ──────────────────────────────────────────────────────────
function selNode(id){ S.sel=id; renderAll(); renderSB(); }
function closeSB(){ S.sel=null; SB.classList.add('h'); renderAll(); }

function delNode(id){
  const p=pkg(); if(!p) return;
  p.nodes=p.nodes.filter(n=>n.id!==id);
  p.edges=p.edges.filter(e=>e.fn!==id&&e.tn!==id);
  if(S.sel===id){ S.sel=null; SB.classList.add('h'); }
  renderAll();
}

function addNode(){
  const p=pkg(); if(!p) return;
  const id=String(S.nid++);
  const off=p.nodes.length;
  p.nodes.push({id, label:'node_'+id,
    x:60+(off%3)*320, y:60+Math.floor(off/3)*200,
    publishers:[], subscribers:[], services:[], clients:[], action_servers:[], action_clients:[]});
  selNode(id); renderAll(); renderSB();
}

function addT(side){
  const p=pkg(); if(!p) return;
  const nd=p.nodes.find(n=>n.id===S.sel); if(!nd) return;
  const t={id:String(S.tid++), name:'/topic', msgType:'std_msgs/String'};
  if(side==='pub') nd.publishers.push(t); else nd.subscribers.push(t);
  renderAll(); renderSB();
}

function remT(side,tid){
  const p=pkg(); if(!p) return;
  const nd=p.nodes.find(n=>n.id===S.sel); if(!nd) return;
  if(side==='pub'){
    nd.publishers=nd.publishers.filter(x=>x.id!==tid);
    p.edges=p.edges.filter(e=>!(e.fn===nd.id&&e.ft===tid));
  } else {
    nd.subscribers=nd.subscribers.filter(s=>s.id!==tid);
    p.edges=p.edges.filter(e=>!(e.tn===nd.id&&e.tt===tid));
  }
  renderAll(); renderSB();
}

function updT(side,tid,field,value){
  const p=pkg(); if(!p) return;
  const nd=p.nodes.find(n=>n.id===S.sel); if(!nd) return;
  const arr=side==='pub'?nd.publishers:nd.subscribers;
  const t=arr.find(x=>x.id===tid);
  if(t){ t[field]=value; renderAll(); }
}

function addEndpoint(kind){
  const p=pkg(); if(!p) return;
  const nd=p.nodes.find(n=>n.id===S.sel); if(!nd) return;
  const isAction=kind.startsWith('action_');
  const defaults=isAction?ACTIONS:SRVS;
  const name=isAction?'/action':'/service';
  nd[kind]=nd[kind]||[];
  nd[kind].push({id:String(S.tid++),name,msgType:defaults[0]});
  renderAll(); renderSB();
}

function remEndpoint(kind,id){
  const p=pkg(); if(!p) return;
  const nd=p.nodes.find(n=>n.id===S.sel); if(!nd) return;
  nd[kind]=(nd[kind]||[]).filter(x=>x.id!==id);
  renderAll(); renderSB();
}

function updEndpoint(kind,id,field,value){
  const p=pkg(); if(!p) return;
  const nd=p.nodes.find(n=>n.id===S.sel); if(!nd) return;
  const endpoint=(nd[kind]||[]).find(x=>x.id===id);
  if(endpoint){ endpoint[field]=value; renderAll(); }
}

// ── node drag ─────────────────────────────────────────────────────────────
let dNode=null, dOX=0, dOY=0;

function onNDown(e,nid){
  if(e.target.classList.contains('ndel')) return;
  e.preventDefault();
  dNode=nid;
  const p=pkg();
  const nd=p&&p.nodes.find(n=>n.id===nid); if(!nd) return;
  const cr=CV.getBoundingClientRect();
  dOX=e.clientX-cr.left-nd.x;
  dOY=e.clientY-cr.top-nd.y;
}

// ── edge drag ─────────────────────────────────────────────────────────────
let eDrag=null;

function onHDown(e){
  if(e.target.dataset.side!=='pub') return;
  e.stopPropagation(); e.preventDefault();
  const nid=e.target.dataset.nid, tid=e.target.dataset.tid;
  const p=pkg();
  const nd=p&&p.nodes.find(n=>n.id===nid); if(!nd) return;
  const idx=nd.publishers.findIndex(x=>x.id===tid);
  const pos=hp(nd,'pub',idx);
  eDrag={fn:nid, ft:tid, sx:pos.x, sy:pos.y};
}

// ── global mouse handlers ─────────────────────────────────────────────────
document.addEventListener('mousemove',e=>{
  if(dNode){
    const cr=CV.getBoundingClientRect();
    const p=pkg();
    const nd=p&&p.nodes.find(n=>n.id===dNode);
    if(nd){ nd.x=e.clientX-cr.left-dOX; nd.y=e.clientY-cr.top-dOY; renderAll(); }
  }
  if(eDrag){
    const cr=CV.getBoundingClientRect();
    const tx=e.clientX-cr.left, ty=e.clientY-cr.top;
    TG.innerHTML=`<path d="${bez(eDrag.sx,eDrag.sy,tx,ty)}"
      stroke="#22d3ee" stroke-width="1.5" fill="none"
      stroke-dasharray="6 4" opacity=".6"/>`;
  }
});

document.addEventListener('mouseup',e=>{
  dNode=null;
  if(eDrag){
    const cr=CV.getBoundingClientRect();
    const mx=e.clientX-cr.left, my=e.clientY-cr.top;
    const p=pkg();
    outer: for(const nd of (p?p.nodes:[])){
      if(nd.id===eDrag.fn) continue;
      for(let i=0;i<nd.subscribers.length;i++){
        const pos=hp(nd,'sub',i);
        if(Math.hypot(mx-pos.x,my-pos.y)<14){
          const sub=nd.subscribers[i];
          const src=p.nodes.find(n=>n.id===eDrag.fn);
          const pub=src&&src.publishers.find(x=>x.id===eDrag.ft);
          if(pub&&pub.msgType!==sub.msgType){
            showToast(`✗ Type mismatch: ${pub.msgType} → ${sub.msgType}. A wire needs the same type on both ends.`,'err');
            break outer;
          }
          if(pub&&pub.name!==sub.name){
            showToast(`Subscriber renamed ${sub.name} → ${pub.name} so the wire is a real connection.`,'ok');
            sub.name=pub.name;
          }
          const dup=p.edges.some(x=>x.fn===eDrag.fn&&x.ft===eDrag.ft&&x.tn===nd.id&&x.tt===sub.id);
          if(!dup) p.edges.push({id:'e'+Date.now(),fn:eDrag.fn,ft:eDrag.ft,tn:nd.id,tt:sub.id});
          renderAll(); if(S.sel) renderSB();
          break outer;
        }
      }
    }
    TG.innerHTML='';
    eDrag=null;
  }
});

CV.addEventListener('click',e=>{
  if(e.target===CV||e.target===SVO) closeSB();
});

// ── pkg name input ────────────────────────────────────────────────────────
PN.addEventListener('input',e=>{
  const p=pkg(); if(p){ p.pkg_name=e.target.value; renderTabs(); }
});

// ── toast ─────────────────────────────────────────────────────────────────
function showToast(msg,type){
  TOAST.textContent=msg;
  TOAST.className='show '+(type||'ok');
  clearTimeout(showToast._t);
  showToast._t=setTimeout(()=>{ TOAST.className=''; },5000);
}

// ── save ──────────────────────────────────────────────────────────────────
async function saveState(){
  const state={version:1, nid:S.nid, tid:S.tid, pid:S.pid, activePkg:S.activePkg, packages:S.packages};
  const sv=document.getElementById('sv');
  sv.disabled=true; sv.textContent='⏳ Saving…';
  try{
    const res=await fetch('/save',{
      method:'POST',
      headers:{'Content-Type':'application/json','X-ROS2Inspector-Token':SESSION_TOKEN},
      body:JSON.stringify(state),
    });
    if(res.ok) showToast('💾 Design saved.','ok');
    else {
      const data=await res.json();
      const detail=(data.fields||[]).map(x=>`${x.path}: ${x.message}`).join('; ');
      showToast(`✗ ${detail||data.error||'Save failed.'}`,'err');
    }
  }catch(err){
    showToast(`✗ ${err.message}`,'err');
  }finally{
    sv.disabled=false; sv.textContent='💾 Save';
  }
}

// ── generate ──────────────────────────────────────────────────────────────
GB.addEventListener('click',async()=>{
  // send full state; server skips empty packages when writing files
  const bad=S.packages.flatMap(p=>badEdges(p).map(e=>p.pkg_name));
  if(bad.length){
    renderAll();
    showToast(`✗ ${bad.length} wire(s) in ${[...new Set(bad)].join(', ')} no longer match (shown in red). Fix or remove them first.`,'err');
    return;
  }
  const payload={version:1, nid:S.nid, tid:S.tid, pid:S.pid, activePkg:S.activePkg, packages:S.packages};
  GB.disabled=true; GB.textContent='⏳ Generating…';
  try{
    const res=await fetch('/generate',{
      method:'POST',
      headers:{'Content-Type':'application/json','X-ROS2Inspector-Token':SESSION_TOKEN},
      body:JSON.stringify(payload),
    });
    const data=await res.json();
    if(res.ok){
      const kept=data.kept_modified||[];
      const written=(data.packages||[]).reduce((n,r)=>n+r.written.length,0);
      showToast(kept.length
        ? `✓ ${written} file(s) written → ${data.output_dir}. Kept your edits in ${kept.length} file(s): ${kept.join(', ')}`
        : `✓ ${written} file(s) written, ${data.files.length-written} unchanged → ${data.output_dir}`,'ok');
    } else {
      const detail=(data.fields||[]).map(x=>`${x.path}: ${x.message}`).join('; ');
      showToast(`✗ ${detail||data.error}`,'err');
    }
  }catch(err){
    showToast(`✗ ${err.message}`,'err');
  }finally{
    GB.disabled=false; GB.textContent='↓ Generate All';
    renderAll();
  }
});

document.getElementById('an').addEventListener('click', addNode);
document.getElementById('sv').addEventListener('click', saveState);
document.addEventListener('keydown', e=>{
  if((e.ctrlKey||e.metaKey)&&e.key==='s'){ e.preventDefault(); saveState(); }
});

// ── init ──────────────────────────────────────────────────────────────────
for(const [id,list] of [['dl-msg',MSGS],['dl-srv',SRVS],['dl-action',ACTIONS]]){
  const dl=document.getElementById(id);
  for(const name of list){ const o=document.createElement('option'); o.value=name; dl.appendChild(o); }
}
(function(){
  const init=__INITIAL_STATE_SLOT__;
  if(init&&Array.isArray(init.packages)&&init.packages.length>0){
    S.packages=init.packages;
    for(const p of S.packages){
      p.edges=p.edges||[];
      for(const n of (p.nodes||[])){
        n.publishers=n.publishers||[];
        n.subscribers=n.subscribers||[];
        n.services=n.services||[];
        n.clients=n.clients||[];
        n.action_servers=n.action_servers||[];
        n.action_clients=n.action_clients||[];
      }
    }
    S.nid=init.nid||1;
    S.tid=init.tid||1;
    S.pid=init.pid||1;
    const saved=S.packages.find(p=>p.id===init.activePkg);
    S.activePkg=(saved||S.packages[0]).id;
    PN.value=(saved||S.packages[0]).pkg_name||'';
  } else {
    // fresh workspace: seed with one example package
    const p1={id:String(S.tid++),name:'/image_raw',msgType:'sensor_msgs/Image'};
    const s1={id:String(S.tid++),name:'/image_raw',msgType:'sensor_msgs/Image'};
    const p2={id:String(S.tid++),name:'/detections',msgType:'std_msgs/String'};
    const pkgId='p'+String(S.pid++);
    S.packages.push({
      id:pkgId, pkg_name:'my_package',
      nodes:[
        {id:String(S.nid++),label:'camera_node',x:60,y:120,publishers:[p1],subscribers:[],services:[],clients:[],action_servers:[],action_clients:[]},
        {id:String(S.nid++),label:'detector_node',x:440,y:120,publishers:[p2],subscribers:[s1],services:[],clients:[],action_servers:[],action_clients:[]},
      ],
      edges:[{id:'e0',fn:'1',ft:p1.id,tn:'2',tt:s1.id}],
    });
    S.activePkg=pkgId;
    PN.value='my_package';
  }
  renderAll();
})();
</script>
</body>
</html>"""


def _script_json(value: object) -> str:
    """JSON that is safe inside an inline <script> block (no '</' or '<!--')."""
    return json.dumps(value).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


def build_html(
    initial_state: dict[str, object] | None = None,
    *,
    session_token: str = "",
    interfaces: dict[str, object] | None = None,
) -> str:
    state_json = _script_json(initial_state) if initial_state else "null"
    slots = {
        "__INITIAL_STATE_SLOT__": state_json,
        "__SESSION_TOKEN_SLOT__": _script_json(session_token),
        "__INTERFACES_SLOT__": _script_json(interfaces or known_interfaces_json()),
    }
    # Single pass, so user data can never be re-scanned for another slot name.
    pattern = re.compile("|".join(re.escape(key) for key in slots))
    return pattern.sub(lambda match: slots[match.group(0)], _TEMPLATE)
