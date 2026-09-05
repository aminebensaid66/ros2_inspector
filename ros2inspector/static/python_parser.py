from __future__ import annotations

import ast
from pathlib import Path

from ros2inspector.discovery.file_walker import iter_package_files
from ros2inspector.model.schemas import (
    DYNAMIC_SENTINEL,
    CommunicationEndpoint,
    DataSource,
    NodeDefinition,
)

_IFACE_MARKERS = ("msg", "srv", "action")
_CANONICAL_NODE_BASES = {
    "rclpy.node.Node",
    "rclpy.lifecycle.LifecycleNode",
    "rclpy_lifecycle.LifecycleNode",
}


def parse_python_nodes(package_path: Path, package_name: str) -> list[NodeDefinition]:
    nodes: list[NodeDefinition] = []
    for py_file in iter_package_files(package_path, suffixes={".py"}):
        try:
            source = py_file.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(py_file))
        except (SyntaxError, UnicodeDecodeError, OSError):
            continue
        visitor = _NodeVisitor(package_name, py_file)
        visitor.visit(tree)
        nodes.extend(visitor.nodes)
    return nodes


class _NodeVisitor(ast.NodeVisitor):
    def __init__(self, package: str, file_path: Path) -> None:
        self.package = package
        self.file_path = file_path
        self.nodes: list[NodeDefinition] = []
        self._current_node: NodeDefinition | None = None

        # Local interface symbol -> canonical ``pkg/Type``.
        self._iface_imports: dict[str, str] = {}
        # Local module alias -> canonical module path, e.g. ``msg -> std_msgs.msg``.
        self._module_aliases: dict[str, str] = {}
        # Module roots proven imported without an alias, e.g. ``std_msgs``.
        self._module_roots: set[str] = set()
        # Bare class aliases proven to be ROS node bases.
        self._node_base_aliases: set[str] = set()

        # Statically resolvable string constants: class attrs + self.attr assignments.
        self._class_attrs: dict[str, str] = {}

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            if alias.asname:
                self._module_aliases[alias.asname] = alias.name
            else:
                self._module_roots.add(alias.name.split(".", 1)[0])
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = node.module
        if not module:
            self.generic_visit(node)
            return

        parts = module.split(".")
        if len(parts) >= 2 and parts[-1] in _IFACE_MARKERS:
            package = ".".join(parts[:-1])
            for alias in node.names:
                if alias.name == "*":
                    continue
                local = alias.asname or alias.name
                self._iface_imports[local] = f"{package}/{alias.name}"

        if module in {"rclpy.node", "rclpy.lifecycle", "rclpy_lifecycle"}:
            for alias in node.names:
                if alias.name in {"Node", "LifecycleNode"}:
                    self._node_base_aliases.add(alias.asname or alias.name)

        # ``from std_msgs import msg as smsg`` and ``from rclpy import node as rn``.
        for alias in node.names:
            if alias.name == "*":
                continue
            local = alias.asname or alias.name
            self._module_aliases[local] = f"{module}.{alias.name}"

        self.generic_visit(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        if self._inherits_from_ros_node(node):
            prev_attrs = self._class_attrs
            class_attrs: dict[str, str] = {}
            for stmt in node.body:
                if isinstance(stmt, ast.Assign):
                    for target in stmt.targets:
                        if (
                            isinstance(target, ast.Name)
                            and isinstance(stmt.value, ast.Constant)
                            and isinstance(stmt.value.value, str)
                        ):
                            class_attrs[target.id] = stmt.value.value
            self._class_attrs = class_attrs

            nd = NodeDefinition(
                name=node.name,
                source_symbol=node.name,
                package=self.package,
                language="python",
                file_path=str(self.file_path),
                line=node.lineno,
                source=DataSource.STATIC,
            )
            prev = self._current_node
            self._current_node = nd
            self.generic_visit(node)
            self._current_node = prev
            self.nodes.append(nd)
            self._class_attrs = prev_attrs
        else:
            self.generic_visit(node)

    def visit_Assign(self, node: ast.Assign) -> None:
        """Track ``self.attr = 'literal'`` for resolvable communication names."""
        if self._current_node is not None:
            for target in node.targets:
                if (
                    isinstance(target, ast.Attribute)
                    and isinstance(target.value, ast.Name)
                    and target.value.id == "self"
                    and isinstance(node.value, ast.Constant)
                    and isinstance(node.value.value, str)
                ):
                    self._class_attrs[target.attr] = node.value.value
        self.generic_visit(node)

    def _inherits_from_ros_node(self, cls: ast.ClassDef) -> bool:
        for base in cls.bases:
            raw = _get_attr_name(base)
            if raw in self._node_base_aliases:
                return True
            canonical = self._canonical_name(raw)
            if canonical in _CANONICAL_NODE_BASES:
                return True
        return False

    def _canonical_name(self, raw: str) -> str:
        if not raw:
            return raw
        head, dot, tail = raw.partition(".")
        if head in self._module_aliases:
            base = self._module_aliases[head]
            return f"{base}.{tail}" if dot else base
        return raw

    def _resolve_name(self, arg: ast.expr) -> str:
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            return arg.value
        if (
            isinstance(arg, ast.Attribute)
            and isinstance(arg.value, ast.Name)
            and arg.value.id == "self"
        ):
            return self._class_attrs.get(arg.attr, DYNAMIC_SENTINEL)
        if isinstance(arg, ast.Name):
            return self._class_attrs.get(arg.id, DYNAMIC_SENTINEL)
        return DYNAMIC_SENTINEL

    def _pick_name(self, call: ast.Call, pos_index: int, *kwarg_names: str) -> str:
        if pos_index < len(call.args):
            return self._resolve_name(call.args[pos_index])
        for kw in call.keywords:
            if kw.arg in kwarg_names:
                return self._resolve_name(kw.value)
        return DYNAMIC_SENTINEL

    def _extract_type_arg(self, call: ast.Call, index: int) -> str:
        """Resolve only interface types whose import provenance is statically proven."""
        if index >= len(call.args):
            return "unknown"
        arg = call.args[index]
        if isinstance(arg, ast.Name):
            return self._iface_imports.get(arg.id, "unknown")
        if not isinstance(arg, ast.Attribute):
            return "unknown"

        raw = _get_attr_name(arg)
        canonical = self._canonical_name(raw)
        parts = canonical.split(".")
        marker_index = next((i for i, part in enumerate(parts) if part in _IFACE_MARKERS), -1)
        if marker_index <= 0 or marker_index + 1 >= len(parts):
            return "unknown"

        raw_root = raw.split(".", 1)[0]
        provenance_known = raw_root in self._module_aliases or raw_root in self._module_roots
        if not provenance_known:
            return "unknown"

        package = ".".join(parts[:marker_index])
        interface_name = parts[-1]
        return f"{package}/{interface_name}"

    def _endpoint(
        self,
        node: ast.Call,
        *,
        name: str,
        interface_type: str,
        evidence: str,
    ) -> CommunicationEndpoint:
        explicit = interface_type != "unknown"
        return CommunicationEndpoint(
            name=name,
            msg_type=interface_type,
            file_path=str(self.file_path),
            line=node.lineno,
            evidence=evidence,
            type_source="explicit" if explicit else "unknown",
            confidence="high" if explicit and name != DYNAMIC_SENTINEL else "low",
        )

    def visit_Call(self, node: ast.Call) -> None:
        if self._current_node is None:
            self.generic_visit(node)
            return

        func_name = _get_attr_name(node.func).split(".")[-1]

        if func_name == "__init__" and node.args:
            ros_name = _literal_string(node.args[0])
            if ros_name is not None and _is_super_init(node.func):
                self._current_node.declared_ros_name = ros_name

        if func_name in ("create_publisher", "create_subscription"):
            msg_type = self._extract_type_arg(node, 0)
            topic = self._pick_name(node, 1, "topic", "topic_name")
            ep = self._endpoint(node, name=topic, interface_type=msg_type, evidence=func_name)
            if func_name == "create_publisher":
                self._current_node.publishers.append(ep)
            else:
                self._current_node.subscriptions.append(ep)
            if topic == DYNAMIC_SENTINEL:
                self._current_node.has_dynamic_names = True

        elif func_name == "create_service":
            srv_type = self._extract_type_arg(node, 0)
            name = self._pick_name(node, 1, "srv_name", "service_name")
            self._current_node.services.append(
                self._endpoint(node, name=name, interface_type=srv_type, evidence=func_name)
            )
            if name == DYNAMIC_SENTINEL:
                self._current_node.has_dynamic_names = True

        elif func_name == "create_client":
            srv_type = self._extract_type_arg(node, 0)
            name = self._pick_name(node, 1, "srv_name", "service_name")
            self._current_node.clients.append(
                self._endpoint(node, name=name, interface_type=srv_type, evidence=func_name)
            )
            if name == DYNAMIC_SENTINEL:
                self._current_node.has_dynamic_names = True

        elif func_name in ("create_action_server", "ActionServer"):
            type_idx = 0 if func_name == "create_action_server" else 1
            name_idx = 1 if func_name == "create_action_server" else 2
            action_type = self._extract_type_arg(node, type_idx)
            name = self._pick_name(node, name_idx, "action_name")
            self._current_node.action_servers.append(
                self._endpoint(
                    node,
                    name=name,
                    interface_type=action_type,
                    evidence=func_name,
                )
            )
            if name == DYNAMIC_SENTINEL:
                self._current_node.has_dynamic_names = True

        elif func_name in ("create_action_client", "ActionClient"):
            type_idx = 0 if func_name == "create_action_client" else 1
            name_idx = 1 if func_name == "create_action_client" else 2
            action_type = self._extract_type_arg(node, type_idx)
            name = self._pick_name(node, name_idx, "action_name")
            self._current_node.action_clients.append(
                self._endpoint(
                    node,
                    name=name,
                    interface_type=action_type,
                    evidence=func_name,
                )
            )
            if name == DYNAMIC_SENTINEL:
                self._current_node.has_dynamic_names = True

        self.generic_visit(node)


def _get_attr_name(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _get_attr_name(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    return ""


def _literal_string(node: ast.expr) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _is_super_init(node: ast.expr) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "__init__"
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Name)
        and node.value.func.id == "super"
    )
