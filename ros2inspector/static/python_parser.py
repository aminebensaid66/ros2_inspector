import ast
from pathlib import Path

from ros2inspector.model.schemas import (
    DYNAMIC_SENTINEL,
    CommunicationEndpoint,
    DataSource,
    NodeDefinition,
)

# Handled import module markers for interface type resolution
_IFACE_MARKERS = (".msg", ".srv", ".action")


def parse_python_nodes(package_path: Path, package_name: str) -> list[NodeDefinition]:
    nodes: list[NodeDefinition] = []
    for py_file in package_path.rglob("*.py"):
        try:
            source = py_file.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(py_file))
        except (SyntaxError, UnicodeDecodeError):
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
        # local name → "pkg/Type" resolved from msg/srv/action imports
        self._imports: dict[str, str] = {}
        # statically resolvable string constants: class attrs + self.attr assignments
        self._class_attrs: dict[str, str] = {}

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        """Resolve msg/srv/action imports → {'MyMsg': 'my_pkg/MyMsg'}."""
        if not node.module:
            self.generic_visit(node)
            return
        for marker in _IFACE_MARKERS:
            if marker in node.module:
                pkg = node.module.split(marker)[0]
                for alias in node.names:
                    local = alias.asname or alias.name
                    self._imports[local] = f"{pkg}/{alias.name}"
                break
        self.generic_visit(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        if _inherits_from_node(node):
            prev_attrs = self._class_attrs
            # Pre-collect class-level string constants (e.g. TOPIC = '/chatter')
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
        """Track self.attr = 'string' so topic/service names are resolvable."""
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

    def _resolve_name(self, arg: ast.expr) -> str:
        """Resolve a topic/service name: string literal, self.attr, or bare name."""
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
        """Return the name from positional arg or keyword arg, resolved."""
        if pos_index < len(call.args):
            return self._resolve_name(call.args[pos_index])
        for kw in call.keywords:
            if kw.arg in kwarg_names:
                return self._resolve_name(kw.value)
        return DYNAMIC_SENTINEL

    def _endpoint(
        self,
        node: ast.Call,
        *,
        name: str,
        interface_type: str,
        evidence: str,
    ) -> CommunicationEndpoint:
        return CommunicationEndpoint(
            name=name,
            msg_type=interface_type,
            file_path=str(self.file_path),
            line=node.lineno,
            evidence=evidence,
            type_source="explicit" if interface_type != "unknown" else "unknown",
            confidence=(
                "high" if interface_type != "unknown" and name != DYNAMIC_SENTINEL else "low"
            ),
        )

    def visit_Call(self, node: ast.Call) -> None:
        if self._current_node is None:
            self.generic_visit(node)
            return

        func_name = _get_attr_name(node.func).split(".")[-1]

        if func_name in ("create_publisher", "create_subscription"):
            msg_type = _extract_type_arg(node, 0, self._imports)
            topic = self._pick_name(node, 1, "topic")
            ep = self._endpoint(node, name=topic, interface_type=msg_type, evidence=func_name)
            if func_name == "create_publisher":
                self._current_node.publishers.append(ep)
            else:
                self._current_node.subscriptions.append(ep)
            if topic == DYNAMIC_SENTINEL:
                self._current_node.has_dynamic_names = True

        elif func_name == "create_service":
            srv_type = _extract_type_arg(node, 0, self._imports)
            name = self._pick_name(node, 1, "srv_name", "service_name")
            self._current_node.services.append(
                self._endpoint(
                    node,
                    name=name,
                    interface_type=srv_type,
                    evidence=func_name,
                )
            )
            if name == DYNAMIC_SENTINEL:
                self._current_node.has_dynamic_names = True

        elif func_name == "create_client":
            srv_type = _extract_type_arg(node, 0, self._imports)
            name = self._pick_name(node, 1, "srv_name", "service_name")
            self._current_node.clients.append(
                self._endpoint(
                    node,
                    name=name,
                    interface_type=srv_type,
                    evidence=func_name,
                )
            )
            if name == DYNAMIC_SENTINEL:
                self._current_node.has_dynamic_names = True

        elif func_name in ("create_action_server", "ActionServer"):
            # create_action_server(type, name, ...)  vs  ActionServer(self, type, name, cb)
            type_idx = 0 if func_name == "create_action_server" else 1
            name_idx = 1 if func_name == "create_action_server" else 2
            action_type = _extract_type_arg(node, type_idx, self._imports)
            name = self._pick_name(node, name_idx, "action_name")
            ep = self._endpoint(
                node,
                name=name,
                interface_type=action_type,
                evidence=func_name,
            )
            self._current_node.action_servers.append(ep)
            if name == DYNAMIC_SENTINEL:
                self._current_node.has_dynamic_names = True

        elif func_name in ("create_action_client", "ActionClient"):
            # create_action_client(type, name, ...)  vs  ActionClient(self, type, name)
            type_idx = 0 if func_name == "create_action_client" else 1
            name_idx = 1 if func_name == "create_action_client" else 2
            action_type = _extract_type_arg(node, type_idx, self._imports)
            name = self._pick_name(node, name_idx, "action_name")
            ep = self._endpoint(
                node,
                name=name,
                interface_type=action_type,
                evidence=func_name,
            )
            self._current_node.action_clients.append(ep)
            if name == DYNAMIC_SENTINEL:
                self._current_node.has_dynamic_names = True

        self.generic_visit(node)


def _inherits_from_node(cls: ast.ClassDef) -> bool:
    for base in cls.bases:
        name = _get_attr_name(base)
        if name in ("Node", "rclpy.node.Node", "LifecycleNode", "rclpy_lifecycle.LifecycleNode"):
            return True
    return False


def _get_attr_name(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{_get_attr_name(node.value)}.{node.attr}"
    return ""


def _extract_type_arg(call: ast.Call, index: int, imports: dict[str, str]) -> str:
    """Extract ROS interface type from the type argument at the given positional index."""
    if index >= len(call.args):
        return "unknown"
    arg = call.args[index]
    if isinstance(arg, ast.Name):
        return imports.get(arg.id, arg.id)
    if isinstance(arg, ast.Attribute):
        full = _get_attr_name(arg)
        parts = full.split(".")
        for marker in ("msg", "srv", "action"):
            if marker in parts:
                idx = parts.index(marker)
                if idx > 0 and idx + 1 < len(parts):
                    return f"{parts[idx - 1]}/{parts[-1]}"
        return full
    return "unknown"
