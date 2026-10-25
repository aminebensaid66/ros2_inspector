"""ROS 2 name expansion and remapping, following the rules rcl applies at runtime."""

from __future__ import annotations

from ros2inspector.model.schemas import DYNAMIC_SENTINEL, UNKNOWN_SENTINEL

_UNKNOWN = {DYNAMIC_SENTINEL, UNKNOWN_SENTINEL}


def _short_name(node_name: str | None) -> str | None:
    short_name = node_name.rsplit("/", 1)[-1] if node_name else None
    return None if short_name in _UNKNOWN else short_name


def _expand(value: str, namespace: str | None, node_name: str | None) -> str:
    """Expand a relative, private or substituted name to a fully qualified one.

    Returns ``DYNAMIC_SENTINEL`` when the result depends on something not known
    statically: an unknown namespace for a relative name, an unknown node name for a
    private name, or an unsupported substitution.
    """
    if value in _UNKNOWN:
        return DYNAMIC_SENTINEL
    short_name = _short_name(node_name)
    ns_known = namespace not in _UNKNOWN
    ns = "/" + (namespace or "").strip("/") if ns_known else ""
    for key in ("{node}", "{nodename}"):
        if key in value:
            if short_name is None:
                return DYNAMIC_SENTINEL
            value = value.replace(key, short_name)
    for key in ("{ns}", "{namespace}"):
        if key in value:
            if not ns_known:
                return DYNAMIC_SENTINEL
            value = value.replace(key, ns)
    if any(char in value for char in "{}*"):
        return DYNAMIC_SENTINEL
    if value.startswith("/"):
        return value
    if value == "~" or value.startswith("~/"):
        if short_name is None or not ns_known:
            return DYNAMIC_SENTINEL
        return (ns.rstrip("/") + "/" + short_name + value[1:]).rstrip("/")
    if value.startswith("~") or not ns_known:
        return DYNAMIC_SENTINEL
    return ns.rstrip("/") + "/" + value.strip("/")


def resolve_endpoint_name(
    name: str,
    remaps: list[tuple[str, str]],
    namespace: str | None,
    node_name: str | None,
    kind: str = "Topic",
) -> str:
    """Return the fully qualified name a node's endpoint gets at runtime.

    rcl expands both the endpoint name and the match side of every remap rule with the
    node's namespace and name, compares the expanded names exactly, takes the first
    matching rule and expands its replacement the same way. ``node:from:=to`` rules only
    apply to the named node, and ``rostopic://``/``rosservice://`` rules only to that kind
    of name.

    Whether a rule that names an action applies depends on the ROS release: Jazzy's rcl_action
    9.2.7 leaves the action name alone (its topics and services live under
    ``NAME/_action/...``, which the rule does not match), while 9.2.11 remaps it. Such an
    action is therefore reported as unresolved rather than guessed.
    """
    expanded = _expand(name, namespace, node_name)
    if expanded == DYNAMIC_SENTINEL:
        return expanded
    short_name = _short_name(node_name)
    for key, replacement in remaps:
        if key.startswith("rostopic://"):
            if kind not in {"Topic", "Action"}:
                continue
            key = key.removeprefix("rostopic://")
        elif key.startswith("rosservice://"):
            if kind not in {"Service", "Action"}:
                continue
            key = key.removeprefix("rosservice://")
        elif ":" in key:
            qualifier, _, key = key.partition(":")
            if short_name is None:
                return DYNAMIC_SENTINEL
            if qualifier != short_name:
                continue
        match = _expand(key, namespace, node_name)
        if match == DYNAMIC_SENTINEL:
            return DYNAMIC_SENTINEL
        if match == expanded:
            if kind == "Action":
                return DYNAMIC_SENTINEL
            return _expand(replacement, namespace, node_name)
    return expanded


def unresolved_name_suffix(
    name: str, remaps: list[tuple[str, str]], node_name: str | None
) -> str | None:
    """Return the tail an endpoint's name must end with when only its namespace is unknown.

    ``None`` means the name could resolve to anything, for example because remap rules
    apply or the name itself is dynamic. Connectivity rules use this so that a peer an
    unresolved deployment could provide is not reported as definitely missing.
    """
    if name in _UNKNOWN or remaps or any(c in name for c in "{}*"):
        return None
    if name.startswith("/"):
        return name
    if name == "~" or name.startswith("~/"):
        short_name = _short_name(node_name)
        if short_name is None:
            return None
        return ("/" + short_name + name[1:]).rstrip("/")
    if name.startswith("~"):
        return None
    return "/" + name.strip("/")


def canonical_interface(value: str) -> str:
    """Drop the ``msg``/``srv``/``action`` segment so both spellings compare equal."""
    parts = value.split("/")
    if len(parts) == 3 and parts[1] in {"msg", "srv", "action"}:
        return parts[0] + "/" + parts[2]
    return value


def compatible_interfaces(first: str, second: str) -> bool:
    """True when two endpoints can talk: equal types, or one side's type is unknown."""
    return (
        first in {"unknown", ""}
        or second in {"unknown", ""}
        or canonical_interface(first) == canonical_interface(second)
    )
