"""Self-contained HTML for the Package Designer UI."""

from __future__ import annotations

import re

from ros2inspector.design.models import known_interfaces_json
from ros2inspector.utils.html import json_for_script

_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ROS2 Inspector — Package Designer</title>
<meta name="theme-color" content="#050B1F">
<link rel="icon" type="image/svg+xml" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'><g fill='none' stroke-linecap='round' stroke-linejoin='round'><path d='M18 5H10Q5 5 5 10V18M46 5H54Q59 5 59 10V18' stroke='%231E5BFF' stroke-width='4'/><path d='M5 46V54Q5 59 10 59H18M46 59H54Q59 59 59 54V46' stroke='%2336D6FF' stroke-width='4'/><path d='M14 27L31 31L34 14M14 27L20 43L31 31' stroke='%231E5BFF' stroke-width='2.5'/><path d='M31 31L49 27L40 50L31 31' stroke='%2336D6FF' stroke-width='2.5'/></g><circle cx='14' cy='27' r='5' fill='%230A1230' stroke='%231E5BFF' stroke-width='1.5'/><circle cx='20' cy='43' r='4.5' fill='%23050B1F' stroke='%231E5BFF' stroke-width='1.5'/><circle cx='34' cy='14' r='4.5' fill='%231E5BFF'/><circle cx='49' cy='27' r='4' fill='none' stroke='%2336D6FF' stroke-width='3'/><circle cx='40' cy='50' r='5' fill='%2336D6FF'/><circle cx='31' cy='31' r='6' fill='%231E5BFF'/><circle cx='31' cy='31' r='2' fill='%237FE5FF'/></svg>">
<style>
/* ── design tokens ────────────────────────────────────────────────────────────
   The ROS2 Inspector brand palette (website and logo pack). Every colour on the page comes
   from here; the script reads the few it needs for SVG attributes through cssToken(). */
:root{
  color-scheme:dark;
  /* surfaces, darkest to lightest */
  --bg:#050b1f;            /* brand navy: canvas */
  --surface:#0a1230;       /* brand navy-2: toolbar, tabs, inspector */
  --card:#0e1838;          /* node cards, toasts */
  --raised:#14214a;        /* active tab, hovered rows */
  --field:rgba(127,168,255,.05);
  --field-border:rgba(127,168,255,.16);
  --line:rgba(74,134,255,.14);
  --line-strong:rgba(74,134,255,.32);
  --grid:rgba(74,134,255,.13);
  /* text */
  --text:#e2e8f0;
  --text-2:#a9b6cc;
  --muted:#7c8aa0;         /* >= 5:1 on every surface above */
  /* brand */
  --primary:#1e5bff;
  --primary-hover:#3a70ff;
  --primary-text:#7fa8ff;  /* brand blue for text on navy */
  --accent:#36d6ff;
  --accent-soft:#7fe5ff;
  --brand-gradient:linear-gradient(135deg,#1e5bff,#36d6ff);
  /* ROS endpoint roles. Publishers are filled cyan dots, subscribers blue rings, so the two
     ends of a wire differ in shape as well as colour. */
  --pub:#36d6ff;
  --pub-ring:#7fe5ff;
  --sub:#7fa8ff;
  --srv:#34d399;
  --srv-client:#10b981;
  --act:#f472b6;
  --act-client:#ec4899;
  /* status */
  --ok:#34d399;
  --warn:#fbbf24;
  --danger:#f87171;
  --danger-soft:#fecaca;
  /* type and shape */
  --font-ui:system-ui,-apple-system,'Segoe UI',Roboto,'Helvetica Neue',sans-serif;
  --font-mono:ui-monospace,'SF Mono','JetBrains Mono',Menlo,Consolas,monospace;
  --radius-sm:6px;
  --radius:8px;
  --radius-lg:12px;
  --shadow:0 8px 28px rgba(0,0,0,.45);
  --focus:0 0 0 2px var(--bg),0 0 0 4px rgba(54,214,255,.65);
}

*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
body{font-family:var(--font-ui);background:var(--bg);color:var(--text);
  display:flex;flex-direction:column;height:100vh;overflow:hidden;user-select:none;
  -webkit-font-smoothing:antialiased}
button{font-family:inherit}
:focus-visible{outline:none;box-shadow:var(--focus)}
.ic{width:14px;height:14px;flex-shrink:0;fill:none;stroke:currentColor;stroke-width:2;
  stroke-linecap:round;stroke-linejoin:round}

/* toolbar */
#tb{height:52px;background:var(--surface);border-bottom:1px solid var(--line);
  display:flex;align-items:center;padding:0 16px;gap:12px;flex-shrink:0}
.brand{display:flex;align-items:center;gap:9px;white-space:nowrap;flex-shrink:0}
.brand-mark{width:26px;height:26px;flex-shrink:0}
.brand-name{font-weight:700;font-size:14px;letter-spacing:.01em;color:var(--text)}
.brand-name b{font-weight:800;background:var(--brand-gradient);
  -webkit-background-clip:text;background-clip:text;color:transparent}
.brand-tag{font-size:11px;font-weight:600;color:var(--primary-text);
  padding:2px 8px;border:1px solid var(--line-strong);border-radius:999px;
  background:rgba(30,91,255,.1)}
.sep{width:1px;height:20px;background:var(--line);flex-shrink:0}
.tb-lbl{color:var(--muted);font-size:12px;white-space:nowrap}
.tb-inp{background:var(--field);border:1px solid var(--field-border);
  border-radius:var(--radius-sm);color:var(--text);padding:5px 9px;
  font-family:var(--font-mono);font-size:12px;outline:none;width:160px;
  transition:border-color .15s}
.tb-inp:focus{border-color:var(--accent);box-shadow:none}

/* buttons: one shape, three emphasis levels */
.btn,.btn-save,.btn-p{display:inline-flex;align-items:center;gap:6px;
  border-radius:var(--radius);padding:6px 13px;font-size:12px;font-weight:600;
  cursor:pointer;white-space:nowrap;line-height:1.2;
  transition:background .15s,border-color .15s,color .15s}
.btn{background:rgba(30,91,255,.1);border:1px solid var(--line-strong);color:var(--primary-text)}
.btn:hover{background:rgba(30,91,255,.2);border-color:rgba(74,134,255,.5);color:#a9c4ff}
.btn-save{background:rgba(52,211,153,.08);border:1px solid rgba(52,211,153,.3);color:var(--ok)}
.btn-save:hover{background:rgba(52,211,153,.16)}
.btn-p{background:var(--primary);border:1px solid var(--primary);color:#fff;
  box-shadow:0 1px 0 rgba(255,255,255,.12) inset,0 4px 14px rgba(30,91,255,.3)}
.btn-p:hover{background:var(--primary-hover);border-color:var(--primary-hover)}
.btn:disabled,.btn-save:disabled,.btn-p:disabled{opacity:.45;cursor:not-allowed;box-shadow:none}
/* busy: a spinner takes the icon's place while a request runs */
.busy .ic{display:none}
.busy::before{content:'';width:12px;height:12px;flex-shrink:0;border-radius:50%;
  border:2px solid currentColor;border-right-color:transparent;animation:spin .7s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}

.tb-hint{color:var(--muted);font-size:11px;margin-left:auto;white-space:nowrap;
  min-width:0;overflow:hidden;text-overflow:ellipsis}
.tb-hint kbd{font-family:var(--font-mono);font-size:10px;color:var(--text-2);
  padding:1px 5px;border:1px solid var(--field-border);border-radius:4px;background:var(--field)}
/* Medium windows: keep the mark and name, drop the tag. */
@media (max-width:1180px){ .brand-tag{display:none} }
/* Narrow windows: the buttons matter more than the brand and the hint. */
@media (max-width:900px){
  .brand,.brand+.sep,.tb-hint{display:none}
  #tb{gap:8px;padding:0 10px}
  .tb-inp{width:120px}
}
/* Phones: wrap the toolbar onto a second row rather than push buttons off-screen. */
@media (max-width:600px){
  #tb{height:auto;min-height:52px;flex-wrap:wrap;row-gap:6px;padding:6px 10px}
  .tb-lbl,.sep{display:none}
  .tb-inp{flex:1 1 100%;width:auto}
  .btn,.btn-save,.btn-p{padding:6px 9px}
  #sb{width:min(300px,82vw)}
}

/* package tabs bar */
#pt{height:40px;background:var(--surface);border-bottom:1px solid var(--line);
  display:flex;align-items:flex-end;padding:0 12px;gap:2px;flex-shrink:0;overflow-x:auto}
#pt::-webkit-scrollbar{height:3px}
#pt::-webkit-scrollbar-track{background:transparent}
#pt::-webkit-scrollbar-thumb{background:var(--line-strong);border-radius:2px}
.ptab{height:32px;display:flex;align-items:center;gap:6px;padding:0 10px 0 12px;
  background:transparent;border:1px solid transparent;border-bottom:none;
  border-radius:var(--radius-sm) var(--radius-sm) 0 0;
  font-size:12px;font-family:var(--font-mono);color:var(--muted);
  cursor:pointer;white-space:nowrap;transition:background .15s,color .15s}
.ptab.active{background:var(--bg);border-color:var(--line-strong);color:var(--text);
  box-shadow:inset 0 2px 0 var(--accent)}
.ptab:hover:not(.active){background:var(--field);color:var(--text-2)}
.ptab-del{background:none;border:none;color:var(--muted);cursor:pointer;
  font-size:14px;padding:1px 3px;line-height:1;border-radius:3px;
  transition:color .15s;flex-shrink:0}
.ptab-del:hover{color:var(--danger)}
.ptab-add{height:26px;align-self:center;margin-left:6px;
  background:transparent;border:1px dashed var(--line-strong);
  border-radius:var(--radius-sm);color:var(--primary-text);padding:0 10px;font-size:11px;
  font-weight:600;cursor:pointer;white-space:nowrap;flex-shrink:0;transition:background .15s}
.ptab-add:hover{background:rgba(30,91,255,.12)}

/* layout */
#main{display:flex;flex:1;overflow:hidden}

/* canvas */
#cv{flex:1;position:relative;overflow:hidden;
  background-color:var(--bg);
  background-image:radial-gradient(var(--grid) 1px,transparent 1px);
  background-size:28px 28px;cursor:grab}
#cv.panning{cursor:grabbing}
/* Everything on the canvas lives in #world, which is translated to pan the view.
   Paint order: wire click targets (#svo) under the cards, so a click on a card's field never
   hits a wire; then the cards; then (#svh) the visible wires, drag preview and handles, so a
   wire is never hidden by a card it passes over and always reads as the right connection. */
#world{position:absolute;left:0;top:0;width:0;height:0}
#svo,#svh{position:absolute;overflow:visible;pointer-events:none}
#svo{z-index:0}
#svh{z-index:2}

/* nodes */
.node{position:absolute;z-index:1;width:280px;cursor:default;background:var(--card);
  border:1px solid var(--line-strong);border-radius:var(--radius-lg);overflow:hidden;
  box-shadow:0 4px 20px rgba(0,0,0,.35);transition:border-color .2s,box-shadow .2s}
.node:hover{border-color:rgba(74,134,255,.5)}
.node.sel{border-color:var(--accent);
  box-shadow:0 0 0 3px rgba(54,214,255,.18),0 10px 32px rgba(0,0,0,.5)}
.nh{height:44px;display:flex;align-items:center;padding:0 12px;gap:8px;
  background:linear-gradient(135deg,rgba(30,91,255,.22),rgba(54,214,255,.05));
  border-bottom:1px solid var(--line);cursor:move}
.ndot{width:9px;height:9px;border-radius:50%;background:var(--brand-gradient);flex-shrink:0;
  box-shadow:0 0 0 3px rgba(30,91,255,.18)}
.nlbl{font-weight:700;font-size:13px;color:var(--text);flex:1;
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-family:var(--font-mono)}
.ndel{background:none;border:none;color:var(--muted);cursor:pointer;
  font-size:16px;padding:2px 4px;line-height:1;border-radius:4px;flex-shrink:0;
  transition:color .15s}
.ndel:hover{color:var(--danger)}
.nch{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);
  border-bottom:1px solid var(--line);padding:3px 0}
.clbl{font-size:9px;font-weight:600;color:var(--muted);text-transform:uppercase;
  letter-spacing:.08em;padding:0 10px}
.clbl.r{text-align:right}
.ncols{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr)}
.sc{border-right:1px solid var(--line)}
.tr{height:40px;display:flex;align-items:center;padding:0 10px}
.tr.sub{padding-left:18px}
.tr.pub{padding-right:18px;flex-direction:row-reverse}
.ti{display:flex;flex-direction:column;gap:1px;min-width:0;max-width:100%}
.tn{font-size:12px;color:var(--text);font-family:var(--font-mono);line-height:1.2}
.tt{font-size:10px;color:var(--muted);line-height:1.2}
.tn,.tt{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.tr.pub .ti{text-align:right}
.ec{height:40px;display:flex;align-items:center;padding:0 10px;
  color:var(--muted);font-size:11px;font-style:italic}
.ec.r{justify-content:flex-end}
.nfoot{padding:7px 10px;border-top:1px solid var(--line);font-size:10px;color:var(--muted);
  background:rgba(5,11,31,.35)}

/* inspector sidebar */
#sb{width:300px;flex-shrink:0;background:var(--surface);
  border-left:1px solid var(--line);
  display:flex;flex-direction:column;overflow-y:auto;
  padding:20px 16px;gap:20px;transition:width .2s,padding .2s}
#sb.h{width:0;padding:0;overflow:hidden;border:none}
#sb::-webkit-scrollbar{width:6px}
#sb::-webkit-scrollbar-thumb{background:var(--line-strong);border-radius:3px}
.shead{display:flex;align-items:center;justify-content:space-between}
.stit{font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.08em;
  color:var(--primary-text)}
.sb-close{background:none;border:none;color:var(--muted);cursor:pointer;font-size:18px;
  line-height:1;padding:2px 6px;border-radius:4px;transition:color .15s,background .15s}
.sb-close:hover{color:var(--text);background:var(--field)}
.slbl{display:flex;align-items:center;gap:6px;font-size:10px;font-weight:700;
  text-transform:uppercase;letter-spacing:.08em;color:var(--muted);margin-bottom:6px}
.sinp{background:var(--field);border:1px solid var(--field-border);
  border-radius:var(--radius-sm);color:var(--text);padding:6px 10px;
  font-family:var(--font-mono);font-size:12px;outline:none;width:100%;transition:border-color .15s}
.sinp:focus,.ssel:focus{border-color:var(--accent);box-shadow:none}
.srow{display:flex;align-items:center;justify-content:space-between;margin-bottom:8px}
.srow .slbl{margin:0}
.addb{background:rgba(30,91,255,.1);border:1px solid var(--line-strong);
  border-radius:var(--radius-sm);color:var(--primary-text);padding:3px 8px;font-size:11px;
  font-weight:600;cursor:pointer;transition:background .15s}
.addb:hover{background:rgba(30,91,255,.2)}
.tc{background:var(--field);border:1px solid var(--line);
  border-radius:var(--radius);padding:10px;margin-bottom:8px}
.tcr{display:flex;align-items:center;gap:6px;margin-bottom:6px}
.tcr .sinp{flex:1;padding:4px 8px;font-size:11px}
/* role markers: filled dot, or ring for subscribers (matching the canvas handles) */
.tdot,.ldot{width:7px;height:7px;border-radius:50%;flex-shrink:0;background:var(--c)}
.tdot.ring,.ldot.ring{background:transparent;box-shadow:inset 0 0 0 1.5px var(--c)}
.trem{background:none;border:none;color:var(--muted);cursor:pointer;
  font-size:14px;padding:2px 4px;line-height:1;transition:color .15s}
.trem:hover{color:var(--danger)}
.ssel{background:var(--bg);border:1px solid var(--field-border);
  border-radius:var(--radius-sm);color:var(--text);padding:4px 8px;
  font-family:var(--font-mono);font-size:11px;width:100%;outline:none;cursor:pointer;
  transition:border-color .15s}
.none{color:var(--muted);font-size:12px;font-style:italic}
.shnote{margin-top:auto;padding:10px 12px;
  background:rgba(30,91,255,.06);border-radius:var(--radius);
  border:1px solid var(--line);
  font-size:11px;color:var(--muted);line-height:1.6}
.k-pub{color:var(--pub)}
.k-sub{color:var(--sub)}

/* empty state */
#es{position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);
  display:flex;flex-direction:column;align-items:center;gap:10px;
  max-width:360px;text-align:center;color:var(--muted);pointer-events:none}
#es.h{display:none}
.es-mark{width:88px;height:88px;margin-bottom:6px;
  filter:drop-shadow(0 6px 24px rgba(30,91,255,.35))}
#es h2{font-size:17px;font-weight:650;color:var(--text);letter-spacing:.01em}
#es p{font-size:13px;line-height:1.55}
#es strong{color:var(--primary-text);font-weight:600}
#es code{font-family:var(--font-mono);font-size:12px;color:var(--text-2)}

/* toast */
#toast{position:fixed;bottom:24px;right:24px;
  background:var(--card);border:1px solid var(--line-strong);border-radius:var(--radius-lg);
  padding:14px 18px;font-size:13px;box-shadow:var(--shadow);
  max-width:420px;line-height:1.5;z-index:9999;
  opacity:0;transform:translateY(16px);
  transition:opacity .25s ease,transform .25s ease;pointer-events:none}
#toast.show{opacity:1;transform:translateY(0)}
#toast.ok{border-color:rgba(52,211,153,.4);color:var(--ok)}
#toast.err{border-color:rgba(248,113,113,.4);color:var(--danger)}
#toast.warn{border-color:rgba(251,191,36,.45);color:var(--warn)}
/* Errors and warnings stay until clicked, so a long message can be read in full. */
#toast.show.err,#toast.show.warn{pointer-events:auto;cursor:pointer}

/* unsaved-design banner: stays until the design is saved */
#banner{display:none;flex-shrink:0;align-items:center;gap:10px;flex-wrap:wrap;
  padding:10px 16px;background:rgba(248,113,113,.1);
  border-bottom:1px solid rgba(248,113,113,.45);color:var(--danger-soft);font-size:13px;line-height:1.5}
#banner.show{display:flex}
#banner .bmsg{flex:1 1 320px;min-width:0;overflow-wrap:anywhere}
#banner .bmsg strong{color:var(--danger)}
.btn-warn{display:inline-flex;align-items:center;gap:6px;
  background:rgba(248,113,113,.14);border:1px solid rgba(248,113,113,.45);
  border-radius:var(--radius);color:var(--danger-soft);padding:6px 14px;font-size:12px;font-weight:600;
  cursor:pointer;white-space:nowrap}
.btn-warn:hover{background:rgba(248,113,113,.26)}

/* field errors in the node inspector */
.sinp.bad,.ssel.bad{border-color:var(--danger);background:rgba(248,113,113,.08)}
.ferr{color:var(--danger);font-size:11px;line-height:1.45;margin:0 0 6px 12px;overflow-wrap:anywhere}

@media (prefers-reduced-motion:reduce){
  *,*::before,*::after{transition:none!important;animation-duration:1.5s!important}
}
</style>
</head>
<body>

<!-- Shared artwork: the ROS2 Inspector mark (dark-background variant) and toolbar icons. -->
<svg width="0" height="0" style="position:absolute" aria-hidden="true" focusable="false">
  <defs>
    <linearGradient id="lg-frame-blue" x1="30" y1="30" x2="170" y2="170" gradientUnits="userSpaceOnUse">
      <stop stop-color="#1E5BFF"/><stop offset="1" stop-color="#36D6FF"/>
    </linearGradient>
    <linearGradient id="lg-frame-cyan" x1="340" y1="30" x2="482" y2="482" gradientUnits="userSpaceOnUse">
      <stop stop-color="#36D6FF"/><stop offset="1" stop-color="#1E5BFF"/>
    </linearGradient>
    <linearGradient id="lg-node" x1="220" y1="210" x2="360" y2="410" gradientUnits="userSpaceOnUse">
      <stop stop-color="#1E5BFF"/><stop offset="1" stop-color="#36D6FF"/>
    </linearGradient>
    <symbol id="logo-mark" viewBox="0 0 512 512">
      <g fill="none" stroke="#36D6FF" stroke-linecap="round" stroke-linejoin="round" opacity=".12">
        <path d="M362 30H450Q482 30 482 62V152M362 482H450Q482 482 482 450V360" stroke-width="36"/>
        <path d="M255 247L396 217L330 400L255 247" stroke-width="21"/>
      </g>
      <circle cx="330" cy="400" r="42" fill="#36D6FF" opacity=".10"/>
      <g fill="none" stroke-linecap="round" stroke-linejoin="round" stroke-width="18">
        <path d="M150 30H62Q30 30 30 62V152" stroke="url(#lg-frame-blue)"/>
        <path d="M362 30H450Q482 30 482 62V152" stroke="url(#lg-frame-cyan)"/>
        <path d="M30 360V450Q30 482 62 482H150" stroke="url(#lg-frame-blue)"/>
        <path d="M362 482H450Q482 482 482 450V360" stroke="url(#lg-frame-cyan)"/>
      </g>
      <g fill="none" stroke-linecap="round" stroke-linejoin="round" stroke-width="7">
        <path d="M111 220L255 247M111 220L155 335L255 247M255 247L274 102M255 247L396 217" stroke="#1E5BFF"/>
        <path d="M255 247L330 400M396 217L330 400" stroke="#36D6FF"/>
      </g>
      <circle cx="111" cy="220" r="36" fill="#0A1230" stroke="#1E5BFF" stroke-width="4"/>
      <circle cx="155" cy="335" r="29" fill="#050B1F" stroke="#1E5BFF" stroke-width="4"/>
      <circle cx="274" cy="102" r="29" fill="#1E5BFF"/>
      <circle cx="396" cy="217" r="28" fill="none" stroke="#36D6FF" stroke-width="13"/>
      <circle cx="330" cy="400" r="31" fill="#36D6FF"/>
      <circle cx="255" cy="247" r="39" fill="url(#lg-node)"/>
      <path d="M269 229A22 22 0 1 0 269 265" fill="none" stroke="#7FE5FF" stroke-width="10" stroke-linecap="round"/>
    </symbol>
    <symbol id="i-plus" viewBox="0 0 24 24"><path d="M12 5v14M5 12h14"/></symbol>
    <symbol id="i-save" viewBox="0 0 24 24"><path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z"/><path d="M17 21v-8H7v8M7 3v5h8"/></symbol>
    <symbol id="i-generate" viewBox="0 0 24 24"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="M7 10l5 5 5-5M12 15V3"/></symbol>
    <symbol id="i-fit" viewBox="0 0 24 24"><path d="M8 3H5a2 2 0 0 0-2 2v3M21 8V5a2 2 0 0 0-2-2h-3M16 21h3a2 2 0 0 0 2-2v-3M3 16v3a2 2 0 0 0 2 2h3"/></symbol>
    <symbol id="i-export" viewBox="0 0 24 24"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="M7 10l5 5 5-5M12 15V3"/></symbol>
    <symbol id="i-retry" viewBox="0 0 24 24"><path d="M21 12a9 9 0 1 1-2.64-6.36L21 8"/><path d="M21 3v5h-5"/></symbol>
  </defs>
</svg>

<div id="tb">
  <span class="brand" title="ROS2 Inspector · Package Designer">
    <svg class="brand-mark" aria-hidden="true"><use href="#logo-mark"/></svg>
    <span class="brand-name">ROS<b>2</b> Inspector</span>
    <span class="brand-tag">Package Designer</span>
  </span>
  <div class="sep"></div>
  <span class="tb-lbl">pkg:</span>
  <input class="tb-inp" id="pn" value="my_package" spellcheck="false" aria-label="Package name">
  <div class="sep"></div>
  <button class="btn" id="an"><svg class="ic" aria-hidden="true"><use href="#i-plus"/></svg><span class="lbl">Add Node</span></button>
  <button class="btn-save" id="sv"><svg class="ic" aria-hidden="true"><use href="#i-save"/></svg><span class="lbl">Save</span></button>
  <button class="btn-p" id="gb" disabled><svg class="ic" aria-hidden="true"><use href="#i-generate"/></svg><span class="lbl">Generate All</span></button>
  <button class="btn" id="fit" title="Bring the design back into view: centred when it fits, otherwise from its top-left node"><svg class="ic" aria-hidden="true"><use href="#i-fit"/></svg><span class="lbl">Show all</span></button>
  <span class="tb-hint">Click a node to configure · Drag cyan handles to connect · Drag the background or scroll to pan · <kbd>Ctrl</kbd>+<kbd>S</kbd> to save</span>
</div>

<div id="banner" role="alert">
  <span class="bmsg" id="bmsg"></span>
  <button class="btn-warn" id="bexp" title="Download the current design as JSON"><svg class="ic" aria-hidden="true"><use href="#i-export"/></svg>Export design</button>
  <button class="btn-warn" id="bretry"><svg class="ic" aria-hidden="true"><use href="#i-retry"/></svg>Try saving again</button>
</div>

<div id="pt"></div>

<div id="main">
  <div id="cv">
    <div id="world">
      <svg id="svo" xmlns="http://www.w3.org/2000/svg">
        <defs>
          <marker id="arr" markerWidth="8" markerHeight="8" refX="8" refY="3" orient="auto">
            <path d="M0,0 L0,6 L8,3 z" style="fill:var(--pub)"/>
          </marker>
          <marker id="arr-bad" markerWidth="8" markerHeight="8" refX="8" refY="3" orient="auto">
            <path d="M0,0 L0,6 L8,3 z" style="fill:var(--danger)"/>
          </marker>
        </defs>
        <g id="wh"></g>
      </svg>
      <svg id="svh" xmlns="http://www.w3.org/2000/svg">
        <g id="eg"></g>
        <g id="tg"></g>
        <g id="hg"></g>
      </svg>
    </div>
    <div id="es">
      <svg class="es-mark" aria-hidden="true"><use href="#logo-mark"/></svg>
      <h2>Design your ROS 2 package</h2>
      <p>Add nodes, give them topics, services and actions, then wire publishers to
        subscribers. Generate writes a ready-to-build <code>ament_python</code> package.</p>
      <p>Click <strong>Add Node</strong> to start designing</p>
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

// SVG presentation attributes cannot use var(), so the few colours drawn from script are read
// once from the design tokens in the stylesheet, which stay the single source of truth.
function cssToken(name){
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}
const COL={
  pub:cssToken('--pub'), pubRing:cssToken('--pub-ring'),
  sub:cssToken('--sub'), card:cssToken('--card'), danger:cssToken('--danger'),
};

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
const SVH = document.getElementById('svh');
const WORLD = document.getElementById('world');
const EG  = document.getElementById('eg');
const TG  = document.getElementById('tg');
const WH  = document.getElementById('wh');
const HG  = document.getElementById('hg');
const SB  = document.getElementById('sb');
const ES  = document.getElementById('es');
const GB  = document.getElementById('gb');
const PT  = document.getElementById('pt');
const PN  = document.getElementById('pn');
const TOAST = document.getElementById('toast');
const BANNER = document.getElementById('banner');

// Endpoint field errors from the server (or the length check), shown in the inspector.
// Keyed by package id, node id, endpoint group and endpoint id.
const FERR={};
function ferrKey(pid,nid,group,eid){ return [pid,nid,group,eid].join('|'); }
function ferrFor(group,eid){
  const p=pkg(); return p&&S.sel ? FERR[ferrKey(p.id,S.sel,group,eid)] : undefined;
}
function ferrHTML(group,eid){
  const msg=ferrFor(group,eid);
  return msg?`<div class="ferr" role="alert">Name ${esc(msg)}</div>`:'';
}

// ── view (pan) ─────────────────────────────────────────────────────────────
// Node x/y are world coordinates; the screen shows the world shifted by VIEW.
const VIEW={x:0,y:0};
// The SVG layers start this far up/left of the world origin, so wires that loop
// left of x=0 or above y=0 are still drawn and clickable.
const SVG_PAD=4000;

function applyView(){
  WORLD.style.transform=`translate(${VIEW.x}px,${VIEW.y}px)`;
  CV.style.backgroundPosition=`${VIEW.x}px ${VIEW.y}px`;
}

function sizeLayers(){
  const p=pkg();
  let w=0,h=0;
  for(const n of (p?p.nodes:[])){ w=Math.max(w,n.x+NW); h=Math.max(h,n.y+cardHeight(n)); }
  for(const svg of [SVO,SVH]){
    svg.style.left=svg.style.top=(-SVG_PAD)+'px';
    svg.setAttribute('width',w+2*SVG_PAD); svg.setAttribute('height',h+2*SVG_PAD);
  }
  for(const g of [EG,WH,TG,HG]) g.setAttribute('transform',`translate(${SVG_PAD},${SVG_PAD})`);
}

// Pointer position in world coordinates.
function toWorld(e){
  const r=CV.getBoundingClientRect();
  return {x:e.clientX-r.left-VIEW.x, y:e.clientY-r.top-VIEW.y};
}

// Width of the canvas once the sidebar has finished opening or closing.
function canvasWidth(){
  return document.getElementById('main').clientWidth-(S.sel?SB_WIDTH:0);
}
const SB_WIDTH=300;

// Pan just enough that a node card is fully on screen.
function ensureVisible(nd){
  const el=document.getElementById('nd'+nd.id);
  const w=canvasWidth(), h=CV.clientHeight;
  const cw=el?el.offsetWidth:NW, ch=el?el.offsetHeight:cardHeight(nd);
  const m=20;
  if(nd.x+VIEW.x+cw>w-m) VIEW.x=w-m-cw-nd.x;
  if(nd.x+VIEW.x<m) VIEW.x=m-nd.x;
  if(nd.y+VIEW.y+ch>h-m) VIEW.y=h-m-ch-nd.y;
  if(nd.y+VIEW.y<m) VIEW.y=m-nd.y;
  applyView();
}

// There is no zoom, so a design larger than the canvas starts at its top-left node.
function showAll(){
  const p=pkg(); if(!p||!p.nodes.length){ VIEW.x=VIEW.y=0; applyView(); return; }
  const rects=p.nodes.map(cardRect), M=40;
  const l=Math.min(...rects.map(r=>r.l)), t=Math.min(...rects.map(r=>r.t));
  const w=Math.max(...rects.map(r=>r.r))-l, h=Math.max(...rects.map(r=>r.b))-t;
  const cw=canvasWidth(), ch=CV.clientHeight;
  VIEW.x=w+2*M<=cw ? (cw-w)/2-l : M-l;
  VIEW.y=h+2*M<=ch ? (ch-h)/2-t : M-t;
  applyView();
}

// ── geometry ───────────────────────────────────────────────────────────────
// Handle position of row i, measured from the rendered card so it always lines up
// with its row (fonts, long names and zoom change the real heights).
function hp(node,side,i){
  const el=document.getElementById('nd'+node.id);
  const row=el&&el.querySelectorAll(side==='pub'?'.tr.pub':'.tr.sub')[i];
  if(row) return {
    x: side==='pub' ? node.x+el.offsetWidth : node.x,
    y: node.y+el.clientTop+row.offsetTop+row.offsetHeight/2,
  };
  return {
    x: side==='pub' ? node.x+NW : node.x,
    y: node.y + NH + CH + i*RH + RH/2,
  };
}
const HR=6;  // handle radius

function bez(sx,sy,tx,ty){
  const cp=Math.max(80,Math.abs(tx-sx)*.5);
  return `M${sx},${sy} C${sx+cp},${sy} ${tx-cp},${ty} ${tx},${ty}`;
}

// ── wire routing ───────────────────────────────────────────────────────────
// A wire is a smooth curve when nothing is in its way. If it would cross a card (and so look
// like it starts or ends at that card's handles) or runs backwards, it detours along a lane
// above or below every card in its span, so it never passes through a card.
function cardRect(n){
  const el=document.getElementById('nd'+n.id);
  return {l:n.x, t:n.y, r:n.x+(el?el.offsetWidth:NW), b:n.y+(el?el.offsetHeight:NH+CH+RH)};
}
function bezPoint(sx,sy,tx,ty,t){
  const cp=Math.max(80,Math.abs(tx-sx)*.5), u=1-t;
  const xs=[sx,sx+cp,tx-cp,tx], ys=[sy,sy,ty,ty];
  const f=v=>u*u*u*v[0]+3*u*u*t*v[1]+3*u*t*t*v[2]+t*t*t*v[3];
  return [f(xs),f(ys)];
}
function bezCrossesCards(sx,sy,tx,ty,rects){
  for(let i=1;i<40;i++){
    const [x,y]=bezPoint(sx,sy,tx,ty,i/40);
    if(rects.some(r=>x>r.l-4&&x<r.r+4&&y>r.t-4&&y<r.b+4)) return true;
  }
  return false;
}
function roundedPath(pts,radius){
  let d=`M${pts[0][0]},${pts[0][1]}`;
  for(let i=1;i<pts.length-1;i++){
    const [px,py]=pts[i-1], [x,y]=pts[i], [nx,ny]=pts[i+1];
    const r=Math.min(radius,Math.hypot(x-px,y-py)/2,Math.hypot(nx-x,ny-y)/2);
    const a=[x+Math.sign(px-x)*r, y+Math.sign(py-y)*r];
    const b=[x+Math.sign(nx-x)*r, y+Math.sign(ny-y)*r];
    d+=` L${a[0]},${a[1]} Q${x},${y} ${b[0]},${b[1]}`;
  }
  const [lx,ly]=pts[pts.length-1];
  return d+` L${lx},${ly}`;
}
function wirePath(p,fn,tn,sx,sy,tx,ty,lane){
  const rects=p.nodes.map(cardRect);
  // Every card counts, its own two included: a curve that bends back into them is wrong too.
  const others=p.nodes.filter(n=>n!==fn&&n!==tn).map(cardRect);
  const forward=tx-sx>=40;
  if(forward && !bezCrossesCards(sx,sy,tx,ty,others)) return bez(sx,sy,tx,ty);
  // The wire turns 20px out from each handle, or halfway to the nearest card beside the
  // handle when cards nearly touch, so its vertical legs run in the gap, not through a card.
  const src=cardRect(fn), dst=cardRect(tn);
  const rightOf=rects.filter(r=>r!==rects[p.nodes.indexOf(fn)]&&r.l>=src.r-1).map(r=>r.l);
  const leftOf=rects.filter(r=>r!==rects[p.nodes.indexOf(tn)]&&r.r<=dst.l+1).map(r=>r.r);
  const x1=sx+Math.min(20,(Math.min(Infinity,...rightOf)-sx)/2);
  const x2=tx-Math.min(20,(tx-Math.max(-Infinity,...leftOf))/2);
  const lo=Math.min(x1,x2), hi=Math.max(x1,x2);
  const span=rects.filter(r=>r.r>lo&&r.l<hi);
  if(!span.length) return roundedPath([[sx,sy],[x1,sy],[x1,ty],[tx,ty]],10);
  const gap=16+8*(lane%4);
  const above=Math.min(...span.map(r=>r.t))-gap, below=Math.max(...span.map(r=>r.b))+gap;
  const y=Math.abs(sy-above)+Math.abs(ty-above)<Math.abs(sy-below)+Math.abs(ty-below)?above:below;
  return roundedPath([[sx,sy],[x1,sy],[x1,y],[x2,y],[x2,ty],[tx,ty]],10);
}

function esc(s){
  return String(s)
    .replace(/&/g,'&amp;').replace(/</g,'&lt;')
    .replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;');
}
// A value embedded as a JS string literal inside an HTML event-handler attribute.
function jsa(v){ return esc(JSON.stringify(String(v))); }

// 'pkg/msg/Type' and 'pkg/Type' are the same interface.
function canonType(t){ return String(t).replace(/^([a-z][a-z0-9_]*)\/(?:msg|srv|action)\//,'$1/'); }
function sameType(a,b){ return canonType(a)===canonType(b); }

function edgeEnds(p,e){
  const fn=p.nodes.find(n=>n.id===e.fn), tn=p.nodes.find(n=>n.id===e.tn);
  if(!fn||!tn) return null;
  const pub=fn.publishers.find(x=>x.id===e.ft), sub=tn.subscribers.find(x=>x.id===e.tt);
  if(!pub||!sub) return null;
  return {fn,tn,pub,sub,ok:expandedName(pub.name,fn.label)===expandedName(sub.name,tn.label)&&sameType(pub.msgType,sub.msgType)};
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
  renderNodes();
  sizeLayers();
  renderEdges();
  renderHandles();
  const p=pkg();
  ES.classList.toggle('h', !!(p&&p.nodes.length>0));
  GB.disabled = !S.packages.some(p=>p.nodes.length>0);
  renderTabs();
}

function renderEdges(){
  EG.innerHTML=''; WH.innerHTML='';
  const p=pkg(); if(!p) return;
  let lane=0;
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
    // End on the handle's rim so the arrowhead is never covered by the handle.
    const d=wirePath(p,fn,tn,sp.x+HR,sp.y,tp2.x-HR,tp2.y,lane);
    if(!d.includes(' C')) lane++;
    path.setAttribute('d',d);
    path.setAttribute('stroke',ends&&ends.ok?COL.pub:COL.danger);
    if(ends&&!ends.ok) path.setAttribute('stroke-dasharray','6 4');
    path.setAttribute('stroke-width','2');
    path.setAttribute('fill','none');
    path.setAttribute('marker-end',ends&&!ends.ok?'url(#arr-bad)':'url(#arr)');
    path.setAttribute('opacity','.85');
    EG.appendChild(path);
    // The click target is a wider invisible copy under the cards (see the paint order above).
    const hit=makeSVG('path');
    hit.setAttribute('d',path.getAttribute('d'));
    hit.setAttribute('stroke','transparent');
    hit.setAttribute('stroke-width','10');
    hit.setAttribute('fill','none');
    hit.style.pointerEvents='stroke';
    hit.style.cursor='pointer';
    const tip=makeSVG('title');
    tip.textContent=ends&&!ends.ok
      ? `${fn.label} → ${tn.label}: topic name or type no longer match (click to remove)`
      : `${fn.label} → ${tn.label} (click to remove)`;
    hit.appendChild(tip);
    hit.addEventListener('click',()=>{ p.edges=p.edges.filter(x=>x.id!==e.id); renderAll(); });
    hit.addEventListener('mouseenter',()=>path.setAttribute('stroke-width','3'));
    hit.addEventListener('mouseleave',()=>path.setAttribute('stroke-width','2'));
    WH.appendChild(hit);
  }
}

function renderHandles(){
  HG.innerHTML='';
  const p=pkg(); if(!p) return;
  for(const node of p.nodes){
    node.publishers.forEach((pub,i)=>{
      const {x,y}=hp(node,'pub',i);
      const c=makeHandle(x,y,COL.pub,COL.pubRing,1.5);
      c.dataset.nid=node.id; c.dataset.tid=pub.id; c.dataset.side='pub';
      c.style.cursor='crosshair';
      c.addEventListener('mousedown',onHDown);
      HG.appendChild(c);
    });
    node.subscribers.forEach((sub,i)=>{
      const {x,y}=hp(node,'sub',i);
      // A ring, not a dot: subscribers differ from publishers in shape as well as colour.
      const c=makeHandle(x,y,COL.card,COL.sub,2.5);
      c.dataset.nid=node.id; c.dataset.tid=sub.id; c.dataset.side='sub';
      HG.appendChild(c);
    });
  }
}

function makeHandle(x,y,fill,stroke,strokeWidth){
  const c=makeSVG('circle');
  c.setAttribute('cx',x); c.setAttribute('cy',y);
  c.setAttribute('r',String(HR));
  c.setAttribute('fill',fill);
  c.setAttribute('stroke',stroke);
  c.setAttribute('stroke-width',String(strokeWidth));
  c.style.pointerEvents='all';
  return c;
}

function makeSVG(tag){
  return document.createElementNS('http://www.w3.org/2000/svg',tag);
}

function renderNodes(){
  const p=pkg();
  const validIds=new Set((p?p.nodes:[]).map(n=>'nd'+n.id));
  WORLD.querySelectorAll('.node').forEach(el=>{ if(!validIds.has(el.id)) el.remove(); });
  if(!p) return;
  for(const node of p.nodes){
    let el=document.getElementById('nd'+node.id);
    if(!el){
      el=document.createElement('div');
      el.className='node'; el.id='nd'+node.id;
      WORLD.appendChild(el);
    }
    el.style.left=node.x+'px'; el.style.top=node.y+'px';
    el.classList.toggle('sel',node.id===S.sel);
    // Only rebuild a card when its content changed: dragging re-renders on every mouse move,
    // and replacing the DOM under the pointer would swallow the user's next click.
    const html=nodeHTML(node);
    if(el._html!==html){
      el.innerHTML=html; el._html=html;
      el.querySelector('.nh').addEventListener('mousedown',e=>onNDown(e,node.id));
      el.querySelector('.ndel').addEventListener('click',e=>{ e.stopPropagation(); delNode(node.id); });
    }
  }
}

// Select on mouseup (delegated): a field's change event fires on blur, between mousedown and
// mouseup, and may re-render the card being clicked, which would cancel a 'click'.
CV.addEventListener('mouseup',e=>{
  if(eDrag||!e.target.closest) return;
  const el=e.target.closest('.node');
  if(!el||e.target.closest('.ndel')) return;
  const id=el.id.slice(2);
  if(S.sel!==id) selNode(id);
});

function nodeHTML(nd){
  const subs=nd.subscribers.map(s=>`
    <div class="tr sub"><div class="ti">
      <div class="tn" title="${esc(s.name)}">${esc(s.name)}</div>
      <div class="tt" title="${esc(s.msgType)}">${esc(s.msgType)}</div>
    </div></div>`).join('')||'<div class="ec">—</div>';
  const pubs=nd.publishers.map(p2=>`
    <div class="tr pub"><div class="ti">
      <div class="tn" title="${esc(p2.name)}">${esc(p2.name)}</div>
      <div class="tt" title="${esc(p2.msgType)}">${esc(p2.msgType)}</div>
    </div></div>`).join('')||'<div class="ec r">—</div>';
  const serviceCount=(nd.services||[]).length;
  const clientCount=(nd.clients||[]).length;
  const actionServerCount=(nd.action_servers||[]).length;
  const actionClientCount=(nd.action_clients||[]).length;
  return `
    <div class="nh">
      <span class="ndot"></span>
      <span class="nlbl" title="${esc(nd.label)}">${esc(nd.label)}</span>
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
    <div class="nfoot">
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
    <div class="shead">
      <span class="stit">Node Config</span>
      <button class="sb-close" onclick="closeSB()" title="Close" aria-label="Close node config">×</button>
    </div>

    <div>
      <label class="slbl">Node Name</label>
      <input class="sinp" id="snm" value="${esc(nd.label)}" spellcheck="false">
    </div>

    <div>
      <div class="srow">
        <label class="slbl">
          <span class="ldot" style="--c:var(--pub)"></span>
          Publishers
        </label>
        <button class="addb" onclick="addT('pub')">+ Add</button>
      </div>
      <div id="spubs">${tHTML(nd.publishers,'pub')}</div>
    </div>

    <div>
      <div class="srow">
        <label class="slbl">
          <span class="ldot ring" style="--c:var(--sub)"></span>
          Subscribers
        </label>
        <button class="addb" onclick="addT('sub')">+ Add</button>
      </div>
      <div id="ssubs">${tHTML(nd.subscribers,'sub')}</div>
    </div>

    ${endpointSection('services','Service Providers',nd.services||[],SRVS,'var(--srv)')}
    ${endpointSection('clients','Service Clients',nd.clients||[],SRVS,'var(--srv-client)')}
    ${endpointSection('action_servers','Action Servers',nd.action_servers||[],ACTIONS,'var(--act)')}
    ${endpointSection('action_clients','Action Clients',nd.action_clients||[],ACTIONS,'var(--act-client)')}

    <div class="shnote">
      Drag from a <span class="k-pub">cyan dot ●</span> (publisher) to a
      <span class="k-sub">blue ring ○</span> (subscriber) to wire topics.
      Click an edge to remove it.
    </div>`;

  document.getElementById('snm').addEventListener('input',e=>{
    const n=pkg()&&pkg().nodes.find(x=>x.id===S.sel);
    if(!n) return;
    // A private '~/' publisher moves with its node: keep the other nodes it feeds connected.
    const p=pkg();
    const wired=p.edges.filter(x=>x.fn===n.id&&x.tn!==n.id&&edgeEnds(p,x)?.ok);
    n.label=e.target.value;
    for(const x of wired){
      const ends=edgeEnds(p,x);
      if(ends&&!ends.ok) ends.sub.name=wiredName(ends.pub.name,n.label,ends.tn.label);
    }
    renderAll();
  });
}

function tHTML(topics,side){
  const dot=side==='pub'
    ? '<span class="tdot" style="--c:var(--pub)"></span>'
    : '<span class="tdot ring" style="--c:var(--sub)"></span>';
  const group=side==='pub'?'publishers':'subscribers';
  if(!topics.length) return '<p class="none">None yet</p>';
  return topics.map(t=>`
    <div class="tc">
      <div class="tcr">
        ${dot}
        <input class="sinp${ferrFor(group,t.id)?' bad':''}"
          data-ep="${esc(group+'|'+t.id)}"
          value="${esc(t.name)}" spellcheck="false"
          onchange="updT(${jsa(side)},${jsa(t.id)},'name',this.value)" placeholder="/topic">
        <button class="trem" onclick="remT(${jsa(side)},${jsa(t.id)})">×</button>
      </div>
      ${ferrHTML(group,t.id)}
      <input class="ssel" list="dl-msg" value="${esc(t.msgType)}" spellcheck="false"
        placeholder="pkg/Type" title="Pick a type or type any pkg/Type"
        onchange="updT(${jsa(side)},${jsa(t.id)},'msgType',this.value)">
    </div>`).join('');
}

function endpointSection(kind,label,endpoints,typeList,color){
  return `<div>
    <div class="srow">
      <label class="slbl">
        <span class="ldot" style="--c:${color}"></span>
        ${label}
      </label>
      <button class="addb" onclick="addEndpoint(${jsa(kind)})">+ Add</button>
    </div>
    <div>${endpointHTML(kind,endpoints,typeList,color)}</div>
  </div>`;
}

function endpointHTML(kind,endpoints,typeList,color){
  if(!endpoints.length) return '<p class="none">None yet</p>';
  return endpoints.map(t=>`
    <div class="tc">
      <div class="tcr">
        <span class="tdot" style="--c:${color}"></span>
        <input class="sinp${ferrFor(kind,t.id)?' bad':''}"
          data-ep="${esc(kind+'|'+t.id)}"
          value="${esc(t.name)}" spellcheck="false"
          onchange="updEndpoint(${jsa(kind)},${jsa(t.id)},'name',this.value)" placeholder="/name">
        <button class="trem" onclick="remEndpoint(${jsa(kind)},${jsa(t.id)})">×</button>
      </div>
      ${ferrHTML(kind,t.id)}
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
  const victim=S.packages.find(p=>p.id===id);
  if(victim&&victim.nodes.length&&!confirm(`Delete package "${victim.pkg_name}" and its ${victim.nodes.length} node(s) from the design? Files already generated on disk are not touched.`)) return;
  S.packages=S.packages.filter(p=>p.id!==id);
  if(S.activePkg===id){
    S.activePkg=S.packages[0].id;
    S.sel=null;
    SB.classList.add('h');
    WORLD.querySelectorAll('.node').forEach(el=>el.remove());
    PN.value=S.packages[0].pkg_name;
    VIEW.x=VIEW.y=0; applyView();
  }
  renderAll();
}

function switchPkg(id){
  if(S.activePkg===id) return;
  S.activePkg=id;
  S.sel=null;
  SB.classList.add('h');
  WORLD.querySelectorAll('.node').forEach(el=>el.remove());
  VIEW.x=VIEW.y=0; applyView();
  const p=pkg();
  if(p) PN.value=p.pkg_name;
  renderAll();
}

// ── node actions ──────────────────────────────────────────────────────────
function selNode(id){
  S.sel=id; renderAll(); renderSB();
  const p=pkg(), nd=p&&p.nodes.find(n=>n.id===id);
  if(nd) ensureVisible(nd);
}
function closeSB(){ S.sel=null; SB.classList.add('h'); renderAll(); }

function delNode(id){
  const p=pkg(); if(!p) return;
  p.nodes=p.nodes.filter(n=>n.id!==id);
  p.edges=p.edges.filter(e=>e.fn!==id&&e.tn!==id);
  if(S.sel===id){ S.sel=null; SB.classList.add('h'); }
  renderAll();
}

// Approximate rendered card height, used to place new nodes without overlap.
function cardHeight(n){
  return NH+CH+Math.max(1,n.publishers.length,n.subscribers.length)*RH+36;
}

// First free grid cell in the part of the world that is currently on screen.
function freeSpot(p){
  const cols=Math.max(1,Math.floor((canvasWidth()-60)/360));
  const ox=Math.max(0,Math.round(-VIEW.x)), oy=Math.max(0,Math.round(-VIEW.y));
  for(let i=0;i<300;i++){
    const x=ox+60+(i%cols)*360, y=oy+60+Math.floor(i/cols)*240;
    const clash=p.nodes.some(n=>x<n.x+NW+40&&n.x<x+NW+40&&y<n.y+cardHeight(n)+30&&n.y<y+NH+CH+RH+66);
    if(!clash) return {x,y};
  }
  return {x:ox+60,y:oy+60};
}

function addNode(){
  const p=pkg(); if(!p) return;
  const id=String(S.nid++);
  const spot=freeSpot(p);
  p.nodes.push({id, label:'node_'+id,
    x:spot.x, y:spot.y,
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
  if(!t) return;
  t[field]=value;
  if(field==='name') checkName(side==='pub'?'publishers':'subscribers',nd,t);
  if(side==='pub'){
    // A wire means "same topic": keep the subscribers it feeds in step with the publisher.
    for(const e of p.edges.filter(x=>x.fn===nd.id&&x.ft===tid)){
      const tn=p.nodes.find(n=>n.id===e.tn);
      const sub=tn&&tn.subscribers.find(x=>x.id===e.tt);
      if(!sub) continue;
      sub[field]=field==='name'?wiredName(value,nd.label,tn.label):value;
      if(field==='name') checkName('subscribers',tn,sub);
    }
  }
  renderAll();
  if(field==='name') renderSB();
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
  if(endpoint){
    endpoint[field]=value;
    if(field==='name'){ checkName(kind,nd,endpoint); }
    renderAll();
    if(field==='name') renderSB();
  }
}

// ROS 2 rejects a fully qualified name over 247 characters at startup; an action also
// needs room for '/_action/cancel_goal'. Same rule as the server, checked as you type so
// the problem shows on the field instead of after Generate.
const MAX_NAME=247, ACTION_SUFFIX='/_action/cancel_goal';
function expandedName(name,label){
  if(name.startsWith('/')) return name;
  if(name.startsWith('~/')) return '/'+label+'/'+name.slice(2);
  return '/'+name;
}
// The name a subscriber needs to hear a publisher: '~/x' is private to the publishing node,
// so a subscriber on another node gets the expanded name instead of a '~/' of its own.
function wiredName(name,fromLabel,toLabel){
  return name.startsWith('~/')&&fromLabel!==toLabel?expandedName(name,fromLabel):name;
}
function nameLengthError(group,name,label){
  const action=group.startsWith('action_');
  const limit=MAX_NAME-(action?ACTION_SUFFIX.length:0);
  const len=expandedName(String(name),label).length;
  if(len<=limit) return null;
  const what={publishers:'a topic',subscribers:'a topic',services:'a service',clients:'a service'}[group]||'an action';
  return `is ${len} characters once expanded; ROS 2 accepts at most ${limit} for ${what} name`
    +(action?`, because ROS adds '${ACTION_SUFFIX}' to it`:'')+`. Shorten it by ${len-limit}.`;
}
function checkName(group,nd,endpoint){
  const p=pkg(); if(!p) return;
  const key=ferrKey(p.id,nd.id,group,endpoint.id);
  const msg=nameLengthError(group,endpoint.name,nd.label);
  if(msg) FERR[key]=msg; else delete FERR[key];
}

// ── node drag ─────────────────────────────────────────────────────────────
let dNode=null, dOX=0, dOY=0;

function onNDown(e,nid){
  if(e.target.classList.contains('ndel')) return;
  e.preventDefault();
  dNode=nid;
  const p=pkg();
  const nd=p&&p.nodes.find(n=>n.id===nid); if(!nd) return;
  const w=toWorld(e);
  dOX=w.x-nd.x;
  dOY=w.y-nd.y;
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
  if(pan){
    const dx=e.clientX-pan.sx, dy=e.clientY-pan.sy;
    if(Math.abs(dx)+Math.abs(dy)>3) pan.moved=true;
    VIEW.x=pan.vx+dx; VIEW.y=pan.vy+dy; applyView();
  }
  if(dNode){
    const w=toWorld(e);
    const p=pkg();
    const nd=p&&p.nodes.find(n=>n.id===dNode);
    if(nd){
      nd.x=Math.max(0,Math.round(w.x-dOX));
      nd.y=Math.max(0,Math.round(w.y-dOY));
      renderAll();
    }
  }
  if(eDrag){
    const {x:tx,y:ty}=toWorld(e);
    TG.innerHTML=`<path d="${bez(eDrag.sx,eDrag.sy,tx,ty)}"
      stroke="${COL.pub}" stroke-width="1.5" fill="none"
      stroke-dasharray="6 4" opacity=".6"/>`;
  }
});

document.addEventListener('mouseup',e=>{
  dNode=null;
  if(pan){ panMoved=pan.moved; pan=null; CV.classList.remove('panning'); }
  if(eDrag){
    const {x:mx,y:my}=toWorld(e);
    const p=pkg();
    outer: for(const nd of (p?p.nodes:[])){
      if(nd.id===eDrag.fn) continue;
      for(let i=0;i<nd.subscribers.length;i++){
        const pos=hp(nd,'sub',i);
        if(Math.hypot(mx-pos.x,my-pos.y)<14){
          const sub=nd.subscribers[i];
          const src=p.nodes.find(n=>n.id===eDrag.fn);
          const pub=src&&src.publishers.find(x=>x.id===eDrag.ft);
          if(pub&&!sameType(pub.msgType,sub.msgType)){
            showToast(`✗ Type mismatch: ${pub.msgType} → ${sub.msgType}. A wire needs the same type on both ends.`,'err');
            break outer;
          }
          if(pub&&expandedName(pub.name,src.label)!==expandedName(sub.name,nd.label)){
            const name=wiredName(pub.name,src.label,nd.label);
            showToast(`Subscriber renamed ${sub.name} → ${name} so the wire is a real connection.`,'ok');
            sub.name=name;
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

// ── pan ───────────────────────────────────────────────────────────────────
let pan=null, panMoved=false;
function isBackground(t){ return t===CV||t===WORLD||t===SVO||t===SVH; }

CV.addEventListener('mousedown',e=>{
  if(e.button!==0||!isBackground(e.target)) return;
  e.preventDefault();
  pan={sx:e.clientX, sy:e.clientY, vx:VIEW.x, vy:VIEW.y, moved:false};
  panMoved=false;
  CV.classList.add('panning');
});

CV.addEventListener('wheel',e=>{
  e.preventDefault();
  VIEW.x-=e.shiftKey?e.deltaY:e.deltaX;
  VIEW.y-=e.shiftKey?0:e.deltaY;
  applyView();
},{passive:false});

// The browser may still scroll the canvas (e.g. when keyboard focus moves to a card that
// sticks out); turn that into a pan so pointer maths never sees a scroll offset.
CV.addEventListener('scroll',()=>{
  if(!CV.scrollLeft&&!CV.scrollTop) return;
  VIEW.x-=CV.scrollLeft; VIEW.y-=CV.scrollTop;
  CV.scrollLeft=0; CV.scrollTop=0;
  applyView();
});

CV.addEventListener('click',e=>{
  if(isBackground(e.target)&&!panMoved) closeSB();
  panMoved=false;
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
  // Successes fade; errors and warnings stay until clicked or replaced.
  if(!type||type==='ok') showToast._t=setTimeout(()=>{ TOAST.className=''; },5000);
  else TOAST.title='Click to dismiss';
}
TOAST.addEventListener('click',()=>{ TOAST.className=''; });

// ── unsaved design banner ─────────────────────────────────────────────────
function showSaveProblem(error,generated){
  const bmsg=document.getElementById('bmsg');
  bmsg.innerHTML=(generated
      ? '<strong>Files were generated, but the design could not be saved:</strong> '
      : '<strong>The design could not be saved:</strong> ')
    +esc(error)+'. Do not reload this page, or the unsaved changes are lost. '
    +'Use <em>Export design</em> to keep a copy (put it back in the output folder as '
    +'<code>.ros2inspector_design.json</code> to restore it), fix the problem, then try saving again.';
  BANNER.classList.add('show');
}
function hideSaveProblem(){ BANNER.classList.remove('show'); }
function exportDesign(){
  const blob=new Blob([JSON.stringify(snapshot(),null,2)],{type:'application/json'});
  const a=document.createElement('a');
  a.href=URL.createObjectURL(blob);
  a.download='ros2inspector_design.json';
  document.body.appendChild(a); a.click(); a.remove();
  setTimeout(()=>URL.revokeObjectURL(a.href),0);
}
document.getElementById('bexp').addEventListener('click',exportDesign);
document.getElementById('bretry').addEventListener('click',()=>saveState());

// Show server field errors on their fields: open the node and mark the input.
function applyFieldErrors(data,state){
  let first=null;
  for(const f of (data&&data.fields)||[]){
    const m=/^packages\.(\d+)\.nodes\.(\d+)\.(\w+)\.(\d+)\.name$/.exec(String(f.path));
    if(!m) continue;
    const p=state.packages[Number(m[1])], nd=p&&p.nodes[Number(m[2])];
    const ep=nd&&(nd[m[3]]||[])[Number(m[4])];
    if(!ep) continue;
    FERR[ferrKey(p.id,nd.id,m[3],ep.id)]=f.message;
    if(!first) first={pid:p.id,nid:nd.id,key:m[3]+'|'+ep.id};
  }
  if(!first) return;
  if(S.activePkg!==first.pid) switchPkg(first.pid);
  S.sel=first.nid; renderAll(); renderSB();
  const input=[...SB.querySelectorAll('[data-ep]')].find(x=>x.dataset.ep===first.key);
  if(input){ input.scrollIntoView({block:'center'}); input.focus(); }
}

// ── server errors ─────────────────────────────────────────────────────────
// Turn "packages.0.nodes.1.label" into "my_package › talker › label".
function prettyPath(path,state){
  const parts=String(path).split('.');
  const out=[]; let cur=state;
  for(let i=0;i<parts.length;i++){
    const key=parts[i];
    if(cur&&Array.isArray(cur)&&/^\d+$/.test(key)){
      cur=cur[Number(key)];
      const label=cur&&(cur.pkg_name||cur.label||cur.name);
      out.push(label?String(label):'#'+(Number(key)+1));
    } else if(cur&&typeof cur==='object'&&key in cur){
      cur=cur[key];
      if(!Array.isArray(cur)) out.push(key);
    } else {
      out.push(key);
    }
  }
  return out.join(' › ');
}

function errorText(data,state){
  const fields=(data&&data.fields)||[];
  if(fields.length) return fields.map(x=>{
    const where=prettyPath(x.path,state);
    return where?`${where}: ${x.message}`:x.message;
  }).join('; ');
  return (data&&data.error)||'Request failed.';
}

function snapshot(){
  return {version:1, nid:S.nid, tid:S.tid, pid:S.pid, activePkg:S.activePkg, packages:S.packages};
}
let lastSaved='';
function markSaved(){ lastSaved=JSON.stringify(snapshot()); }
window.addEventListener('beforeunload',e=>{
  if(JSON.stringify(snapshot())!==lastSaved){ e.preventDefault(); e.returnValue=''; }
});

// A toolbar button shows a spinner and a short label while its request runs.
function setBusy(button,busy,label){
  button.disabled=busy;
  button.classList.toggle('busy',busy);
  button.setAttribute('aria-busy',String(busy));
  button.querySelector('.lbl').textContent=label;
}

// ── save ──────────────────────────────────────────────────────────────────
async function saveState(){
  const state=snapshot();
  const sv=document.getElementById('sv');
  setBusy(sv,true,'Saving…');
  try{
    const res=await fetch('/save',{
      method:'POST',
      headers:{'Content-Type':'application/json','X-ROS2Inspector-Token':SESSION_TOKEN},
      body:JSON.stringify(state),
    });
    if(res.ok){ markSaved(); hideSaveProblem(); showToast('✓ Design saved.','ok'); }
    else {
      const data=await res.json().catch(()=>({error:`HTTP ${res.status}`}));
      if(data.state_saved===false) showSaveProblem(data.state_error||data.error,false);
      showToast(`✗ ${errorText(data,state)}`,'err');
    }
  }catch(err){
    showToast(`✗ ${err.message}`,'err');
  }finally{
    setBusy(sv,false,'Save');
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
  const payload=snapshot();
  setBusy(GB,true,'Generating…');
  try{
    const res=await fetch('/generate',{
      method:'POST',
      headers:{'Content-Type':'application/json','X-ROS2Inspector-Token':SESSION_TOKEN},
      body:JSON.stringify(payload),
    });
    const data=await res.json().catch(()=>({error:`HTTP ${res.status}`}));
    if(res.ok){
      const saved=data.state_saved!==false;
      for(const key of Object.keys(FERR)) delete FERR[key];
      if(saved){ markSaved(); hideSaveProblem(); }
      else showSaveProblem(data.state_error||'unknown error',true);
      const kept=data.kept_modified||[], renamedIn=data.renamed_in||[];
      const updatedIn=data.updated_in||[], warnings=data.warnings||[];
      const written=(data.packages||[]).reduce((n,r)=>n+r.written.length,0);
      let msg=kept.length
        ? `${written} file(s) written → ${data.output_dir}. Kept your edits in ${kept.length} file(s): ${kept.join(', ')}`
        : `${written} file(s) written, ${data.files.length-written} unchanged → ${data.output_dir}`;
      if(renamedIn.length) msg+=`. Updated the package name in your edited file(s): ${renamedIn.join(', ')}`;
      if(updatedIn.length) msg+=`. In your edited files: ${updatedIn.join('; ')}`;
      if(warnings.length) msg+=`. Check: ${warnings.join(' ')}`;
      if(!saved) showToast(`⚠ ${msg}. The design itself was NOT saved; see the banner.`,'err');
      else showToast((warnings.length?'⚠ ':'✓ ')+msg, warnings.length?'warn':'ok');
    } else {
      if(data.state_saved===false) showSaveProblem(data.state_error||'unknown error',true);
      if(res.status===422) applyFieldErrors(data,payload);
      showToast(`✗ ${errorText(data,payload)}`,'err');
    }
  }catch(err){
    showToast(`✗ ${err.message}`,'err');
  }finally{
    setBusy(GB,false,'Generate All');
    renderAll();
  }
});

document.getElementById('an').addEventListener('click', addNode);
document.getElementById('fit').addEventListener('click', showAll);
document.getElementById('sv').addEventListener('click', saveState);
document.addEventListener('keydown', e=>{
  if((e.ctrlKey||e.metaKey)&&e.key==='s'){ e.preventDefault(); saveState(); }
  else if(e.key==='Escape'&&S.sel) closeSB();
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
  markSaved();
})();
</script>
</body>
</html>"""


def _script_json(value: object) -> str:
    """JSON that is safe inside an inline <script> block."""
    return json_for_script(value, compact=False)


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
