from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from ros2inspector.model.uam import UAM


@pytest.mark.requires_ros2
def test_static_topic_prediction_matches_live_ros_graph(tmp_path: Path) -> None:
    rclpy = pytest.importorskip("rclpy")

    package = tmp_path / "src" / "runtime_demo"
    module_dir = package / "runtime_demo"
    launch_dir = package / "launch"
    module_dir.mkdir(parents=True)
    launch_dir.mkdir()
    (package / "package.xml").write_text(
        "<package format='3'><name>runtime_demo</name><version>0.1.0</version>"
        "<description>runtime comparison fixture</description>"
        "<maintainer email='dev@example.com'>Dev</maintainer>"
        "<license>Apache-2.0</license><exec_depend>rclpy</exec_depend>"
        "<exec_depend>std_msgs</exec_depend></package>",
        encoding="utf-8",
    )
    (package / "setup.py").write_text(
        "from setuptools import setup\n"
        "setup(name='runtime_demo', entry_points={'console_scripts': "
        "['runtime_node = runtime_demo.runtime_node:main']})\n",
        encoding="utf-8",
    )
    (launch_dir / "runtime.launch.xml").write_text(
        "<launch><node pkg='runtime_demo' exec='runtime_node' name='runtime_fixture'/></launch>",
        encoding="utf-8",
    )

    source = module_dir / "runtime_node.py"
    source.write_text(
        "import rclpy\n"
        "from rclpy.node import Node\n"
        "from std_msgs.msg import String\n"
        "class RuntimeFixture(Node):\n"
        "    def __init__(self):\n"
        "        super().__init__('runtime_fixture')\n"
        "        self.pub = self.create_publisher(String, '/audit_chatter', 10)\n"
        "        self.sub = self.create_subscription(String, '/audit_chatter', self.cb, 10)\n"
        "    def cb(self, msg):\n"
        "        pass\n"
        "def main():\n"
        "    rclpy.init()\n"
        "    node = RuntimeFixture()\n"
        "    try:\n"
        "        rclpy.spin(node)\n"
        "    finally:\n"
        "        node.destroy_node()\n"
        "        rclpy.shutdown()\n"
        "if __name__ == '__main__':\n"
        "    main()\n",
        encoding="utf-8",
    )

    static_model = UAM.build(tmp_path, use_cache=False)
    static_topic = next(
        topic for topic in static_model.topics() if topic["name"] == "/audit_chatter"
    )
    assert static_topic["msg_type"] == "std_msgs/String"
    assert static_topic["publishers"] == ["runtime_fixture"]
    assert static_topic["subscribers"] == ["runtime_fixture"]

    child_env = dict(os.environ)
    process = subprocess.Popen([sys.executable, str(source)], env=child_env)
    observer = None
    try:
        rclpy.init()
        observer = rclpy.create_node("ros2inspector_runtime_observer")
        deadline = time.monotonic() + 15.0
        runtime_node_names: set[str] = set()
        publishers = []
        subscribers = []
        while time.monotonic() < deadline:
            rclpy.spin_once(observer, timeout_sec=0.2)
            runtime_node_names = {
                name
                for name, _namespace in observer.get_node_names_and_namespaces()
            }
            publishers = observer.get_publishers_info_by_topic("/audit_chatter")
            subscribers = observer.get_subscriptions_info_by_topic("/audit_chatter")
            if "runtime_fixture" in runtime_node_names and publishers and subscribers:
                break

        assert "runtime_fixture" in runtime_node_names
        assert publishers
        assert subscribers
        assert {info.topic_type for info in publishers} == {"std_msgs/msg/String"}
        assert {info.topic_type for info in subscribers} == {"std_msgs/msg/String"}
    finally:
        if observer is not None:
            observer.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
