from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from ros2inspector.cli.app import app
from ros2inspector.model.schemas import (
    CommunicationEndpoint,
    NodeDefinition,
    PackageMetadata,
)
from ros2inspector.model.uam import UAM
from ros2inspector.policy.rules import (
    rule_action_connectivity,
    rule_node_isolation,
    rule_service_connectivity,
    rule_topic_connectivity,
)
from ros2inspector.static.cpp_parser import parse_cpp_nodes
from ros2inspector.static.launch_analyzer import LaunchNode, analyze_launch_file
from ros2inspector.viz.renderer import _cy_elements

runner = CliRunner()
WORKSPACE_A = Path(__file__).parent.parent / "fixtures" / "workspaces" / "workspace_a"


def test_empty_package_filter_is_valid_json_and_yaml() -> None:
    result = runner.invoke(
        app, ["--quiet", "packages", "-C", str(WORKSPACE_A), "--filter", "cpp", "--format", "json"]
    )
    assert result.exit_code == 0
    assert json.loads(result.stdout) == []
    result = runner.invoke(
        app, ["--quiet", "packages", "-C", str(WORKSPACE_A), "--filter", "cpp", "--format", "yaml"]
    )
    assert result.exit_code == 0
    assert yaml.safe_load(result.stdout) == []


def test_invalid_output_parent_has_clean_exit() -> None:
    out = WORKSPACE_A / "does-not-exist" / "nodes.json"
    result = runner.invoke(
        app, ["--quiet", "nodes", "-C", str(WORKSPACE_A), "--format", "json", "-o", str(out)]
    )
    assert result.exit_code == 2
    assert "Output error" in result.stderr
    assert "Traceback" not in result.output


def test_cpp_header_source_methods_are_associated(tmp_path: Path) -> None:
    (tmp_path / "driver.hpp").write_text(
        "namespace alpha { class Driver : public rclcpp::Node { public: "
        "Driver(); void setup(); }; }\n"
    )
    (tmp_path / "driver.cpp").write_text(
        '#include "driver.hpp"\nnamespace alpha { Driver::Driver() : rclcpp::Node("driver") { '
        'srv_=this->create_service<std_srvs::srv::Trigger>("/reset", cb); } '
        "void Driver::setup() { "
        "pub_=this->create_publisher<std_msgs::msg::String>(topic_name, 10); } }\n"
    )
    nodes = parse_cpp_nodes(tmp_path, "demo")
    driver = next(n for n in nodes if n.source_symbol == "alpha::Driver")
    assert [ep.name for ep in driver.services] == ["/reset"]
    assert driver.publishers and driver.publishers[0].name == "<dynamic>"
    assert driver.publishers[0].file_path.endswith("driver.cpp")


def test_cpp_duplicate_namespaces_do_not_cross_attach(tmp_path: Path) -> None:
    (tmp_path / "nodes.hpp").write_text(
        "namespace a { class Driver : public rclcpp::Node { public: void setup(); }; }\n"
        "namespace b { class Driver : public rclcpp::Node { public: void setup(); }; }\n"
    )
    (tmp_path / "nodes.cpp").write_text(
        'void a::Driver::setup(){ this->create_publisher<std_msgs::msg::String>("/a",10); }\n'
        'void b::Driver::setup(){ this->create_publisher<std_msgs::msg::String>("/b",10); }\n'
    )
    by_symbol = {n.source_symbol: n for n in parse_cpp_nodes(tmp_path, "demo")}
    assert [e.name for e in by_symbol["a::Driver"].publishers] == ["/a"]
    assert [e.name for e in by_symbol["b::Driver"].publishers] == ["/b"]


def test_conditional_deployment_does_not_make_connectivity_definitive(tmp_path: Path) -> None:
    pkg = PackageMetadata(name="demo", path=str(tmp_path))
    pub = NodeDefinition(
        name="Talker",
        declared_ros_name="talker",
        package="demo",
        language="python",
        publishers=[CommunicationEndpoint(name="/maybe")],
    )
    model = UAM()
    model._nodes = [pub]
    model._launch_remaps["demo"] = [
        LaunchNode(
            executable="talker",
            package="demo",
            name="talker",
            conditions=["IfCondition(flag)"],
            presence="conditional",
        )
    ]
    model._build_graph([pkg], [pub], [], {"demo": pkg})
    assert rule_topic_connectivity(model, {}) == []
    assert rule_node_isolation(model, {}) == []
    edge = next(d for _, _, d in model.graph.edges(data=True) if d.get("rel") == "publishes")
    assert edge["resolution"] == "conditional"


@pytest.mark.parametrize(
    ("kind", "present", "possible", "rule"),
    [
        ("Topic", "publishes", "subscribes", rule_topic_connectivity),
        ("Service", "provides", "calls", rule_service_connectivity),
        ("Action", "provides", "calls", rule_action_connectivity),
    ],
)
def test_possible_counterpart_prevents_definitive_connectivity_warning(
    kind: str, present: str, possible: str, rule: object
) -> None:
    model = UAM()
    graph = model.graph
    graph.add_node("source", kind="Node", name="source")
    graph.add_node("maybe", kind="Node", name="maybe")
    graph.add_node("entity", kind=kind, name="/example")
    graph.add_edge("source", "entity", rel=present, resolution="known")
    graph.add_edge("maybe", "entity", rel=possible, resolution="conditional")
    assert rule(model, {}) == []  # type: ignore[operator]


def test_xml_group_condition_is_inherited(tmp_path: Path) -> None:
    launch = tmp_path / "robot.launch.xml"
    launch.write_text(
        "<launch><group if='$(var enabled)'><node pkg='demo' exec='driver'/></group></launch>"
    )
    node = analyze_launch_file(launch).nodes[0]
    assert node.presence == "conditional"
    assert node.conditions == ["if:$(var enabled)"]


def test_python_group_condition_on_assigned_node(tmp_path: Path) -> None:
    launch = tmp_path / "robot.launch.py"
    launch.write_text(
        "from launch.actions import GroupAction\n"
        "from launch_ros.actions import Node\n"
        "robot = Node(package='demo', executable='driver')\n"
        "group = GroupAction(actions=[robot], condition=IfCondition(flag))\n"
    )
    node = analyze_launch_file(launch).nodes[0]
    assert node.presence == "conditional"
    assert node.conditions == ["IfCondition(flag)"]


def test_yaml_group_condition_is_inherited(tmp_path: Path) -> None:
    launch = tmp_path / "robot.launch.yaml"
    launch.write_text(
        "launch:\n"
        "  - group:\n"
        "      if: enabled\n"
        "      children:\n"
        "        - node: {pkg: demo, exec: driver}\n"
    )
    node = analyze_launch_file(launch).nodes[0]
    assert node.presence == "conditional"
    assert node.conditions == ["if:enabled"]


def test_direct_cpp_node_only_attributes_calls_through_its_variable(tmp_path: Path) -> None:
    (tmp_path / "main.cpp").write_text(
        'void run() { auto node = rclcpp::Node::make_shared("driver"); '
        'node->create_publisher<std_msgs::msg::String>("/owned", 10); '
        'other->create_publisher<std_msgs::msg::String>("/other", 10); }\n'
    )
    node = next(n for n in parse_cpp_nodes(tmp_path, "demo") if n.declared_ros_name == "driver")
    assert [ep.name for ep in node.publishers] == ["/owned"]
    assert node.analysis_incomplete


def test_html_payload_keeps_unique_packages_and_uses_interface() -> None:
    model = UAM()
    g = model.graph
    g.add_node("pkg:demo", kind="Package", name="demo")
    g.add_node("node:demo/n", kind="Node", name="n", package="demo")
    g.add_node("iface:demo/String", kind="Interface", name="String", package="demo")
    g.add_edge("node:demo/n", "pkg:demo", rel="defined_in")
    g.add_edge("node:demo/n", "iface:demo/String", rel="uses_interface")
    data = _cy_elements(model, "full")
    ids = [n["data"]["id"] for n in data["nodes"]]
    assert len(ids) == len(set(ids))
    assert sum(i == "pkg:demo" for i in ids) == 1
    assert any(e["data"]["rel"] == "uses_interface" for e in data["edges"])
    assert all(e["data"]["source"] in ids and e["data"]["target"] in ids for e in data["edges"])
