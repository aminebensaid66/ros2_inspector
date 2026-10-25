from __future__ import annotations

import html as _html
import json
import re
from importlib.resources import files
from pathlib import Path
from typing import Any

from ros2inspector.graph.renderer import _subgraph, flow_direction
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


def _short_endpoint_labels(sub: Any) -> dict[str, str]:
    """Shorten deep topic/service/action names to their last two segments, as '…/a/b'.

    A name is only shortened when no other endpoint of the same kind would get the same
    label, so '/robot1/front/scan' and '/robot2/front/scan' are never both shown as one.
    """
    candidates: dict[str, tuple[str, str]] = {}
    for nid, attrs in sub.nodes(data=True):
        kind = str(attrs.get("kind", ""))
        name = str(attrs.get("name", nid))
        if kind in ("Topic", "Service", "Action") and name.lstrip("/").count("/") > 1:
            candidates[nid] = (kind, "…/" + "/".join(name.rsplit("/", 2)[-2:]))
    names: dict[tuple[str, str], set[str]] = {}
    for nid, attrs in sub.nodes(data=True):
        key = candidates.get(nid, (str(attrs.get("kind", "")), str(attrs.get("name", nid))))
        names.setdefault(key, set()).add(str(attrs.get("name", nid)))
    return {nid: key[1] for nid, key in candidates.items() if len(names[key]) == 1}


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

    # ── comms / full: package/node compound hierarchy ─────────────────────
    # Packages contain source nodes. Communication entities stay global because
    # topics, services, and actions are shared graph objects rather than being
    # owned by whichever publisher/provider happened to be encountered first.
    # Interfaces remain grouped under their defining package.

    seen_pkgs: set[str] = set()
    seen_nodes: set[str] = set()
    nodes = []

    def _inject_pkg(pkg_name: str) -> None:
        pkg_id = f"pkg:{pkg_name}"
        if pkg_id in seen_pkgs:
            return
        seen_pkgs.add(pkg_id)
        seen_nodes.add(pkg_id)
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

    short_labels = _short_endpoint_labels(sub)
    for nid, attrs in sub.nodes(data=True):
        if nid in seen_nodes:
            continue
        seen_nodes.add(nid)
        kind = attrs.get("kind", "")
        if kind == "Package":
            seen_pkgs.add(nid)
        raw_name = str(attrs.get("name", nid))
        data: dict[str, object] = {
            "id": nid,
            "label": short_labels.get(nid, raw_name),
            "name": raw_name,
        }
        for k, v in attrs.items():
            if k != "name":
                data[k] = _safe(v)

        if kind == "Node":
            pkg = str(attrs.get("package", ""))
            if pkg:
                _inject_pkg(pkg)
                data["parent"] = f"pkg:{pkg}"

        elif kind == "Deployment":
            source_node_id = attrs.get("source_node_id")
            if isinstance(source_node_id, str) and source_node_id in sub:
                data["parent"] = source_node_id
            else:
                pkg = str(attrs.get("package", ""))
                if pkg:
                    _inject_pkg(pkg)
                    data["parent"] = f"pkg:{pkg}"

        elif kind == "Interface":
            pkg = str(attrs.get("package", ""))
            if pkg:
                _inject_pkg(pkg)
                data["parent"] = f"pkg:{pkg}"

        nodes.append({"data": data})

    edges = []
    for i, (src, dst, attrs) in enumerate(sub.edges(data=True)):
        # Skip structural edges already encoded by compound nesting.
        if attrs.get("rel") in ("defined_in", "deploys_as"):
            continue
        source, target = flow_direction(src, dst, attrs.get("rel"))
        d = {"id": f"e{i}", "source": source, "target": target}
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

    replacements = {
        "__CYTOSCAPE_SCRIPT_TAG__": _cytoscape_script_tag(),
        "__GRAPHS_JSON__": graphs_json,
        "__SUMMARY_JSON__": summary_json,
        "__DIAGNOSTICS_JSON__": json.dumps(
            uam.to_dict().get("diagnostics", []), separators=(",", ":")
        ).replace("</", "<\\/"),
        "__WORKSPACE__": workspace_html,
    }
    placeholders = re.compile(
        r"__(?:CYTOSCAPE_SCRIPT_TAG|GRAPHS_JSON|SUMMARY_JSON|DIAGNOSTICS_JSON|WORKSPACE)__"
    )
    # Substitute only placeholders from the original template. Replacement values may
    # contain placeholder-looking text as valid source data and must remain untouched.
    return placeholders.sub(lambda match: replacements[match.group(0)], _TEMPLATE)


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
      <label class="frow"><input type="checkbox" data-kind="Deployment" checked><span class="sq" style="border:1px solid #4cc9f0"></span>Deployment</label>
      <label class="frow"><input type="checkbox" data-kind="Topic"     checked><span class="dot" style="background:#06d6a0"></span>Topic</label>
      <label class="frow"><input type="checkbox" data-kind="Service"   checked><span class="dot" style="background:#c77dff"></span>Service</label>
      <label class="frow"><input type="checkbox" data-kind="Action"    checked><span class="dot" style="background:#f72585"></span>Action</label>
      <label class="frow"><input type="checkbox" data-kind="Interface" checked><span class="sq" style="background:#2d3748;border:1px solid #adb5bd"></span>Interface</label>
    </div>

    <div class="sec">
      <div class="sec-title">Edge types</div>
      <label class="frow"><input type="checkbox" data-rel="publishes"      checked><span style="color:#06d6a0">→</span>Publishes</label>
      <label class="frow"><input type="checkbox" data-rel="subscribes"     checked><span style="color:#3a86ff">→</span>Subscribes</label>
      <label class="frow"><input type="checkbox" data-rel="depends_on"     checked><span style="color:#a5b1c2">⤍</span>Depends on</label>
      <label class="frow"><input type="checkbox" data-rel="provides"       checked><span style="color:#c77dff">→</span>Provides</label>
      <label class="frow"><input type="checkbox" data-rel="calls"          checked><span style="color:#c77dff">⤍</span>Calls</label>
      <label class="frow"><input type="checkbox" data-rel="defined_in"          ><span style="color:#444">→</span>Defined in</label>
      <label class="frow"><input type="checkbox" data-rel="uses_interface"      ><span style="color:#555">⤍</span>Uses interface</label>
    </div>

    <div class="sec">
      <div class="sec-title">Stats</div>
      <div id="stats"></div>
    </div>

    <details class="sec"><summary>Analysis diagnostics</summary><pre id="diagnostics" style="white-space:pre-wrap;overflow-wrap:anywhere;font-size:11px"></pre></details>

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
const DIAGNOSTICS = __DIAGNOSTICS_JSON__;
const SUMMARY = __SUMMARY_JSON__;

// ── styles config ──────────────────────────────────────────────────────────
const K = {
  Deployment:{bg:'#16213e', border:'#4cc9f0', shape:'roundrectangle', tc:'#9be7ff'},
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
  depends_on:     {c:'#a5b1c2', dash:true },
  provides:       {c:'#c77dff', dash:false},
  calls:          {c:'#c77dff', dash:true },
  defined_in:     {c:'#2a2a2a', dash:false},
  uses_interface: {c:'#333',    dash:true },
};

// ── Cytoscape stylesheet ───────────────────────────────────────────────────
// Leaf size from the label. Diamonds and hexagons only fit text in their middle.
const _measure=document.createElement('canvas').getContext('2d');
function fontSize(ele){ return parseFloat(ele.pstyle('font-size').strValue)||9; }
function textWidth(ele){
  _measure.font=`${fontSize(ele)}px 'Segoe UI',system-ui,sans-serif`;
  return _measure.measureText(String(ele.data('label')||'')).width;
}
const SHAPE_ROOM={Service:[1.9,2.4], Action:[1.45,1.6]};
function leafWidth(ele){
  const [w]=SHAPE_ROOM[ele.data('kind')]||[1];
  return Math.ceil(textWidth(ele)*w+12);
}
function leafHeight(ele){
  const [,h]=SHAPE_ROOM[ele.data('kind')]||[0,1];
  return Math.ceil((fontSize(ele)+8)*h);
}

function mkStyle() {
  const s = [
    // leaf nodes (topics, services, actions, interfaces)
    { selector:'node:childless', style:{
        label:'data(label)', 'font-size':'9px', color:'#c9d1d9',
        'text-valign':'center','text-halign':'center',
        'text-wrap':'wrap','text-max-width':'75px',
        'border-width':1.5,
        'width':leafWidth,'height':leafHeight,
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
        'font-weight':'bold',
        'color':'#58a6ff',
        'padding':'22px',
        'text-margin-y':'-3px',
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
        'font-weight':'bold',
        'color':'#ffb380',
        'padding':'6px',
        'text-margin-y':'-3px',
        'shape':'round-rectangle',
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
    { selector:'edge[rel="depends_on"]', style:{
        width:1.8, 'arrow-scale':1.5, opacity:.95,
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
    { selector:'node[resolution="conditional"]', style:{'border-style':'dashed'} },
    { selector:'node[resolution="unresolved"]', style:{'border-style':'dotted','border-width':3} },
    { selector:'edge[resolution="conditional"]', style:{'line-style':'dashed','opacity':.62} },
    { selector:'edge[resolution="unresolved"]', style:{'line-style':'dotted','opacity':.5} },
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
  // Flat layouts only: what a package (or node) contains. Structure, not data flow.
  s.push({selector:'edge[rel="contains"]', style:{
    'line-color':'#484f58','line-style':'dotted',width:1,'target-arrow-shape':'none',
  }});
  return s;
}

// ── layout configs ─────────────────────────────────────────────────────────
const LAYOUTS = {
  // Strong overlap repulsion and slow cooling: the previous settings stopped after ~200
  // iterations with nodes stacked on top of each other (and their edges undrawable).
  cose:{name:'cose',animate:'end',animationDuration:500,
    nodeRepulsion:400000,idealEdgeLength:110,edgeElasticity:100,
    gravity:.8,numIter:2500,randomize:true,componentSpacing:120,
    nodeOverlap:80,coolingFactor:.995,padding:30,nodeDimensionsIncludeLabels:true},
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
let cy, curGraph='deps', curLayout='breadthfirst', isGrouped=true, searchQuery='';

// ── manual compound preset layout ─────────────────────────────────────────
// Sizes derived from actual content. Packages row-packed by area so no
// row has wasted whitespace from a rigid column grid.
function runCompoundPreset(){
  const positions = {};
  const gap = 64;
  const packages = cy.nodes('[kind="Package"]:parent').sort((a,b)=>a.id().localeCompare(b.id()));
  const leafSize = n => {
    const box = n.boundingBox({includeLabels:true,includeOverlays:false});
    return {w:Math.max(120,box.w)+28,h:Math.max(32,box.h)+20};
  };
  const groups = packages.map(pkg => {
    const leaves = pkg.descendants(':childless').sort((a,b)=>a.id().localeCompare(b.id()));
    const sizes = leaves.map(leafSize);
    const cellW = Math.max(160,...sizes.map(v=>v.w));
    const cellH = Math.max(58,...sizes.map(v=>v.h));
    const cols = Math.max(1,Math.ceil(Math.sqrt(leaves.length)));
    const rows = Math.ceil(leaves.length/cols);
    return {pkg,leaves,cellW,cellH,cols,w:cols*cellW+64,h:rows*cellH+94};
  });
  const totalArea = groups.reduce((sum,g)=>sum+(g.w+gap)*(g.h+gap),0);
  const targetW = Math.max(1200,Math.sqrt(totalArea)*1.5);
  let x = 0, y = 0, rowH = 0;
  groups.forEach(g=>{
    if(x && x+g.w>targetW){x=0;y+=rowH+gap;rowH=0;}
    g.leaves.forEach((n,i)=>{
      positions[n.id()]={x:x+32+(i%g.cols+.5)*g.cellW,
                         y:y+62+(Math.floor(i/g.cols)+.5)*g.cellH};
    });
    x+=g.w+gap;rowH=Math.max(rowH,g.h);
  });
  y+=rowH+gap;
  // Global topics, services, actions and standalone packages have no package parent.
  const globals = cy.nodes(':childless').filter(n=>!positions[n.id()])
    .sort((a,b)=>a.id().localeCompare(b.id()));
  const globalW=Math.max(180,...globals.map(n=>leafSize(n).w));
  const globalH=Math.max(70,...globals.map(n=>leafSize(n).h));
  const cols=Math.max(1,Math.ceil(Math.sqrt(globals.length)));
  globals.forEach((n,i)=>{
    positions[n.id()]={x:(i%cols+.5)*globalW,y:y+(Math.floor(i/cols)+.5)*globalH};
  });
  cy.layout({name:'preset',positions:n=>positions[n.id()]||n.position(),
    animate:true,animationDuration:400,fit:true,padding:24,stop:afterLayout}).run();
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
  // Count by the filter class: ':visible' still reads the old style right after a change.
  cy.nodes().filter(n=>!n.hasClass('hidden')).forEach(n=>{
    const k=n.data('kind')||'?';
    counts[k]=(counts[k]||0)+1;
  });
  document.getElementById('stats').innerHTML=
    Object.entries(counts).sort(([a],[b])=>a.localeCompare(b))
    .map(([k,v])=>`<div class="srow"><span class="dk">${k}</span><span class="sv">${v}</span></div>`)
    .join('');
}

// Force-directed layouts stop with some nodes still stacked on large graphs (nav2), so the
// free layouts finish with a pass that pushes overlapping nodes apart along the shorter
// overlap, keeping the layout's shape.
function removeOverlaps(margin){
  const nodes=cy.nodes(':childless:visible');
  const items=nodes.map(n=>{
    const b=n.boundingBox({includeLabels:true,includeOverlays:false}), p=n.position();
    return {n,x:p.x,y:p.y,w:b.w+margin,h:b.h+margin};
  });
  for(let sweep=0;sweep<200;sweep++){
    let moved=false;
    for(let i=0;i<items.length;i++) for(let j=i+1;j<items.length;j++){
      const a=items[i], b=items[j];
      let dx=b.x-a.x, dy=b.y-a.y;
      const ox=(a.w+b.w)/2-Math.abs(dx), oy=(a.h+b.h)/2-Math.abs(dy);
      if(ox<=0||oy<=0) continue;
      moved=true;
      if(!dx&&!dy){ dx=(i+j)%2?1:-1; }  // exactly stacked: pick a side deterministically
      if(ox<oy){ const s=(dx<0?-1:1)*(ox/2+.5); a.x-=s; b.x+=s; }
      else { const s=(dy<0?-1:1)*(oy/2+.5); a.y-=s; b.y+=s; }
    }
    if(!moved) break;
  }
  cy.batch(()=>items.forEach(it=>it.n.position({x:it.x,y:it.y})));
}

// Everything is on screen after a layout; zoom in with the wheel for detail.
function afterLayout(){
  if(!isGrouped) removeOverlaps(8);
  cy.fit(cy.nodes(':visible').length <= 80 ? 48 : 24);
}

// Only the Grouped layout understands package/node nesting. Other layouts place children
// freely and Cytoscape stretches the containers around them, so they get a flat graph in
// which each container is a plain node linked to its children (see addGraph).
function setGrouping(grouped){
  if(grouped===isGrouped) return;
  isGrouped=grouped;
  cy.elements().remove();
  addGraph(curGraph, grouped);
  applyFilters();
  refreshStats();
  applySearch();
}

function runLayout(){
  setGrouping(curLayout === 'compound');
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
  isGrouped=curLayout==='compound';
  addGraph(g, isGrouped);
  applyFilters();
  runLayout();
  refreshStats();
  clearDetails();
}

function addGraph(g, grouped){
  const d=GRAPHS[g];
  if(grouped){ cy.add([...d.nodes,...d.edges]); }
  else {
    // Flat: containers (packages, nodes with deployments) become ordinary nodes linked to
    // what they contain, so packages and their dependency edges stay in every layout.
    const nodes=d.nodes.map(n=>{ const data=Object.assign({},n.data); delete data.parent; return {data}; });
    const contains=d.nodes.filter(n=>n.data.parent).map(n=>({data:{
      id:`contains:${n.data.id}`, source:n.data.parent, target:n.data.id, rel:'contains'}}));
    cy.add([...nodes,...d.edges,...contains]);
  }
  // Edges between packages are drawn a little heavier (by attribute, so it works flat too).
  cy.edges().forEach(e=>{
    const sp=e.source().data('package'), tp=e.target().data('package');
    if(sp&&tp&&sp!==tp) e.addClass('cross-pkg');
  });
}

function applySearch(){
  if(!searchQuery){ cy.elements().removeClass('faded'); return; }
  cy.elements().addClass('faded');
  const hits=cy.nodes().filter(n=>[n.data('label'),n.data('name'),n.id(),n.data('source_symbol'),n.data('package')]
    .filter(Boolean).join(' ').toLowerCase().includes(searchQuery));
  hits.removeClass('faded');
  hits.neighborhood().removeClass('faded');
  hits.ancestors().removeClass('faded');
}

function clearDetails(){
  cy.elements().removeClass('faded');
  applySearch();
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
  document.getElementById('diagnostics').textContent = DIAGNOSTICS.length ? DIAGNOSTICS.map(d=>d.message).join('\n\n') : 'No diagnostics recorded. Static analysis may still be incomplete.';
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
    searchQuery=e.target.value.trim().toLowerCase();
    applySearch();
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
