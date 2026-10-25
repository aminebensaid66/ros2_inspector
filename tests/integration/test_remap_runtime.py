from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from ros2inspector.model.names import resolve_endpoint_name

_REMAP_CASES = json.loads(
    (Path(__file__).parents[1] / "data/remap_cases.json").read_text(encoding="utf-8")
)


@pytest.mark.requires_ros2
@pytest.mark.parametrize(
    "case",
    [
        *_REMAP_CASES,
        {"source": "chatter", "key": "rosservice://chatter", "target": "out"},
        {"source": "chatter", "key": "other:chatter", "target": "out"},
    ],
    ids=lambda c: f"{c['source']}|{c['key']}",
)
def test_remap_resolution_matches_rclpy(case: dict[str, str]) -> None:
    import rclpy
    from rclpy.context import Context

    context = Context()
    rclpy.init(args=["--ros-args", "-r", f"{case['key']}:={case['target']}"], context=context)
    node = rclpy.create_node("talker", namespace="robot", context=context)
    try:
        remaps = {case["key"]: case["target"]}
        for kind, live in [
            ("Topic", node.resolve_topic_name(case["source"])),
            ("Service", node.resolve_service_name(case["source"])),
        ]:
            assert resolve_endpoint_name(case["source"], remaps, "robot", "talker", kind) == live
        if "actual" in case:
            assert node.resolve_topic_name(case["source"]) == case["actual"]
    finally:
        node.destroy_node()
        rclpy.shutdown(context=context)


@pytest.mark.requires_ros2
def test_action_names_are_not_remapped_like_topics() -> None:
    import rclpy
    from rclpy.action import ActionClient
    from rclpy.context import Context

    action = pytest.importorskip("example_interfaces.action")
    context = Context()
    rclpy.init(args=["--ros-args", "-r", "chatter:=out"], context=context)
    node = rclpy.create_node("talker", namespace="robot", context=context)
    client = ActionClient(node, action.Fibonacci, "chatter")
    try:
        expected = resolve_endpoint_name("chatter", {"chatter": "out"}, "robot", "talker", "Action")
        deadline = time.monotonic() + 5.0
        services: set[str] = set()
        while time.monotonic() < deadline and not services:
            services = {
                name.split("/_action/")[0]
                for name, _ in node.get_service_names_and_types()
                if "/_action/" in name
            }
            time.sleep(0.1)
        assert services == {expected} == {"/robot/chatter"}
    finally:
        client.destroy()
        node.destroy_node()
        rclpy.shutdown(context=context)
