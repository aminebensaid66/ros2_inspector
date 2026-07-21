from __future__ import annotations

import hashlib
import re
from typing import Any

import networkx as nx
import orjson

from ros2inspector.model.uam import UnifiedArchitectureModel

_COMMS_RELS = {"publishes", "subscribes", "provides", "calls"}
_COMMS_KINDS = {"Node", "Topic", "Service", "Action"}
_DEPS_KINDS = {"Package"}
_DEPS_RELS = {"depends_on"}


def _slugify(text: str) -> str:
    """Make a Mermaid/DOT-safe node ID.

    Different source IDs can share the same base after stripping special chars
    (e.g. 'topic:/foo/bar' and 'topic:/foo-bar' both become 'topic___foo_bar').
    Appending a short hash of the original text keeps the IDs unique while
    preserving human-readability.
    """
    base = re.sub(r"[^a-zA-Z0-9_]", "_", text)
    suffix = hashlib.sha1(text.encode("utf-8"), usedforsecurity=False).hexdigest()[:8]
    return f"{base}_{suffix}"


def _subgraph(
    uam: UnifiedArchitectureModel,
    graph_type: str,
    package: str | None,
) -> nx.MultiDiGraph:
    g = uam.graph

    if graph_type == "deps":
        allowed_kinds = _DEPS_KINDS
        allowed_rels = _DEPS_RELS
    elif graph_type == "comms":
        allowed_kinds = _COMMS_KINDS
        allowed_rels = _COMMS_RELS
    else:
        allowed_kinds = set()
        allowed_rels = set()

    if graph_type == "full":
        sub = g.copy()
    else:
        keep_nodes = {n for n, d in g.nodes(data=True) if d.get("kind") in allowed_kinds}
        sub = g.subgraph(keep_nodes).copy()
        remove_edges = [
            (s, t, key)
            for s, t, key, d in sub.edges(keys=True, data=True)
            if d.get("rel") not in allowed_rels
        ]
        sub.remove_edges_from(remove_edges)

    if package:
        pkg_node_ids = {n for n, d in sub.nodes(data=True) if d.get("name") == package}
        if not pkg_node_ids:
            # Try matching by package attribute on Node kind too
            pkg_node_ids = {
                n
                for n, d in sub.nodes(data=True)
                if d.get("package") == package or d.get("name") == package
            }
        neighbors: set[str] = set()
        for pid in pkg_node_ids:
            neighbors.update(sub.predecessors(pid))
            neighbors.update(sub.successors(pid))
        keep = pkg_node_ids | neighbors
        sub = sub.subgraph(keep).copy()

    return sub


def render_mermaid(
    uam: UnifiedArchitectureModel, graph_type: str, package: str | None = None
) -> str:
    sub = _subgraph(uam, graph_type, package)
    lines = ["flowchart LR"]

    for nid, attrs in sub.nodes(data=True):
        sid = _slugify(nid)
        name = attrs.get("name", nid)
        kind = attrs.get("kind", "")
        safe_name = name.replace('"', "'")

        if kind == "Package":
            lines.append(f'    {sid}["{safe_name}"]')
        elif kind == "Topic":
            lines.append(f'    {sid}(["{safe_name}"])')
        elif kind == "Service":
            lines.append(f'    {sid}{{{{"{safe_name}"}}}}')
        elif kind == "Action":
            lines.append(f'    {sid}>"{safe_name}"]')
        elif kind == "Node":
            lines.append(f'    {sid}("{safe_name}")')
        elif kind == "Interface":
            lines.append(f'    {sid}[/"{safe_name}"/]')
        else:
            lines.append(f'    {sid}["{safe_name}"]')

    for src, dst, attrs in sub.edges(data=True):
        rel = attrs.get("rel", "")
        ssrc = _slugify(src)
        sdst = _slugify(dst)
        if rel == "publishes":
            lines.append(f"    {ssrc} -->|pub| {sdst}")
        elif rel == "subscribes":
            lines.append(f"    {ssrc} -->|sub| {sdst}")
        elif rel == "depends_on":
            lines.append(f"    {ssrc} -.-> {sdst}")
        elif rel == "provides":
            lines.append(f"    {ssrc} -->|srv/act| {sdst}")
        elif rel == "calls":
            lines.append(f"    {ssrc} -.->|call| {sdst}")
        else:
            lines.append(f"    {ssrc} --> {sdst}")

    return "\n".join(lines) + "\n"


def render_dot(uam: UnifiedArchitectureModel, graph_type: str, package: str | None = None) -> str:
    sub = _subgraph(uam, graph_type, package)
    lines = ["digraph ros2inspector {", "    rankdir=LR;"]

    for nid, attrs in sub.nodes(data=True):
        sid = _slugify(nid)
        name = attrs.get("name", nid).replace('"', "'")
        kind = attrs.get("kind", "")

        if kind == "Package":
            lines.append(f'    {sid} [label="{name}" shape=box];')
        elif kind == "Topic":
            lines.append(f'    {sid} [label="{name}" shape=ellipse color=blue];')
        elif kind == "Service":
            lines.append(f'    {sid} [label="{name}" shape=diamond color=green];')
        elif kind == "Action":
            lines.append(f'    {sid} [label="{name}" shape=hexagon color=red];')
        elif kind == "Node":
            lines.append(f'    {sid} [label="{name}" shape=rectangle color=orange];')
        elif kind == "Interface":
            lines.append(f'    {sid} [label="{name}" shape=parallelogram];')
        else:
            lines.append(f'    {sid} [label="{name}"];')

    for src, dst, attrs in sub.edges(data=True):
        ssrc = _slugify(src)
        sdst = _slugify(dst)
        rel = attrs.get("rel", "")
        if rel == "depends_on":
            lines.append(f"    {ssrc} -> {sdst} [style=dashed];")
        else:
            lines.append(f'    {ssrc} -> {sdst} [label="{rel}"];')

    lines.append("}")
    return "\n".join(lines) + "\n"


def render_json(uam: UnifiedArchitectureModel, graph_type: str, package: str | None = None) -> str:
    if graph_type == "full" and package is None:
        data: dict[str, Any] = uam.to_dict()
    else:
        sub = _subgraph(uam, graph_type, package)
        data = {
            "nodes": [
                {"id": nid, **{k: v for k, v in attrs.items()}}
                for nid, attrs in sub.nodes(data=True)
            ],
            "edges": [
                {"source": s, "target": t, "key": key, **dict(d)}
                for s, t, key, d in sub.edges(keys=True, data=True)
            ],
        }
    return orjson.dumps(data, option=orjson.OPT_INDENT_2).decode() + "\n"
