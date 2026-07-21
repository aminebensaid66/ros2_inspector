"""Regression tests for release-blocking workspace and policy failure modes."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from ros2inspector.cli.app import app
from ros2inspector.discovery import find_package_xml_files
from ros2inspector.model.uam import UAM

runner = CliRunner()


def _valid_policy(path: Path) -> Path:
    policy = path / "policy.yaml"
    policy.write_text("version: 1\nrules: []\n", encoding="utf-8")
    return policy


@pytest.mark.parametrize(
    "args",
    [
        ["scan"],
        ["packages"],
        ["nodes"],
        ["graph"],
        ["viz", "--no-open"],
        ["audit"],
    ],
)
def test_analysis_commands_reject_empty_workspace(tmp_path: Path, args: list[str]) -> None:
    command = [*args]
    if args[0] in {"scan", "audit"}:
        command.append(str(tmp_path))
    else:
        command.extend(["-C", str(tmp_path)])
    result = runner.invoke(app, command)
    assert result.exit_code in {1, 3}, result.output
    assert "package" in result.output.lower()


def test_validate_rejects_empty_workspace(tmp_path: Path) -> None:
    policy = _valid_policy(tmp_path)
    result = runner.invoke(app, ["validate", str(tmp_path), "--policy", str(policy)])
    assert result.exit_code == 3
    assert "No valid ROS 2 packages" in result.output


def test_source_discovery_ignores_colcon_outputs(tmp_path: Path) -> None:
    package_xml = (
        "<package format='3'><name>only_source</name><version>0.1.0</version>"
        "<description>x</description><maintainer email='a@b.c'>A</maintainer>"
        "<license>Apache-2.0</license></package>"
    )
    source = tmp_path / "src" / "only_source"
    source.mkdir(parents=True)
    (source / "package.xml").write_text(package_xml)
    for generated in ("build", "install", "log"):
        copied = tmp_path / generated / "only_source"
        copied.mkdir(parents=True)
        (copied / "package.xml").write_text(package_xml)

    found = find_package_xml_files(tmp_path)
    assert found == [source / "package.xml"]
    assert [package.name for package in UAM.build(tmp_path, use_cache=False).packages()] == [
        "only_source"
    ]


def test_invalid_policy_is_reported_without_traceback(tmp_path: Path) -> None:
    fixture = Path(__file__).parent.parent / "fixtures" / "workspaces" / "workspace_a"
    bad_policy = tmp_path / "bad.yaml"
    bad_policy.write_text(
        "version: 1\nrules:\n  - type: naming\n    packages:\n      pattern: '[invalid'\n"
    )
    result = runner.invoke(
        app,
        ["validate", str(fixture), "--policy", str(bad_policy)],
    )
    assert result.exit_code == 2
    assert "Policy error" in result.output
    assert "Traceback" not in result.output
