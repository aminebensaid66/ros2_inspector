from __future__ import annotations

from pathlib import Path

import pytest

from ros2inspector.model.uam import UAM
from ros2inspector.policy.rules import rule_qos_compatibility
from ros2inspector.static.python_parser import parse_python_nodes

_HEADER = """
import rclpy
from rclpy.node import Node
from rclpy.qos import (QoSProfile, QoSPresetProfiles, qos_profile_sensor_data,
                       qos_profile_system_default)
from std_msgs.msg import String


class Demo(Node):
    def __init__(self):
        super().__init__('demo')
"""


def _publisher_qos(tmp_path: Path, expr: str):
    (tmp_path / "node.py").write_text(
        _HEADER + f"        self.create_publisher(String, 'topic', {expr})\n"
    )
    return parse_python_nodes(tmp_path, "demo")[0].publishers[0].qos


@pytest.mark.parametrize(
    ("expr", "depth", "reliability", "durability"),
    [
        ("qos_profile_sensor_data", 5, "best_effort", "volatile"),
        ("QoSPresetProfiles.SENSOR_DATA.value", 5, "best_effort", "volatile"),
        ("QoSPresetProfiles.PARAMETERS.value", 1000, "reliable", "volatile"),
    ],
)
def test_presets_are_resolved(
    tmp_path: Path, expr: str, depth: int, reliability: str, durability: str
) -> None:
    qos = _publisher_qos(tmp_path, expr)
    assert qos is not None
    assert (qos.depth, qos.reliability, qos.durability) == (depth, reliability, durability)


def test_keyword_enums_with_transient_local(tmp_path: Path) -> None:
    qos = _publisher_qos(
        tmp_path, "QoSProfile(depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL)"
    )
    assert qos is not None
    assert qos.depth == 1
    assert qos.durability == "transient_local"
    assert qos.reliability == "reliable"


def test_system_default_is_unknown(tmp_path: Path) -> None:
    assert _publisher_qos(tmp_path, "qos_profile_system_default") is None
    assert _publisher_qos(tmp_path, "QoSPresetProfiles.SYSTEM_DEFAULT.value") is None


def test_unknown_expression_stays_unknown(tmp_path: Path) -> None:
    assert _publisher_qos(tmp_path, "self.make_qos()") is None
    assert _publisher_qos(tmp_path, "QoSPresetProfiles.NOT_A_PRESET.value") is None


def test_sensor_data_publisher_vs_default_subscriber_is_flagged(tmp_path: Path) -> None:
    pkg = tmp_path / "src" / "demo"
    pkg.mkdir(parents=True)
    (pkg / "package.xml").write_text(
        '<package format="3"><name>demo</name><version>0.1</version></package>'
    )
    (pkg / "pub.py").write_text(
        _HEADER.replace("Demo", "Pub")
        + "        self.create_publisher(String, 'topic', qos_profile_sensor_data)\n"
    )
    (pkg / "sub.py").write_text(
        _HEADER.replace("Demo", "Sub")
        + "        self.create_subscription(String, 'topic', self.cb, 10)\n"
        + "    def cb(self, msg):\n        pass\n"
    )
    (pkg / "launch").mkdir()
    (pkg / "launch" / "demo.launch.py").write_text(
        "from launch_ros.actions import Node\n"
        "def generate_launch_description():\n"
        "    return [Node(package='demo', executable='pub'),\n"
        "            Node(package='demo', executable='sub')]\n"
    )
    findings = rule_qos_compatibility(UAM.build(tmp_path, use_cache=False), {})
    assert len(findings) == 1
    assert "reliability" in findings[0].message
