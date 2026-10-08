from __future__ import annotations

import ast
from pathlib import Path

from ros2inspector.discovery.file_walker import iter_package_files
from ros2inspector.model.schemas import (
    DYNAMIC_SENTINEL,
    CommunicationEndpoint,
    DataSource,
    NodeDefinition,
    QoSProfile,
)

# (depth, reliability, durability) of the profiles shipped in rclpy.qos.
_QOS_PRESETS = {
    "qos_profile_sensor_data": (5, "best_effort", "volatile"),
    "qos_profile_services_default": (10, "reliable", "volatile"),
    "qos_profile_parameters": (1000, "reliable", "volatile"),
    "qos_profile_parameter_events": (1000, "reliable", "volatile"),
    "qos_profile_action_status_default": (1, "reliable", "transient_local"),
    "sensor_data": (5, "best_effort", "volatile"),
    "services_default": (10, "reliable", "volatile"),
    "parameters": (1000, "reliable", "volatile"),
    "parameter_events": (1000, "reliable", "volatile"),
    "action_status_default": (1, "reliable", "transient_local"),
}

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
        self._parameter_defaults: dict[str, str] = {}
        self._parameter_vars: set[str] = set()
        self._last_name_source = "literal"

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
            prev_parameters = self._parameter_defaults
            prev_parameter_vars = self._parameter_vars
            self._parameter_defaults = {}
            self._parameter_vars = set()
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
            self._parameter_defaults = prev_parameters
            self._parameter_vars = prev_parameter_vars
        else:
            self.generic_visit(node)

    def visit_Assign(self, node: ast.Assign) -> None:
        """Track ``self.attr = 'literal'`` for resolvable communication names."""
        if self._current_node is not None:
            for target in node.targets:
                key = None
                if isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name):
                    if target.value.id == "self":
                        key = target.attr
                elif isinstance(target, ast.Name):
                    key = target.id
                if key:
                    resolved = self._resolve_name(node.value)
                    if resolved != DYNAMIC_SENTINEL:
                        self._class_attrs[key] = resolved
                        if self._from_parameter(node.value):
                            self._parameter_vars.add(key)
                        else:
                            self._parameter_vars.discard(key)
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
        if isinstance(arg, ast.Attribute) and arg.attr in {"value", "string_value"}:
            return self._resolve_name(arg.value)
        if isinstance(arg, ast.Call):
            name = _get_attr_name(arg.func).split(".")[-1]
            if name == "get_parameter" and arg.args:
                key = self._resolve_name(arg.args[0])
                return self._parameter_defaults.get(key, DYNAMIC_SENTINEL)
            if name == "declare_parameter" and len(arg.args) > 1:
                return self._resolve_name(arg.args[1])
            if name == "get_parameter_value" and isinstance(arg.func, ast.Attribute):
                return self._resolve_name(arg.func.value)
        return DYNAMIC_SENTINEL

    def _pick_name(self, call: ast.Call, pos_index: int, *kwarg_names: str) -> str:
        if pos_index < len(call.args):
            arg = call.args[pos_index]
            self._last_name_source = "parameter_default" if self._from_parameter(arg) else "literal"
            value = self._resolve_name(arg)
            if value == DYNAMIC_SENTINEL:
                self._last_name_source = "unresolved"
            return value
        for kw in call.keywords:
            if kw.arg in kwarg_names:
                self._last_name_source = (
                    "parameter_default" if self._from_parameter(kw.value) else "literal"
                )
                value = self._resolve_name(kw.value)
                if value == DYNAMIC_SENTINEL:
                    self._last_name_source = "unresolved"
                return value
        self._last_name_source = "unresolved"
        return DYNAMIC_SENTINEL

    def _from_parameter(self, expr: ast.expr) -> bool:
        if isinstance(expr, ast.Name):
            return expr.id in self._parameter_vars
        if isinstance(expr, ast.Attribute):
            if isinstance(expr.value, ast.Name) and expr.value.id == "self":
                return expr.attr in self._parameter_vars
            return self._from_parameter(expr.value)
        if isinstance(expr, ast.Call):
            method = _get_attr_name(expr.func).split(".")[-1]
            if method in {"get_parameter", "declare_parameter"}:
                return True
            if method == "get_parameter_value" and isinstance(expr.func, ast.Attribute):
                return self._from_parameter(expr.func.value)
        return False

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
        qos: QoSProfile | None = None,
    ) -> CommunicationEndpoint:
        explicit = interface_type != "unknown"
        return CommunicationEndpoint(
            name=name,
            name_source=self._last_name_source,
            msg_type=interface_type,
            file_path=str(self.file_path),
            line=node.lineno,
            evidence=evidence,
            type_source="explicit" if explicit else "unknown",
            confidence=(
                "low"
                if not explicit or name == DYNAMIC_SENTINEL
                else "medium"
                if self._last_name_source == "parameter_default"
                else "high"
            ),
            qos=qos,
        )

    def _qos(self, call: ast.Call, index: int) -> QoSProfile | None:
        expr = (
            call.args[index]
            if len(call.args) > index
            else next((kw.value for kw in call.keywords if kw.arg in {"qos_profile", "qos"}), None)
        )
        if isinstance(expr, ast.Constant) and isinstance(expr.value, int):
            return QoSProfile(
                depth=expr.value, history="keep_last", reliability="reliable", durability="volatile"
            )
        preset = _qos_preset(expr)
        if preset is not None:
            return preset
        if (
            not isinstance(expr, ast.Call)
            or _get_attr_name(expr.func).split(".")[-1] != "QoSProfile"
        ):
            return None
        profile = QoSProfile(reliability="reliable", durability="volatile")
        if (
            expr.args
            and isinstance(expr.args[0], ast.Constant)
            and isinstance(expr.args[0].value, int)
        ):
            profile.depth = expr.args[0].value
            profile.history = "keep_last"
        for kw in expr.keywords:
            if kw.arg == "depth" and isinstance(kw.value, ast.Constant):
                if isinstance(kw.value.value, int):
                    profile.depth = kw.value.value
            elif kw.arg in {"reliability", "durability", "history"}:
                value = _get_attr_name(kw.value).split(".")[-1].lower()
                if value in {
                    "reliable",
                    "best_effort",
                    "volatile",
                    "transient_local",
                    "keep_last",
                    "keep_all",
                }:
                    setattr(profile, kw.arg, value)
        return profile

    def visit_Call(self, node: ast.Call) -> None:
        if self._current_node is None:
            self.generic_visit(node)
            return

        func_name = _get_attr_name(node.func).split(".")[-1]

        if func_name == "declare_parameter" and len(node.args) > 1:
            key = self._resolve_name(node.args[0])
            value = self._resolve_name(node.args[1])
            if key != DYNAMIC_SENTINEL and value != DYNAMIC_SENTINEL:
                self._parameter_defaults[key] = value

        if func_name == "__init__" and node.args:
            ros_name = _literal_string(node.args[0])
            if ros_name is not None and _is_super_init(node.func):
                self._current_node.declared_ros_name = ros_name

        if func_name in ("create_publisher", "create_subscription"):
            msg_type = self._extract_type_arg(node, 0)
            topic = self._pick_name(node, 1, "topic", "topic_name")
            qos_index = 2 if func_name == "create_publisher" else 3
            ep = self._endpoint(
                node,
                name=topic,
                interface_type=msg_type,
                evidence=func_name,
                qos=self._qos(node, qos_index),
            )
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


def _qos_preset(expr: ast.expr | None) -> QoSProfile | None:
    """Resolve ``qos_profile_sensor_data`` and ``QoSPresetProfiles.SENSOR_DATA.value``."""
    if expr is None:
        return None
    if isinstance(expr, ast.Attribute) and expr.attr == "value":
        inner = expr.value
        if isinstance(inner, ast.Attribute) and "QoSPresetProfiles" in _get_attr_name(inner):
            key = inner.attr.lower()
            if key in _QOS_PRESETS and not key.startswith("qos_profile"):
                return _preset_profile(key)
        return None
    if isinstance(expr, (ast.Name, ast.Attribute)):
        name = _get_attr_name(expr).split(".")[-1]
        if name.startswith("qos_profile") and name in _QOS_PRESETS:
            return _preset_profile(name)
    return None


def _preset_profile(key: str) -> QoSProfile:
    depth, reliability, durability = _QOS_PRESETS[key]
    return QoSProfile(
        depth=depth, history="keep_last", reliability=reliability, durability=durability
    )
