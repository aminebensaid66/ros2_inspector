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
