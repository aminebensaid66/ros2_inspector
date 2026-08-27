from pathlib import Path

from ros2inspector.static.python_parser import parse_python_nodes

FIXTURE_PKG_A = (
    Path(__file__).parent.parent / "fixtures" / "workspaces" / "workspace_a" / "src" / "pkg_a"
)


def test_detects_talker_node():
    nodes = parse_python_nodes(FIXTURE_PKG_A, "pkg_a")
    assert len(nodes) == 1
    node = nodes[0]
    assert node.name == "TalkerNode"
    assert node.package == "pkg_a"
    assert node.language == "python"
    assert any(ep.name == "/chatter" for ep in node.publishers)
    assert node.subscriptions == []
    assert not node.has_dynamic_names


def test_preserves_source_and_ros_node_identity(tmp_path: Path):
    source = tmp_path / "nodes.py"
    source.write_text(
        "from rclpy.node import Node\n"
        "class TalkerNode(Node):\n"
        "    def __init__(self):\n"
        "        super().__init__('talker')\n"
    )

    node = parse_python_nodes(tmp_path, "demo")[0]

    assert node.name == "TalkerNode"
    assert node.source_symbol == "TalkerNode"
    assert node.declared_ros_name == "talker"
