from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ros2inspector.cli.app import app
from ros2inspector.model.schemas import DYNAMIC_SENTINEL
from ros2inspector.model.uam import UAM
from tests.unit.name_resolution_fixtures import make_workspace, write_launch

_NODE = "Node(package='demo', executable='talker', name='talker')"


def _balanced(root: Path) -> Path:
    launch = make_workspace(root)
    (root / "src/demo/demo/node.py").write_text(
        "from rclpy.node import Node\nfrom std_msgs.msg import String\n"
        'class Talker(Node):\n def __init__(self):\n  super().__init__("talker")\n'
        '  self.create_publisher(String, "chatter", 10)\n'
        '  self.create_subscription(String, "chatter", print, 10)\n'
        '  self.create_publisher(String, "~/x", 10)\n'
        '  self.create_subscription(String, "~/x", print, 10)\n'
    )
    return launch


def _audit(root: Path) -> list[dict]:
    result = CliRunner().invoke(app, ["audit", str(root), "--no-cache", "-f", "json"])
    assert result.exit_code in (0, 1), result.output
    return json.loads(result.stdout)["findings"]


def _known_topics(model: UAM) -> set[str]:
    return {
        model.graph.nodes[target]["name"]
        for _, target, edge in model.graph.edges(data=True)
        if edge.get("rel") in {"publishes", "subscribes"}
        and edge.get("resolution", "known") == "known"
    }


def test_literal_push_conditions_follow_launch_truth_values(tmp_path: Path) -> None:
    cases = [
        ("IfCondition", "false", None),
        ("UnlessCondition", "true", None),
        ("IfCondition", "  FaLsE  ", None),
        ("IfCondition", "0", None),
        ("IfCondition", "TRUE", "/robot"),
        ("UnlessCondition", "false", "/robot"),
        ("UnlessCondition", "0", "/robot"),
        ("IfCondition", "1", "/robot"),
    ]
    for index, (condition, value, expected) in enumerate(cases):
        root = tmp_path / str(index)
        launch = _balanced(root)
        write_launch(
            launch,
            "GroupAction(actions=[PushRosNamespace('robot', condition="
            f"{condition}({value!r})), {_NODE}])",
        )
        model = UAM.build(root, use_cache=False)
        deployment = model.deployments()[0]
        assert deployment["namespace"] == expected
        assert deployment["resolution"] == "known"
        assert deployment["unresolved_fields"] == []
        assert not model.diagnostics()
        assert _known_topics(model) == {f"{expected or ''}/chatter", f"{expected or ''}/talker/x"}
        assert _audit(root) == []


def test_conditional_push_marks_only_dependent_names_unresolved(tmp_path: Path) -> None:
    launch = _balanced(tmp_path)
    write_launch(
        launch,
        "GroupAction(actions=[PushRosNamespace('robot', "
        "condition=IfCondition(LaunchConfiguration('enabled'))), " + _NODE + "])",
    )
    model = UAM.build(tmp_path, use_cache=False)
    deployment = model.deployments()[0]
    assert deployment["namespace"] == DYNAMIC_SENTINEL
    assert deployment["resolution"] == "unresolved"
    assert "namespace" in deployment["unresolved_fields"]
    assert any("enabled" in c for c in deployment["conditions"])
    assert model.diagnostics()
    assert _audit(tmp_path) == []


def test_unscoped_nested_groups_and_absolute_resets(tmp_path: Path) -> None:
    cases = [
        ("GroupAction(actions=[PushRosNamespace('robot')], scoped=False), " + _NODE, "/robot"),
        (
            "GroupAction(actions=[PushRosNamespace('outer'), GroupAction(actions=["
            "PushRosNamespace('inner')], scoped=False), " + _NODE + "])",
            "/outer/inner",
        ),
        (
            "GroupAction(actions=[PushRosNamespace('outer'), GroupAction(actions=["
            "PushRosNamespace('inner')], scoped=True), " + _NODE + "])",
            "/outer",
        ),
        (
            "GroupAction(actions=[PushRosNamespace('outer'), GroupAction(actions=["
            "PushRosNamespace('/reset')], scoped=False), " + _NODE + "])",
            "/reset",
        ),
        (
            "GroupAction(actions=[PushRosNamespace('outer'), GroupAction(actions=["
            "PushRosNamespace('inner'), " + _NODE + "], forwarding=False)])",
            "/inner",
        ),
    ]
    for index, (expression, expected) in enumerate(cases):
        root = tmp_path / str(index)
        write_launch(_balanced(root), expression)
        model = UAM.build(root, use_cache=False)
        assert model.deployments()[0]["namespace"] == expected
        assert model.deployments()[0]["resolution"] == "known"
        assert _known_topics(model) == {expected + "/chatter", expected + "/talker/x"}
        assert _audit(root) == []


def test_unknown_scoping_keeps_following_namespace_uncertain(tmp_path: Path) -> None:
    launch = _balanced(tmp_path)
    write_launch(launch, "GroupAction(actions=[PushRosNamespace('robot')], scoped=scope), " + _NODE)
    deployment = UAM.build(tmp_path, use_cache=False).deployments()[0]
    assert deployment["resolution"] == "unresolved"
    assert "namespace" in deployment["unresolved_fields"]
    assert _audit(tmp_path) == []


def test_appends_keep_constructor_and_push_order(tmp_path: Path) -> None:
    launch = _balanced(tmp_path)
    launch.write_text(
        "def generate_launch_description():\n"
        " ld = LaunchDescription([Node(package='demo', executable='talker', name='before')])\n"
        " ld.add_action(PushRosNamespace('robot'))\n"
        " ld.add_action(Node(package='demo', executable='talker', name='after'))\n"
        " alias = ld\n"
        " alias.add_entity(PushRosNamespace('next'))\n"
        " alias.add_action(Node(package='demo', executable='talker', name='last'))\n"
        " return ld\n"
    )
    deployments = UAM.build(tmp_path, use_cache=False).deployments()
    assert {d["deployment_name"]: d["namespace"] for d in deployments} == {
        "before": None,
        "after": "/robot",
        "last": "/robot/next",
    }
    assert all(d["resolution"] == "known" for d in deployments)
    assert _audit(tmp_path) == []


@pytest.mark.parametrize(
    "mutation",
    [
        " for value in choices:\n  ld.add_action(PushRosNamespace(value))\n",
        " if enabled:\n  ld.add_action(PushRosNamespace('robot'))\n",
        " configure_launch(ld)\n",
    ],
)
def test_unsupported_mutations_do_not_invent_root_names(tmp_path: Path, mutation: str) -> None:
    launch = _balanced(tmp_path)
    launch.write_text(
        "def generate_launch_description():\n"
        " ld = LaunchDescription([Node(package='demo', executable='talker', name='before')])\n"
        + mutation
        + " ld.add_action(Node(package='demo', executable='talker', name='after'))\n"
        " return ld\n"
    )
    model = UAM.build(tmp_path, use_cache=False)
    deployments = {d["deployment_name"]: d for d in model.deployments()}
    assert deployments["before"]["namespace"] is None
    assert deployments["before"]["resolution"] == "known"
    assert deployments["after"]["namespace"] == DYNAMIC_SENTINEL
    assert deployments["after"]["resolution"] == "unresolved"
    assert model.diagnostics()
