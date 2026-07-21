from __future__ import annotations

import html as _html
import json
from importlib.resources import files
from pathlib import Path
from typing import Any

from ros2inspector.graph.renderer import _subgraph
from ros2inspector.model.uam import UAM


def _cytoscape_script_tag() -> str:
    """Return the bundled Cytoscape.js asset as an inline, offline script."""
    try:
        source = (
            files("ros2inspector.viz").joinpath("_cytoscape.min.js").read_text(encoding="utf-8")
        )
    except (FileNotFoundError, OSError) as exc:
        raise RuntimeError(
            "The bundled Cytoscape visualization asset is missing from this installation. "
            "Reinstall ros2inspector from a complete wheel."
        ) from exc
    return f"<script>{source}</script>"


# ---------------------------------------------------------------------------
# Graph data builder
# ---------------------------------------------------------------------------

_CY_EDGE_RESERVED = {"id", "source", "target"}  # Cytoscape reserved keys for edges


def _safe(v: object) -> object:
    """Ensure a node/edge attribute is JSON-serializable."""
    if isinstance(v, list):
        return ", ".join(str(x) for x in v)
    if isinstance(v, (str, int, float, bool)) or v is None:
        return v
    return str(v)


def _cy_elements(uam: UAM, graph_type: str) -> dict[str, Any]:
    sub = _subgraph(uam, graph_type, None)
    g = uam.graph

    if graph_type not in ("comms", "full"):
        # deps view: flat nodes, no compound grouping
        nodes = []
        for nid, attrs in sub.nodes(data=True):
            node_data: dict[str, object] = {"id": nid, "label": attrs.get("name", nid)}
            for k, v in attrs.items():
                if k != "name":
                    node_data[k] = _safe(v)
            nodes.append({"data": node_data})
        edges = []
        for i, (src, dst, attrs) in enumerate(sub.edges(data=True)):
            d = {"id": f"e{i}", "source": src, "target": dst}
            for k, v in attrs.items():
                if k not in _CY_EDGE_RESERVED:
                    d[k] = _safe(v)
            edges.append({"data": d})
        return {"nodes": nodes, "edges": edges}

    # ── comms / full: 3-level compound hierarchy ───────────────────────────
    # Level 1: Package  (big circle)
    # Level 2: ROS Node (medium circle, inside Package)
    # Level 3: Topic / Service / Action / Interface (leaves, inside ROS Node)
    #
    # Topics/services go inside the ROS node that PUBLISHES / PROVIDES them.
    # If no publisher exists they float as orphans (no parent).
    # Interfaces go inside their package.

    # Build: which ROS node is the publisher/provider of each comm element
    publisher_of: dict[str, str] = {}  # topic_id / svc_id → ros_node_id
    for src, dst, edata in sub.edges(data=True):
        if edata.get("rel") in ("publishes", "provides") and dst not in publisher_of:
            if sub.nodes[src].get("kind") == "Node":
                publisher_of[dst] = src

    seen_pkgs: set[str] = set()
    nodes = []

    def _inject_pkg(pkg_name: str) -> None:
        pkg_id = f"pkg:{pkg_name}"
        if pkg_id in seen_pkgs:
            return
        seen_pkgs.add(pkg_id)
        pkg_attrs = g.nodes.get(pkg_id, {})
        nodes.append(
            {
                "data": {
                    "id": pkg_id,
                    "label": pkg_name,
                    "kind": "Package",
                    "name": pkg_name,
                    **{k: _safe(v) for k, v in pkg_attrs.items() if k not in ("name",)},
                }
            }
        )

    for nid, attrs in sub.nodes(data=True):
        kind = attrs.get("kind", "")
        raw_name = str(attrs.get("name", nid))
        # Preserve exact ROS names (including a leading slash). For very deep
        # names, keep the final two segments while retaining absolute-name form.
        short = raw_name
        if kind in ("Topic", "Service", "Action"):
            body = raw_name.lstrip("/")
            if body.count("/") > 1:
                body = "/".join(body.rsplit("/", 2)[-2:])
                short = f"/{body}" if raw_name.startswith("/") else body
        data: dict[str, object] = {"id": nid, "label": short}
        for k, v in attrs.items():
            if k != "name":
                data[k] = _safe(v)

        if kind == "Node":
            pkg = str(attrs.get("package", ""))
            if pkg:
                _inject_pkg(pkg)
                data["parent"] = f"pkg:{pkg}"

        elif kind in ("Topic", "Service", "Action"):
            if nid in publisher_of:
                data["parent"] = publisher_of[nid]  # inside publisher's ROS node
            else:
                # find the subscriber's package and put topic there
                for src, _, edata in sub.in_edges(nid, data=True):
                    if edata.get("rel") in ("subscribes", "calls"):
                        pkg = str(sub.nodes[src].get("package", ""))
                        if pkg:
                            _inject_pkg(pkg)
                            data["parent"] = f"pkg:{pkg}"
                            break

        elif kind == "Interface":
            pkg = str(attrs.get("package", ""))
            if pkg:
                _inject_pkg(pkg)
                data["parent"] = f"pkg:{pkg}"

        nodes.append({"data": data})

    edges = []
    for i, (src, dst, attrs) in enumerate(sub.edges(data=True)):
        # Skip defined_in / uses_interface — the compound nesting already encodes this
        if attrs.get("rel") in ("defined_in", "uses_interface"):
            continue
        d = {"id": f"e{i}", "source": src, "target": dst}
        for k, v in attrs.items():
            if k not in _CY_EDGE_RESERVED:
                d[k] = _safe(v)
        edges.append({"data": d})

    return {"nodes": nodes, "edges": edges}


# ---------------------------------------------------------------------------
# HTML generator
# ---------------------------------------------------------------------------


def generate_html(uam: UAM, workspace_root: Path) -> str:
    graphs = {
        "deps": _cy_elements(uam, "deps"),
        "comms": _cy_elements(uam, "comms"),
        "full": _cy_elements(uam, "full"),
    }
    summary = uam.summary()

    # Replace </ with <\/ so that node names containing "</script>" cannot
    # break out of the embedded <script> block in the generated HTML.
    graphs_json = json.dumps(graphs, separators=(",", ":")).replace("</", "<\\/")
    summary_json = json.dumps(summary, separators=(",", ":")).replace("</", "<\\/")
    # HTML-escape the path so characters like <, >, & in directory names cannot
    # break the generated markup (e.g. in <title> and the workspace <div>).
    workspace_html = _html.escape(str(workspace_root))

    return (
        _TEMPLATE.replace("__CYTOSCAPE_SCRIPT_TAG__", _cytoscape_script_tag())
        .replace("__GRAPHS_JSON__", graphs_json)
        .replace("__SUMMARY_JSON__", summary_json)
        .replace("__WORKSPACE__", workspace_html)
    )


# ---------------------------------------------------------------------------
# HTML template
# ---------------------------------------------------------------------------

_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ROS2 Inspector — __WORKSPACE__</title>
__CYTOSCAPE_SCRIPT_TAG__
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Segoe UI',system-ui,sans-serif;background:#0d1117;color:#c9d1d9;display:flex;flex-direction:column;height:100vh;overflow:hidden}

/* ── header ── */
#hdr{height:52px;background:#161b22;border-bottom:1px solid #30363d;display:flex;align-items:center;padding:0 14px;gap:12px;flex-shrink:0;overflow:hidden}
#brand{font-weight:700;font-size:15px;color:#58a6ff;white-space:nowrap}
#ws{font-size:11px;color:#8b949e;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}

.tab-group{display:flex;gap:2px;background:#0d1117;border-radius:6px;padding:3px;flex-shrink:0}
.tab{padding:4px 12px;border:none;background:transparent;color:#8b949e;border-radius:4px;cursor:pointer;font-size:13px;font-weight:500;transition:all .15s}
.tab:hover{background:#21262d;color:#c9d1d9}
.tab.on{background:#388bfd1a;color:#58a6ff}

select,button.btn{padding:5px 9px;background:#21262d;border:1px solid #30363d;color:#c9d1d9;border-radius:6px;cursor:pointer;font-size:12px;transition:all .15s}
select:hover,button.btn:hover{background:#30363d;border-color:#58a6ff;outline:none}
select:focus{outline:none;border-color:#388bfd}

/* ── body ── */
#main{display:flex;flex:1;overflow:hidden}

/* ── sidebar ── */
#sb{width:230px;background:#161b22;border-right:1px solid #30363d;display:flex;flex-direction:column;overflow-y:auto;flex-shrink:0}
.sec{padding:10px 12px;border-bottom:1px solid #21262d}
.sec-title{font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.08em;color:#8b949e;margin-bottom:7px}

#search{width:100%;padding:5px 9px;background:#0d1117;border:1px solid #30363d;color:#c9d1d9;border-radius:6px;font-size:12px}
#search:focus{outline:none;border-color:#388bfd}

.frow{display:flex;align-items:center;gap:7px;padding:2px 0;cursor:pointer;font-size:12px;user-select:none}
.frow input{cursor:pointer;accent-color:#58a6ff;flex-shrink:0}
.dot{width:9px;height:9px;border-radius:50%;display:inline-block;flex-shrink:0}
.sq{width:9px;height:9px;border-radius:2px;display:inline-block;flex-shrink:0}

.srow{display:flex;justify-content:space-between;font-size:12px;padding:2px 0}
.sv{font-weight:600;color:#58a6ff}

/* ── details ── */
#det{flex:1;padding:10px 12px;overflow-y:auto}
.det-title{font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.08em;color:#8b949e;margin-bottom:7px}
#det-empty{color:#8b949e;font-size:12px;font-style:italic}
#det-body{display:none}
#det-name{font-weight:700;font-size:14px;margin-bottom:2px;word-break:break-all}
#det-kind{font-size:11px;color:#8b949e;margin-bottom:8px}
.drow{font-size:12px;padding:3px 0;border-top:1px solid #21262d}
.dk{color:#8b949e;margin-bottom:1px}
.dv{color:#c9d1d9;word-break:break-all}

/* ── canvas ── */
#cy{flex:1;background:#0d1117}

/* ── tooltip ── */
#tip{position:fixed;background:#1c2128;border:1px solid #30363d;border-radius:6px;padding:6px 10px;font-size:12px;pointer-events:none;display:none;z-index:999;max-width:260px;line-height:1.4}
</style>
</head>
<body>

<div id="hdr">
  <div id="brand">🔬 ROS2 Inspector</div>
  <div id="ws">__WORKSPACE__</div>

  <div class="tab-group">
    <button class="tab on" data-g="deps">Deps</button>
    <button class="tab" data-g="comms">Comms</button>
    <button class="tab" data-g="full">Full</button>
  </div>

  <select id="lay-sel">
    <option value="compound">📦 Grouped</option>
    <option value="cose">⚛ Force</option>
    <option value="breadthfirst" selected>⬇ Hierarchy</option>
    <option value="circle">⭕ Circle</option>
    <option value="concentric">🎯 Concentric</option>
    <option value="grid">▦ Grid</option>
  </select>
  <button class="btn" id="fit-btn">⊡ Fit</button>
  <button class="btn" id="png-btn">📷 PNG</button>
</div>

<div id="main">
  <div id="sb">

    <div class="sec">
      <div class="sec-title">Search</div>
      <input id="search" type="text" placeholder="Filter by name…">
    </div>

    <div class="sec">
      <div class="sec-title">Node types</div>
      <label class="frow"><input type="checkbox" data-kind="Package"   checked><span class="sq" style="background:#1d3a6e;border:1px solid #3a86ff"></span>Package</label>
      <label class="frow"><input type="checkbox" data-kind="Node"      checked><span class="sq" style="background:#6e3a1d;border:1px solid #ff8c42"></span>ROS Node</label>
      <label class="frow"><input type="checkbox" data-kind="Topic"     checked><span class="dot" style="background:#06d6a0"></span>Topic</label>
      <label class="frow"><input type="checkbox" data-kind="Service"   checked><span class="dot" style="background:#c77dff"></span>Service</label>
      <label class="frow"><input type="checkbox" data-kind="Action"    checked><span class="dot" style="background:#f72585"></span>Action</label>
      <label class="frow"><input type="checkbox" data-kind="Interface" checked><span class="sq" style="background:#2d3748;border:1px solid #adb5bd"></span>Interface</label>
    </div>

    <div class="sec">
      <div class="sec-title">Edge types</div>
      <label class="frow"><input type="checkbox" data-rel="publishes"      checked><span style="color:#06d6a0">→</span>Publishes</label>
      <label class="frow"><input type="checkbox" data-rel="subscribes"     checked><span style="color:#3a86ff">→</span>Subscribes</label>
      <label class="frow"><input type="checkbox" data-rel="depends_on"     checked><span style="color:#8b949e">⤍</span>Depends on</label>
      <label class="frow"><input type="checkbox" data-rel="provides"       checked><span style="color:#c77dff">→</span>Provides</label>
      <label class="frow"><input type="checkbox" data-rel="calls"          checked><span style="color:#c77dff">⤍</span>Calls</label>
      <label class="frow"><input type="checkbox" data-rel="defined_in"          ><span style="color:#444">→</span>Defined in</label>
      <label class="frow"><input type="checkbox" data-rel="uses_interface"      ><span style="color:#555">⤍</span>Uses interface</label>
    </div>

    <div class="sec">
      <div class="sec-title">Stats</div>
      <div id="stats"></div>
    </div>

    <div id="det">
      <div class="det-title">Node details</div>
      <div id="det-empty">Click a node to inspect</div>
      <div id="det-body"></div>
    </div>

  </div><!-- /sb -->

  <div id="cy"></div>
</div>

<div id="tip"></div>

<script>
const GRAPHS  = __GRAPHS_JSON__;
const SUMMARY = __SUMMARY_JSON__;

// ── styles config ──────────────────────────────────────────────────────────
const K = {
  Package:   {bg:'#0d2045', border:'#3a86ff', shape:'rectangle',      tc:'#90c0ff'},
  Node:      {bg:'#3d1f00', border:'#ff8c42', shape:'roundrectangle', tc:'#ffb380'},
  Topic:     {bg:'#003d2a', border:'#06d6a0', shape:'ellipse',        tc:'#7fffda'},
  Service:   {bg:'#2d0d4e', border:'#c77dff', shape:'diamond',        tc:'#dbb0ff'},
  Action:    {bg:'#4e0020', border:'#f72585', shape:'hexagon',        tc:'#ff80b8'},
  Interface: {bg:'#1c2128', border:'#adb5bd', shape:'cut-rectangle',  tc:'#c9d1d9'},
};
const E = {
  publishes:      {c:'#06d6a0', dash:false},
  subscribes:     {c:'#3a86ff', dash:false},
  depends_on:     {c:'#555',    dash:true },
  provides:       {c:'#c77dff', dash:false},
  calls:          {c:'#c77dff', dash:true },
  defined_in:     {c:'#2a2a2a', dash:false},
  uses_interface: {c:'#333',    dash:true },
};

// ── Cytoscape stylesheet ───────────────────────────────────────────────────
function mkStyle() {
  const s = [
    // leaf nodes (topics, services, actions, interfaces)
    { selector:'node:childless', style:{
        label:'data(label)', 'font-size':'9px', color:'#c9d1d9',
        'text-valign':'center','text-halign':'center',
        'text-wrap':'wrap','text-max-width':'75px',
        'border-width':1.5, padding:'4px',
        'width':'label','height':'label',
      }
    },
    // Level-1 container: Package (rounded rect, dashed blue border)
    { selector:'node[kind="Package"]:parent', style:{
        'background-opacity':.06,
        'background-color':'#0d2045',
        'border-color':'#3a86ff',
        'border-width':2,
        'border-style':'dashed',
        'label':'data(label)',
        'text-valign':'top',
        'text-halign':'center',
        'font-size':'11px',
        'font-weight':'700',
        'color':'#58a6ff',
        'padding':'10px',
        'shape':'round-rectangle',
      }
    },
    // Level-1 Package as leaf (deps view)
    { selector:'node[kind="Package"]:childless', style:{
        'background-color':'#0d2045','border-color':'#3a86ff',
        shape:'rectangle',color:'#90c0ff',
        'font-size':'11px',padding:'6px',
      }
    },
    // Level-2 container: ROS Node (ellipse, orange border)
    { selector:'node[kind="Node"]:parent', style:{
        'background-opacity':.14,
        'background-color':'#3d1f00',
        'border-color':'#ff8c42',
        'border-width':2,
        'border-style':'solid',
        'label':'data(label)',
        'text-valign':'top',
        'text-halign':'center',
        'font-size':'10px',
        'font-weight':'700',
        'color':'#ffb380',
        'padding':'6px',
        'text-margin-y':'9px',
        'shape':'ellipse',
      }
    },
    // Level-2 ROS Node as leaf (no children)
    { selector:'node[kind="Node"]:childless', style:{
        'background-color':'#3d1f00','border-color':'#ff8c42',
        shape:'roundrectangle',color:'#ffb380',
        'font-size':'10px',padding:'5px',
      }
    },
    { selector:'edge', style:{
        width:1.5,'target-arrow-shape':'triangle','arrow-scale':1.1,
        'curve-style':'bezier',
        opacity:.78,
      }
    },
    { selector:'edge.cross-pkg', style:{
        width:2, opacity:.9,
      }
    },
    { selector:'node:selected', style:{
        'border-width':3,'overlay-color':'#fff',
        'overlay-padding':4,'overlay-opacity':.12,
      }
    },
    { selector:'.faded', style:{opacity:.07} },
    { selector:'.hidden', style:{display:'none'} },
    { selector:'edge:selected', style:{
        width:3, label:'data(rel)', 'font-size':'10px',
        'text-background-color':'#0d1117','text-background-opacity':.85,
        'text-background-padding':'2px', color:'#c9d1d9',
      }
    },
  ];
  // Leaf node colours per kind (Topics, Services, Actions, Interfaces)
  for(const [kind, v] of Object.entries(K)){
    if(kind==='Package'||kind==='Node') continue;
    s.push({selector:`node[kind="${kind}"]:childless`, style:{
      'background-color':v.bg,'border-color':v.border,
      shape:v.shape,color:v.tc,
    }});
  }
  for(const [rel, v] of Object.entries(E)){
    s.push({selector:`edge[rel="${rel}"]`, style:{
      'line-color':v.c,'target-arrow-color':v.c,
      'line-style':v.dash?'dashed':'solid',
    }});
  }
  return s;
}

// ── layout configs ─────────────────────────────────────────────────────────
const LAYOUTS = {
  cose:{name:'cose',animate:true,animationDuration:800,
    nodeRepulsion:80000,idealEdgeLength:180,edgeElasticity:.25,
    gravity:.05,numIter:3000,randomize:true,componentSpacing:100,
    nestingFactor:.1,coolingFactor:.97},
  breadthfirst:{name:'breadthfirst',animate:true,animationDuration:450,
    directed:true,spacingFactor:1.6,padding:30},
  circle:{name:'circle',animate:true,animationDuration:400,spacingFactor:1.5},
  concentric:{name:'concentric',animate:true,animationDuration:500,
    concentric:n=>n.degree(),levelWidth:()=>3,spacingFactor:2.0,
    minNodeSpacing:40,startAngle:Math.PI*3/2},
  grid:{name:'grid',animate:true,animationDuration:400,spacingFactor:1.5},
};

// best default layout per graph type
const GRAPH_DEFAULT_LAYOUT = {deps:'breadthfirst', comms:'compound', full:'compound'};

// ── state ──────────────────────────────────────────────────────────────────
let cy, curGraph='deps', curLayout='breadthfirst';

// ── manual compound preset layout ─────────────────────────────────────────
// Sizes derived from actual content. Packages row-packed by area so no
// row has wasted whitespace from a rigid column grid.
function runCompoundPreset(){
  const LEAF_W   = 70;   // estimated leaf node width (px)
  const LEAF_H   = 18;   // estimated leaf node height (px)
  const LEAF_GAP = 6;    // gap between leaves on the arc
  const NODE_PAD = 8;    // padding inside each ROS-node ellipse
  const NODE_GAP = 18;   // gap between sibling node ellipses
  const PKG_PAD  = 8;    // padding inside each package rectangle
  const PKG_GAP  = 50;   // gap between packages
  const PKG_LBL  = 18;   // vertical space reserved for package label
  const DL_W     = 78;   // width of direct-leaf (orphan topic) column
  const DL_H     = 22;   // row height for each direct leaf

  // ── node geometry: circle radius so N leaves fit without overlap ──────
  function nodeGeom(rn){
    const leaves = rn.children();
    const lc = leaves.length;
    if(lc === 0) return {rn, leaves, lc, r:0, ew:70, eh:36};
    const r  = Math.max(LEAF_H*2+10, (lc*(LEAF_W+LEAF_GAP))/(2*Math.PI));
    const d  = r*2 + LEAF_W + NODE_PAD*2;
    return {rn, leaves, lc, r, ew:d, eh:d};
  }

  // ── package geometry: tight grid of node ellipses + orphan leaf column ─
  function pkgGeom(pkg){
    const nms = pkg.children('[kind="Node"]').map(rn => nodeGeom(rn));
    const dls = pkg.children('[kind!="Node"]:childless');
    if(!nms.length && !dls.length) return {pkg, nms, dls, nodeRows:[], w:80, h:40+PKG_LBL};

    const nc = Math.max(1, Math.ceil(Math.sqrt(nms.length)));
    const nodeRows = [];
    for(let i=0; i<nms.length; i+=nc){
      const seg = nms.slice(i, i+nc);
      nodeRows.push({ items:seg, h:Math.max(...seg.map(m=>m.eh)), ws:seg.map(m=>m.ew) });
    }
    const gridW = nodeRows.length
      ? Math.max(...nodeRows.map(r=>r.ws.reduce((a,b)=>a+b,0)+(r.ws.length-1)*NODE_GAP))
      : 0;
    const gridH = nodeRows.reduce((s,r)=>s+r.h,0) + Math.max(0,nodeRows.length-1)*NODE_GAP;
    const dlColW = dls.length ? DL_W+NODE_GAP : 0;
    return {
      pkg, nms, dls, nodeRows,
      w: Math.max(gridW+dlColW, dls.length?DL_W:0) + PKG_PAD*2,
      h: Math.max(gridH, dls.length*DL_H) + PKG_PAD*2 + PKG_LBL,
    };
  }

  const pkgs = cy.nodes('[kind="Package"]:parent');
  if(!pkgs.length){ cy.fit(15); return; }
  const allMeta = pkgs.map(p => pkgGeom(p));

  // ── row-pack: sort largest first, fill rows to target width ───────────
  allMeta.sort((a,b) => (b.w*b.h)-(a.w*a.h));
  const totalArea = allMeta.reduce((s,m)=>(s+(m.w+PKG_GAP)*(m.h+PKG_GAP)),0);
  const targetW   = Math.sqrt(totalArea)*1.4;

  const pkgRows = [];
  let cur=[], curW=0;
  allMeta.forEach(m=>{
    if(cur.length && curW+m.w+PKG_GAP > targetW){ pkgRows.push(cur); cur=[]; curW=0; }
    cur.push(m); curW+=m.w+PKG_GAP;
  });
  if(cur.length) pkgRows.push(cur);

  // ── assign leaf positions ─────────────────────────────────────────────
  const positions = {};
  let baseY = 0;

  pkgRows.forEach(pkgRow => {
    const pkgRowH = Math.max(...pkgRow.map(m=>m.h));
    let baseX = 0;

    pkgRow.forEach(pm => {
      const ox = baseX + PKG_PAD;      // content left edge
      const oy = baseY + PKG_PAD + PKG_LBL; // content top edge

      // Position each node ellipse using per-nodeRow grid
      let ny = oy;
      pm.nodeRows.forEach(nr => {
        let nx = ox;
        nr.items.forEach(nm => {
          const cx = nx + nm.ew/2;
          const cy_ = ny + nr.h/2;
          if(nm.lc === 0){
            positions[nm.rn.id()] = {x:cx, y:cy_};
          } else {
            nm.leaves.forEach((leaf,li)=>{
              const a = (2*Math.PI*li/nm.lc);
              positions[leaf.id()] = {x: cx+nm.r*Math.cos(a), y: cy_+nm.r*Math.sin(a)};
            });
          }
          nx += nm.ew + NODE_GAP;
        });
        ny += nr.h + NODE_GAP;
      });

      // Direct-leaf column: right of the node grid
      const gridW = pm.nodeRows.length
        ? Math.max(...pm.nodeRows.map(r=>r.ws.reduce((a,b)=>a+b,0)+(r.ws.length-1)*NODE_GAP))
        : 0;
      const dlX = ox + gridW + (pm.nodeRows.length ? NODE_GAP : 0) + DL_W/2;
      pm.dls.forEach((leaf,li)=>{
        positions[leaf.id()] = {x: dlX, y: oy + li*DL_H + DL_H/2};
      });

      baseX += pm.w + PKG_GAP;
    });

    baseY += pkgRowH + PKG_GAP;
  });

  cy.layout({
    name:'preset',
    positions: n => positions[n.id()],
    animate:true, animationDuration:400,
    fit:true, padding:15,
    stop:afterLayout,
  }).run();
}

// ── mathematical edge routing (runs after every layout) ────────────────────
// For each source-target pair we compute the exact perpendicular offset needed
// to clear both compound containers. Parallel edges on the same pair are
// distributed on alternating sides so they never stack.
function routeEdges(){
  // Group edges by unordered source-target id pair
  const pairs = {};
  cy.edges(':visible').forEach(e => {
    const key = [e.source().id(), e.target().id()].sort().join('\x00');
    (pairs[key] = pairs[key]||[]).push(e);
  });

  Object.values(pairs).forEach(group => {
    const n = group.length;
    group.forEach((edge, i) => {
      const src = edge.source();
      const tgt = edge.target();

      // Self-loop: leave Cytoscape default
      if(src.id() === tgt.id()) return;

      const sp = src.position();
      const tp = tgt.position();

      // Direction vector and its perpendicular
      const dx = tp.x - sp.x, dy = tp.y - sp.y;
      const len = Math.hypot(dx, dy);
      if(len < 1) return;

      const srcPkg = src.ancestors('[kind="Package"]').first();
      const tgtPkg = tgt.ancestors('[kind="Package"]').first();
      const crossPkg = srcPkg.nonempty() && tgtPkg.nonempty() && srcPkg.id() !== tgtPkg.id();

      // Minimum clearance = enough to arc outside the largest container involved
      let baseDist;
      if(crossPkg){
        const sb = srcPkg.boundingBox(), tb = tgtPkg.boundingBox();
        // Diagonal of each package is the worst-case extent we must clear.
        // The bezier midpoint sits at: midLine + 0.5 * controlPointOffset
        // so controlPointOffset = 2 * required_midpoint_clearance.
        // We want the midpoint of the curve to sit outside both bounding boxes.
        // Required clearance from the source-target midpoint to the nearest
        // package edge = half the diagonal of the larger package + margin.
        const diagSrc = Math.hypot(sb.w, sb.h);
        const diagTgt = Math.hypot(tb.w, tb.h);
        baseDist = Math.max(diagSrc, diagTgt) * 0.55 + 30;
      } else {
        // Within same package or no package: small default separation
        baseDist = 25;
      }

      // Distribute parallel edges: alternating sign, increasing offset per pair
      // i=0 → +base, i=1 → -base, i=2 → +base+step, i=3 → -base+step …
      const sign  = (i % 2 === 0) ? 1 : -1;
      const extra = Math.floor(i / 2) * 20;
      const dist  = sign * (baseDist + extra);

      edge.style('control-point-distance', dist);
    });
  });
}

// ── helpers ────────────────────────────────────────────────────────────────
function applyFilters(){
  cy.elements().removeClass('hidden');
  document.querySelectorAll('[data-kind]').forEach(cb=>{
    if(!cb.checked){
      // Never hide compound parent nodes — they are package containers
      cy.nodes(`[kind="${cb.dataset.kind}"]`).filter(n=>!n.isParent()).addClass('hidden');
    }
  });
  document.querySelectorAll('[data-rel]').forEach(cb=>{
    if(!cb.checked) cy.edges(`[rel="${cb.dataset.rel}"]`).addClass('hidden');
  });
}

function refreshStats(){
  const counts={};
  cy.nodes('.hidden').length; // touch
  cy.nodes(':visible').forEach(n=>{
    const k=n.data('kind')||'?';
    counts[k]=(counts[k]||0)+1;
  });
  document.getElementById('stats').innerHTML=
    Object.entries(counts).sort(([a],[b])=>a.localeCompare(b))
    .map(([k,v])=>`<div class="srow"><span class="dk">${k}</span><span class="sv">${v}</span></div>`)
    .join('');
}

function afterLayout(){
  cy.fit(48);
  routeEdges();
}

function runLayout(){
  if(curLayout === 'compound'){ runCompoundPreset(); return; }
  // Pre-scatter leaf nodes so physics layouts never start from origin
  const w=cy.width()||1000, h=cy.height()||800;
  cy.nodes(':childless').positions(()=>({
    x: w*0.1 + Math.random()*w*0.8,
    y: h*0.1 + Math.random()*h*0.8,
  }));
  const cfg=Object.assign({},LAYOUTS[curLayout],{stop:afterLayout});
  cy.layout(cfg).run();
}

function loadGraph(g){
  curGraph=g;
  // pick the best layout for this graph type and sync dropdown
  curLayout=GRAPH_DEFAULT_LAYOUT[g]||'concentric';
  document.getElementById('lay-sel').value=curLayout;
  cy.elements().remove();
  const d=GRAPHS[g];
  cy.add([...d.nodes,...d.edges]);
  // Tag edges that cross package boundaries for special curve styling
  cy.edges().forEach(e=>{
    const sp=e.source().ancestors('[kind="Package"]').first();
    const tp=e.target().ancestors('[kind="Package"]').first();
    if(sp.nonempty()&&tp.nonempty()&&sp.id()!==tp.id()) e.addClass('cross-pkg');
  });
  applyFilters();
  runLayout();
  refreshStats();
  clearDetails();
}

function clearDetails(){
  cy.elements().removeClass('faded');
  document.getElementById('det-empty').style.display='';
  document.getElementById('det-body').style.display='none';
}

function _esc(s){
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}
function _renderRows(d){
  const skip=new Set(['id','label','parent','is_container']);
  return Object.entries(d)
    .filter(([k,val])=>!skip.has(k)&&val!=null&&val!==''&&val!==false)
    .map(([k,val])=>`<div class="drow"><div class="dk">${_esc(k)}</div><div class="dv">${_esc(val)}</div></div>`)
    .join('');
}

function showDetails(node){
  if(node.isParent()&&node.data('kind')==='Package') return; // don't inspect container frames
  const d=node.data();
  const v=K[d.kind]||{border:'#fff'};
  document.getElementById('det-empty').style.display='none';
  document.getElementById('det-body').style.display='block';
  document.getElementById('det-body').innerHTML=
    `<div id="det-name" style="color:${v.border}">${_esc(d.label)}</div>`+
    `<div id="det-kind">${_esc(d.kind||'')}</div>`+_renderRows(d);
  cy.elements().addClass('faded');
  node.removeClass('faded');
  node.ancestors().removeClass('faded');   // keep parent containers visible
  node.neighborhood().removeClass('faded');
  node.neighborhood().ancestors().removeClass('faded');
}

function showEdgeDetails(edge){
  const d=edge.data();
  const src=edge.source(), tgt=edge.target();
  const relColor=(E[d.rel]||{c:'#fff'}).c;
  const srcD=src.data(), tgtD=tgt.data();
  const srcV=K[srcD.kind]||{border:'#aaa'}, tgtV=K[tgtD.kind]||{border:'#aaa'};

  document.getElementById('det-empty').style.display='none';
  document.getElementById('det-body').style.display='block';
  document.getElementById('det-body').innerHTML=
    `<div id="det-name" style="color:${relColor}">${_esc(d.rel||'edge')}</div>`+
    `<div id="det-kind" style="margin-bottom:10px">connection</div>`+
    `<div class="drow"><div class="dk">from</div>`+
      `<div class="dv" style="color:${srcV.border}">${_esc(srcD.label)}</div></div>`+
    `<div class="drow"><div class="dk">to</div>`+
      `<div class="dv" style="color:${tgtV.border}">${_esc(tgtD.label)}</div></div>`+
    (srcD.package||tgtD.package?
      `<div class="drow"><div class="dk">src pkg</div><div class="dv">${_esc(srcD.package||'—')}</div></div>`+
      `<div class="drow"><div class="dk">dst pkg</div><div class="dv">${_esc(tgtD.package||tgt.ancestors('[kind="Package"]').first().data('label')||'—')}</div></div>`
    :'')+
    _renderRows(d);

  // Fade everything; keep source + target + all their ancestor containers + the edge
  cy.elements().addClass('faded');
  const keep=src.union(src.ancestors()).union(tgt).union(tgt.ancestors()).union(edge);
  keep.removeClass('faded');
}

// ── init ───────────────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded',()=>{
  cy=cytoscape({
    container:document.getElementById('cy'),
    style:mkStyle(),
    elements:[],
    wheelSensitivity:.3,
    minZoom:.005, maxZoom:8,
  });

  loadGraph('deps');

  // tab switching
  document.querySelectorAll('.tab').forEach(b=>{
    b.addEventListener('click',()=>{
      document.querySelectorAll('.tab').forEach(x=>x.classList.remove('on'));
      b.classList.add('on');
      loadGraph(b.dataset.g);
    });
  });

  // layout
  document.getElementById('lay-sel').addEventListener('change',e=>{
    curLayout=e.target.value;
    runLayout();
  });

  // fit
  document.getElementById('fit-btn').addEventListener('click',()=>cy.fit(48));

  // PNG export
  document.getElementById('png-btn').addEventListener('click',()=>{
    const blob=cy.png({output:'blob',scale:2,bg:'#0d1117'});
    const url=URL.createObjectURL(blob);
    const a=document.createElement('a');
    a.href=url; a.download=`ros2_${curGraph}.png`; a.click();
    setTimeout(()=>URL.revokeObjectURL(url),2000);
  });

  // filters
  document.querySelectorAll('[data-kind],[data-rel]').forEach(cb=>{
    cb.addEventListener('change',()=>{applyFilters();refreshStats();});
  });

  // search
  document.getElementById('search').addEventListener('input',e=>{
    const q=e.target.value.trim().toLowerCase();
    if(!q){cy.elements().removeClass('faded');return;}
    cy.elements().addClass('faded');
    const hits=cy.nodes().filter(n=>n.data('label').toLowerCase().includes(q));
    hits.removeClass('faded');
    hits.neighborhood().removeClass('faded');
  });

  // node click
  cy.on('tap','node',e=>showDetails(e.target));
  // edge click — highlight only the two endpoints and their package containers
  cy.on('tap','edge',e=>showEdgeDetails(e.target));
  cy.on('tap',e=>{ if(e.target===cy) clearDetails(); });

  // tooltip
  const tip=document.getElementById('tip');
  cy.on('mouseover','node',e=>{
    const d=e.target.data();
    if(e.target.isParent()) return;
    tip.innerHTML=`<strong>${_esc(d.label)}</strong><br><span style="color:#8b949e">${_esc(d.kind||'')}</span>`;
    tip.style.display='block';
  });
  cy.on('mousemove',e=>{
    tip.style.left=(e.originalEvent.clientX+14)+'px';
    tip.style.top=(e.originalEvent.clientY+10)+'px';
  });
  cy.on('mouseout','node',()=>tip.style.display='none');
  cy.on('mouseover','edge',e=>{
    const edge=e.target, d=edge.data();
    const relColor=(E[d.rel]||{c:'#8b949e'}).c;
    const sp=edge.source().ancestors('[kind="Package"]').first().data('label')||'';
    const tp=edge.target().ancestors('[kind="Package"]').first().data('label')||'';
    const pkgInfo=sp&&tp&&sp!==tp?`<br><span style="color:#555">${_esc(sp)} → ${_esc(tp)}</span>`:'';
    tip.innerHTML=`<span style="color:${relColor}">${_esc(d.rel||'edge')}</span>`+
      `<br><span style="color:#8b949e;font-size:11px">${_esc(edge.source().data('label'))} → ${_esc(edge.target().data('label'))}</span>`+pkgInfo;
    tip.style.display='block';
  });
  cy.on('mouseout','edge',()=>tip.style.display='none');
});
</script>
</body>
</html>
"""
