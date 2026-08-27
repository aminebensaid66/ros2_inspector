"""Portable installed-surface integration tests against a committed ROS 2 fixture."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ros2inspector.cli.app import app
from ros2inspector.model.uam import UAM

pytestmark = pytest.mark.integration
runner = CliRunner()


@pytest.fixture(scope="module")
def uam(real_ws: Path) -> UAM:
    return UAM.build(real_ws, use_cache=False)


def test_full_pipeline_finds_packages_nodes_interfaces_and_edges(uam: UAM) -> None:
    assert {package.name for package in uam.packages()} == {"pkg_a", "pkg_b", "pkg_c"}
    assert {node.name for node in uam.nodes()} >= {"TalkerNode", "ListenerNode"}
    assert len(uam.interfaces()) >= 1
    assert uam.graph.number_of_edges() >= 1


def test_cache_round_trip_is_equivalent(real_ws: Path, tmp_path: Path) -> None:
    uncached = UAM.build(real_ws, use_cache=False)
    first = UAM.build(real_ws, use_cache=True, cache_dir=tmp_path / "cache")
    second = UAM.build(real_ws, use_cache=True, cache_dir=tmp_path / "cache")
    expected = uncached.to_dict()
    assert first.to_dict() == expected
    assert second.to_dict() == expected


def test_cli_scan_packages_and_nodes(real_ws: Path) -> None:
    scan = runner.invoke(app, ["scan", str(real_ws), "--format", "json"])
    assert scan.exit_code == 0, scan.output
    assert len(json.loads(scan.output[scan.output.index("[") :])) == 3

    packages = runner.invoke(app, ["packages", "-C", str(real_ws), "--format", "json"])
    assert packages.exit_code == 0, packages.output
    assert {item["name"] for item in json.loads(packages.output)} == {"pkg_a", "pkg_b", "pkg_c"}

    meta_packages = runner.invoke(
        app, ["packages", "-C", str(real_ws), "--filter", "meta", "--format", "json"]
    )
    assert meta_packages.exit_code == 0, meta_packages.output
    assert [item["name"] for item in json.loads(meta_packages.output)] == ["pkg_c"]

    nodes = runner.invoke(
        app,
        ["nodes", "-C", str(real_ws), "--format", "json", "--no-cache"],
    )
    assert nodes.exit_code == 0, nodes.output
    assert {item["name"] for item in json.loads(nodes.output)} >= {"TalkerNode", "ListenerNode"}


@pytest.mark.parametrize(
    ("graph_type", "fmt", "needle"),
    [("deps", "mermaid", "flowchart"), ("full", "dot", "digraph"), ("full", "json", '"nodes"')],
)
def test_cli_graph_exports(real_ws: Path, graph_type: str, fmt: str, needle: str) -> None:
    result = runner.invoke(
        app,
        ["graph", graph_type, "-C", str(real_ws), "--format", fmt, "--no-cache"],
    )
    assert result.exit_code == 0, result.output
    assert needle in result.output


def test_cli_viz_is_self_contained(real_ws: Path, tmp_path: Path) -> None:
    output = tmp_path / "graph.html"
    result = runner.invoke(
        app,
        ["viz", "-C", str(real_ws), "-o", str(output), "--no-open", "--no-cache"],
    )
    assert result.exit_code == 0, result.output
    html = output.read_text(encoding="utf-8")
    assert "cytoscape" in html.lower()
    assert "https://unpkg.com" not in html
    assert "https://cdn" not in html


def test_cli_version() -> None:
    version = runner.invoke(app, ["--version"])
    assert version.exit_code == 0
    assert "0.1.1" in version.output
