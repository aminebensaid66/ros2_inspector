import re
from collections import deque
from collections.abc import Iterator
from pathlib import Path

import tree_sitter_cpp
from tree_sitter import Language, Node, Parser

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


def parse_cpp_nodes(package_path: Path, package_name: str) -> list[NodeDefinition]:
    parser = Parser(_CPP_LANG)
    nodes: list[NodeDefinition] = []

    for cpp_file in _iter_cpp_files(package_path):
        try:
            source = cpp_file.read_bytes()
            tree = parser.parse(source)
        except Exception:
            continue

        for node_def in _extract_nodes(tree.root_node, source, package_name, cpp_file):
            nodes.append(node_def)

    return nodes


_CPP_SUFFIXES = frozenset((".cpp", ".cxx", ".cc", ".hpp", ".h"))


def _iter_cpp_files(root: Path) -> Iterator[Path]:
    for f in root.rglob("*"):
        if f.is_file() and f.suffix in _CPP_SUFFIXES:
            yield f


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


def _get_rclcpp_action_kind(func_node: Node, source: bytes) -> str | None:
    """Return 'server' or 'client' for rclcpp_action free-function calls, else None.

    Handles:
        rclcpp_action::create_server<ActionT>(node, "name", goal_cb, cancel_cb, accepted_cb)
        rclcpp_action::create_client<ActionT>(node, "name")

    These cannot be matched by bare method name alone because "create_server" and
    "create_client" are generic identifiers.  We verify the rclcpp_action namespace
    by inspecting the raw text of the qualified_identifier node.
    """
    target = func_node
    if func_node.type == "template_function":
        for child in func_node.children:
            if child.type == "qualified_identifier":
                target = child
                break
    if target.type != "qualified_identifier":
        return None
    full_text = source[target.start_byte : target.end_byte].decode("utf-8", errors="replace")
    if _RCLCPP_ACTION_NS not in full_text:
        return None
    bare = full_text.rsplit("::", 1)[-1].strip()
    return _RCLCPP_ACTION_FREE_FUNC_MAP.get(bare)


def _collect_calls(node: Node, source: bytes, nd: NodeDefinition) -> None:
    for call_node in _query_nodes(node, "call_expression"):
        func_node = call_node.child_by_field_name("function")
        if func_node is None:
            continue

        args_node = call_node.child_by_field_name("arguments")

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
    """Convert 'std_msgs::msg::String' → 'std_msgs/String'."""
    parts = [p.strip() for p in cpp_type.split("::") if p.strip()]
    for marker in ("msg", "srv", "action"):
        if marker in parts:
            idx = parts.index(marker)
            if idx > 0 and idx + 1 < len(parts):
                return f"{parts[idx - 1]}/{parts[-1]}"
    return parts[-1] if parts else cpp_type


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
        for child in func_node.children:
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
