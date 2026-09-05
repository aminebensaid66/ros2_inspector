from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from ros2inspector.cache.analysis_cache import _pkg_fingerprint
from ros2inspector.cli.cmd_nodes import _render_table_str
from ros2inspector.discovery.file_walker import iter_package_files
from ros2inspector.model.schemas import (
    DYNAMIC_SENTINEL,
    CommunicationEndpoint,
    NodeDefinition,
    PackageMetadata,
    PackageType,
)
from ros2inspector.model.uam import UAM, _apply_namespace, _node_id, _topic_id
from ros2inspector.policy.rules import (
    rule_action_connectivity,
    rule_service_connectivity,
    rule_topic_connectivity,
)
from ros2inspector.static.cpp_parser import _cpp_type_to_ros, _extract_declared_ros_name
from ros2inspector.static.launch_analyzer import LaunchNode
from ros2inspector.viz.renderer import _cy_elements


def _package(name: str, path: Path) -> PackageMetadata:
    return PackageMetadata(
        name=name,
        version="0.1.0",
        package_type=PackageType.AMENT_PYTHON,
        path=str(path),
    )


def test_dynamic_name_stays_unresolved_when_namespace_is_applied() -> None:
    assert _apply_namespace(DYNAMIC_SENTINEL, "robot") == DYNAMIC_SENTINEL


def test_dynamic_endpoints_in_same_namespace_remain_isolated(tmp_path: Path) -> None:
    package = _package("demo", tmp_path)
    publisher = NodeDefinition(
        name="Publisher",
        source_symbol="Publisher",
        declared_ros_name="publisher",
        package="demo",
        language="python",
        publishers=[CommunicationEndpoint(name=DYNAMIC_SENTINEL)],
    )
    subscriber = NodeDefinition(
        name="Subscriber",
        source_symbol="Subscriber",
        declared_ros_name="subscriber",
        package="demo",
        language="python",
        subscriptions=[CommunicationEndpoint(name=DYNAMIC_SENTINEL)],
    )
    model = UAM()
    model._launch_remaps["demo"] = [
        LaunchNode(executable="publisher", package="demo", name="publisher", namespace="robot"),
        LaunchNode(executable="subscriber", package="demo", name="subscriber", namespace="robot"),
    ]
    model._build_graph([package], [publisher, subscriber], [], {"demo": package})

    unresolved = [
        node_id
        for node_id, attrs in model.graph.nodes(data=True)
        if attrs.get("kind") == "Topic" and attrs.get("resolution") == "unresolved"
    ]
    assert len(unresolved) == 2
    assert _topic_id("/robot/<dynamic>") not in model.graph


def test_connectivity_rules_skip_unresolved_endpoints_by_default() -> None:
    cases = (
        ("Topic", "publishes", rule_topic_connectivity),
        ("Service", "provides", rule_service_connectivity),
        ("Action", "provides", rule_action_connectivity),
    )
    for index, (kind, rel, runner) in enumerate(cases):
        model = UAM()
        graph = model.graph
        source_id = f"node:demo/source{index}"
        target_id = f"unresolved:{kind.lower()}:source{index}"
        graph.add_node(source_id, kind="Node", name=f"source{index}")
        graph.add_node(
            target_id,
            kind=kind,
            name=DYNAMIC_SENTINEL,
            resolution="unresolved",
        )
        graph.add_edge(source_id, target_id, rel=rel)

        assert runner(model, {}) == []
        assert len(runner(model, {"include_unresolved": True})) == 1


def test_multiple_launch_instances_emit_all_effective_topics(tmp_path: Path) -> None:
    package = _package("driver_pkg", tmp_path)
    camera = NodeDefinition(
        name="Camera",
        source_symbol="Camera",
        declared_ros_name="camera",
        package="driver_pkg",
        language="python",
        publishers=[CommunicationEndpoint(name="image")],
    )
    model = UAM()
    model._launch_remaps["driver_pkg"] = [
        LaunchNode(
            executable="camera",
            package="driver_pkg",
            name="front_camera",
            namespace="front",
            source_file="robot.launch.xml",
        ),
        LaunchNode(
            executable="camera",
            package="driver_pkg",
            name="rear_camera",
            namespace="rear",
            source_file="robot.launch.xml",
        ),
    ]
    model._build_graph([package], [camera], [], {"driver_pkg": package})

    assert _topic_id("/front/image") in model.graph
    assert _topic_id("/rear/image") in model.graph
    node_id = _node_id("driver_pkg", "Camera")
    assert len(model.graph.nodes[node_id]["deployments"]) == 2
    deployment_ids = {
        target
        for _, target, data in model.graph.out_edges(node_id, data=True)
        if data.get("rel") == "deploys_as"
    }
    assert len(deployment_ids) == 2
    assert {model.graph.nodes[deployment_id]["name"] for deployment_id in deployment_ids} == {
        "/front/front_camera",
        "/rear/rear_camera",
    }
    published_topics = {
        model.graph.nodes[target]["name"]
        for deployment_id in deployment_ids
        for _, target, data in model.graph.out_edges(deployment_id, data=True)
        if data.get("rel") == "publishes"
    }
    assert published_topics == {"/front/image", "/rear/image"}


def test_nodes_table_uses_ros_name_and_keeps_source_symbol() -> None:
    node = NodeDefinition(
        name="TalkerNode",
        source_symbol="TalkerNode",
        declared_ros_name="talker",
        package="demo",
        language="python",
    )
    rendered = _render_table_str([node])
    assert "ROS Name" in rendered
    assert "Source Symbol" in rendered
    assert "talker" in rendered
    assert "TalkerNode" in rendered
    assert "Node Name" not in rendered


def test_cpp_declared_name_and_unqualified_types_are_conservative() -> None:
    source = (
        b'class Camera : public rclcpp::Node {'
        b'public: Camera() : rclcpp::Node("camera") {} };'
    )
    fake_node = SimpleNamespace(start_byte=0, end_byte=len(source))
    assert _extract_declared_ros_name(fake_node, source) == "camera"
    assert _cpp_type_to_ros("std_msgs::msg::String") == "std_msgs/String"
    assert _cpp_type_to_ros("MessageType") == "unknown"
    assert _cpp_type_to_ros("Msg") == "unknown"
    assert _cpp_type_to_ros("ConcreteAlias") == "unknown"


def test_visualizer_does_not_make_shared_topic_a_child_of_publisher() -> None:
    model = UAM()
    graph = model.graph
    graph.add_node("pkg:demo", kind="Package", name="demo")
    graph.add_node("node:demo/a", kind="Node", name="a", package="demo")
    graph.add_node("node:demo/b", kind="Node", name="b", package="demo")
    graph.add_node("topic:/state", kind="Topic", name="/state")
    graph.add_edge("node:demo/a", "topic:/state", rel="publishes")
    graph.add_edge("node:demo/b", "topic:/state", rel="subscribes")

    elements = _cy_elements(model, "comms")
    by_id = {entry["data"]["id"]: entry["data"] for entry in elements["nodes"]}
    assert by_id["node:demo/a"]["parent"] == "pkg:demo"
    assert "parent" not in by_id["topic:/state"]


def test_package_walker_prunes_generated_and_user_ignored_trees(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "keep.py").write_text("x = 1\n")
    (tmp_path / "build").mkdir()
    (tmp_path / "build" / "generated.py").write_text("x = 2\n")
    (tmp_path / "vendor").mkdir()
    (tmp_path / "vendor" / "huge.py").write_text("x = 3\n")
    (tmp_path / ".ros2inspectorignore").write_text("vendor/\n")

    found = {
        path.relative_to(tmp_path).as_posix()
        for path in iter_package_files(tmp_path, suffixes={".py"})
    }
    assert found == {"src/keep.py"}


def test_cache_fingerprint_tracks_cc_and_cxx_sources(tmp_path: Path) -> None:
    package_xml = tmp_path / "package.xml"
    source_cc = tmp_path / "src" / "node.cc"
    source_cxx = tmp_path / "src" / "node.cxx"
    source_cc.parent.mkdir()
    package_xml.write_text("<package/>")
    source_cc.write_text("int a = 1;\n")
    source_cxx.write_text("int b = 1;\n")

    original = _pkg_fingerprint(tmp_path)
    source_cc.write_text("int a = 2;\n")
    after_cc = _pkg_fingerprint(tmp_path)
    source_cxx.write_text("int b = 2;\n")
    after_cxx = _pkg_fingerprint(tmp_path)

    assert original != after_cc
    assert after_cc != after_cxx
