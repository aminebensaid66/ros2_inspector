from __future__ import annotations

import ast
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from ros2inspector.discovery.file_walker import iter_package_files
from ros2inspector.model.schemas import DYNAMIC_SENTINEL, UNKNOWN_SENTINEL


@dataclass
class LaunchNode:
    executable: str
    package: str
    name: str | None = None
    remaps: dict[str, str] = field(default_factory=dict)
    namespace: str | None = None
    source_file: str | None = None
    unresolved_fields: list[str] = field(default_factory=list)

    conditions: list[str] = field(default_factory=list)
    presence: str = "known"
    plugin: str | None = None

    @property
    def is_conditional(self) -> bool:
        return self.presence == "conditional"

    @property
    def is_unresolved(self) -> bool:
        return self.presence == "unresolved" or bool(self.unresolved_fields)


@dataclass
class LaunchInclude:
    source_file: str
    target_file: str
    unresolved: bool = False


@dataclass
class LaunchGraph:
    nodes: list[LaunchNode] = field(default_factory=list)
    includes: list[LaunchInclude] = field(default_factory=list)
    source_file: str = ""
    unresolved_branches: bool = False


def analyze_launch_file(path: Path) -> LaunchGraph:
    suffix = path.suffix.lower()
    if suffix == ".py":
        return _analyze_python_launch(path)
    if suffix == ".xml":
        return _analyze_xml_launch(path)
    if suffix in (".yaml", ".yml"):
        return _analyze_yaml_launch(path)
    return LaunchGraph(source_file=str(path))


_LAUNCH_SUFFIXES = frozenset((".py", ".xml", ".yaml", ".yml"))
_LAUNCH_DIR_NAMES = frozenset(("launch", "bringup"))
_SUBSTITUTION_RE = re.compile(r"\$\([^)]*\)")


def find_launch_files(package_path: Path) -> list[Path]:
    found: set[Path] = set()
    launch_dirs = {package_path / d for d in _LAUNCH_DIR_NAMES}
    for file_path in iter_package_files(package_path, suffixes=_LAUNCH_SUFFIXES):
        if file_path.stem.endswith(".launch"):
            found.add(file_path)
            continue
        if any(directory in file_path.parents for directory in launch_dirs):
            found.add(file_path)
    return sorted(found)


def _contains_substitution(value: str) -> bool:
    return bool(_SUBSTITUTION_RE.search(value))


def _include_target_string(value: object) -> tuple[str, bool]:
    """Preserve literal include-path evidence even when it needs substitution."""
    if value is None:
        return UNKNOWN_SENTINEL, True
    if not isinstance(value, str):
        return DYNAMIC_SENTINEL, True
    return value, _contains_substitution(value)


# ── Python launch files ──────────────────────────────────────────────────────


class _PythonLaunchVisitor(ast.NodeVisitor):
    def __init__(self, file_path: str) -> None:
        self.graph = LaunchGraph(source_file=file_path)
        self._assignments: dict[str, ast.expr] = {}
        self._condition_stack: list[str] = []
        self._node_calls: dict[int, LaunchNode] = {}

    def visit_Assign(self, node: ast.Assign) -> None:
        for target in node.targets:
            if isinstance(target, ast.Name):
                self._assignments[target.id] = node.value
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        func = _get_call_name(node.func)

        if func in (
            "Node",
            "launch_ros.actions.Node",
            "ComposableNode",
            "launch_ros.descriptions.ComposableNode",
        ):
            composable = func.split(".")[-1] == "ComposableNode"
            launch_node = _extract_python_node(node, composable=composable)
            if self._condition_stack:
                launch_node.conditions = [*self._condition_stack, *launch_node.conditions]
                if launch_node.presence == "known":
                    launch_node.presence = "conditional"
            self.graph.nodes.append(launch_node)
            self._node_calls[id(node)] = launch_node
            if launch_node.is_unresolved:
                self.graph.unresolved_branches = True

        elif func in (
            "IncludeLaunchDescription",
            "launch.actions.IncludeLaunchDescription",
        ):
            target, unresolved = _extract_include_target(node)
            if target:
                self.graph.includes.append(
                    LaunchInclude(
                        source_file=self.graph.source_file,
                        target_file=target,
                        unresolved=unresolved,
                    )
                )
            if unresolved or not target:
                self.graph.unresolved_branches = True

        elif func in (
            "GroupAction",
            "launch.actions.GroupAction",
            "LoadComposableNodes",
            "launch_ros.actions.LoadComposableNodes",
            "ComposableNodeContainer",
            "launch_ros.actions.ComposableNodeContainer",
        ):
            condition = next((kw.value for kw in node.keywords if kw.arg == "condition"), None)
            rendered = ast.unparse(condition) if condition is not None else None
            if rendered is not None:
                self._condition_stack.append(rendered)
            elif condition is not None:
                self._condition_stack.append(DYNAMIC_SENTINEL)
                self.graph.unresolved_branches = True
            self.generic_visit(node)
            if condition is not None:
                self._condition_stack.pop()
            return

        elif func in ("OpaqueFunction", "launch.actions.OpaqueFunction"):
            self.graph.unresolved_branches = True

        self.generic_visit(node)


def _analyze_python_launch(path: Path) -> LaunchGraph:
    try:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
    except (SyntaxError, UnicodeDecodeError, OSError):
        return LaunchGraph(source_file=str(path), unresolved_branches=True)

    visitor = _PythonLaunchVisitor(str(path))
    visitor.visit(tree)

    # GroupAction often refers to a Node or list assigned earlier. Propagate its
    # condition through those AST references without executing launch code.
    def propagate(expr: ast.AST, conditions: list[str], seen: set[int]) -> None:
        if id(expr) in seen:
            return
        seen = seen | {id(expr)}
        if isinstance(expr, ast.Name) and expr.id in visitor._assignments:
            propagate(visitor._assignments[expr.id], conditions, seen)
        record = visitor._node_calls.get(id(expr))
        if record is not None:
            record.conditions = list(dict.fromkeys([*record.conditions, *conditions]))
            if record.conditions and record.presence == "known":
                record.presence = "conditional"
        for child in ast.iter_child_nodes(expr):
            propagate(child, conditions, seen)

    for expr in ast.walk(tree):
        if not isinstance(expr, ast.Call) or _get_call_name(expr.func) not in (
            "GroupAction",
            "launch.actions.GroupAction",
            "LoadComposableNodes",
            "launch_ros.actions.LoadComposableNodes",
            "ComposableNodeContainer",
            "launch_ros.actions.ComposableNodeContainer",
        ):
            continue
        condition = next((kw.value for kw in expr.keywords if kw.arg == "condition"), None)
        if condition is not None:
            propagate(expr, [ast.unparse(condition)], set())
    return visitor.graph


def _extract_python_node(call: ast.Call, *, composable: bool = False) -> LaunchNode:
    kwargs = {kw.arg: kw.value for kw in call.keywords if kw.arg}
    executable, executable_unresolved = _python_string(
        kwargs.get("plugin" if composable else "executable"), required=True
    )
    package, package_unresolved = _python_string(kwargs.get("package"), required=True)
    name, name_unresolved = _python_string(kwargs.get("name"), required=False)
    namespace, namespace_unresolved = _python_string(kwargs.get("namespace"), required=False)
    remaps, remaps_unresolved = _extract_remaps(kwargs.get("remappings"))

    unresolved_fields = [
        field_name
        for field_name, unresolved in (
            ("executable", executable_unresolved),
            ("package", package_unresolved),
            ("name", name_unresolved),
            ("namespace", namespace_unresolved),
            ("remappings", remaps_unresolved),
        )
        if unresolved
    ]
    condition = kwargs.get("condition")
    conditions = [ast.unparse(condition)] if condition is not None else []

    return LaunchNode(
        plugin=executable if composable else None,
        executable=executable or UNKNOWN_SENTINEL,
        package=package or UNKNOWN_SENTINEL,
        name=name,
        remaps=remaps,
        namespace=namespace,
        unresolved_fields=unresolved_fields,
        conditions=conditions,
        presence="conditional" if conditions else "known",
    )


def _extract_remaps(node: ast.expr | None) -> tuple[dict[str, str], bool]:
    if node is None:
        return {}, False
    if not isinstance(node, (ast.List, ast.Tuple)):
        return {}, True
    remaps: dict[str, str] = {}
    unresolved = False
    for elt in node.elts:
        if not isinstance(elt, ast.Tuple) or len(elt.elts) != 2:
            unresolved = True
            continue
        src, src_unresolved = _python_string(elt.elts[0], required=True)
        dst, dst_unresolved = _python_string(elt.elts[1], required=True)
        unresolved = unresolved or src_unresolved or dst_unresolved
        if src and dst:
            remaps[src] = dst
    return remaps, unresolved


def _extract_include_target(call: ast.Call) -> tuple[str | None, bool]:
    node: ast.expr | None = call.args[0] if call.args else None
    if node is None:
        for kw in call.keywords:
            if kw.arg == "launch_description_source":
                node = kw.value
                break
    if node is None:
        return None, True
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return _include_target_string(node.value)
    value, unresolved = _python_string(node, required=True)
    return value, unresolved


def _get_call_name(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _get_call_name(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    return ""


def _python_string(node: ast.expr | None, *, required: bool) -> tuple[str | None, bool]:
    if node is None:
        return (UNKNOWN_SENTINEL, True) if required else (None, False)
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        if _contains_substitution(node.value):
            return DYNAMIC_SENTINEL, True
        return node.value, False
    return DYNAMIC_SENTINEL, True


# ── XML launch files ─────────────────────────────────────────────────────────


def _analyze_xml_launch(path: Path) -> LaunchGraph:
    graph = LaunchGraph(source_file=str(path))
    try:
        tree = ET.parse(path)
        root = tree.getroot()
    except (ET.ParseError, OSError):
        graph.unresolved_branches = True
        return graph

    for elem in root.iter():
        if elem.get("if") is not None or elem.get("unless") is not None:
            graph.unresolved_branches = True

    def walk(
        elem: ET.Element, inherited: tuple[str, ...] = (), container_ns: str | None = None
    ) -> None:
        local = list(inherited)
        if elem.tag == "node_container" and elem.get("namespace", elem.get("ns")):
            container_ns = elem.get("namespace", elem.get("ns"))
        if elem.get("if") is not None:
            local.append(f"if:{elem.get('if')}")
        if elem.get("unless") is not None:
            local.append(f"unless:{elem.get('unless')}")
        if elem.tag == "node":
            _append_xml_node(elem, graph, local)
        elif elem.tag == "composable_node":
            _append_xml_node(elem, graph, local, composable=True, container_ns=container_ns)
        for child in elem:
            walk(child, tuple(local), container_ns)

    walk(root)

    for elem in root.iter("include"):
        target, unresolved = _include_target_string(elem.get("file"))
        if target:
            graph.includes.append(
                LaunchInclude(source_file=str(path), target_file=target, unresolved=unresolved)
            )
        if unresolved:
            graph.unresolved_branches = True
    return graph


def _append_xml_node(
    elem: ET.Element,
    graph: LaunchGraph,
    inherited: list[str],
    *,
    composable: bool = False,
    container_ns: str | None = None,
) -> None:
    executable, executable_unresolved = _xml_string(
        elem.get("plugin") if composable else elem.get("exec", elem.get("type")), required=True
    )
    package, package_unresolved = _xml_string(elem.get("pkg"), required=True)
    name, name_unresolved = _xml_string(elem.get("name"), required=False)
    namespace, namespace_unresolved = _xml_string(
        (elem.get("namespace", elem.get("ns")) or container_ns) if composable else elem.get("ns"),
        required=False,
    )
    remaps: dict[str, str] = {}
    remaps_unresolved = False
    for remap in elem.findall("remap"):
        src, src_unresolved = _xml_string(remap.get("from"), required=True)
        dst, dst_unresolved = _xml_string(remap.get("to"), required=True)
        remaps_unresolved = remaps_unresolved or src_unresolved or dst_unresolved
        if src and dst:
            remaps[src] = dst

    unresolved_fields = [
        field_name
        for field_name, unresolved in (
            ("executable", executable_unresolved),
            ("package", package_unresolved),
            ("name", name_unresolved),
            ("namespace", namespace_unresolved),
            ("remappings", remaps_unresolved),
        )
        if unresolved
    ]
    launch_node = LaunchNode(
        plugin=executable if composable else None,
        executable=executable or UNKNOWN_SENTINEL,
        package=package or UNKNOWN_SENTINEL,
        name=name,
        remaps=remaps,
        namespace=namespace,
        unresolved_fields=unresolved_fields,
        conditions=list(inherited),
        presence="conditional" if inherited else "known",
    )
    graph.nodes.append(launch_node)
    if launch_node.is_unresolved:
        graph.unresolved_branches = True


def _xml_string(value: str | None, *, required: bool) -> tuple[str | None, bool]:
    if value is None:
        return (UNKNOWN_SENTINEL, True) if required else (None, False)
    if _contains_substitution(value):
        return DYNAMIC_SENTINEL, True
    return value, False


# ── YAML launch files ────────────────────────────────────────────────────────


def _analyze_yaml_launch(path: Path) -> LaunchGraph:
    graph = LaunchGraph(source_file=str(path))
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (yaml.YAMLError, UnicodeDecodeError, OSError):
        graph.unresolved_branches = True
        return graph

    if not isinstance(data, dict):
        graph.unresolved_branches = True
        return graph

    entries = data.get("launch", [])
    if not isinstance(entries, list):
        graph.unresolved_branches = True
        return graph

    def flatten(items: list[object], inherited: list[str]) -> list[tuple[object, list[str]]]:
        result: list[tuple[object, list[str]]] = []
        for item in items:
            group = item.get("group") if isinstance(item, dict) else None
            if isinstance(group, dict):
                conditions = inherited + [f"{k}:{group[k]}" for k in ("if", "unless") if k in group]
                children = group.get("children", group.get("actions", []))
                if isinstance(children, list):
                    result.extend(flatten(children, conditions))
                else:
                    graph.unresolved_branches = True
            else:
                result.append((item, inherited))
        return result

    for entry, inherited in flatten(entries, []):
        if not isinstance(entry, dict):
            graph.unresolved_branches = True
            continue
        node_cfg = entry.get("node")
        if isinstance(node_cfg, dict):
            executable, executable_unresolved = _yaml_string(node_cfg.get("exec"), required=True)
            package, package_unresolved = _yaml_string(node_cfg.get("pkg"), required=True)
            name, name_unresolved = _yaml_string(node_cfg.get("name"), required=False)
            namespace, namespace_unresolved = _yaml_string(
                node_cfg.get("namespace"), required=False
            )
            remaps, remaps_unresolved = _extract_yaml_remaps(node_cfg.get("remap"))
            unresolved_fields = [
                field_name
                for field_name, unresolved in (
                    ("executable", executable_unresolved),
                    ("package", package_unresolved),
                    ("name", name_unresolved),
                    ("namespace", namespace_unresolved),
                    ("remappings", remaps_unresolved),
                )
                if unresolved
            ]
            conditions = inherited + [
                f"{key}:{node_cfg[key]}" for key in ("if", "unless") if key in node_cfg
            ]

            launch_node = LaunchNode(
                executable=executable or UNKNOWN_SENTINEL,
                package=package or UNKNOWN_SENTINEL,
                name=name,
                remaps=remaps,
                namespace=namespace,
                unresolved_fields=unresolved_fields,
                conditions=conditions,
                presence="conditional" if conditions else "known",
            )
            graph.nodes.append(launch_node)
            if launch_node.is_unresolved:
                graph.unresolved_branches = True
            continue

        include_cfg = entry.get("include")
        if include_cfg is not None:
            target_value: object = include_cfg
            if isinstance(include_cfg, dict):
                target_value = include_cfg.get("file") or include_cfg.get("path")
            target, unresolved = _include_target_string(target_value)
            if target:
                graph.includes.append(
                    LaunchInclude(
                        source_file=str(path),
                        target_file=target,
                        unresolved=unresolved,
                    )
                )
            if unresolved:
                graph.unresolved_branches = True
            continue

        graph.unresolved_branches = True

    return graph


def _yaml_string(value: object, *, required: bool) -> tuple[str | None, bool]:
    if value is None:
        return (UNKNOWN_SENTINEL, True) if required else (None, False)
    if not isinstance(value, str):
        return DYNAMIC_SENTINEL, True
    if _contains_substitution(value):
        return DYNAMIC_SENTINEL, True
    return value, False


def _extract_yaml_remaps(value: object) -> tuple[dict[str, str], bool]:
    if value is None:
        return {}, False
    if not isinstance(value, list):
        return {}, True
    remaps: dict[str, str] = {}
    unresolved = False
    for item in value:
        if isinstance(item, dict):
            src_value = item.get("from")
            dst_value = item.get("to")
        elif isinstance(item, (list, tuple)) and len(item) == 2:
            src_value, dst_value = item
        else:
            unresolved = True
            continue
        src, src_unresolved = _yaml_string(src_value, required=True)
        dst, dst_unresolved = _yaml_string(dst_value, required=True)
        unresolved = unresolved or src_unresolved or dst_unresolved
        if src and dst:
            remaps[src] = dst
    return remaps, unresolved
