"""Small ROS workspaces with one talker node and a probe launch file."""

from __future__ import annotations

from pathlib import Path


def make_workspace(root: Path, topic: str = "chatter", *, parameters: bool = False) -> Path:
    package = root / "src/demo"
    module = package / "demo"
    module.mkdir(parents=True, exist_ok=True)
    (package / "package.xml").write_text(
        '<package format="3"><name>demo</name><version>0.1.0</version>'
        "<description>fixture</description><maintainer "
        'email="dev@example.com">Dev</maintainer>'
        "<license>MIT</license></package>"
    )
    (package / "setup.py").write_text(
        "setup(entry_points={'console_scripts':['talker = demo.node:main']})\n"
    )
    parameter = "  self.declare_parameter('output_topic', '/fallback')\n" if parameters else ""
    name = "self.get_parameter('output_topic').value" if parameters else repr(topic)
    (module / "node.py").write_text(
        "from rclpy.node import Node\nfrom std_msgs.msg import String\n"
        "class Talker(Node):\n def __init__(self):\n  super().__init__('talker')\n"
        + parameter
        + f"  self.create_publisher(String, {name}, 10)\n"
    )
    launch = package / "launch/probe.launch.py"
    launch.parent.mkdir(exist_ok=True)
    return launch


def write_launch(path: Path, expression: str) -> None:
    path.write_text(
        "from launch import LaunchDescription\n"
        "from launch.actions import GroupAction\n"
        "from launch_ros.actions import Node, PushRosNamespace\n"
        "def generate_launch_description():\n return LaunchDescription([" + expression + "])\n"
    )
