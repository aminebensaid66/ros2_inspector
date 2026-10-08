from __future__ import annotations

from pathlib import Path

from ros2inspector.model.uam import UAM
from ros2inspector.static.cmake_components import find_component_executables
from ros2inspector.static.launch_analyzer import analyze_launch_file


def test_xml_composable_nodes(tmp_path: Path) -> None:
    launch = tmp_path / "demo.launch.xml"
    launch.write_text("""<launch>
  <node_container pkg="rclcpp_components" exec="component_container" name="c" namespace="">
    <composable_node pkg="demo" plugin="demo::Pub" name="pub" namespace="ns">
      <remap from="a" to="b"/>
    </composable_node>
  </node_container>
  <load_composable_node target="/c" if="$(var use_sub)">
    <composable_node pkg="demo" plugin="demo::Sub" name="sub"/>
  </load_composable_node>
</launch>""")
    nodes = {n.plugin: n for n in analyze_launch_file(launch).nodes if n.plugin}
    assert set(nodes) == {"demo::Pub", "demo::Sub"}
    assert nodes["demo::Pub"].presence == "known"
    assert nodes["demo::Pub"].namespace == "ns"
    assert nodes["demo::Pub"].remaps == {"a": "b"}
    assert nodes["demo::Sub"].presence == "conditional"


def test_xml_composable_inherits_container_namespace(tmp_path: Path) -> None:
    launch = tmp_path / "demo.launch.xml"
    launch.write_text("""<launch>
  <node_container pkg="rclcpp_components" exec="component_container" name="c" namespace="robot">
    <composable_node pkg="demo" plugin="demo::A" name="a"/>
    <composable_node pkg="demo" plugin="demo::B" name="b" namespace="other"/>
  </node_container>
</launch>""")
    nodes = {n.plugin: n for n in analyze_launch_file(launch).nodes if n.plugin}
    assert nodes["demo::A"].namespace == "robot"
    assert nodes["demo::B"].namespace == "other"


def test_cmake_component_executables(tmp_path: Path) -> None:
    (tmp_path / "CMakeLists.txt").write_text("""
# rclcpp_components_register_node(ignored PLUGIN "x::Y" EXECUTABLE nope)
rclcpp_components_register_node(
  demo_lib
  PLUGIN "demo::Driver"
  EXECUTABLE driver_exe)
rclcpp_components_register_node(demo_lib PLUGIN "${VAR}" EXECUTABLE skipped)
""")
    assert find_component_executables(tmp_path) == {"driver_exe": "demo::Driver"}


def test_component_executable_matches_registered_class(tmp_path: Path) -> None:
    pkg = tmp_path / "src" / "demo"
    (pkg / "launch").mkdir(parents=True)
    (pkg / "package.xml").write_text(
        '<package format="3"><name>demo</name><version>0.1</version></package>'
    )
    (pkg / "CMakeLists.txt").write_text(
        'rclcpp_components_register_node(lib PLUGIN "demo::Driver" EXECUTABLE driver_exe)\n'
    )
    (pkg / "driver.cpp").write_text("""
namespace demo {
class Driver : public rclcpp::Node {
void setup() { create_publisher<std_msgs::msg::String>("out", 10); }
};
}
""")
    (pkg / "launch" / "demo.launch.py").write_text(
        "from launch_ros.actions import Node\n"
        "def generate_launch_description():\n"
        "    return [Node(package='demo', executable='driver_exe', name='drv')]\n"
    )
    uam = UAM.build(tmp_path, use_cache=False)
    deployments = [d for _, d in uam.graph.nodes(data=True) if d.get("kind") == "Deployment"]
    assert [d["name"] for d in deployments] == ["drv"]
    assert deployments[0]["resolution"] == "known"
