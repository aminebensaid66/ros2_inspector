from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from ros2inspector.model.names import resolve_endpoint_name
from ros2inspector.model.schemas import DYNAMIC_SENTINEL

_REMAP_CASES = json.loads(
    (Path(__file__).parents[1] / "data/remap_cases.json").read_text(encoding="utf-8")
)

_QUALIFIER_CASES = json.loads(
    (Path(__file__).parents[1] / "data/remap_qualifier_cases.json").read_text(encoding="utf-8")
)


@pytest.mark.requires_ros2
@pytest.mark.parametrize(
    "case",
    [
        *_REMAP_CASES,
        *_QUALIFIER_CASES,
        {"source": "chatter", "key": "rosservice://chatter", "target": "out"},
        {"source": "chatter", "key": "other:chatter", "target": "out"},
    ],
    ids=lambda c: f"{c['source']}|{c['key']}",
)
def test_remap_resolution_matches_rclpy(case: dict[str, str], ros_domain: int) -> None:
    import rclpy
    from rclpy.context import Context

    context = Context()
    rclpy.init(
        domain_id=ros_domain,
        args=["--ros-args", "-r", f"{case['key']}:={case['target']}"],
        context=context,
    )
    node = rclpy.create_node("talker", namespace="robot", context=context)
    try:
        remaps = [(case["key"], case["target"])]
        for kind, live in [
            ("Topic", node.resolve_topic_name(case["source"])),
            ("Service", node.resolve_service_name(case["source"])),
        ]:
            assert resolve_endpoint_name(case["source"], remaps, "robot", "talker", kind) == live
        if "actual" in case:
            live_name = (
                node.resolve_service_name(case["source"])
                if case.get("kind") == "Service"
                else node.resolve_topic_name(case["source"])
            )
            assert live_name == case["actual"]
    finally:
        node.destroy_node()
        rclpy.shutdown(context=context)


@pytest.mark.requires_ros2
def test_remapped_action_names_are_left_unresolved(ros_domain: int) -> None:
    import rclpy
    from rclpy.action import ActionClient
    from rclpy.context import Context

    action = pytest.importorskip("example_interfaces.action")
    context = Context()
    rclpy.init(domain_id=ros_domain, args=["--ros-args", "-r", "chatter:=out"], context=context)
    node = rclpy.create_node("talker", namespace="robot", context=context)
    client = ActionClient(node, action.Fibonacci, "chatter")
    try:
        expected = resolve_endpoint_name(
            "chatter", [("chatter", "out")], "robot", "talker", "Action"
        )
        deadline = time.monotonic() + 5.0
        services: set[str] = set()
        while time.monotonic() < deadline and not services:
            services = {
                name.split("/_action/")[0]
                for name, _ in node.get_service_names_and_types()
                if "/_action/" in name
            }
            time.sleep(0.1)
        # Jazzy releases disagree (rcl_action 9.2.7 keeps the name, 9.2.11 remaps it), so the
        # static result must be unresolved rather than either guess.
        assert services in ({"/robot/chatter"}, {"/robot/out"})
        assert expected == DYNAMIC_SENTINEL
    finally:
        client.destroy()
        node.destroy_node()
        rclpy.shutdown(context=context)
