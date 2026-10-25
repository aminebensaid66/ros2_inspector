from __future__ import annotations

import json
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ros2inspector.cli.app import app
from ros2inspector.model.uam import UAM
from tests.integration.test_source_forms_runtime import _write


def _package(root: Path, name: str, sources: dict[str, str], dependencies: list[str]) -> None:
    package = root / "src" / name
    _write(
        package,
        "package.xml",
        f'<package format="3"><name>{name}</name><version>0.1.0</version>'
        '<description>fixture</description><maintainer email="dev@example.com">Dev</maintainer>'
        "<license>MIT</license>"
        + "".join(f"<depend>{dep}</depend>" for dep in dependencies)
        + "<export><build_type>ament_python</build_type></export></package>",
    )
    _write(package, "resource/" + name, "")
    _write(
        package,
        "setup.cfg",
        f"[develop]\nscript_dir=$base/lib/{name}\n[install]\ninstall_scripts=$base/lib/{name}\n",
    )
    _write(package, name + "/__init__.py", "")
    scripts = [
        f"{module} = {name}.{module}:main"
        for module, source in sources.items()
        if "def main():" in source
    ]
    _write(
        package,
        "setup.py",
        "from setuptools import setup\nfrom glob import glob\n"
        f"setup(name={name!r}, packages=[{name!r}], tests_require=['pytest'], data_files=["
        f"('share/ament_index/resource_index/packages', ['resource/{name}']),"
        f"('share/{name}', ['package.xml']),('share/{name}/launch', glob('launch/*.py'))],"
        f"entry_points={{'console_scripts': {scripts!r}}})\n",
    )
    for module, source in sources.items():
        _write(package, f"{name}/{module}.py", source)
    _write(
        package,
        "test/test_compile.py",
        "import py_compile\nfrom pathlib import Path\n"
        "def test_compile():\n"
        f" for source in Path({name!r}).glob('*.py'):\n"
        "  py_compile.compile(str(source), doraise=True)\n",
    )


@pytest.mark.requires_ros2
@pytest.mark.parametrize("case", ["expression", "action_alias", "inheritance"])
def test_python_gaps_match_live_graph(
    tmp_path: Path,
    case: str,
    ros_domain: int,
    ros_environment: dict[str, str],
    ros_process: Callable[..., subprocess.Popen[bytes]],
) -> None:
    import rclpy
    from rclpy.executors import SingleThreadedExecutor

    header = "import rclpy\nfrom rclpy.node import Node\nfrom std_msgs.msg import String\n"
    if case == "expression":
        _package(
            tmp_path,
            "gap_demo",
            {
                "talker": header + "x = " + " + ".join(["1"] * 800) + "\n"
                "class Talker(Node):\n def __init__(self):\n  super().__init__('talker')\n"
                "  self.pub = self.create_publisher(String, '/healthy', 10)\n"
                "def main():\n rclpy.init()\n rclpy.spin(Talker())\n",
                "listener": header + "class Listener(Node):\n def __init__(self):\n"
                "  super().__init__('listener')\n"
                "  self.sub = self.create_subscription(String, '/healthy', print, 10)\n"
                "def main():\n rclpy.init()\n rclpy.spin(Listener())\n",
            },
            ["rclpy", "std_msgs"],
        )
        commands = ["ros2 run gap_demo talker", "ros2 run gap_demo listener"]
        expected_nodes = {"/talker", "/listener"}
        expected_topics = {"/healthy": ["std_msgs/msg/String"]}
    elif case == "action_alias":
        _package(
            tmp_path,
            "alias_demo",
            {
                "alias": header + "from rclpy.action import ActionClient as AC\n"
                "from example_interfaces.action import Fibonacci\n"
                "class Alias(Node):\n def __init__(self):\n  super().__init__('alias')\n"
                "  self.client = AC(self, Fibonacci, '/work')\n"
                "def main():\n rclpy.init()\n rclpy.spin(Alias())\n"
            },
            ["rclpy", "std_msgs", "example_interfaces"],
        )
        commands = ["ros2 run alias_demo alias"]
        expected_nodes = {"/alias"}
        expected_topics = {
            "/work/_action/feedback": ["example_interfaces/action/Fibonacci_FeedbackMessage"],
            "/work/_action/status": ["action_msgs/msg/GoalStatusArray"],
        }
    else:
        _package(
            tmp_path,
            "base_pkg",
            {
                "base": header
                + "class Base(Node):\n def __init__(self):\n  super().__init__('base')\n"
                "  self.pub = self.create_publisher(String, 'chatter', 10)\n"
            },
            ["rclpy", "std_msgs"],
        )
        _package(
            tmp_path,
            "child_pkg",
            {
                "child": "import rclpy\nfrom base_pkg.base import Base\n"
                "class Child(Base):\n pass\n"
                "def main():\n rclpy.init()\n rclpy.spin(Child())\n"
            },
            ["base_pkg", "rclpy", "std_msgs", "launch_ros"],
        )
        _write(
            tmp_path / "src/child_pkg",
            "launch/system.launch.py",
            "from launch import LaunchDescription\nfrom launch_ros.actions import Node\n"
            "def generate_launch_description():\n return LaunchDescription(["
            "Node(package='child_pkg', executable='child', name='child', namespace='robot')])\n",
        )
        commands = ["ros2 launch child_pkg system.launch.py"]
        expected_nodes = {"/robot/child"}
        expected_topics = {"/robot/chatter": ["std_msgs/msg/String"]}
    model = UAM.build(tmp_path, use_cache=False)
    assert not model.diagnostics()
    if case == "action_alias":
        assert model.actions()[0]["name"] == "/work"
        assert model.nodes()[0].action_clients[0].msg_type == "example_interfaces/Fibonacci"
    else:
        assert {t["name"] for t in model.topics()} == set(expected_topics)
    if case == "inheritance":
        assert model.deployments()[0]["name"] == "/robot/child"
    if case == "expression":
        audit = CliRunner().invoke(
            app, ["audit", str(tmp_path), "--fail-on", "warning", "-f", "json"]
        )
        assert audit.exit_code == 0, audit.output
        assert json.loads(audit.stdout)["findings"] == []
    for command in (
        ["colcon", "build"],
        ["colcon", "test"],
        ["colcon", "test-result", "--verbose"],
    ):
        result = subprocess.run(
            command, cwd=tmp_path, env=ros_environment, capture_output=True, text=True, timeout=180
        )
        assert result.returncode == 0, result.stdout + result.stderr
    processes = [
        ros_process(
            ["bash", "-c", "source install/setup.bash && exec " + command],
            cwd=tmp_path,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        for command in commands
    ]
    context = rclpy.Context()
    rclpy.init(context=context, domain_id=ros_domain)
    observer = rclpy.create_node("gap_observer", context=context)
    executor = SingleThreadedExecutor(context=context)
    try:
        live = {}
        nodes = set()
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            rclpy.spin_once(observer, executor=executor, timeout_sec=0.2)
            live = {
                name: types
                for name, types in observer.get_topic_names_and_types()
                if name not in {"/rosout", "/parameter_events"}
            }
            nodes = {
                ns.rstrip("/") + "/" + name for name, ns in observer.get_node_names_and_namespaces()
            }
            ready = True
            if case == "expression":
                ready = (
                    len(observer.get_publishers_info_by_topic("/healthy")) == 1
                    and len(observer.get_subscriptions_info_by_topic("/healthy")) == 1
                )
            elif case == "action_alias" and "/alias" in nodes:
                clients = dict(observer.get_client_names_and_types_by_node("alias", "/"))
                ready = {name for name in clients if "/_action/" in name} == {
                    "/work/_action/cancel_goal",
                    "/work/_action/get_result",
                    "/work/_action/send_goal",
                }
            if ready and live == expected_topics and nodes == expected_nodes | {"/gap_observer"}:
                break
        assert all(process.poll() is None for process in processes)
        assert nodes == expected_nodes | {"/gap_observer"}
        assert live == expected_topics
        if case == "expression":
            assert len(observer.get_publishers_info_by_topic("/healthy")) == 1
            assert len(observer.get_subscriptions_info_by_topic("/healthy")) == 1
        elif case == "action_alias":
            clients = dict(observer.get_client_names_and_types_by_node("alias", "/"))
            assert {name for name in clients if "/_action/" in name} == {
                "/work/_action/cancel_goal",
                "/work/_action/get_result",
                "/work/_action/send_goal",
            }
    finally:
        executor.shutdown()
        observer.destroy_node()
        context.shutdown()
