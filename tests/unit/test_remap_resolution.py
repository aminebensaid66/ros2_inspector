from __future__ import annotations

import json
from pathlib import Path

import pytest

from ros2inspector.model.names import resolve_endpoint_name
from ros2inspector.model.schemas import DYNAMIC_SENTINEL
from ros2inspector.model.uam import UAM
from tests.unit.name_resolution_fixtures import make_workspace, write_launch

_REMAP_CASES = json.loads((Path(__file__).parents[1] / "data/remap_cases.json").read_text())


@pytest.mark.parametrize("case", _REMAP_CASES, ids=lambda c: f"{c['source']}|{c['key']}")
def test_remap_matrix_matches_ros_names(tmp_path: Path, case: dict[str, str]) -> None:
    launch = make_workspace(tmp_path, case["source"])
    write_launch(
        launch,
        "Node(package='demo', executable='talker', name='talker', "
        f"namespace='robot', remappings=[({case['key']!r}, {case['target']!r})])",
    )
    model = UAM.build(tmp_path, use_cache=False)
    assert [t["name"] for t in model.topics()] == [case["actual"]]


@pytest.mark.parametrize(
    "name, key, kind, expected",
    [
        ("chatter", "rosservice://chatter", "Topic", "/robot/chatter"),
        ("chatter", "rosservice://chatter", "Service", "/robot/out"),
        ("chatter", "rostopic://chatter", "Service", "/robot/chatter"),
        ("chatter", "other:chatter", "Topic", "/robot/chatter"),
        ("chatter", "chatter", "Action", DYNAMIC_SENTINEL),
        ("chatter", "other", "Action", "/robot/chatter"),
    ],
)
def test_remap_rules_apply_only_to_their_kind_and_node(
    name: str, key: str, kind: str, expected: str
) -> None:
    assert resolve_endpoint_name(name, {key: "out"}, "robot", "talker", kind) == expected


def test_unknown_namespace_keeps_absolute_names_and_hides_relative_ones() -> None:
    assert resolve_endpoint_name("/chatter", {}, DYNAMIC_SENTINEL, "talker") == "/chatter"
    assert resolve_endpoint_name("chatter", {}, DYNAMIC_SENTINEL, "talker") == DYNAMIC_SENTINEL
    assert resolve_endpoint_name("~/x", {}, "robot", DYNAMIC_SENTINEL) == DYNAMIC_SENTINEL
