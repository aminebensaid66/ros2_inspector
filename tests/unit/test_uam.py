from pathlib import Path

import networkx as nx
import orjson
import pytest

from ros2inspector.model.uam import UAM, _iface_id, _node_id, _pkg_id, _svc_id, _topic_id

WORKSPACE_A = Path(__file__).parent.parent / "fixtures" / "workspaces" / "workspace_a"


@pytest.fixture(scope="module")
def uam() -> UAM:
    return UAM.build(WORKSPACE_A, use_cache=False)


def _has_rel(graph: nx.MultiDiGraph, source: str, target: str, rel: str) -> bool:
    return any(
        data.get("rel") == rel for data in graph.get_edge_data(source, target, default={}).values()
    )


def test_build_and_summary(uam: UAM) -> None:
    assert {package.name for package in uam.packages()} == {"pkg_a", "pkg_b", "pkg_c"}
    assert {node.name for node in uam.nodes()} >= {"TalkerNode", "ListenerNode"}
    assert uam.summary()["packages"] == 3
    assert uam.summary()["nodes"] >= 2


def test_chatter_has_publisher_and_subscriber(uam: UAM) -> None:
    chatter = next(topic for topic in uam.topics() if topic["name"] == "/chatter")
    assert "TalkerNode" in chatter["publishers"]
    assert "ListenerNode" in chatter["subscribers"]
    graph = uam.graph
    topic = _topic_id("/chatter")
    assert _has_rel(graph, _node_id("pkg_a", "TalkerNode"), topic, "publishes")
    assert _has_rel(graph, _node_id("pkg_b", "ListenerNode"), topic, "subscribes")


def test_service_and_dependency_edges(uam: UAM) -> None:
    graph = uam.graph
    assert _has_rel(
        graph,
        _node_id("pkg_b", "ListenerNode"),
        _svc_id("/ping"),
        "calls",
    )
    assert _has_rel(graph, _pkg_id("pkg_b"), _pkg_id("pkg_a"), "depends_on")
    assert _has_rel(
        graph,
        _node_id("pkg_a", "TalkerNode"),
        _pkg_id("pkg_a"),
        "defined_in",
    )


def test_explicit_topic_type_is_not_replaced_by_fuzzy_interface(uam: UAM) -> None:
    graph = uam.graph
    topic = graph.nodes[_topic_id("/chatter")]
    assert topic["msg_type"] == "std_msgs/String"
    assert topic["type_source"] == "explicit"
    assert topic["confidence"] == "high"
    assert _iface_id("pkg_a", "Chatter") in graph
    assert not graph.has_edge(_node_id("pkg_a", "TalkerNode"), _iface_id("pkg_a", "Chatter"))


def test_graph_is_lossless_multidigraph(tmp_path: Path) -> None:
    package = tmp_path / "src" / "dual_pkg"
    source_dir = package / "dual_pkg"
    source_dir.mkdir(parents=True)
    (package / "package.xml").write_text(
        "<package format='3'><name>dual_pkg</name><version>0.1.0</version>"
        "<description>x</description><maintainer email='a@b.c'>A</maintainer>"
        "<license>Apache-2.0</license><exec_depend>rclpy</exec_depend></package>"
    )
    (source_dir / "dual.py").write_text(
        "from rclpy.node import Node\n"
        "from std_msgs.msg import String\n"
        "class DualNode(Node):\n"
        "    def __init__(self):\n"
        "        super().__init__('dual')\n"
        "        self.pub = self.create_publisher(String, '/same', 10)\n"
        "        self.sub = self.create_subscription(String, '/same', self.cb, 10)\n"
        "    def cb(self, msg):\n"
        "        pass\n"
    )
    model = UAM.build(tmp_path, use_cache=False)
    graph = model.graph
    node = _node_id("dual_pkg", "DualNode")
    topic = _topic_id("/same")
    relations = {data["rel"] for data in graph.get_edge_data(node, topic, default={}).values()}
    assert relations == {"publishes", "subscribes"}


def test_dynamic_endpoints_do_not_share_a_communication_node(tmp_path: Path) -> None:
    package = tmp_path / "src" / "dynamic_pkg"
    source_dir = package / "dynamic_pkg"
    source_dir.mkdir(parents=True)
    (package / "package.xml").write_text(
        "<package format='3'><name>dynamic_pkg</name><version>0.1.0</version>"
        "<description>x</description><maintainer email='a@b.c'>A</maintainer>"
        "<license>Apache-2.0</license><exec_depend>rclpy</exec_depend></package>"
    )
    (source_dir / "dynamic.py").write_text(
        "from rclpy.node import Node\n"
        "class Publisher(Node):\n"
        "    def __init__(self):\n"
        "        super().__init__('publisher')\n"
        "        self.create_publisher(object, topic_name, 10)\n"
        "class Subscriber(Node):\n"
        "    def __init__(self):\n"
        "        super().__init__('subscriber')\n"
        "        self.create_subscription(object, topic_name, self.cb, 10)\n"
        "    def cb(self, msg):\n"
        "        pass\n"
    )

    graph = UAM.build(tmp_path, use_cache=False).graph
    dynamic_topics = [
        node_id
        for node_id, attrs in graph.nodes(data=True)
        if attrs.get("kind") == "Topic" and attrs.get("resolution") == "unresolved"
    ]

    assert len(dynamic_topics) == 2
    for topic_id in dynamic_topics:
        relations = {
            data["rel"]
            for _, _, data in graph.in_edges(topic_id, data=True)
        }
        assert len(relations) == 1


def test_launch_records_match_nodes_from_another_package(tmp_path: Path) -> None:
    driver = tmp_path / "src" / "driver_pkg"
    (driver / "driver_pkg").mkdir(parents=True)
    (driver / "package.xml").write_text(
        "<package format='3'><name>driver_pkg</name><version>0.1.0</version>"
        "<description>x</description><maintainer email='a@b.c'>A</maintainer>"
        "<license>Apache-2.0</license><exec_depend>rclpy</exec_depend></package>"
    )
    (driver / "driver_pkg" / "driver.py").write_text(
        "from rclpy.node import Node\n"
        "class Camera(Node):\n"
        "    def __init__(self):\n"
        "        super().__init__('camera')\n"
        "        self.create_publisher(object, 'image', 10)\n"
    )

    bringup = tmp_path / "src" / "robot_bringup"
    (bringup / "launch").mkdir(parents=True)
    (bringup / "package.xml").write_text(
        "<package format='3'><name>robot_bringup</name><version>0.1.0</version>"
        "<description>x</description><maintainer email='a@b.c'>A</maintainer>"
        "<license>Apache-2.0</license><exec_depend>driver_pkg</exec_depend></package>"
    )
    launch = bringup / "launch" / "robot.launch.xml"
    launch.write_text(
        "<launch><node pkg='driver_pkg' exec='camera' name='front_camera' ns='robot' "
        "/><node pkg='driver_pkg' exec='camera' name='rear_camera' ns='robot2' /></launch>"
    )

    model = UAM.build(tmp_path, use_cache=False)
    camera = next(node for node in model.graph.nodes if node == _node_id("driver_pkg", "Camera"))
    attrs = model.graph.nodes[camera]
    assert attrs["deployment_name"] == "front_camera"
    assert attrs["namespace"] == "robot"
    assert attrs["launch_file"] == str(launch)


def test_to_dict_is_json_serializable_and_keeps_edge_keys(uam: UAM) -> None:
    data = uam.to_dict()
    assert all("key" in edge for edge in data["graph"]["edges"])
    assert orjson.dumps(data)
