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
    assert "extras.launch.xml" in graph.includes[0].target_file


def test_xml_launch_remaps():
    graph = analyze_launch_file(FIXTURE_DIR / "bringup.launch.xml")
    talker = next(n for n in graph.nodes if n.name == "talker_node")
    assert talker.remaps.get("/chatter") == "/my_chatter"
