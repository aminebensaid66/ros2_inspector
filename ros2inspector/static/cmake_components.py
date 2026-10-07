"""Executable-to-component mappings declared in a package's CMakeLists.txt."""

from __future__ import annotations

import re
from pathlib import Path

_REGISTER_NODE = re.compile(r"rclcpp_components_register_node\s*\(([^)]*)\)", re.IGNORECASE)
_COMMENT = re.compile(r"#[^\n]*")


def find_component_executables(package_path: Path) -> dict[str, str]:
    """Map executable name to component plugin for ``rclcpp_components_register_node``."""
    cmake = package_path / "CMakeLists.txt"
    try:
        text = _COMMENT.sub("", cmake.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return {}
    mapping: dict[str, str] = {}
    for match in _REGISTER_NODE.finditer(text):
        tokens = [t.strip().strip('"') for t in match.group(1).split()]
        keywords = {t.upper(): i for i, t in enumerate(tokens)}
        plugin_at = keywords.get("PLUGIN")
        exe_at = keywords.get("EXECUTABLE")
        if plugin_at is None or exe_at is None:
            continue
        if plugin_at + 1 >= len(tokens) or exe_at + 1 >= len(tokens):
            continue
        plugin = tokens[plugin_at + 1].lstrip(":")
        executable = tokens[exe_at + 1]
        if "${" in plugin or "${" in executable:
            continue
        mapping[executable] = plugin
    return mapping
