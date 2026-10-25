from __future__ import annotations

import ast
import configparser
from pathlib import Path
from typing import Any

from ros2inspector.discovery.file_walker import iter_package_files
from ros2inspector.model.schemas import (
    DYNAMIC_SENTINEL,
    CommunicationEndpoint,
    DataSource,
    NodeDefinition,
    QoSProfile,
)
from ros2inspector.static.python_entrypoints import find_python_entrypoints
from ros2inspector.static.python_source import read_python_tree, source_failure

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
# Endpoint factories: (communication kind, graph relation, name argument position, keyword).
_ENDPOINT_FACTORIES = {
    "create_publisher": ("topic", "publishes", 1, "topic"),
    "create_subscription": ("topic", "subscribes", 1, "topic"),
    "create_service": ("service", "provides", 1, "srv_name"),
    "create_client": ("service", "calls", 1, "srv_name"),
    "ActionServer": ("action", "provides", 2, "action_name"),
    "ActionClient": ("action", "calls", 2, "action_name"),
}
_CANONICAL_NODE_BASES = {
    "rclpy.node.Node",
    "rclpy.lifecycle.LifecycleNode",
    "rclpy_lifecycle.LifecycleNode",
}


def parse_python_nodes(
    package_path: Path, package_name: str, diagnostics: list[dict[str, Any]] | None = None
) -> list[NodeDefinition]:
    return parse_python_workspace_nodes({package_name: package_path}, diagnostics)[package_name]


def _package_directories(package_path: Path) -> dict[str, str]:
    mappings: dict[str, str] = {}
    setup = package_path / "setup.py"
    try:
        tree = ast.parse(setup.read_bytes())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            for kw in node.keywords:
                if kw.arg == "package_dir":
                    value = ast.literal_eval(kw.value)
                    if isinstance(value, dict):
                        mappings.update(
                            {
                                k: v
                                for k, v in value.items()
                                if isinstance(k, str) and isinstance(v, str)
                            }
                        )
    except (OSError, SyntaxError, ValueError, RecursionError, MemoryError):
        pass
    try:
        config = configparser.ConfigParser(interpolation=None)
        config.read_string((package_path / "setup.cfg").read_text(encoding="utf-8"))
        for line in config.get("options", "package_dir", fallback="").splitlines():
            prefix, separator, directory = line.partition("=")
            if separator:
                mappings[prefix.strip()] = directory.strip()
    except (OSError, UnicodeDecodeError, configparser.Error):
        pass
    return mappings


def _module_name(path: Path, package_path: Path, directories: dict[str, str]) -> str:
    relative = path.relative_to(package_path).with_suffix("")
    candidates = [
        (prefix, Path(directory))
        for prefix, directory in directories.items()
        if relative.is_relative_to(directory)
    ]
    if candidates:
        prefix, directory = max(candidates, key=lambda item: len(item[1].parts))
        parts = [*prefix.split("."), *relative.relative_to(directory).parts]
    else:
        parts = list(relative.parts[1:] if relative.parts[0] == "src" else relative.parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    return ".".join(p for p in parts if p)


def parse_python_workspace_nodes(
    package_paths: dict[str, Path], diagnostics: list[dict[str, Any]] | None = None
) -> dict[str, list[NodeDefinition]]:
    errors = diagnostics if diagnostics is not None else []
    sources: list[tuple[str, Path, str, ast.Module]] = []
    entry_functions: dict[tuple[str, str], set[str]] = {}
    for package, path in package_paths.items():
        for entry in find_python_entrypoints(path).values():
            if entry.callable:
                entry_functions.setdefault((package, entry.module), {"main"}).add(entry.callable)
    for package_name, package_path in package_paths.items():
        directories = _package_directories(package_path)
        for path in iter_package_files(package_path, suffixes={".py"}):
            tree = read_python_tree(path, errors)
            if tree is None:
                continue
            module = _module_name(path, package_path, directories)
            sources.append((package_name, path, module, tree))
    known: dict[str, NodeDefinition] = {}
    failed: set[Path] = set()
    inherited_symbols: set[str] = set()
    instantiated_symbols: set[str] = set()
    workspace_modules = set(package_paths) | {module.split(".")[0] for _, _, module, _ in sources}
    while True:
        count = len(known)
        for package_name, path, module, tree in sources:
            if path in failed:
                continue
            visitor = _NodeVisitor(
                package_name,
                path,
                module,
                known,
                workspace_modules,
                entry_functions.get((package_name, module)),
            )
            try:
                visitor.visit(tree)
            except (RecursionError, MemoryError) as exc:
                source_failure(path, exc, errors)
                failed.add(path)
                continue
            inherited_symbols.update(visitor.inherited_symbols)
            instantiated_symbols.update(visitor.instantiated_symbols)
            for node in visitor.nodes:
                if not node.analysis_incomplete:
                    known[f"{module}.{node.source_symbol}"] = node
        if len(known) == count:
            break
    nodes: dict[str, list[NodeDefinition]] = {package: [] for package in package_paths}
    for package, path, module, tree in sources:
        if path in failed:
            continue
        visitor = _NodeVisitor(
            package, path, module, known, workspace_modules, entry_functions.get((package, module))
        )
        try:
            visitor.visit(tree)
        except (RecursionError, MemoryError) as exc:
            source_failure(path, exc, errors)
            continue
        for item in visitor.unattributed:
            diagnostic = {
                "severity": "warning",
                "code": "source_analysis_incomplete",
                "file": str(path),
                "package": package,
                **item,
            }
            if diagnostic not in errors:
                errors.append(diagnostic)
        for node in visitor.nodes:
            symbol = f"{module}.{node.source_symbol}"
            node.is_abstract = symbol in inherited_symbols and symbol not in instantiated_symbols
            nodes[package].append(node)
    return nodes


class _NodeVisitor(ast.NodeVisitor):
    def __init__(
        self,
        package: str,
        file_path: Path,
        module: str = "",
        known: dict[str, NodeDefinition] | None = None,
        workspace_modules: set[str] | None = None,
        entry_functions: set[str] | None = None,
    ) -> None:
        self._workspace_modules = workspace_modules or set()
        self._entry_functions = entry_functions or {"main"}
        self.module = module
        self._known = known if known is not None else {}
        self._variables: dict[str, NodeDefinition] = {}
        self._scope: list[str] = []
        self.package = package
        self.file_path = file_path
        self.nodes: list[NodeDefinition] = []
        self.unattributed: list[dict[str, Any]] = []
        self.inherited_symbols: set[str] = set()
        self.instantiated_symbols: set[str] = set()
        self._entrypoint_instances: set[str] = set()
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
        self._parameter_keys: dict[str, str] = {}
        self._last_parameter: str | None = None
        self._last_name_source = "literal"

    def visit_Module(self, node: ast.Module) -> None:
        self._entrypoint_instances = {
            _get_attr_name(call.func)
            for statement in node.body
            if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef))
            and statement.name in self._entry_functions
            for call in ast.walk(statement)
            if isinstance(call, ast.Call)
        }
        self.generic_visit(node)

    def visit_BinOp(self, node: ast.expr) -> None:
        pending: list[ast.AST] = list(ast.iter_child_nodes(node))
        while pending:
            child = pending.pop()
            if isinstance(child, (ast.Call, ast.Lambda, ast.NamedExpr)):
                self.visit(child)
            else:
                pending.extend(ast.iter_child_nodes(child))

    visit_UnaryOp = visit_BinOp
    visit_BoolOp = visit_BinOp

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            if alias.asname:
                self._module_aliases[alias.asname] = alias.name
            else:
                self._module_roots.add(alias.name.split(".", 1)[0])
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = node.module
        if node.level:
            parts = self.module.split(".")
            if self.file_path.name == "__init__.py":
                parts.append("__init__")
            parent = parts[: -node.level]
            module = ".".join([*parent, *([module] if module else [])])
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

    def visit_FunctionDef(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        previous = self._variables
        self._variables = {}
        self._scope.append(node.name)
        self.generic_visit(node)
        self._scope.pop()
        self._variables = previous

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        proven = self._inherits_from_ros_node(node)
        endpoint_methods = {
            "create_publisher",
            "create_subscription",
            "create_service",
            "create_client",
        }
        relevant = any(
            isinstance(expr, ast.Call)
            and isinstance(expr.func, ast.Attribute)
            and isinstance(expr.func.value, ast.Name)
            and expr.func.value.id == "self"
            and expr.func.attr in endpoint_methods
            for expr in ast.walk(node)
        )
        imported_node_base = any(
            (canonical := self._canonical_name(_get_attr_name(base))).split(".")[0]
            in self._workspace_modules
            or (
                node.name in self._entrypoint_instances
                and canonical.split(".")[0]
                in {v.split(".")[0] for v in self._module_aliases.values()}
            )
            for base in node.bases
        )
        if proven or (node.bases and (relevant or imported_node_base)):
            prev_attrs = self._class_attrs
            prev_keys = self._parameter_keys
            self._parameter_keys = {}
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
            if not proven:
                nd.analysis_incomplete = True
                nd.analysis_notes.append(
                    "Cannot resolve node base: "
                    + ", ".join(_get_attr_name(base) for base in node.bases)
                )
            for base in node.bases:
                raw = self._canonical_name(_get_attr_name(base))
                inherited = self._known.get(raw) or self._known.get(f"{self.module}.{raw}")
                if inherited is not None:
                    self.inherited_symbols.add(
                        raw if raw in self._known else f"{self.module}.{raw}"
                    )
                    nd.declared_ros_name = inherited.declared_ros_name
                    for field in (
                        "publishers",
                        "subscriptions",
                        "services",
                        "clients",
                        "action_servers",
                        "action_clients",
                    ):
                        setattr(
                            nd,
                            field,
                            [ep.model_copy(deep=True) for ep in getattr(inherited, field)],
                        )
            prev = self._current_node
            self._current_node = nd
            self.generic_visit(node)
            self._current_node = prev
            self.nodes.append(nd)
            self._class_attrs = prev_attrs
            self._parameter_keys = prev_keys
            self._parameter_defaults = prev_parameters
            self._parameter_vars = prev_parameter_vars
        else:
            self.generic_visit(node)

    def visit_Assign(self, node: ast.Assign) -> None:
        """Track ``self.attr = 'literal'`` for resolvable communication names."""
        if isinstance(node.value, ast.Call):
            canonical = self._canonical_name(_get_attr_name(node.value.func))
            if canonical in _CANONICAL_NODE_BASES | {"rclpy.create_node"}:
                for target in node.targets:
                    if not isinstance(target, ast.Name):
                        continue
                    name = self._pick_name(node.value, 0, "node_name")
                    definition = NodeDefinition(
                        name=name if name != DYNAMIC_SENTINEL else target.id,
                        source_symbol=".".join([*self._scope, target.id]),
                        declared_ros_name=name,
                        package=self.package,
                        language="python",
                        file_path=str(self.file_path),
                        line=node.lineno,
                        has_dynamic_names=name == DYNAMIC_SENTINEL,
                    )
                    self._variables[target.id] = definition
                    self.nodes.append(definition)
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
                            parameter_key = self._parameter_key(node.value)
                            if parameter_key is not None:
                                self._parameter_keys[key] = parameter_key
                        else:
                            self._parameter_vars.discard(key)
                            self._parameter_keys.pop(key, None)
        self.generic_visit(node)

    def _inherits_from_ros_node(self, cls: ast.ClassDef) -> bool:
        for base in cls.bases:
            raw = _get_attr_name(base)
            if raw in self._node_base_aliases:
                return True
            canonical = self._canonical_name(raw)
            if (
                canonical in _CANONICAL_NODE_BASES
                or canonical in self._known
                or f"{self.module}.{canonical}" in self._known
            ):
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
            self._last_parameter = self._parameter_key(arg)
            self._last_name_source = "parameter_default" if self._from_parameter(arg) else "literal"
            value = self._resolve_name(arg)
            if value == DYNAMIC_SENTINEL:
                self._last_name_source = "unresolved"
            return value
        for kw in call.keywords:
            if kw.arg in kwarg_names:
                self._last_parameter = self._parameter_key(kw.value)
                self._last_name_source = (
                    "parameter_default" if self._from_parameter(kw.value) else "literal"
                )
                value = self._resolve_name(kw.value)
                if value == DYNAMIC_SENTINEL:
                    self._last_name_source = "unresolved"
                return value
        self._last_name_source = "unresolved"
        return DYNAMIC_SENTINEL

    def _parameter_key(self, expr: ast.expr) -> str | None:
        if isinstance(expr, ast.Name):
            return self._parameter_keys.get(expr.id)
        if isinstance(expr, ast.Attribute):
            if isinstance(expr.value, ast.Name) and expr.value.id == "self":
                return self._parameter_keys.get(expr.attr)
            return self._parameter_key(expr.value)
        if isinstance(expr, ast.Call):
            method = _get_attr_name(expr.func).split(".")[-1]
            if method in {"get_parameter", "declare_parameter"} and expr.args:
                return _literal_string(expr.args[0])
            if method == "get_parameter_value" and isinstance(expr.func, ast.Attribute):
                return self._parameter_key(expr.func.value)
        return None

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
            name_parameter=self._last_parameter,
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

    def _endpoint_method(self, func: ast.expr) -> str:
        canonical = self._canonical_name(_get_attr_name(func))
        if canonical in {
            "rclpy.action.ActionClient",
            "rclpy.action.client.ActionClient",
            "rclpy.action.ActionServer",
            "rclpy.action.server.ActionServer",
        }:
            return canonical.rsplit(".", 1)[-1]
        return _get_attr_name(func).rsplit(".", 1)[-1]

    def visit_Call(self, node: ast.Call) -> None:
        canonical = self._canonical_name(_get_attr_name(node.func))
        self.instantiated_symbols.add(
            canonical if canonical in self._known else f"{self.module}.{canonical}"
        )
        previous = self._current_node
        method = self._endpoint_method(node.func)
        receiver = node.func.value if isinstance(node.func, ast.Attribute) else None
        if method in {"ActionServer", "ActionClient"} and node.args:
            receiver = node.args[0]
        if isinstance(receiver, ast.Name) and receiver.id in self._variables:
            self._current_node = self._variables[receiver.id]
        if method not in _ENDPOINT_FACTORIES and len(node.args) >= 2:
            first = node.args[0]
            owner = self._variables.get(first.id) if isinstance(first, ast.Name) else None
            if isinstance(first, ast.Name) and first.id == "self":
                owner = previous
            if owner is not None and self._extract_type_arg(node, 1) != "unknown":
                owner.analysis_incomplete = True
                note = "Cannot resolve endpoint constructor: " + ast.unparse(node.func)
                if note not in owner.analysis_notes:
                    owner.analysis_notes.append(note)
        factory = _ENDPOINT_FACTORIES.get(method)
        name_arg: ast.expr | None = None
        if factory is not None:
            position, keyword = factory[2], factory[3]
            name_arg = node.args[position] if len(node.args) > position else None
            for kw in node.keywords:
                if kw.arg == keyword:
                    name_arg = kw.value
        # Without a name argument the call is not a ROS endpoint (e.g. a cloud SDK's create_client).
        if (
            factory is not None
            and name_arg is not None
            and not (
                isinstance(receiver, ast.Name)
                and (
                    receiver.id in self._variables or receiver.id == "self" and previous is not None
                )
            )
        ):
            literal = name_arg.value if isinstance(name_arg, ast.Constant) else None
            self.unattributed.append(
                {
                    "message": "Cannot attribute endpoint receiver: " + ast.unparse(node.func),
                    "endpoint_kind": factory[0],
                    "endpoint_rel": factory[1],
                    "endpoint_name": literal if isinstance(literal, str) else None,
                }
            )
            for definition in self.nodes + ([previous] if previous is not None else []):
                definition.analysis_incomplete = True
                note = "Cannot attribute endpoint receiver: " + ast.unparse(node.func)
                if note not in definition.analysis_notes:
                    definition.analysis_notes.append(note)
            self._current_node = previous
            self.generic_visit(node)
            return
        try:
            self._visit_node_call(node)
        finally:
            self._current_node = previous

    def _visit_node_call(self, node: ast.Call) -> None:
        if self._current_node is None:
            self.generic_visit(node)
            return

        func_name = self._endpoint_method(node.func)

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
