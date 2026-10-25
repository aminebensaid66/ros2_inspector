from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ros2inspector.cli.app import app
from ros2inspector.model.names import canonical_interface
from ros2inspector.model.uam import UAM


def _write(root: Path, relative: str, text: str) -> None:
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


_NODES = {
    "talker": (
        "from std_msgs.msg import String\n"
        "class Talker(Node):\n"
        "    def __init__(self):\n"
        "        super().__init__('talker')\n"
        "        self.declare_parameter('output_topic', '/fallback')\n"
        "        self.create_publisher(String, self.get_parameter('output_topic').value, 10)\n"
        "        self.create_publisher(String, 'chatter', 10)\n"
        "        self.create_publisher(String, '~/x', 10)\n"
    ),
    "listener": (
        "from std_msgs.msg import String\n"
        "class Listener(Node):\n"
        "    def __init__(self):\n"
        "        super().__init__('listener')\n"
        "        self.create_subscription(String, 'chatter', self.cb, 10)\n"
        "        self.create_subscription(String, '/configured', self.cb, 10)\n"
        "        self.create_subscription(String, '/robot/talker/x', self.cb, 10)\n"
        "    def cb(self, msg):\n"
        "        pass\n"
    ),
    "typed": (
        "from std_msgs.msg import Int32\n"
        "class Typed(Node):\n"
        "    def __init__(self):\n"
        "        super().__init__('typed')\n"
        "        self.create_subscription(Int32, 'chatter', self.cb, 10)\n"
        "    def cb(self, msg):\n"
        "        pass\n"
    ),
}


def _fixture(root: Path) -> None:
    package = root / "src/resolution_demo"
    _write(
        package,
        "package.xml",
        '<package format="3"><name>resolution_demo</name><version>0.1.0</version>'
        "<description>fixture</description>"
        '<maintainer email="dev@example.com">Dev</maintainer><license>MIT</license>'
        "<exec_depend>rclpy</exec_depend><exec_depend>std_msgs</exec_depend>"
        "<exec_depend>launch_ros</exec_depend>"
        "<export><build_type>ament_python</build_type></export></package>",
    )
    _write(package, "resource/resolution_demo", "")
    _write(
        package,
        "setup.cfg",
        "[develop]\nscript_dir=$base/lib/resolution_demo\n"
        "[install]\ninstall_scripts=$base/lib/resolution_demo\n",
    )
    scripts = ", ".join(f"'{name} = resolution_demo.{name}:main'" for name in _NODES)
    _write(
        package,
        "setup.py",
        "from setuptools import setup\n"
        "setup(name='resolution_demo', packages=['resolution_demo'], data_files=["
        "('share/ament_index/resource_index/packages', ['resource/resolution_demo']),"
        "('share/resolution_demo', ['package.xml']),"
        "('share/resolution_demo/launch', ['launch/system.launch.py'])],"
        f"entry_points={{'console_scripts': [{scripts}]}})\n",
    )
    _write(package, "resolution_demo/__init__.py", "")
    for name, body in _NODES.items():
        cls = body.split("class ", 1)[1].split("(", 1)[0]
        _write(
            package,
            f"resolution_demo/{name}.py",
            "import rclpy\nfrom rclpy.node import Node\n" + body + "def main():\n"
            f"    rclpy.init()\n    rclpy.spin({cls}())\n",
        )
    _write(
        package,
        "launch/system.launch.py",
        "from launch import LaunchDescription\n"
        "from launch.actions import GroupAction\n"
        "from launch_ros.actions import Node, PushRosNamespace\n"
        "def generate_launch_description():\n"
        "    return LaunchDescription([GroupAction(actions=[\n"
        "        PushRosNamespace('robot'),\n"
        "        Node(package='resolution_demo', executable='talker', name='talker',\n"
        "             parameters=[{'output_topic': '/configured'}]),\n"
        "        Node(package='resolution_demo', executable='listener', name='listener'),\n"
        "        Node(package='resolution_demo', executable='typed', name='typed'),\n"
        "    ])])\n",
    )


@pytest.mark.requires_ros2
def test_group_namespace_parameters_and_types_match_live_graph(tmp_path: Path) -> None:
    import rclpy

    if shutil.which("colcon") is None or shutil.which("ros2") is None:
        pytest.skip("colcon and the ros2 CLI are required")
    _fixture(tmp_path)
    model = UAM.build(tmp_path, use_cache=False)
    static = {t["name"]: canonical_interface(t["msg_type"]) for t in model.topics()}
    assert set(static) == {"/robot/chatter", "/robot/talker/x", "/configured"}
    assert sorted(d["namespace"] for d in model.deployments()) == ["/robot"] * 3
    audit = CliRunner().invoke(app, ["audit", str(tmp_path), "-f", "json"])
    findings = json.loads(audit.stdout)["findings"]
    assert [f["rule_type"] for f in findings] == ["interface_type_mismatch"]
    assert "'/robot/chatter'" in findings[0]["message"]

    result = subprocess.run(
        ["colcon", "build"], cwd=tmp_path, capture_output=True, text=True, timeout=180
    )
    assert result.returncode == 0, result.stdout + result.stderr
    launch = subprocess.Popen(
        [
            "bash",
            "-c",
            "source install/setup.bash && exec ros2 launch resolution_demo system.launch.py",
        ],
        cwd=tmp_path,
        env=dict(os.environ),
        start_new_session=True,
    )
    rclpy.init()
    observer = rclpy.create_node("resolution_observer")
    expected_nodes = {"/robot/talker", "/robot/listener", "/robot/typed"}
    try:
        live_nodes: set[str] = set()
        live: dict[str, list[str]] = {}
        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline:
            rclpy.spin_once(observer, timeout_sec=0.2)
            live_nodes = {
                (ns.rstrip("/") + "/" + name)
                for name, ns in observer.get_node_names_and_namespaces()
            }
            live = {
                name: sorted(canonical_interface(t) for t in types)
                for name, types in observer.get_topic_names_and_types()
                if name not in {"/rosout", "/parameter_events"}
            }
            if (
                expected_nodes <= live_nodes
                and set(live) == set(static)
                and all(len(v) == (2 if k == "/robot/chatter" else 1) for k, v in live.items())
            ):
                break
        assert launch.poll() is None
        assert expected_nodes <= live_nodes
        assert set(live) == set(static)
        assert live["/robot/chatter"] == ["std_msgs/Int32", "std_msgs/String"]
        for name in ("/robot/talker/x", "/configured"):
            assert live[name] == [static[name]]
    finally:
        observer.destroy_node()
        rclpy.shutdown()
        os.killpg(launch.pid, signal.SIGINT)
        try:
            launch.wait(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(launch.pid, signal.SIGKILL)
            launch.wait(timeout=5)
