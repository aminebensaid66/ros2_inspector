from pathlib import Path

from ros2inspector.static.cpp_parser import parse_cpp_nodes

FIXTURE_DIR = Path(__file__).parent.parent / "fixtures" / "cpp"


def test_detects_class_name():
    nodes = parse_cpp_nodes(FIXTURE_DIR, "test_pkg")
    assert len(nodes) == 1
    assert nodes[0].name == "MinimalPublisher"
    assert nodes[0].language == "cpp"
    assert nodes[0].package == "test_pkg"


def test_detects_publisher():
    nodes = parse_cpp_nodes(FIXTURE_DIR, "test_pkg")
    assert any(ep.name == "/chatter" for ep in nodes[0].publishers)


def test_detects_subscription():
    nodes = parse_cpp_nodes(FIXTURE_DIR, "test_pkg")
    assert any(ep.name == "/cmd_vel" for ep in nodes[0].subscriptions)


def test_detects_service():
    nodes = parse_cpp_nodes(FIXTURE_DIR, "test_pkg")
    assert any(ep.name == "/set_mode" for ep in nodes[0].services)


def test_no_dynamic_names():
    nodes = parse_cpp_nodes(FIXTURE_DIR, "test_pkg")
    assert not nodes[0].has_dynamic_names


def test_dependent_template_type_is_unresolved(tmp_path: Path):
    source = tmp_path / "generic.cpp"
    source.write_text(
        "#include <rclcpp/rclcpp.hpp>\n"
        "template<typename T>\n"
        "class GenericNode : public rclcpp::Node {\n"
        "public:\n"
        "  GenericNode() : Node(\"generic\") {\n"
        "    pub_ = this->create_publisher<T>(\"/data\", 10);\n"
        "  }\n"
        "private:\n"
        "  rclcpp::Publisher<T>::SharedPtr pub_;\n"
        "};\n"
    )

    node = parse_cpp_nodes(tmp_path, "generic_pkg")[0]

    assert node.publishers[0].msg_type == "unknown"
    assert node.publishers[0].type_source == "unknown"
    assert node.publishers[0].confidence == "low"
