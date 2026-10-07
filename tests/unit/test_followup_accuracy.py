from pathlib import Path

from typer.testing import CliRunner

from ros2inspector.cli.app import app
from ros2inspector.model.uam import UAM
from ros2inspector.policy.rules import rule_qos_compatibility
from ros2inspector.static.cpp_parser import parse_cpp_nodes
from ros2inspector.static.factory_patterns import NAV2_PATTERNS
from ros2inspector.static.launch_analyzer import analyze_launch_file


def test_cross_file_parameter_and_factory_alias(tmp_path: Path) -> None:
    (tmp_path / "node.hpp").write_text("""
namespace demo {
class Driver : public rclcpp::Node {
using Action = nav2_msgs::action::FollowPath;
using ActionServer = nav2_util::SimpleActionServer<Action>;
};
}
""")
    (tmp_path / "node.cpp").write_text("""
namespace demo {
Driver::Driver() { declare_parameter("topic_name", "map"); }
void Driver::on_configure() {
  std::string topic;
  get_parameter("topic_name", topic);
  create_publisher<nav_msgs::msg::OccupancyGrid>(topic,
    rclcpp::QoS(rclcpp::KeepLast(1)).transient_local().reliable());
  auto server = std::make_shared<ActionServer>(this, "follow_path");
}
}
""")
    node = parse_cpp_nodes(tmp_path, "demo", factory_patterns=NAV2_PATTERNS)[0]
    assert node.publishers[0].name == "map"
    assert node.publishers[0].name_source == "parameter_default"
    assert node.publishers[0].qos is not None
    assert node.publishers[0].qos.durability == "transient_local"
    assert node.publishers[0].qos.depth == 1
    assert node.action_servers[0].msg_type == "nav2_msgs/FollowPath"


def test_composable_deployments_enable_qos_audit(tmp_path: Path) -> None:
    pkg = tmp_path / "src" / "demo"
    (pkg / "launch").mkdir(parents=True)
    (pkg / "package.xml").write_text(
        '<package format="3"><name>demo</name><version>0.1</version></package>'
    )
    (pkg / "nodes.cpp").write_text("""
namespace demo {
class Pub : public rclcpp::Node {
void setup() { create_publisher<std_msgs::msg::String>("topic", rclcpp::SensorDataQoS()); }
};
class Sub : public rclcpp::Node {
void setup() { create_subscription<std_msgs::msg::String>("topic", 10, callback); }
};
}
""")
    launch = pkg / "launch" / "demo.launch.py"
    launch.write_text("""
LoadComposableNodes(target_container='container', composable_node_descriptions=[
  ComposableNode(package='demo', plugin='demo::Pub', name='pub'),
  ComposableNode(package='demo', plugin='demo::Sub', name='sub')])
""")
    assert len(analyze_launch_file(launch).nodes) == 2
    findings = rule_qos_compatibility(UAM.build(tmp_path, use_cache=False), {})
    assert len(findings) == 1
    assert "reliability" in findings[0].message


def test_composable_condition_is_preserved(tmp_path: Path) -> None:
    launch = tmp_path / "demo.launch.py"
    launch.write_text("""
nodes = [ComposableNode(package='demo', plugin='demo::Driver', name='driver')]
LoadComposableNodes(condition=IfCondition(use_composition),
                    composable_node_descriptions=nodes)
""")
    node = analyze_launch_file(launch).nodes[0]
    assert node.plugin == "demo::Driver"
    assert node.presence == "conditional"


def test_invalid_preset_rejected_by_scan(tmp_path: Path) -> None:
    result = CliRunner().invoke(app, ["--preset", "foo", "scan", str(tmp_path)])
    assert result.exit_code != 0
    assert "unknown factory preset" in result.output


def test_test_package_nodes_are_opt_in(tmp_path: Path) -> None:
    pkg = tmp_path / "demo_system_tests"
    pkg.mkdir()
    (pkg / "package.xml").write_text(
        '<package format="3"><name>demo_system_tests</name><version>0.1</version></package>'
    )
    (pkg / "fixture.py").write_text("from rclpy.node import Node\nclass Fixture(Node): pass\n")
    assert UAM.build(tmp_path, use_cache=False).nodes() == []
    assert len(UAM.build(tmp_path, use_cache=False, include_tests=True).nodes()) == 1


def test_parameter_defaults_do_not_leak_between_classes(tmp_path: Path) -> None:
    (tmp_path / "nodes.cpp").write_text("""
class A : public rclcpp::Node {};
class B : public rclcpp::Node {};
A::A() { declare_parameter("topic", "a_topic"); }
void A::setup() {
  get_parameter("topic", topic_);
  create_publisher<std_msgs::msg::String>(topic_, 10);
}
void B::setup() {
  get_parameter("topic", topic_);
  create_publisher<std_msgs::msg::String>(topic_, 10);
}
""")
    nodes = {node.name: node for node in parse_cpp_nodes(tmp_path, "demo")}
    assert nodes["A"].publishers[0].name == "a_topic"
    assert nodes["B"].publishers[0].name == "<dynamic>"


def test_deadline_and_liveliness_are_parsed(tmp_path: Path) -> None:
    (tmp_path / "node.cpp").write_text("""
class Driver : public rclcpp::Node {
void setup() {
  create_publisher<std_msgs::msg::String>("topic",
    rclcpp::QoS(10).deadline(rclcpp::Duration(2, 500000000))
      .liveliness_manual_by_topic().liveliness_lease_duration(rclcpp::Duration(3, 0)));
}
};
""")
    qos = parse_cpp_nodes(tmp_path, "demo")[0].publishers[0].qos
    assert qos is not None
    assert qos.deadline == 2.5
    assert qos.liveliness == "manual_by_topic"
    assert qos.liveliness_lease_duration == 3
