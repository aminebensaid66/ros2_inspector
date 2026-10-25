from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ros2inspector.cli.app import app
from ros2inspector.model.uam import UAM
from ros2inspector.static.launch_analyzer import analyze_launch_file
from tests.unit.name_resolution_fixtures import make_workspace, write_launch


@pytest.mark.parametrize(
    "expression, expected",
    [
        (
            "GroupAction(actions=[PushRosNamespace('robot'), "
            "Node(package='demo', executable='talker')])",
            "/robot",
        ),
        (
            "GroupAction(actions=[PushRosNamespace('a'), "
            "GroupAction(actions=[PushRosNamespace(namespace='b'), "
            "Node(package='demo', executable='talker', namespace='c')])])",
            "/a/b/c",
        ),
        (
            "GroupAction(actions=[PushRosNamespace('a'), "
            "Node(package='demo', executable='talker', "
            "namespace='/reset')])",
            "/reset",
        ),
    ],
)
def test_group_namespace_scopes_compose(tmp_path: Path, expression: str, expected: str) -> None:
    launch = make_workspace(tmp_path)
    write_launch(launch, expression)
    model = UAM.build(tmp_path, use_cache=False)
    assert model.deployments()[0]["namespace"] == expected
    assert [t["name"] for t in model.topics()] == [expected + "/chatter"]


def test_group_namespace_follows_assigned_actions(tmp_path: Path) -> None:
    launch = make_workspace(tmp_path, "~/x")
    launch.write_text(
        "def generate_launch_description():\n"
        " node = Node(package='demo', executable='talker')\n"
        " actions = [PushRosNamespace('robot'), node]\n"
        " return LaunchDescription([GroupAction(actions=actions)])\n"
    )
    assert UAM.build(tmp_path, use_cache=False).topics()[0]["name"] == "/robot/talker/x"


@pytest.mark.parametrize(
    "suffix, source",
    [
        (
            ".xml",
            '<launch><group><push-ros-namespace namespace="robot"/>'
            '<node pkg="demo" exec="talker"/></group></launch>',
        ),
        (
            ".yaml",
            "launch:\n- group:\n    children:\n    - push_ros_namespace:"
            "\n        namespace: robot\n    - node:\n        pkg: demo"
            "\n        exec: talker\n",
        ),
    ],
)
def test_frontend_group_namespace(tmp_path: Path, suffix: str, source: str) -> None:
    launch = make_workspace(tmp_path)
    launch = launch.with_suffix(suffix)
    launch.write_text(source)
    assert analyze_launch_file(launch).nodes[0].namespace == "/robot"
    assert UAM.build(tmp_path, use_cache=False).topics()[0]["name"] == "/robot/chatter"


def test_dynamic_namespace_suppresses_definite_absence(tmp_path: Path) -> None:
    launch = make_workspace(tmp_path)
    write_launch(
        launch,
        "GroupAction(actions=[PushRosNamespace(LaunchConfiguration('ns')),"
        " Node(package='demo', executable='talker')])",
    )
    model = UAM.build(tmp_path, use_cache=False)
    assert "namespace" in model.deployments()[0]["unresolved_fields"]
    assert model.deployments()[0]["resolution"] == "unresolved"
    result = CliRunner().invoke(app, ["audit", str(tmp_path), "-f", "json"])
    assert not any(
        f["rule_type"] == "topic_connectivity" for f in json.loads(result.stdout)["findings"]
    )


def test_issue_40_group_namespace_has_no_false_connectivity_findings(tmp_path: Path) -> None:
    launch = make_workspace(tmp_path)
    (tmp_path / "src/demo/demo/node.py").write_text(
        "from rclpy.node import Node\nfrom std_msgs.msg import String\n"
        "class Talker(Node):\n def __init__(self):\n  super().__init__('talker')\n"
        "  self.create_publisher(String, 'chatter', 10)\n"
        "  self.create_subscription(String, 'chatter', lambda m: None, 10)\n"
        "  self.create_publisher(String, '~/x', 10)\n"
        "  self.create_subscription(String, '~/x', lambda m: None, 10)\n"
    )
    write_launch(
        launch,
        "GroupAction(actions=[PushRosNamespace('robot'), "
        "Node(package='demo', executable='talker', name='talker')])",
    )
    model = UAM.build(tmp_path, use_cache=False)
    assert sorted(t["name"] for t in model.topics()) == ["/robot/chatter", "/robot/talker/x"]
    result = CliRunner().invoke(app, ["audit", str(tmp_path), "-f", "json"])
    assert json.loads(result.stdout)["findings"] == []


def test_unresolved_namespace_only_hides_findings_it_could_explain(tmp_path: Path) -> None:
    launch = make_workspace(tmp_path)
    (tmp_path / "src/demo/demo/listen.py").write_text(
        "from rclpy.node import Node\nfrom std_msgs.msg import String\n"
        "class Listener(Node):\n def __init__(self):\n  super().__init__('listener')\n"
        "  self.create_subscription(String, '/robot/chatter', lambda m: None, 10)\n"
        "  self.create_subscription(String, '/robot/status', lambda m: None, 10)\n"
    )
    write_launch(
        launch,
        "GroupAction(actions=[PushRosNamespace(LaunchConfiguration('ns')),"
        " Node(package='demo', executable='talker')])",
    )
    result = CliRunner().invoke(app, ["audit", str(tmp_path), "-f", "json"])
    messages = [f["message"] for f in json.loads(result.stdout)["findings"]]
    assert len(messages) == 1
    assert "'/robot/status'" in messages[0] and "no publisher" in messages[0].lower()
