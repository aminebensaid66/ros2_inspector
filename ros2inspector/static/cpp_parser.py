import re
from collections import deque
from collections.abc import Iterator
from pathlib import Path

import tree_sitter_cpp
from tree_sitter import Language, Node, Parser

from ros2inspector.discovery.file_walker import iter_package_files
from ros2inspector.model.schemas import (
    DYNAMIC_SENTINEL,
    CommunicationEndpoint,
    DataSource,
    NodeDefinition,
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
    package_path: Path, package_name: str, known_node_bases: set[str] | None = None
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
        for node_def in _extract_nodes(tree.root_node, source, package_name, cpp_file):
            nodes.append(node_def)

    _discover_indirect_nodes(nodes, parsed, package_name, known_node_bases or set())
    _attach_out_of_class_calls(nodes, parsed)
    _discover_direct_node_objects(nodes, parsed, package_name)
    return nodes


_CPP_SUFFIXES = frozenset((".cpp", ".cxx", ".cc", ".hpp", ".h"))


def _iter_cpp_files(root: Path) -> Iterator[Path]:
    yield from iter_package_files(root, suffixes=_CPP_SUFFIXES)


def _extract_nodes(
    root_node: Node, source: bytes, package: str, file_path: Path
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
        _collect_calls(class_node, source, nd)
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
    node: Node, source: bytes, nd: NodeDefinition, receiver: str | None = None
) -> None:
    for call_node in _query_nodes(node, "call_expression"):
        func_node = call_node.child_by_field_name("function")
        if func_node is None:
            continue

        args_node = call_node.child_by_field_name("arguments")
        if receiver is not None:
            function_text = source[func_node.start_byte : func_node.end_byte].decode(
                "utf-8", errors="replace"
            )
            args = args_node.named_children if args_node is not None else []
            owner_text = source[args[0].start_byte : args[0].end_byte].decode() if args else ""
            if not (
                function_text.startswith(receiver + "->")
                or function_text.startswith(receiver + ".")
                or (_get_rclcpp_action_kind(func_node, source) and owner_text == receiver)
            ):
                continue

        # rclcpp_action free functions must be checked before the generic dispatch
        # because their bare names ("create_server", "create_client") would otherwise
        # be missed entirely — _resolve_method_name returns "" for template_function
        # nodes whose child is a qualified_identifier rather than a bare identifier.
        action_kind = _get_rclcpp_action_kind(func_node, source)
        if action_kind is not None:
            topic_name = _extract_first_string_arg(args_node, source)
            cpp_type = _extract_cpp_type(func_node, source)
            ep = CommunicationEndpoint(
                name=topic_name,
                msg_type=cpp_type,
                file_path=nd.file_path,
                line=call_node.start_point[0] + 1,
                evidence="rclcpp_action",
                type_source="explicit" if cpp_type != "unknown" else "unknown",
                confidence=(
                    "high" if cpp_type != "unknown" and topic_name != DYNAMIC_SENTINEL else "low"
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

        topic_name = _extract_first_string_arg(args_node, source)
        cpp_type = _extract_cpp_type(func_node, source)
        ep = CommunicationEndpoint(
            name=topic_name,
            msg_type=cpp_type,
            file_path=nd.file_path,
            line=call_node.start_point[0] + 1,
            evidence=method_name,
            type_source="explicit" if cpp_type != "unknown" else "unknown",
            confidence=(
                "high" if cpp_type != "unknown" and topic_name != DYNAMIC_SENTINEL else "low"
            ),
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
            _collect_calls(cls, source, nd)
            nodes.append(nd)
            seen.add((symbol, str(path)))
            known.add(symbol)
            changed = True
        if not changed:
            break


def _attach_out_of_class_calls(
    nodes: list[NodeDefinition], parsed: list[tuple[Path, bytes, Node]]
) -> None:
    by_symbol: dict[str, list[NodeDefinition]] = {}
    for nd in nodes:
        by_symbol.setdefault(nd.source_symbol or nd.name, []).append(nd)
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
            _collect_calls(fn, source, proxy)
            for field in _ENDPOINT_FIELDS:
                getattr(nd, field).extend(getattr(proxy, field))
            nd.has_dynamic_names |= proxy.has_dynamic_names
            if nd.declared_ros_name is None:
                nd.declared_ros_name = _extract_declared_ros_name(fn, source)


def _discover_direct_node_objects(
    nodes: list[NodeDefinition], parsed: list[tuple[Path, bytes, Node]], package: str
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
            _collect_calls(owner, source, nd, receiver=receiver)
            nodes.append(nd)
