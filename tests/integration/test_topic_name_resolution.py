"""Regression (M1): ``cmd_vel`` and ``/cmd_vel`` are the same topic, as ROS resolves them."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ros2inspector.cli.app import app
from ros2inspector.model.uam import UAM

runner = CliRunner()

_PACKAGE_XML = """<?xml version="1.0"?>
<package format="3">
  <name>{name}</name>
  <version>0.1.0</version>
  <description>x</description>
  <maintainer email="a@b.c">A</maintainer>
  <license>Apache-2.0</license>
  <depend>rclpy</depend>
  <depend>geometry_msgs</depend>
  <export><build_type>ament_python</build_type></export>
</package>
"""

_SETUP_PY = """from setuptools import setup

setup(
    name='{name}',
    packages=['{name}'],
    entry_points={{'console_scripts': ['{node} = {name}.{node}:main']}},
)
"""

_NODE = """import rclpy
from geometry_msgs.msg import Twist
from rclpy.node import Node


class {cls}(Node):
    def __init__(self):
        super().__init__('{node}')
        {line}


def main():
    rclpy.init()
    rclpy.spin({cls}())
"""


def _package(root: Path, name: str, node: str, cls: str, line: str) -> None:
    package = root / "src" / name
    (package / name).mkdir(parents=True)
    (package / "package.xml").write_text(_PACKAGE_XML.format(name=name))
    (package / "setup.py").write_text(_SETUP_PY.format(name=name, node=node))
    (package / name / "__init__.py").write_text("")
    (package / name / f"{node}.py").write_text(_NODE.format(cls=cls, node=node, line=line))


@pytest.fixture
def mixed_workspace(tmp_path: Path) -> Path:
    _package(
        tmp_path,
        "teleop",
        "teleop",
        "Teleop",
        "self.pub = self.create_publisher(Twist, 'cmd_vel', 10)",
    )
    _package(
        tmp_path,
        "base",
        "base_driver",
        "BaseDriver",
        "self.sub = self.create_subscription(Twist, '/cmd_vel', self.cb, 10)",
    )
    return tmp_path


def test_relative_and_absolute_names_are_one_topic(mixed_workspace: Path) -> None:
    model = UAM.build(mixed_workspace, use_cache=False)
    assert [topic["name"] for topic in model.topics()] == ["/cmd_vel"]
    assert model.summary()["topics"] == 1
    rels = {
        data["rel"]
        for _, target, data in model.graph.edges(data=True)
        if target == "topic:/cmd_vel"
    }
    assert rels == {"publishes", "subscribes"}


def test_audit_reports_no_dead_topic(mixed_workspace: Path) -> None:
    result = runner.invoke(app, ["audit", str(mixed_workspace), "--format", "json", "--no-cache"])
    assert "cmd_vel" not in result.output, result.output


def test_show_connections_links_the_two_nodes(mixed_workspace: Path) -> None:
    result = runner.invoke(
        app,
        [
            "nodes",
            "--show-connections",
            "--format",
            "json",
            "--no-cache",
            "-C",
            str(mixed_workspace),
        ],
    )
    assert result.exit_code == 0, result.output
    nodes = {item["name"]: item for item in json.loads(result.output)}
    (connection,) = nodes["Teleop"]["connections"]
    assert connection["name"] == "/cmd_vel"
    assert connection["other_subscribers"] == ["base_driver"]


def test_viz_and_graph_draw_the_link(mixed_workspace: Path) -> None:
    from ros2inspector.viz.renderer import _cy_elements

    model = UAM.build(mixed_workspace, use_cache=False)
    elements = _cy_elements(model, "comms")
    serialized = json.dumps(elements)
    assert serialized.count('"topic:/cmd_vel"') >= 3  # the topic and both of its edges
    assert '"topic:cmd_vel"' not in serialized
    result = runner.invoke(
        app, ["graph", "comms", "--format", "mermaid", "--no-cache", "-C", str(mixed_workspace)]
    )
    assert result.exit_code == 0, result.output
    assert result.output.count('(["') == 1, result.output  # one topic shape
    assert "-->|sub|" in result.output and "-->|pub|" in result.output
