from __future__ import annotations

from pathlib import Path

from ros2inspector.model.schemas import (
    CommunicationEndpoint,
    DepType,
    InterfaceDefinition,
    NodeDefinition,
    PackageMetadata,
    PackageType,
)
from ros2inspector.model.uam import UAM, _iface_id, _topic_id
from ros2inspector.policy.rules import rule_license, rule_node_isolation
from ros2inspector.static.launch_analyzer import LaunchNode, analyze_launch_file
from ros2inspector.static.package_xml import parse_package_xml
from ros2inspector.static.python_entrypoints import find_python_entrypoints
from ros2inspector.static.python_parser import parse_python_nodes


def _package(name: str, path: Path) -> PackageMetadata:
    return PackageMetadata(
        name=name,
        version="0.1.0",
        package_type=PackageType.AMENT_PYTHON,
        path=str(path),
    )


def _manifest(package: Path, name: str) -> None:
    (package / "package.xml").write_text(
        "<package format='3'>"
        f"<name>{name}</name><version>0.1.0</version>"
        "<description>test package</description>"
        "<maintainer email='dev@example.com'>Dev</maintainer>"
        "<license>Apache-2.0</license><exec_depend>rclpy</exec_depend>"
        "</package>",
        encoding="utf-8",
    )


def test_endpoint_name_never_infers_interface_type(tmp_path: Path) -> None:
    package = _package("demo", tmp_path)
    node = NodeDefinition(
        name="StatusPublisher",
        package="demo",
        language="python",
        publishers=[CommunicationEndpoint(name="/Status", msg_type="unknown")],
    )
    interface = InterfaceDefinition(
        name="Status",
        package="demo",
        kind="msg",
        fields=["bool ok"],
        file_path=str(tmp_path / "msg" / "Status.msg"),
    )
    model = UAM()
    model._build_graph([package], [node], [interface], {"demo": package})
    model._resolve_interface_types([interface])

    topic = model.graph.nodes[_topic_id("/Status")]
    assert topic["msg_type"] == "unknown"
    assert topic["type_source"] == "unknown"
    source_id = model.node_graph_id(node)
    assert not model.graph.has_edge(source_id, _iface_id("demo", "Status"))


def test_duplicate_source_symbols_get_distinct_graph_nodes(tmp_path: Path) -> None:
    package = _package("demo", tmp_path)
    front = NodeDefinition(
        name="Camera",
        source_symbol="Camera",
        package="demo",
        language="python",
        file_path=str(tmp_path / "front.py"),
        line=3,
        publishers=[CommunicationEndpoint(name="/front/image")],
    )
    rear = NodeDefinition(
        name="Camera",
        source_symbol="Camera",
        package="demo",
        language="python",
        file_path=str(tmp_path / "rear.py"),
        line=7,
        publishers=[CommunicationEndpoint(name="/rear/image")],
    )
    model = UAM()
    model._nodes = [front, rear]
    model._build_graph([package], [front, rear], [], {"demo": package})

    front_id = model.node_graph_id(front)
    rear_id = model.node_graph_id(rear)
    assert front_id != rear_id
    assert front_id in model.graph and rear_id in model.graph
    assert model.summary()["nodes"] == 2


def test_launch_instances_are_first_class_deployments(tmp_path: Path) -> None:
    package = _package("driver_pkg", tmp_path)
    camera = NodeDefinition(
        name="Camera",
        source_symbol="Camera",
        declared_ros_name="camera",
        package="driver_pkg",
        language="python",
        file_path=str(tmp_path / "camera.py"),
        publishers=[CommunicationEndpoint(name="image")],
    )
    model = UAM()
    model._nodes = [camera]
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

    source_id = model.node_graph_id(camera)
    deployments = [
        target
        for _, target, data in model.graph.out_edges(source_id, data=True)
        if data.get("rel") == "deploys_as"
    ]
    assert len(deployments) == 2
    assert {model.graph.nodes[item]["kind"] for item in deployments} == {"Deployment"}
    assert {model.graph.nodes[item]["name"] for item in deployments} == {
        "/front/front_camera",
        "/rear/rear_camera",
    }
    assert _topic_id("/front/image") in model.graph
    assert _topic_id("/rear/image") in model.graph
    assert model.summary()["deployments"] == 2
    front_topic = next(topic for topic in model.topics() if topic["name"] == "/front/image")
    assert front_topic["publishers"] == ["/front/front_camera"]


def test_node_isolation_counts_deployment_communications(tmp_path: Path) -> None:
    package = _package("driver_pkg", tmp_path)
    node = NodeDefinition(
        name="Driver",
        declared_ros_name="driver",
        package="driver_pkg",
        language="python",
        publishers=[CommunicationEndpoint(name="state")],
    )
    model = UAM()
    model._nodes = [node]
    model._launch_remaps["driver_pkg"] = [
        LaunchNode(executable="driver", package="driver_pkg", name="driver", namespace="robot")
    ]
    model._build_graph([package], [node], [], {"driver_pkg": package})
    assert rule_node_isolation(model, {}) == []


def test_python_node_and_interface_types_require_import_provenance(tmp_path: Path) -> None:
    source = tmp_path / "nodes.py"
    source.write_text(
        "from unrelated import Node\n"
        "class FakeNode(Node):\n"
        "    pass\n\n"
        "from rclpy.node import Node as RosNode\n"
        "from std_msgs.msg import String as Text\n"
        "class RealNode(RosNode):\n"
        "    def __init__(self):\n"
        "        super().__init__('real')\n"
        "        self.create_publisher(object, '/unknown_type', 10)\n"
        "        self.create_publisher(Text, '/text', 10)\n",
        encoding="utf-8",
    )

    nodes = parse_python_nodes(tmp_path, "demo")
    assert [node.name for node in nodes] == ["RealNode"]
    unknown, explicit = nodes[0].publishers
    assert unknown.msg_type == "unknown"
    assert unknown.type_source == "unknown"
    assert unknown.confidence == "low"
    assert explicit.msg_type == "std_msgs/String"
    assert explicit.type_source == "explicit"
    assert explicit.confidence == "high"


def test_launch_substitutions_are_reported_as_unresolved(tmp_path: Path) -> None:
    python_launch = tmp_path / "dynamic.launch.py"
    python_launch.write_text(
        "from launch_ros.actions import Node\n"
        "from launch.substitutions import LaunchConfiguration\n"
        "node = Node(package='demo', executable='driver', "
        "namespace=LaunchConfiguration('ns'))\n",
        encoding="utf-8",
    )
    xml_launch = tmp_path / "dynamic.launch.xml"
    xml_launch.write_text(
        "<launch><node pkg='demo' exec='driver' ns='$(var ns)'/></launch>",
        encoding="utf-8",
    )
    yaml_launch = tmp_path / "dynamic.launch.yaml"
    yaml_launch.write_text(
        "launch:\n  - node:\n      pkg: demo\n      exec: driver\n      namespace: '$(var ns)'\n",
        encoding="utf-8",
    )

    for launch_file in (python_launch, xml_launch, yaml_launch):
        graph = analyze_launch_file(launch_file)
        assert graph.unresolved_branches
        assert graph.nodes[0].namespace == "<dynamic>"
        assert "namespace" in graph.nodes[0].unresolved_fields


def test_console_script_maps_launch_executable_to_python_source(tmp_path: Path) -> None:
    package = tmp_path / "src" / "demo"
    module_dir = package / "demo"
    launch_dir = package / "launch"
    module_dir.mkdir(parents=True)
    launch_dir.mkdir()
    _manifest(package, "demo")
    (package / "setup.py").write_text(
        "from setuptools import setup\n"
        "setup(name='demo', entry_points={'console_scripts': "
        "['camera_node = demo.camera:main']})\n",
        encoding="utf-8",
    )
    (module_dir / "camera.py").write_text(
        "from rclpy.node import Node\n"
        "class CameraDriver(Node):\n"
        "    def __init__(self):\n"
        "        super().__init__('camera')\n"
        "        self.create_publisher(object, 'image', 10)\n"
        "def main():\n"
        "    pass\n",
        encoding="utf-8",
    )
    (module_dir / "other.py").write_text(
        "from rclpy.node import Node\n"
        "class OtherDriver(Node):\n"
        "    def __init__(self):\n"
        "        super().__init__('front_camera')\n",
        encoding="utf-8",
    )
    (launch_dir / "robot.launch.xml").write_text(
        "<launch><node pkg='demo' exec='camera_node' name='front_camera' ns='front'/></launch>",
        encoding="utf-8",
    )

    entrypoints = find_python_entrypoints(package)
    assert entrypoints["camera_node"].module == "demo.camera"
    model = UAM.build(tmp_path, use_cache=False)
    assert model.summary()["deployments"] == 1
    assert _topic_id("/front/image") in model.graph
    deployment = model.deployments()[0]
    assert deployment["executable"] == "camera_node"
    assert deployment["name"] == "/front/front_camera"
    other = next(node for node in model.nodes() if node.name == "OtherDriver")
    assert "deployments" not in model.graph.nodes[model.node_graph_id(other)]


def test_console_scripts_support_setup_cfg_and_pyproject(tmp_path: Path) -> None:
    (tmp_path / "setup.cfg").write_text(
        "[options.entry_points]\nconsole_scripts =\n    cfg_node = demo.cfg:main\n",
        encoding="utf-8",
    )
    (tmp_path / "pyproject.toml").write_text(
        "[project.scripts]\ntoml-node = 'demo.toml:main'\n",
        encoding="utf-8",
    )
    entrypoints = find_python_entrypoints(tmp_path)
    assert entrypoints["cfg_node"].module == "demo.cfg"
    assert entrypoints["toml-node"].module == "demo.toml"


def test_package_xml_preserves_multiple_licenses_and_conditional_dependencies(
    tmp_path: Path,
) -> None:
    manifest = tmp_path / "package.xml"
    manifest.write_text(
        "<package format='3'><name>demo</name><version>1.0.0</version>"
        "<description>demo package</description>"
        "<maintainer email='dev@example.com'>Dev</maintainer>"
        "<license>Apache-2.0</license><license>MIT</license>"
        "<buildtool_depend>ament_python</buildtool_depend>"
        "<exec_depend>always_dep</exec_depend>"
        "<exec_depend condition=\"$ROS_DISTRO == 'jazzy'\">jazzy_dep</exec_depend>"
        "</package>",
        encoding="utf-8",
    )
    package = parse_package_xml(manifest)
    assert package is not None
    assert package.licenses == ["Apache-2.0", "MIT"]
    assert package.license == "Apache-2.0"
    assert package.dependencies[DepType.EXEC] == ["always_dep"]
    assert package.conditional_dependencies[DepType.EXEC] == ["jazzy_dep"]
    assert package.dependency_conditions[DepType.EXEC]["jazzy_dep"] == "$ROS_DISTRO == 'jazzy'"
    assert package.package_type == PackageType.AMENT_PYTHON


def test_license_policy_checks_every_declared_license(tmp_path: Path) -> None:
    package = PackageMetadata(
        name="demo",
        path=str(tmp_path),
        license="Apache-2.0",
        licenses=["Apache-2.0", "MIT"],
    )
    model = UAM()
    model._packages = [package]
    assert rule_license(model, {"allowed": ["Apache-2.0", "MIT"]}) == []
    violations = rule_license(model, {"allowed": ["Apache-2.0"]})
    assert len(violations) == 1
    assert "MIT" in violations[0].message


def test_file_digest_cache_reuses_unchanged_content_hash(tmp_path: Path, monkeypatch) -> None:
    import diskcache

    from ros2inspector.cache import analysis_cache

    source = tmp_path / "node.py"
    source.write_text("value = 1\n", encoding="utf-8")
    digest_cache = diskcache.Cache(str(tmp_path / "digest-cache"))
    calls = 0
    original = analysis_cache._content_digest

    def counted(path: Path) -> str:
        nonlocal calls
        calls += 1
        return original(path)

    monkeypatch.setattr(analysis_cache, "_content_digest", counted)
    try:
        first = analysis_cache._cached_content_digest(source, digest_cache)
        second = analysis_cache._cached_content_digest(source, digest_cache)
        assert first == second
        assert calls == 1

        source.write_text("value = 2\n", encoding="utf-8")
        third = analysis_cache._cached_content_digest(source, digest_cache)
        assert third != first
        assert calls == 2
    finally:
        digest_cache.close()
