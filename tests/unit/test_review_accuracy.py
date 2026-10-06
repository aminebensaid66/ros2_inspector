from pathlib import Path

from typer.testing import CliRunner

from ros2inspector.cli.app import app
from ros2inspector.model.uam import UAM
from ros2inspector.policy.engine import load_policy, run_policy
from ros2inspector.policy.rules import rule_qos_compatibility
from ros2inspector.static.cpp_parser import parse_cpp_nodes
from ros2inspector.static.python_parser import parse_python_nodes


def test_test_sources_are_opt_in(tmp_path: Path) -> None:
    pkg = tmp_path / "src" / "demo"
    (pkg / "test").mkdir(parents=True)
    (pkg / "package.xml").write_text(
        '<package format="3"><name>demo</name><version>0.1.0</version>'
        '<description>Demo package</description><maintainer email="a@b.com">A</maintainer>'
        "<license>MIT</license></package>"
    )
    (pkg / "test" / "fixture.py").write_text(
        "from rclpy.node import Node\nclass Fixture(Node): pass\n"
    )
    assert UAM.build(tmp_path, use_cache=False).nodes() == []
    included = UAM.build(tmp_path, use_cache=False, include_tests=True).nodes()
    assert [node.name for node in included] == ["Fixture"]


def test_parameter_default_and_python_qos(tmp_path: Path) -> None:
    (tmp_path / "node.py").write_text(
        "from rclpy.node import Node\n"
        "from std_msgs.msg import String\n"
        "from rclpy.qos import QoSProfile, ReliabilityPolicy\n"
        "class Talker(Node):\n"
        "    def __init__(self):\n"
        "        super().__init__('talker')\n"
        "        self.declare_parameter('topic', 'chatter')\n"
        "        self.topic = self.get_parameter('topic').value\n"
        "        self.create_publisher(String, self.topic, QoSProfile(\n"
        "            depth=4, reliability=ReliabilityPolicy.BEST_EFFORT))\n"
    )
    node = parse_python_nodes(tmp_path, "demo")[0]
    assert node.publishers[0].name == "chatter"
    assert node.publishers[0].name_source == "parameter_default"
    assert node.publishers[0].confidence == "medium"
    assert node.publishers[0].qos is not None
    assert node.publishers[0].qos.depth == 4
    assert node.publishers[0].qos.reliability == "best_effort"


def test_cpp_factory_pattern(tmp_path: Path) -> None:
    (tmp_path / "node.cpp").write_text(
        "class Driver : public rclcpp::Node {\n"
        "  void setup() { auto pub = std::make_shared<nav2_util::TwistPublisher>(\n"
        '      node, "cmd_vel"); }\n'
        "  void action() { auto server = std::make_shared<\n"
        "      nav2_util::SimpleActionServer<nav2_msgs::action::FollowPath>>(\n"
        '      node, "follow_path"); }\n'
        "};\n"
    )
    patterns = [
        {"call": "nav2_util::TwistPublisher", "kind": "publisher", "name_arg": 1},
        {"call": "nav2_util::SimpleActionServer", "kind": "action_server", "name_arg": 1},
    ]
    nodes = parse_cpp_nodes(tmp_path, "demo", factory_patterns=patterns)
    assert nodes[0].publishers[0].name == "cmd_vel"
    assert nodes[0].action_servers[0].msg_type == "nav2_msgs/FollowPath"


def test_qos_rule_reports_only_proven_mismatch(tmp_path: Path) -> None:
    pkg = tmp_path / "src" / "demo"
    pkg.mkdir(parents=True)
    (pkg / "package.xml").write_text(
        '<package format="3"><name>demo</name><version>0.1</version></package>'
    )
    (pkg / "nodes.py").write_text(
        "from rclpy.node import Node\n"
        "from std_msgs.msg import String\n"
        "from rclpy.qos import QoSProfile, ReliabilityPolicy\n"
        "class Pub(Node):\n"
        "    def go(self):\n"
        "        self.create_publisher(String, 'topic', QoSProfile(\n"
        "            reliability=ReliabilityPolicy.BEST_EFFORT))\n"
        "class Sub(Node):\n"
        "    def go(self):\n"
        "        self.create_subscription(String, 'topic', lambda x: None, QoSProfile(\n"
        "            reliability=ReliabilityPolicy.RELIABLE))\n"
    )
    findings = rule_qos_compatibility(UAM.build(tmp_path, use_cache=False), {})
    assert len(findings) == 1
    assert findings[0].file_path is not None and findings[0].line is not None


def test_cpp_parameter_default_resolves_endpoint_name(tmp_path: Path) -> None:
    (tmp_path / "node.cpp").write_text(
        "class Driver : public rclcpp::Node {\n"
        "  void setup() {\n"
        '    declare_parameter<std::string>("topic", "cmd_vel");\n'
        '    std::string topic = get_parameter("topic").as_string();\n'
        "    create_publisher<geometry_msgs::msg::Twist>(topic, rclcpp::QoS(10));\n"
        "  }\n};\n"
    )
    node = parse_cpp_nodes(tmp_path, "demo")[0]
    assert node.publishers[0].name == "cmd_vel"
    assert node.publishers[0].name_source == "parameter_default"
    assert node.publishers[0].qos is not None and node.publishers[0].qos.depth == 10


def test_quiet_suppresses_workspace_warning(tmp_path: Path) -> None:
    result = CliRunner().invoke(app, ["--quiet", "scan", str(tmp_path), "--format", "json"])
    assert "no ROS 2 workspace root" not in result.output


def test_policy_uses_yaml_line_and_node_source(tmp_path: Path) -> None:
    pkg = tmp_path / "src" / "demo"
    pkg.mkdir(parents=True)
    (pkg / "package.xml").write_text(
        '<package format="3"><name>demo</name><version>0.1</version></package>'
    )
    (pkg / "isolated.py").write_text("from rclpy.node import Node\nclass Isolated(Node): pass\n")
    policy = tmp_path / "policy.yaml"
    policy.write_text("version: 1\nrules:\n  - type: node_isolation\n")
    findings = run_policy(UAM.build(tmp_path, use_cache=False), load_policy(policy))
    assert len(findings) == 1
    assert findings[0].policy_line == 3
    assert findings[0].file_path == str(pkg / "isolated.py")
    assert findings[0].line == 2
