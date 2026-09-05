import ast
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from ros2inspector.discovery.file_walker import iter_package_files


@dataclass
class LaunchNode:
    executable: str
    package: str
    name: str | None = None
    remaps: dict[str, str] = field(default_factory=dict)
    namespace: str | None = None
    source_file: str | None = None


@dataclass
class LaunchInclude:
    source_file: str
    target_file: str


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


def find_launch_files(package_path: Path) -> list[Path]:
    found: set[Path] = set()
    # Directories whose contents are all considered launch files.
    launch_dirs = {package_path / d for d in _LAUNCH_DIR_NAMES}
    for f in iter_package_files(package_path, suffixes=_LAUNCH_SUFFIXES):
        # Always include files whose stem explicitly marks them as launch files
        # (e.g. bringup.launch.py, robot.launch.xml, nav.launch.yaml).
        if f.stem.endswith(".launch"):
            found.add(f)
            continue
        # Include any supported file that lives inside a recognised launch
        # directory — this catches plain names like my_robot.yaml or
        # sensors.xml that don't carry ".launch" in the stem.
        if any(d in f.parents for d in launch_dirs):
            found.add(f)
    return sorted(found)


# ── Python launch files ──────────────────────────────────────────────────────


class _PythonLaunchVisitor(ast.NodeVisitor):
    def __init__(self, file_path: str) -> None:
        self.graph = LaunchGraph(source_file=file_path)
        self._assignments: dict[str, ast.expr] = {}

    def visit_Assign(self, node: ast.Assign) -> None:
        for target in node.targets:
            if isinstance(target, ast.Name):
                self._assignments[target.id] = node.value
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        func = _get_call_name(node.func)

        if func in ("Node", "launch_ros.actions.Node"):
            self.graph.nodes.append(_extract_python_node(node))

        elif func in (
            "IncludeLaunchDescription",
            "launch.actions.IncludeLaunchDescription",
        ):
            target = _extract_include_target(node)
            if target:
                self.graph.includes.append(
                    LaunchInclude(source_file=self.graph.source_file, target_file=target)
                )

        elif func in ("OpaqueFunction", "launch.actions.OpaqueFunction"):
            # Cannot statically resolve opaque functions
            self.graph.unresolved_branches = True

        self.generic_visit(node)


def _analyze_python_launch(path: Path) -> LaunchGraph:
    try:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
    except (SyntaxError, UnicodeDecodeError):
        return LaunchGraph(source_file=str(path), unresolved_branches=True)

    visitor = _PythonLaunchVisitor(str(path))
    visitor.visit(tree)
    return visitor.graph


def _extract_python_node(call: ast.Call) -> LaunchNode:
    kwargs = {kw.arg: kw.value for kw in call.keywords if kw.arg}
    return LaunchNode(
        executable=_str_value(kwargs.get("executable")),
        package=_str_value(kwargs.get("package")),
        name=_str_value(kwargs.get("name")),
        remaps=_extract_remaps(kwargs.get("remappings")),
        namespace=_str_value(kwargs.get("namespace")),
    )


def _extract_remaps(node: ast.expr | None) -> dict[str, str]:
    if node is None or not isinstance(node, ast.List):
        return {}
    remaps: dict[str, str] = {}
    for elt in node.elts:
        if isinstance(elt, ast.Tuple) and len(elt.elts) == 2:
            src = _str_value(elt.elts[0])
            dst = _str_value(elt.elts[1])
            if src and dst:
                remaps[src] = dst
    return remaps


def _extract_include_target(call: ast.Call) -> str | None:
    if call.args:
        return _str_value(call.args[0])
    for kw in call.keywords:
        if kw.arg == "launch_description_source":
            return _str_value(kw.value)
    return None


def _get_call_name(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{_get_call_name(node.value)}.{node.attr}"
    return ""


def _str_value(node: ast.expr | None) -> str:
    if node is None:
        return "<unknown>"
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return "<dynamic>"


# ── XML launch files ─────────────────────────────────────────────────────────


def _analyze_xml_launch(path: Path) -> LaunchGraph:
    graph = LaunchGraph(source_file=str(path))
    try:
        tree = ET.parse(path)
        root = tree.getroot()
    except ET.ParseError:
        graph.unresolved_branches = True
        return graph

    for elem in root.iter("node"):
        remaps: dict[str, str] = {}
        for remap in elem.findall("remap"):
            src = remap.get("from", "")
            dst = remap.get("to", "")
            if src and dst:
                remaps[src] = dst
        graph.nodes.append(
            LaunchNode(
                executable=elem.get("exec", elem.get("type", "<unknown>")),
                package=elem.get("pkg", "<unknown>"),
                name=elem.get("name"),
                remaps=remaps,
                namespace=elem.get("ns"),
            )
        )

    for elem in root.iter("include"):
        target = elem.get("file", "")
        if target:
            graph.includes.append(LaunchInclude(source_file=str(path), target_file=target))

    return graph


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

    for entry in entries:
        if not isinstance(entry, dict):
            graph.unresolved_branches = True
            continue
        node_cfg = entry.get("node")
        if isinstance(node_cfg, dict):
            graph.nodes.append(
                LaunchNode(
                    executable=_yaml_string(node_cfg.get("exec")),
                    package=_yaml_string(node_cfg.get("pkg")),
                    name=_yaml_string(node_cfg.get("name")),
                    remaps=_extract_yaml_remaps(node_cfg.get("remap")),
                    namespace=_yaml_string(node_cfg.get("namespace")),
                )
            )
            continue

        include_cfg = entry.get("include")
        if include_cfg is not None:
            target = include_cfg if isinstance(include_cfg, str) else None
            if isinstance(include_cfg, dict):
                target = include_cfg.get("file") or include_cfg.get("path")
            if target:
                graph.includes.append(
                    LaunchInclude(source_file=str(path), target_file=target)
                )
            else:
                graph.unresolved_branches = True
            continue

        # Conditions, substitutions, groups, and other frontend constructs may
        # affect the effective deployment but cannot be resolved safely here.
        graph.unresolved_branches = True

    return graph


def _yaml_string(value: object) -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return "<unknown>"
    return "<dynamic>"


def _extract_yaml_remaps(value: object) -> dict[str, str]:
    if not isinstance(value, list):
        return {}
    remaps: dict[str, str] = {}
    for item in value:
        if isinstance(item, dict):
            src = item.get("from")
            dst = item.get("to")
        elif isinstance(item, list) and len(item) == 2:
            src, dst = item
        else:
            continue
        if isinstance(src, str) and isinstance(dst, str):
            remaps[src] = dst
    return remaps
