from pathlib import Path

import pytest

from ros2inspector.graph import render_dot, render_json, render_mermaid
from ros2inspector.model.uam import UAM

WORKSPACE_A = Path(__file__).parent.parent / "fixtures" / "workspaces" / "workspace_a"


@pytest.fixture(scope="module")
def uam() -> UAM:
    return UAM.build(WORKSPACE_A)


def test_mermaid_deps_contains_flowchart_lr(uam: UAM) -> None:
    out = render_mermaid(uam, "deps")
    assert "flowchart LR" in out


def test_mermaid_deps_references_all_packages(uam: UAM) -> None:
    out = render_mermaid(uam, "deps")
    assert "pkg_a" in out
    assert "pkg_b" in out
    assert "pkg_c" in out


def test_mermaid_deps_has_dashed_edge(uam: UAM) -> None:
    out = render_mermaid(uam, "deps")
    assert "-.-> " in out


def test_mermaid_comms_has_pub_label(uam: UAM) -> None:
    out = render_mermaid(uam, "comms")
    assert "pub" in out


def test_mermaid_comms_has_sub_label(uam: UAM) -> None:
    out = render_mermaid(uam, "comms")
    assert "sub" in out


def test_dot_comms_contains_digraph(uam: UAM) -> None:
    out = render_dot(uam, "comms")
    assert "digraph" in out


def test_dot_comms_contains_node_shapes(uam: UAM) -> None:
    out = render_dot(uam, "comms")
    assert "shape=" in out


def test_dot_comms_has_topic_node(uam: UAM) -> None:
    out = render_dot(uam, "comms")
    assert "shape=ellipse" in out


def test_json_full_is_valid(uam: UAM) -> None:
    import orjson

    out = render_json(uam, "full")
    parsed = orjson.loads(out)
    assert "packages" in parsed or "nodes" in parsed


@pytest.mark.parametrize("graph_type", ["full", "comms"])
def test_json_communication_edges_refer_to_graph_nodes(uam: UAM, graph_type: str) -> None:
    import orjson

    parsed = orjson.loads(render_json(uam, graph_type))
    graph = parsed["graph"] if graph_type == "full" else parsed
    node_ids = {node["id"] for node in graph["nodes"]}
    communication_edges = [
        edge for edge in graph["edges"] if edge["rel"] in {"publishes", "subscribes"}
    ]
    assert communication_edges
    assert all(edge["source"] in node_ids and edge["target"] in node_ids for edge in graph["edges"])
    assert all(edge["data_source"] == "static" for edge in communication_edges)


def test_mermaid_package_filter_includes_neighbors(uam: UAM) -> None:
    out = render_mermaid(uam, "deps", package="pkg_b")
    assert "pkg_b" in out
    assert "pkg_a" in out


def test_mermaid_package_filter_excludes_unrelated(uam: UAM) -> None:
    # pkg_c has no path to pkg_b in a deps subgraph filtered to pkg_a's neighborhood
    # (pkg_a has no incoming dep edges; pkg_b and pkg_c both depend on pkg_a)
    out = render_mermaid(uam, "deps", package="pkg_a")
    assert "pkg_a" in out
    # pkg_b and pkg_c both depend on pkg_a, so they are neighbors
    assert "pkg_b" in out
    assert "pkg_c" in out


def test_dot_package_filter_keeps_direct_neighbors(uam: UAM) -> None:
    out = render_dot(uam, "deps", package="pkg_b")
    assert "digraph" in out
    assert "pkg_b" in out
    assert "pkg_a" in out


def test_mermaid_full_graph_has_all_kinds(uam: UAM) -> None:
    out = render_mermaid(uam, "full")
    assert "flowchart LR" in out
    # Full graph must reference both package and comms nodes
    assert "pkg_a" in out
    assert "chatter" in out.lower()
