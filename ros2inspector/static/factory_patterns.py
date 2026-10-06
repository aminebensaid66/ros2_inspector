"""Conservative descriptions of C++ helpers that create ROS endpoints."""

from pathlib import Path
from typing import Any

import yaml

_KINDS = {"publisher", "subscription", "service", "client", "action_server", "action_client"}

NAV2_PATTERNS: list[dict[str, Any]] = [
    {"call": "nav2_util::TwistPublisher", "kind": "publisher", "name_arg": 1},
    {"call": "nav2_util::TwistSubscriber", "kind": "subscription", "name_arg": 1},
    {"call": "nav2_util::SimpleActionServer", "kind": "action_server", "name_arg": 1},
]


def load_factory_patterns(path: Path | None, preset: str | None) -> list[dict[str, Any]]:
    if preset not in {None, "nav2"}:
        raise ValueError(f"unknown factory preset '{preset}'; supported: nav2")
    patterns = list(NAV2_PATTERNS) if preset == "nav2" else []
    if path is None:
        return patterns
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    entries = raw.get("factories") if isinstance(raw, dict) else raw
    if not isinstance(entries, list):
        raise ValueError("factory patterns must be a list or a mapping with 'factories'")
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("each factory pattern must be a mapping")
        call = entry.get("call")
        kind = entry.get("kind")
        index = entry.get("name_arg")
        if not isinstance(call, str) or not call or kind not in _KINDS:
            raise ValueError("factory requires a nonempty call and supported kind")
        if not isinstance(index, int) or isinstance(index, bool) or index < 0:
            raise ValueError("factory name_arg must be a nonnegative integer")
        patterns.append(entry)
    return patterns
