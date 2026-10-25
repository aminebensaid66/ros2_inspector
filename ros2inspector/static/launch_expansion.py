from __future__ import annotations

import copy
from pathlib import Path

from ros2inspector.model.schemas import DYNAMIC_SENTINEL, UNKNOWN_SENTINEL
from ros2inspector.static.launch_analyzer import LaunchGraph, LaunchNode, _compose_namespace


def expand_launch_includes(graphs: list[LaunchGraph]) -> list[LaunchNode]:
    """Retain standalone definitions and instantiate source-local include scopes."""
    by_path = {Path(graph.source_file).resolve(): graph for graph in graphs}
    targets: set[Path] = set()

    def target(source: LaunchGraph, name: str) -> LaunchGraph | None:
        if name in {DYNAMIC_SENTINEL, UNKNOWN_SENTINEL} or "$(" in name:
            return None
        path = Path(name)
        if not path.is_absolute():
            path = Path(source.source_file).parent / path
        return by_path.get(path.resolve())

    for graph in graphs:
        for include in graph.includes:
            child = target(graph, include.target_file)
            if child is not None:
                targets.add(Path(child.source_file).resolve())

    result: list[LaunchNode] = []

    def diagnostic(graph: LaunchGraph, code: str, message: str) -> None:
        graph.unresolved_branches = True
        entry = {"severity": "warning", "code": code, "file": graph.source_file, "message": message}
        if entry not in graph.diagnostics:
            graph.diagnostics.append(entry)

    def walk(
        graph: LaunchGraph,
        namespace: str | None,
        conditions: list[str],
        conditional: bool,
        parent: str | None,
        stack: frozenset[Path],
        unresolved: tuple[str, ...] = (),
    ) -> None:
        for original in graph.nodes:
            node = copy.deepcopy(original)
            node.source_file = graph.source_file
            node.included_from = parent
            node.unresolved_fields = list(dict.fromkeys([*node.unresolved_fields, *unresolved]))
            if parent is not None and not node.namespace_absolute:
                node.namespace = _compose_namespace(
                    namespace, node.namespace.lstrip("/") if node.namespace else None
                )
            node.conditions = list(dict.fromkeys([*conditions, *node.conditions]))
            if conditional or conditions:
                node.presence = "conditional"
            if node.namespace == DYNAMIC_SENTINEL and "namespace" not in node.unresolved_fields:
                node.unresolved_fields.append("namespace")
            result.append(node)
        for include in graph.includes:
            child = target(graph, include.target_file)
            if child is None:
                diagnostic(
                    graph,
                    "launch_include_unresolved",
                    f"Included launch target could not be resolved: {include.target_file}",
                )
                continue
            if any(
                d.get("code") in {"source_parse_failed", "launch_scope_unresolved"}
                for d in child.diagnostics
            ):
                diagnostic(
                    graph,
                    "launch_include_unresolved",
                    f"Included launch actions were not fully resolved: {child.source_file}",
                )
            child_path = Path(child.source_file).resolve()
            if child_path in stack:
                diagnostic(
                    graph,
                    "launch_include_unresolved",
                    f"Launch include cycle reaches {child.source_file}",
                )
                continue
            include_namespace = include.namespace
            if parent is not None and not include.namespace_absolute:
                include_namespace = _compose_namespace(
                    namespace, include_namespace.lstrip("/") if include_namespace else None
                )
            if include.arguments_unresolved:
                include_namespace = DYNAMIC_SENTINEL
                diagnostic(
                    graph,
                    "launch_include_arguments_unresolved",
                    "Included launch arguments could not be consumed statically",
                )
            walk(
                child,
                include_namespace,
                list(
                    dict.fromkeys([*conditions, *include.conditions, *include.namespace_conditions])
                ),
                conditional or bool(include.conditions),
                graph.source_file,
                stack | {child_path},
                (*unresolved, "launch_arguments") if include.arguments_unresolved else unresolved,
            )

    for graph in graphs:
        path = Path(graph.source_file).resolve()
        walk(graph, None, [], path in targets, None, frozenset({path}))
    return result
