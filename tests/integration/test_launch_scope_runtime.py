from __future__ import annotations

import json
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ros2inspector.cli.app import app
from ros2inspector.model.names import canonical_interface
from ros2inspector.model.uam import UAM
from tests.integration.test_name_resolution_runtime import _fixture


@pytest.mark.requires_ros2
@pytest.mark.parametrize(
    "case, namespace",
    [
        ("conditional", ""),
        ("conditional_true", "/robot"),
        ("unscoped", "/robot"),
        ("append", "/robot"),
        ("include", "/robot"),
        ("nested_include", "/robot/inner"),
        ("include_forwarding", "/inner"),
        ("forwarding", "/inner"),
    ],
)
def test_launch_scopes_match_live_graph(
    tmp_path: Path,
    case: str,
    namespace: str,
    ros_domain: int,
    ros_environment: dict[str, str],
    ros_process: Callable[..., subprocess.Popen[bytes]],
) -> None:
    import rclpy
    from rclpy.executors import SingleThreadedExecutor

    _fixture(tmp_path)
    package = tmp_path / "src/resolution_demo"
    for name in ("listener", "typed"):
        (package / f"resolution_demo/{name}.py").unlink()
    node_file = package / "resolution_demo/talker.py"
    source = node_file.read_text()
    source = "\n".join(line for line in source.splitlines() if "parameter" not in line) + "\n"
    source = source.replace(
        "def main():",
        "        self.create_subscription(String, 'chatter', print, 10)\n"
        "        self.create_subscription(String, '~/x', print, 10)\ndef main():",
    )
    node_file.write_text(source)
    setup = package / "setup.py"
    setup.write_text(
        setup.read_text()
        .replace("setup(", "setup(tests_require=['pytest'], ", 1)
        .replace(
            "['launch/system.launch.py']", "['launch/system.launch.py', 'launch/child.launch.py']"
        )
    )
    header = (
        "from launch import LaunchDescription\n"
        "from launch.actions import GroupAction, IncludeLaunchDescription\n"
        "from launch.conditions import IfCondition\n"
        "from launch.launch_description_sources import PythonLaunchDescriptionSource\n"
        "from launch.substitutions import PathJoinSubstitution\n"
        "from launch_ros.substitutions import FindPackageShare\n"
        "from launch_ros.actions import Node, PushRosNamespace\n"
        "def generate_launch_description():\n"
    )
    node = "Node(package='resolution_demo', executable='talker', name='talker')"
    child = package / "launch/child.launch.py"
    child.write_text(header + "    return LaunchDescription([])\n")
    include = (
        "IncludeLaunchDescription(PythonLaunchDescriptionSource(PathJoinSubstitution(["
        "FindPackageShare('resolution_demo'), 'launch', 'child.launch.py'])))"
    )
    if case in {"conditional", "conditional_true"}:
        expression = (
            "GroupAction(actions=[PushRosNamespace('robot', "
            f"condition=IfCondition({str(case == 'conditional_true').lower()!r})), {node}])"
        )
    elif case == "unscoped":
        expression = f"GroupAction(actions=[PushRosNamespace('robot')], scoped=False), {node}"
    elif case == "forwarding":
        expression = (
            "GroupAction(actions=[PushRosNamespace('outer'), GroupAction(actions=["
            f"PushRosNamespace('inner'), {node}], forwarding=False)])"
        )
    elif case in {"include", "nested_include", "include_forwarding"}:
        if case == "include_forwarding":
            child.write_text(
                header + "    return LaunchDescription([GroupAction(actions=["
                f"PushRosNamespace('inner'), {node}], forwarding=False)])\n"
            )
        elif case == "include":
            child.write_text(header + f"    return LaunchDescription([{node}])\n")
        else:
            grandchild = package / "launch/grandchild.launch.py"
            grandchild.write_text(header + f"    return LaunchDescription([{node}])\n")
            setup.write_text(
                setup.read_text().replace(
                    "'launch/child.launch.py']",
                    "'launch/child.launch.py', 'launch/grandchild.launch.py']",
                )
            )
            nested = include.replace("child.launch.py", "grandchild.launch.py")
            child.write_text(
                header + "    return LaunchDescription([GroupAction(actions=["
                "PushRosNamespace('inner')], scoped=False), " + nested + "])\n"
            )
        expression = f"GroupAction(actions=[PushRosNamespace('robot'), {include}])"
    else:
        expression = ""
    body = (
        "    ld = LaunchDescription()\n"
        "    ld.add_action(PushRosNamespace('robot'))\n"
        f"    ld.add_action({node})\n    return ld\n"
        if case == "append"
        else f"    return LaunchDescription([{expression}])\n"
    )
    (package / "launch/system.launch.py").write_text(header + body)
    tests = package / "test"
    tests.mkdir()
    (tests / "test_compile.py").write_text(
        "import py_compile\nfrom pathlib import Path\n"
        "def test_compile():\n"
        " for path in Path('resolution_demo').glob('*.py'):\n"
        "  py_compile.compile(str(path), doraise=True)\n"
    )
    model = UAM.build(tmp_path, use_cache=False)
    static = {
        model.graph.nodes[target]["name"]: canonical_interface(edge["msg_type"])
        for _, target, edge in model.graph.edges(data=True)
        if edge.get("rel") == "publishes" and edge.get("resolution") == "known"
    }
    assert static == {
        namespace + "/chatter": "std_msgs/String",
        namespace + "/talker/x": "std_msgs/String",
    }
    audit = CliRunner().invoke(app, ["audit", str(tmp_path), "--no-cache", "-f", "json"])
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
    process = ros_process(
        [
            "bash",
            "-c",
            "source install/setup.bash && exec ros2 launch resolution_demo system.launch.py",
        ],
        cwd=tmp_path,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    context = rclpy.Context()
    rclpy.init(context=context, domain_id=ros_domain)
    observer = rclpy.create_node("scope_observer", context=context)
    executor = SingleThreadedExecutor(context=context)
    try:
        live = {}
        nodes = set()
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            rclpy.spin_once(observer, executor=executor, timeout_sec=0.2)
            live = {
                name: [canonical_interface(t) for t in types]
                for name, types in observer.get_topic_names_and_types()
                if name not in {"/rosout", "/parameter_events"}
            }
            nodes = {
                ns.rstrip("/") + "/" + name for name, ns in observer.get_node_names_and_namespaces()
            }
            if live == {name: [kind] for name, kind in static.items()} and nodes == {
                namespace + "/talker",
                "/scope_observer",
            }:
                break
        assert process.poll() is None
        assert nodes == {namespace + "/talker", "/scope_observer"}
        assert live == {name: [kind] for name, kind in static.items()}
    finally:
        executor.shutdown()
        observer.destroy_node()
        context.shutdown()
