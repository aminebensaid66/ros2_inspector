import re
from collections import deque
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import tree_sitter_cpp
from tree_sitter import Language, Node, Parser

from ros2inspector.discovery.file_walker import iter_package_files
from ros2inspector.model.schemas import (
    DYNAMIC_SENTINEL,
    CommunicationEndpoint,
    DataSource,
    NodeDefinition,
    QoSProfile,
)

_CPP_LANG = Language(tree_sitter_cpp.language())

_PUBLISHER_CALLS = {"create_publisher"}
_SUBSCRIPTION_CALLS = {"create_subscription"}
_SERVICE_CALLS = {"create_service"}
_CLIENT_CALLS = {"create_client"}
_ACTION_SERVER_CALLS = {"create_action_server"}
_ACTION_CLIENT_CALLS = {"create_action_client"}

_ALL_CREATE_CALLS = (
    _PUBLISHER_CALLS
    | _SUBSCRIPTION_CALLS
    | _SERVICE_CALLS
    | _CLIENT_CALLS
    | _ACTION_SERVER_CALLS
    | _ACTION_CLIENT_CALLS
)

# rclcpp_action free functions: rclcpp_action::create_server<T>(node, "name", ...)
# and rclcpp_action::create_client<T>(node, "name").  Their bare names ("create_server",
# "create_client") are too generic to add to _ALL_CREATE_CALLS, so they are detected
# by checking the rclcpp_action namespace qualifier separately.
_RCLCPP_ACTION_NS = "rclcpp_action"
_RCLCPP_ACTION_FREE_FUNC_MAP: dict[str, str] = {
    "create_server": "server",
    "create_client": "client",
}


def parse_cpp_nodes(
    package_path: Path,
    package_name: str,
    known_node_bases: set[str] | None = None,
    *,
    factory_patterns: list[dict[str, Any]] | None = None,
) -> list[NodeDefinition]:
    parser = Parser(_CPP_LANG)
    nodes: list[NodeDefinition] = []
    parsed: list[tuple[Path, bytes, Node]] = []

    for cpp_file in _iter_cpp_files(package_path):
        try:
            source = cpp_file.read_bytes()
            tree = parser.parse(source)
        except Exception:
            continue

        parsed.append((cpp_file, source, tree.root_node))
        for node_def in _extract_nodes(
            tree.root_node, source, package_name, cpp_file, factory_patterns or []
        ):
            nodes.append(node_def)

    _discover_indirect_nodes(
        nodes, parsed, package_name, known_node_bases or set(), factory_patterns or []
    )
    _attach_out_of_class_calls(nodes, parsed, factory_patterns or [])
    _discover_direct_node_objects(nodes, parsed, package_name, factory_patterns or [])
    return nodes


_CPP_SUFFIXES = frozenset((".cpp", ".cxx", ".cc", ".hpp", ".h"))


def _iter_cpp_files(root: Path) -> Iterator[Path]:
    yield from iter_package_files(root, suffixes=_CPP_SUFFIXES)


def _extract_nodes(
    root_node: Node,
    source: bytes,
    package: str,
    file_path: Path,
    factory_patterns: list[dict[str, Any]],
) -> list[NodeDefinition]:
    results: list[NodeDefinition] = []

    # Find class definitions that inherit from rclcpp::Node
    for class_node in _query_nodes(root_node, "class_specifier"):
        if not _inherits_from_rclcpp_node(class_node, source):
            continue

        name = _get_class_name(class_node, source)
        if not name:
            continue

        nd = NodeDefinition(
            name=name,
            source_symbol=_qualified_symbol(class_node, source, name),
            declared_ros_name=_extract_declared_ros_name(class_node, source),
            package=package,
            language="cpp",
            file_path=str(file_path),
            line=class_node.start_point[0] + 1,
            source=DataSource.STATIC,
        )

        # Walk the class body for create_* calls
        _collect_calls(class_node, source, nd, factory_patterns=factory_patterns)
        results.append(nd)

    return results


_RCLCPP_NODE_RE = re.compile(r"\brclcpp(?:_lifecycle)?::(LifecycleNode|Node)\b")


def _inherits_from_rclcpp_node(class_node: Node, source: bytes) -> bool:
    base_clause = _first_child_of_type(class_node, "base_class_clause")
    if base_clause is None:
        return False
    text = source[base_clause.start_byte : base_clause.end_byte].decode("utf-8", errors="replace")
    return bool(_RCLCPP_NODE_RE.search(text))


def _get_class_name(class_node: Node, source: bytes) -> str | None:
    name_node = _first_child_of_type(class_node, "type_identifier")
    if name_node is None:
        return None
    return source[name_node.start_byte : name_node.end_byte].decode("utf-8", errors="replace")


_CPP_NODE_INIT_RE = re.compile(
    r"(?:^|[:,])\s*(?:rclcpp(?:_lifecycle)?::)?(?:LifecycleNode|Node)"
    r'\s*\(\s*"((?:[^"\\]|\\.)*)"',
    re.MULTILINE,
)


def _extract_declared_ros_name(class_node: Node, source: bytes) -> str | None:
    """Extract a literal node name from a C++ constructor initializer list."""
    text = source[class_node.start_byte : class_node.end_byte].decode("utf-8", errors="replace")
    match = _CPP_NODE_INIT_RE.search(text)
    return match.group(1) if match else None


def _get_rclcpp_action_kind(func_node: Node, source: bytes) -> str | None:
    """Return 'server' or 'client' for rclcpp_action free-function calls, else None.

    Handles:
        rclcpp_action::create_server<ActionT>(node, "name", goal_cb, cancel_cb, accepted_cb)
        rclcpp_action::create_client<ActionT>(node, "name")

    These cannot be matched by bare method name alone because "create_server" and
    "create_client" are generic identifiers.  We verify the rclcpp_action namespace
    by inspecting the raw text of the qualified_identifier node.
    """
    text = source[func_node.start_byte : func_node.end_byte].decode("utf-8", errors="replace")
    match = re.match(r"^rclcpp_action::(create_server|create_client)\s*(?:<|$)", text)
    return _RCLCPP_ACTION_FREE_FUNC_MAP.get(match.group(1)) if match else None


def _collect_calls(
    node: Node,
    source: bytes,
    nd: NodeDefinition,
    receiver: str | None = None,
    factory_patterns: list[dict[str, Any]] | None = None,
    context: bytes | None = None,
) -> None:
    parameter_names = _cpp_parameter_names(node, source, context)
    aliases = dict(
        re.findall(
            r"using\s+(\w+)\s*=\s*([^;]+);", (context or source).decode("utf-8", errors="replace")
        )
    )
    for call_node in _query_nodes(node, "call_expression"):
        func_node = call_node.child_by_field_name("function")
        if func_node is None:
            continue

        args_node = call_node.child_by_field_name("arguments")
        function_text = source[func_node.start_byte : func_node.end_byte].decode(
            "utf-8", errors="replace"
        )
        if receiver is not None:
            args = args_node.named_children if args_node is not None else []
            owner_text = source[args[0].start_byte : args[0].end_byte].decode() if args else ""
            if not (
                function_text.startswith(receiver + "->")
                or function_text.startswith(receiver + ".")
                or (_get_rclcpp_action_kind(func_node, source) and owner_text == receiver)
            ):
                continue

        expanded_function = function_text
        for _ in range(len(aliases)):
            expanded_function = re.sub(
                r"\b[A-Za-z_]\w*\b",
                lambda match: aliases.get(match.group(), match.group()),
                expanded_function,
            )
        for pattern in factory_patterns or []:
            call_name = str(pattern["call"])
            if not re.search(rf"(?<![\w:]){re.escape(call_name)}(?![\w:])", expanded_function):
                continue
            name, name_source = _cpp_name(
                args_node, int(pattern["name_arg"]), source, parameter_names
            )
            msg_type = str(pattern.get("msg_type", "unknown"))
            if msg_type == "unknown":
                template = re.search(rf"{re.escape(call_name)}\s*<\s*([\w:]+)", expanded_function)
                if template:
                    msg_type = _cpp_type_to_ros(template.group(1))
            ep = CommunicationEndpoint(
                name=name,
                name_source=name_source,
                msg_type=msg_type,
                file_path=nd.file_path,
                line=call_node.start_point[0] + 1,
                evidence=function_text,
                type_source="factory_pattern" if msg_type != "unknown" else "unknown",
                confidence="medium" if name != DYNAMIC_SENTINEL else "low",
            )
            field = {
                "publisher": "publishers",
                "subscription": "subscriptions",
                "service": "services",
                "client": "clients",
                "action_server": "action_servers",
                "action_client": "action_clients",
            }[str(pattern["kind"])]
            getattr(nd, field).append(ep)
            if name == DYNAMIC_SENTINEL:
                nd.has_dynamic_names = True

        # rclcpp_action free functions must be checked before the generic dispatch
        # because their bare names ("create_server", "create_client") would otherwise
        # be missed entirely — _resolve_method_name returns "" for template_function
        # nodes whose child is a qualified_identifier rather than a bare identifier.
        action_kind = _get_rclcpp_action_kind(func_node, source)
        if action_kind is not None:
            topic_name, name_source = _cpp_name(args_node, 1, source, parameter_names)
            cpp_type = _extract_cpp_type(func_node, source)
            ep = CommunicationEndpoint(
                name=topic_name,
                name_source=name_source,
                msg_type=cpp_type,
                file_path=nd.file_path,
                line=call_node.start_point[0] + 1,
                evidence="rclcpp_action",
                type_source="explicit" if cpp_type != "unknown" else "unknown",
                confidence=(
                    "low"
                    if cpp_type == "unknown" or topic_name == DYNAMIC_SENTINEL
                    else "medium"
                    if name_source == "parameter_default"
                    else "high"
                ),
            )
            if action_kind == "server":
                nd.action_servers.append(ep)
            else:
                nd.action_clients.append(ep)
            if topic_name == DYNAMIC_SENTINEL:
                nd.has_dynamic_names = True
            continue

        method_name = _resolve_method_name(func_node, source)
        if method_name not in _ALL_CREATE_CALLS:
            continue

        topic_name, name_source = _cpp_name(args_node, 0, source, parameter_names)
        cpp_type = _extract_cpp_type(func_node, source)
        ep = CommunicationEndpoint(
            name=topic_name,
            name_source=name_source,
            msg_type=cpp_type,
            file_path=nd.file_path,
            line=call_node.start_point[0] + 1,
            evidence=method_name,
            type_source="explicit" if cpp_type != "unknown" else "unknown",
            confidence=(
                "low"
                if cpp_type == "unknown" or topic_name == DYNAMIC_SENTINEL
                else "medium"
                if name_source == "parameter_default"
                else "high"
            ),
            qos=_extract_cpp_qos(call_node, source),
        )

        if method_name in _PUBLISHER_CALLS:
            nd.publishers.append(ep)
        elif method_name in _SUBSCRIPTION_CALLS:
            nd.subscriptions.append(ep)
        elif method_name in _SERVICE_CALLS:
            nd.services.append(ep)
        elif method_name in _CLIENT_CALLS:
            nd.clients.append(ep)
        elif method_name in _ACTION_SERVER_CALLS:
            nd.action_servers.append(ep)
        elif method_name in _ACTION_CLIENT_CALLS:
            nd.action_clients.append(ep)

        if topic_name == DYNAMIC_SENTINEL:
            nd.has_dynamic_names = True


def _extract_cpp_qos(call_node: Node, source: bytes) -> QoSProfile | None:
    args = call_node.child_by_field_name("arguments")
    if args is None or len(args.named_children) < 2:
        return None
    arg = args.named_children[1]
    text = source[arg.start_byte : arg.end_byte].decode("utf-8", errors="replace")
    profile = QoSProfile(reliability="reliable", durability="volatile")
    presets = {
        "SensorDataQoS": (5, "best_effort", "volatile"),
        "ParametersQoS": (1000, "reliable", "volatile"),
        "ParameterEventsQoS": (1000, "reliable", "volatile"),
        "ServicesQoS": (10, "reliable", "volatile"),
        "SystemDefaultsQoS": (None, "system_default", "system_default"),
    }
    preset = next((v for k, v in presets.items() if re.search(rf"\b{k}\s*\(", text)), None)
    depth = re.search(r"(?:QoS|KeepLast|keep_last)\s*\(\s*(\d+)\s*\)", text)
    if text.strip().isdigit():
        profile.depth = int(text.strip())
        profile.history = "keep_last"
    elif preset:
        profile.depth, profile.reliability, profile.durability = preset
        profile.history = "keep_last" if profile.depth is not None else "system_default"
    elif depth:
        profile.depth = int(depth.group(1))
        profile.history = "keep_last"
    elif re.search(r"KeepAll\s*\(", text):
        profile.history = "keep_all"
    else:
        return None
    for method, field, value in (
        ("best_effort", "reliability", "best_effort"),
        ("reliable", "reliability", "reliable"),
        ("transient_local", "durability", "transient_local"),
        ("durability_volatile", "durability", "volatile"),
        ("keep_all", "history", "keep_all"),
        ("liveliness_automatic", "liveliness", "automatic"),
        ("liveliness_manual_by_topic", "liveliness", "manual_by_topic"),
    ):
        if re.search(rf"\.\s*{method}\s*\(\s*\)", text):
            setattr(profile, field, value)
    for policy in ("deadline", "liveliness_lease_duration"):
        duration = re.search(
            rf"\.\s*{policy}\s*\(\s*rclcpp::Duration\s*\(\s*(\d+)\s*,\s*(\d+)\s*\)", text
        )
        if duration:
            setattr(profile, policy, int(duration.group(1)) + int(duration.group(2)) / 1e9)
    return profile


def _extract_cpp_type(func_node: Node, source: bytes) -> str:
    """Extract the first template type arg from a templated call (create_publisher<T>)."""
    node_type = func_node.type
    if node_type == "qualified_identifier":
        name = func_node.child_by_field_name("name")
        if name is not None:
            return _extract_cpp_type(name, source)
    if node_type == "field_expression":
        for child in func_node.children:
            if child.type == "template_method":
                return _extract_cpp_type(child, source)
    if node_type in ("template_method", "template_function"):
        for child in func_node.children:
            if child.type == "template_argument_list":
                return _parse_cpp_template_arg(child, source)
    return "unknown"


def _parse_cpp_template_arg(args_node: Node, source: bytes) -> str:
    """Return the first type from a template_argument_list as 'pkg/Type'."""
    for child in args_node.children:
        if child.type in ("type_descriptor", "qualified_identifier", "type_identifier"):
            raw = (
                source[child.start_byte : child.end_byte].decode("utf-8", errors="replace").strip()
            )
            return _cpp_type_to_ros(raw)
    return "unknown"


def _cpp_type_to_ros(cpp_type: str) -> str:
    """Convert a concrete ROS C++ interface type to ``pkg/Type``.

    Bare identifiers may be template parameters or aliases and cannot be
    resolved safely without semantic C++ analysis, so keep them unknown rather
    than presenting them as explicit/high-confidence interface types.
    """
    parts = [p.strip() for p in cpp_type.split("::") if p.strip()]
    for marker in ("msg", "srv", "action"):
        if marker in parts:
            idx = parts.index(marker)
            if idx > 0 and idx + 1 < len(parts):
                return f"{parts[idx - 1]}/{parts[-1]}"
    return "unknown"


def _resolve_method_name(func_node: Node, source: bytes) -> str:
    """
    Extract the bare method name from any call-function node shape:
      - field_expression  (this->method, obj.method)
      - template_function / template_method  (method<T>)
      - qualified_identifier  (ns::method)
      - identifier  (method)
    """
    node_type = func_node.type

    if node_type == "field_expression":
        # Recurse into the RHS of -> or .
        for child in reversed(func_node.children):
            if child.type in ("template_method", "field_identifier", "identifier"):
                return _resolve_method_name(child, source)

    if node_type == "template_method":
        # template_method has a field_identifier child for the name
        for child in func_node.children:
            if child.type == "field_identifier":
                return source[child.start_byte : child.end_byte].decode("utf-8", errors="replace")

    if node_type in ("template_function", "qualified_identifier"):
        # Last identifier segment
        for child in reversed(func_node.children):
            if child.type in ("identifier", "field_identifier"):
                return source[child.start_byte : child.end_byte].decode("utf-8", errors="replace")

    if node_type in ("identifier", "field_identifier"):
        return source[func_node.start_byte : func_node.end_byte].decode("utf-8", errors="replace")

    return ""


def _extract_first_string_arg(args_node: Node | None, source: bytes) -> str:
    if args_node is None:
        return DYNAMIC_SENTINEL
    for child in args_node.children:
        if child.type == "string_literal":
            # Prefer the string_content child (excludes quote characters)
            content = _first_child_of_type(child, "string_content")
            if content:
                return source[content.start_byte : content.end_byte].decode(
                    "utf-8", errors="replace"
                )
            # Fallback: strip surrounding quotes from raw text
            raw = source[child.start_byte : child.end_byte].decode("utf-8", errors="replace")
            return raw.strip('"').strip("'")
    return DYNAMIC_SENTINEL


def _extract_string_arg(args_node: Node | None, index: int, source: bytes) -> str:
    if args_node is None or index >= len(args_node.named_children):
        return DYNAMIC_SENTINEL
    arg = args_node.named_children[index]
    if arg.type != "string_literal":
        return DYNAMIC_SENTINEL
    content = _first_child_of_type(arg, "string_content")
    if content is not None:
        return source[content.start_byte : content.end_byte].decode("utf-8", errors="replace")
    return source[arg.start_byte : arg.end_byte].decode("utf-8", errors="replace").strip('"')


def _cpp_parameter_names(node: Node, source: bytes, context: bytes | None = None) -> dict[str, str]:
    """Resolve local string variables read from literal parameter defaults."""
    text = source[node.start_byte : node.end_byte].decode("utf-8", errors="replace")
    declaration_text = context.decode("utf-8", errors="replace") if context else text
    defaults = dict(
        re.findall(
            r'declare_parameter(?:\s*<[^>]+>)?\s*\(\s*"([^"\\]+)"\s*,\s*"([^"\\]+)"',
            declaration_text,
        )
    )
    names: dict[str, str] = {}
    for key, default in defaults.items():
        escaped = re.escape(key)
        for match in re.finditer(
            rf'get_parameter\s*\(\s*"{escaped}"\s*,\s*(?:this->)?([A-Za-z_]\w*)\s*\)', text
        ):
            names[match.group(1)] = default
        for match in re.finditer(
            rf'([A-Za-z_]\w*)\s*=\s*(?:this->)?get_parameter\s*\(\s*"{escaped}"\s*\)',
            text,
        ):
            names[match.group(1)] = default
    return names


def _cpp_name(
    args_node: Node | None, index: int, source: bytes, parameter_names: dict[str, str]
) -> tuple[str, str]:
    literal = _extract_string_arg(args_node, index, source)
    if literal != DYNAMIC_SENTINEL:
        return literal, "literal"
    if args_node is not None and index < len(args_node.named_children):
        arg = args_node.named_children[index]
        raw = source[arg.start_byte : arg.end_byte].decode("utf-8", errors="replace")
        variable = raw.strip().removeprefix("this->")
        if variable in parameter_names:
            return parameter_names[variable], "parameter_default"
    return DYNAMIC_SENTINEL, "unresolved"


def _query_nodes(root: Node, node_type: str) -> Iterator[Node]:
    """BFS yielding all descendant nodes of the given type."""
    queue: deque[Node] = deque(root.children)
    while queue:
        node = queue.popleft()
        if node.type == node_type:
            yield node
        queue.extend(node.children)


def _first_child_of_type(node: Node, child_type: str) -> Node | None:
    for child in node.children:
        if child.type == child_type:
            return child
    return None


_ENDPOINT_FIELDS = (
    "publishers",
    "subscriptions",
    "services",
    "clients",
    "action_servers",
    "action_clients",
)


def _qualified_symbol(node: Node, source: bytes, name: str) -> str:
    scopes = []
    parent = node.parent
    while parent is not None:
        if parent.type in ("namespace_definition", "class_specifier"):
            ident = parent.child_by_field_name("name")
            if ident is not None:
                scopes.append(
                    source[ident.start_byte : ident.end_byte].decode("utf-8", errors="replace")
                )
        parent = parent.parent
    return "::".join([*reversed(scopes), name])


def _class_records(
    parsed: list[tuple[Path, bytes, Node]],
) -> list[tuple[Node, bytes, Path, str, list[str]]]:
    records = []
    for path, source, root in parsed:
        for cls in _query_nodes(root, "class_specifier"):
            name = _get_class_name(cls, source)
            base = _first_child_of_type(cls, "base_class_clause")
            if name and base is not None:
                text = source[base.start_byte : base.end_byte].decode("utf-8", errors="replace")
                bases = re.findall(r"[A-Za-z_]\w*(?:::[A-Za-z_]\w*)*", text)
                records.append((cls, source, path, _qualified_symbol(cls, source, name), bases))
    return records


def _resolve_base(base: str, symbol: str, known: set[str]) -> bool:
    scope = symbol.split("::")[:-1]
    while scope:
        if "::".join([*scope, base]) in known:
            return True
        scope.pop()
    return base in known


def discover_workspace_node_bases(package_paths: list[Path]) -> set[str]:
    """Build a qualified inheritance index without attributing communication calls."""
    parsed = []
    parser = Parser(_CPP_LANG)
    for path in package_paths:
        for file in _iter_cpp_files(path):
            try:
                source = file.read_bytes()
            except OSError:
                continue
            parsed.append((file, source, parser.parse(source).root_node))
    known = {"rclcpp::Node", "rclcpp_lifecycle::LifecycleNode"}
    records = _class_records(parsed)
    while True:
        additions = {
            symbol
            for _, _, _, symbol, bases in records
            if any(_resolve_base(b, symbol, known) for b in bases)
        }
        if additions <= known:
            return known
        known.update(additions)


def _discover_indirect_nodes(
    nodes: list[NodeDefinition],
    parsed: list[tuple[Path, bytes, Node]],
    package: str,
    known: set[str],
    factory_patterns: list[dict[str, Any]],
) -> None:
    known = set(known) | {"rclcpp::Node", "rclcpp_lifecycle::LifecycleNode"}
    known.update(n.source_symbol or n.name for n in nodes)
    records = _class_records(parsed)
    seen = {(n.source_symbol, n.file_path) for n in nodes}
    while True:
        changed = False
        for cls, source, path, symbol, bases in records:
            if (symbol, str(path)) in seen or not any(
                _resolve_base(b, symbol, known) for b in bases
            ):
                continue
            nd = NodeDefinition(
                name=symbol.split("::")[-1],
                source_symbol=symbol,
                declared_ros_name=_extract_declared_ros_name(cls, source),
                package=package,
                language="cpp",
                file_path=str(path),
                line=cls.start_point[0] + 1,
            )
            _collect_calls(cls, source, nd, factory_patterns=factory_patterns)
            nodes.append(nd)
            seen.add((symbol, str(path)))
            known.add(symbol)
            changed = True
        if not changed:
            break


def _attach_out_of_class_calls(
    nodes: list[NodeDefinition],
    parsed: list[tuple[Path, bytes, Node]],
    factory_patterns: list[dict[str, Any]],
) -> None:
    by_symbol: dict[str, list[NodeDefinition]] = {}
    for nd in nodes:
        by_symbol.setdefault(nd.source_symbol or nd.name, []).append(nd)
    contexts: dict[str, bytes] = {}
    for _, source, root in parsed:
        for cls in _query_nodes(root, "class_specifier"):
            name = _get_class_name(cls, source)
            if name:
                symbol = _qualified_symbol(cls, source, name)
                contexts[symbol] = contexts.get(symbol, b"") + source[cls.start_byte : cls.end_byte]
        for fn in _query_nodes(root, "function_definition"):
            decl = fn.child_by_field_name("declarator")
            qualified = next(_query_nodes(decl, "qualified_identifier"), None) if decl else None
            if qualified:
                text = source[qualified.start_byte : qualified.end_byte].decode()
                if "::" in text:
                    symbol = _qualified_symbol(fn, source, text.rsplit("::", 1)[0])
                    contexts[symbol] = (
                        contexts.get(symbol, b"") + source[fn.start_byte : fn.end_byte]
                    )
    for nd in nodes:
        context = contexts.get(nd.source_symbol or nd.name)
        if context:
            for field in _ENDPOINT_FIELDS:
                getattr(nd, field).clear()
            nd.has_dynamic_names = False
            for _path, source, root in parsed:
                for cls in _query_nodes(root, "class_specifier"):
                    name = _get_class_name(cls, source)
                    if name and _qualified_symbol(cls, source, name) == (
                        nd.source_symbol or nd.name
                    ):
                        _collect_calls(
                            cls, source, nd, factory_patterns=factory_patterns, context=context
                        )
    for path, source, root in parsed:
        for fn in _query_nodes(root, "function_definition"):
            declarator = fn.child_by_field_name("declarator")
            if declarator is None:
                continue
            qualified = next(_query_nodes(declarator, "qualified_identifier"), None)
            if qualified is None:
                continue
            text = source[qualified.start_byte : qualified.end_byte].decode(
                "utf-8", errors="replace"
            )
            if "::" not in text:
                continue
            class_name = text.rsplit("::", 1)[0]
            full = _qualified_symbol(fn, source, class_name)
            candidates = by_symbol.get(full, by_symbol.get(class_name, []))
            if len(candidates) != 1:
                for nd in candidates:
                    nd.analysis_incomplete = True
                    nd.analysis_notes.append(f"Ambiguous method ownership in {path}")
                continue
            nd = candidates[0]
            proxy = NodeDefinition(
                name=nd.name, package=nd.package, language="cpp", file_path=str(path)
            )
            _collect_calls(
                fn,
                source,
                proxy,
                factory_patterns=factory_patterns,
                context=contexts.get(nd.source_symbol or nd.name),
            )
            for field in _ENDPOINT_FIELDS:
                getattr(nd, field).extend(getattr(proxy, field))
            nd.has_dynamic_names |= proxy.has_dynamic_names
            if nd.declared_ros_name is None:
                nd.declared_ros_name = _extract_declared_ros_name(fn, source)


def _discover_direct_node_objects(
    nodes: list[NodeDefinition],
    parsed: list[tuple[Path, bytes, Node]],
    package: str,
    factory_patterns: list[dict[str, Any]],
) -> None:
    pattern = re.compile(r"^(?:rclcpp::Node::make_shared|std::make_shared\s*<\s*rclcpp::Node\s*>)$")
    for path, source, root in parsed:
        for call in _query_nodes(root, "call_expression"):
            func = call.child_by_field_name("function")
            if func is None or not pattern.match(
                source[func.start_byte : func.end_byte].decode("utf-8", errors="replace")
            ):
                continue
            parent = call.parent
            if parent is None or parent.type != "init_declarator":
                continue
            var = parent.child_by_field_name("declarator")
            if var is None or var.type != "identifier":
                continue
            receiver = source[var.start_byte : var.end_byte].decode()
            owner = parent
            while owner.parent is not None and owner.type != "function_definition":
                owner = owner.parent
            name = _extract_first_string_arg(call.child_by_field_name("arguments"), source)
            nd = NodeDefinition(
                name=name if name != DYNAMIC_SENTINEL else receiver,
                source_symbol=f"{path.name}:{call.start_point[0] + 1}:{receiver}",
                declared_ros_name=name if name != DYNAMIC_SENTINEL else None,
                package=package,
                language="cpp",
                file_path=str(path),
                line=call.start_point[0] + 1,
                analysis_incomplete=True,
                analysis_notes=[
                    "Direct node: only calls through its local variable are attributed; "
                    "aliases and helper ownership are unresolved."
                ],
            )
            _collect_calls(owner, source, nd, receiver=receiver, factory_patterns=factory_patterns)
            nodes.append(nd)
