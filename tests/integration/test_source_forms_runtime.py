from __future__ import annotations

import os
import signal
import subprocess
import time
from pathlib import Path

import pytest

from ros2inspector.model.uam import UAM


def _write(root: Path, relative: str, text: str) -> None:
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)


@pytest.mark.requires_ros2
def test_source_forms_match_built_executables(tmp_path: Path) -> None:
    import rclpy

    names = ["factory", "direct", "child", "speaker_hh", "speaker_hxx"]
    for suffix in ["hh", "hxx"]:
        name = "speaker_" + suffix
        package = tmp_path / "src" / name
        _write(
            package,
            "package.xml",
            f'<package format="3"><name>{name}</name><version>0.1.0</version>'
            "<description>fixture</description>"
            '<maintainer email="dev@example.com">Dev</maintainer>'
            "<license>MIT</license><buildtool_depend>ament_cmake</buildtool_depend>"
            "<depend>rclcpp</depend><depend>std_msgs</depend>"
            "<export><build_type>ament_cmake</build_type></export></package>",
        )
        _write(
            package,
            "CMakeLists.txt",
            f"cmake_minimum_required(VERSION 3.8)\nproject({name})\n"
            "find_package(ament_cmake REQUIRED)\nfind_package(rclcpp REQUIRED)\n"
            "find_package(std_msgs REQUIRED)\nadd_executable(speaker main.cpp)\n"
            "ament_target_dependencies(speaker rclcpp std_msgs)\n"
            "install(TARGETS speaker DESTINATION lib/${PROJECT_NAME})\nament_package()\n",
        )
        _write(
            package,
            f"speaker.{suffix}",
            "#include <rclcpp/rclcpp.hpp>\n#include <std_msgs/msg/string.hpp>\n"
            "class Speaker : public rclcpp::Node { public: "
            f'Speaker() : Node("{name}") {{ '
            f'pub = create_publisher<std_msgs::msg::String>("/{name}_topic",10); '
            "} rclcpp::Publisher<std_msgs::msg::String>::SharedPtr pub; };\n",
        )
        _write(
            package,
            "main.cpp",
            f'#include "speaker.{suffix}"\nint main(int argc,char** argv){{'
            "rclcpp::init(argc,argv);rclcpp::spin(std::make_shared<Speaker>());"
            "rclcpp::shutdown();}\n",
        )
    package = tmp_path / "src/forms"
    _write(
        package,
        "package.xml",
        '<package format="3"><name>forms</name><version>0.1.0</version>'
        "<description>fixture</description>"
        '<maintainer email="dev@example.com">Dev</maintainer>'
        "<license>MIT</license><exec_depend>rclpy</exec_depend><exec_depend>std_msgs</exec_depend>"
        "<export><build_type>ament_python</build_type></export></package>",
    )
    _write(package, "resource/forms", "")
    _write(
        package,
        "setup.cfg",
        "[develop]\nscript_dir=$base/lib/forms\n[install]\ninstall_scripts=$base/lib/forms\n",
    )
    _write(package, "forms/__init__.py", "")
    _write(
        package,
        "setup.py",
        "from setuptools import setup\n"
        'setup(name="forms",packages=["forms"],tests_require=["pytest"],'
        'data_files=[("share/ament_index/resource_index/packages",["resource/forms"]),'
        '("share/forms",["package.xml"])],entry_points={"console_scripts":'
        '["factory = forms.factory:main","direct = forms.direct:main",'
        '"child = forms.child:main"]})\n',
    )
    for name, ctor in [("factory", "rclpy.create_node"), ("direct", "Node")]:
        _write(
            package,
            f"forms/{name}.py",
            "import rclpy\nfrom rclpy.node import Node\nfrom std_msgs.msg import String\n"
            f'def main():\n rclpy.init()\n node = {ctor}("{name}")\n'
            f' node.create_publisher(String,"/{name}_topic",10)\n rclpy.spin(node)\n',
        )
    _write(
        package,
        "forms/base.py",
        "from rclpy.node import Node\nclass Base(Node):\n"
        ' def __init__(self):\n  super().__init__("child")\n',
    )
    _write(
        package,
        "forms/child.py",
        "import rclpy\nfrom .base import Base\nfrom std_msgs.msg import String\n"
        "class Child(Base):\n def __init__(self):\n  super().__init__()\n"
        '  self.create_publisher(String,"/child_topic",10)\n'
        "def main():\n rclpy.init()\n rclpy.spin(Child())\n",
    )
    _write(
        package,
        "test/test_construction.py",
        "import rclpy\nfrom forms.child import Child\n"
        "def test_child_constructor():\n rclpy.init()\n node = Child()\n"
        " try:\n  assert node.get_name() == 'child'\n"
        " finally:\n  node.destroy_node()\n  rclpy.shutdown()\n",
    )
    model = UAM.build(tmp_path, use_cache=False)
    assert {f"/{name}_topic" for name in names} <= {t["name"] for t in model.topics()}
    for command in [
        ["colcon", "build", "--parallel-workers", "2"],
        ["colcon", "test"],
        ["colcon", "test-result", "--verbose"],
    ]:
        result = subprocess.run(command, cwd=tmp_path, capture_output=True, text=True, timeout=120)
        assert result.returncode == 0, result.stdout + result.stderr
    env = dict(os.environ)
    env["AMENT_PREFIX_PATH"] = (
        ":".join(str(tmp_path / "install" / p) for p in ["forms", "speaker_hh", "speaker_hxx"])
        + ":"
        + env.get("AMENT_PREFIX_PATH", "")
    )
    env["PYTHONPATH"] = (
        ":".join(str(p) for p in (tmp_path / "install/forms/lib").glob("python*/site-packages"))
        + ":"
        + env.get("PYTHONPATH", "")
    )
    processes = []
    rclpy.init()
    observer = rclpy.create_node("source_forms_observer")
    try:
        for name in names:
            pkg = "forms" if name in ["factory", "direct", "child"] else name
            executable = name if pkg == "forms" else "speaker"
            processes.append(
                subprocess.Popen(
                    ["ros2", "run", pkg, executable],
                    env=env,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    start_new_session=True,
                )
            )
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            rclpy.spin_once(observer, timeout_sec=0.2)
            topics = dict(observer.get_topic_names_and_types())
            runtime_names = set(observer.get_node_names())
            if set(names) <= runtime_names and all(f"/{name}_topic" in topics for name in names):
                break
        errors = {
            name: process.stderr.read().decode()
            for name, process in zip(names, processes, strict=True)
            if process.poll() is not None and process.stderr is not None
        }
        assert set(names) <= runtime_names, errors
        assert all(process.poll() is None for process in processes), errors
        for name in names:
            assert topics[f"/{name}_topic"] == ["std_msgs/msg/String"]
    finally:
        for process in processes:
            os.killpg(process.pid, signal.SIGTERM)
        for process in processes:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)
        observer.destroy_node()
        rclpy.shutdown()
