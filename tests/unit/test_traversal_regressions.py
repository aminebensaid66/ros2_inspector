from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ros2inspector.cli.app import app
from ros2inspector.cli.cmd_watch import _changed_files, _snapshot
from ros2inspector.discovery import find_package_xml_files
from ros2inspector.model.uam import UAM


def _package(root: Path) -> Path:
    package = root / "src/demo"
    package.mkdir(parents=True)
    (package / "package.xml").write_text(
        '<package format="3"><name>demo</name><version>0.1.0</version>'
        '<description>Demo</description><maintainer email="dev@example.com">Dev</maintainer>'
        "<license>MIT</license><export><build_type>ament_python</build_type></export></package>"
    )
    return package


def _link(path: Path, target: Path) -> None:
    try:
        path.symlink_to(target, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("directory symlinks unavailable")


def test_workspace_and_package_cycles_do_not_repeat_sources(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    package = _package(root)
    _link(root / "src/loop", root)
    _link(package / "back", root)
    files = find_package_xml_files(root)
    assert files == [(package / "package.xml").resolve()]
    result = CliRunner().invoke(app, ["scan", str(root), "-f", "json"])
    assert result.exit_code == 0, result.output
    assert len(json.loads(result.stdout)) == 1


def test_linked_module_changes_reach_model_cache_and_watch(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    package = _package(root)
    shared = tmp_path / "shared"
    shared.mkdir()
    source = shared / "driver.py"
    source.write_text(
        "from rclpy.node import Node\nfrom std_msgs.msg import String\n"
        "class Driver(Node):\n    def __init__(self):\n"
        "        super().__init__('driver')\n"
        "        self.create_publisher(String, '/old', 10)\n"
    )
    _link(package / "demo", shared)
    _link(shared / "back", package)
    model = UAM.build(root)
    assert [n.name for n in model.nodes()] == ["Driver"]
    assert "/old" in {n["name"] for n in model.topics()}
    before = _snapshot(root)
    assert "demo/demo/driver.py" in before
    stamp = source.stat()
    source.write_text(source.read_text().replace("/old", "/new"))
    os.utime(source, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    assert "/new" in {n["name"] for n in UAM.build(root).topics()}
    assert "demo/demo/driver.py" in _changed_files(before, _snapshot(root))
    result = CliRunner().invoke(app, ["nodes", "-C", str(root), "-f", "json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)[0]["publishers"][0]["name"] == "/new"


@pytest.mark.parametrize(
    "command", ["scan", "nodes", "graph", "packages", "audit", "docs", "watch", "viz", "validate"]
)
def test_missing_workspace_is_reported_before_discovery(tmp_path: Path, command: str) -> None:
    missing = tmp_path / "missing"
    policy = tmp_path / "policy.yaml"
    policy.write_text("rules: []\n")
    args = [command]
    args += (
        ["-C", str(missing)] if command in {"nodes", "graph", "packages", "viz"} else [str(missing)]
    )
    if command == "validate":
        args += ["--policy", str(policy)]
    if command == "watch":
        args += ["--audit", "--once"]
    if command == "viz":
        args += ["--no-open"]
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 3, result.output
    assert "does not exist" in result.stderr
    assert "using it as root" not in result.stderr


def test_file_workspace_is_rejected(tmp_path: Path) -> None:
    source = tmp_path / "file"
    source.touch()
    result = CliRunner().invoke(app, ["scan", str(source)])
    assert result.exit_code == 3
    assert "not a directory" in result.stderr


@pytest.mark.parametrize("command", ["nodes", "graph"])
def test_unknown_focus_names_available_package(tmp_path: Path, command: str) -> None:
    _package(tmp_path)
    result = CliRunner().invoke(app, [command, "-C", str(tmp_path), "-p", "dmeo", "-f", "json"])
    assert result.exit_code == 2, result.output
    assert result.stdout.strip() == ""
    assert "Unknown package 'dmeo'" in result.stderr
    assert "Available: demo" in result.stderr
    assert "did you mean 'demo'" in result.stderr
