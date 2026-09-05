from pathlib import Path

from ros2inspector.static.launch_analyzer import analyze_launch_file

FIXTURE_DIR = Path(__file__).parent.parent / "fixtures" / "launch"


def test_python_launch_nodes():
    graph = analyze_launch_file(FIXTURE_DIR / "bringup.launch.py")
    assert len(graph.nodes) == 2
    names = {n.name for n in graph.nodes}
    assert "talker_node" in names
    assert "listener_node" in names


def test_python_launch_remaps():
    graph = analyze_launch_file(FIXTURE_DIR / "bringup.launch.py")
    talker = next(n for n in graph.nodes if n.name == "talker_node")
    assert talker.remaps.get("/chatter") == "/my_chatter"


def test_python_launch_namespace():
    graph = analyze_launch_file(FIXTURE_DIR / "bringup.launch.py")
    listener = next(n for n in graph.nodes if n.name == "listener_node")
    assert listener.namespace == "/robot"


def test_xml_launch_nodes():
    graph = analyze_launch_file(FIXTURE_DIR / "bringup.launch.xml")
    assert len(graph.nodes) == 2
    packages = {n.package for n in graph.nodes}
    assert "pkg_a" in packages
    assert "pkg_b" in packages


def test_xml_launch_includes():
    graph = analyze_launch_file(FIXTURE_DIR / "bringup.launch.xml")
    assert len(graph.includes) == 1
    include = graph.includes[0]
    assert include.target_file == "$(find pkg_c)/launch/extras.launch.xml"
    assert include.unresolved
    assert graph.unresolved_branches


def test_dynamic_include_paths_preserve_literal_evidence(tmp_path: Path):
    target = "$(find pkg_c)/launch/extras.launch.xml"
    launch_sources = {
        "include.launch.py": f'IncludeLaunchDescription("{target}")\n',
        "include.launch.xml": f'<launch><include file="{target}"/></launch>\n',
        "include.launch.yaml": f"launch:\n  - include: '{target}'\n",
    }

    for filename, source in launch_sources.items():
        launch_file = tmp_path / filename
        launch_file.write_text(source, encoding="utf-8")
        graph = analyze_launch_file(launch_file)

        assert len(graph.includes) == 1
        assert graph.includes[0].target_file == target
        assert graph.includes[0].unresolved
        assert graph.unresolved_branches


def test_xml_launch_remaps():
    graph = analyze_launch_file(FIXTURE_DIR / "bringup.launch.xml")
    talker = next(n for n in graph.nodes if n.name == "talker_node")
    assert talker.remaps.get("/chatter") == "/my_chatter"


def test_yaml_launch_uses_ros_frontend_keys_and_metadata(tmp_path: Path):
    launch_file = tmp_path / "standard.launch.yaml"
    launch_file.write_text(
        "launch:\n"
        "  - node:\n"
        "      pkg: turtlesim\n"
        "      exec: mimic\n"
        "      name: mimic\n"
        "      namespace: robot1\n"
        "      remap:\n"
        "        - from: /input/pose\n"
        "          to: /robot1/pose\n"
        "  - include: other.launch.yaml\n"
    )

    graph = analyze_launch_file(launch_file)

    assert len(graph.nodes) == 1
    node = graph.nodes[0]
    assert node.package == "turtlesim"
    assert node.executable == "mimic"
    assert node.name == "mimic"
    assert node.namespace == "robot1"
    assert node.remaps == {"/input/pose": "/robot1/pose"}
    assert graph.includes[0].target_file == "other.launch.yaml"
