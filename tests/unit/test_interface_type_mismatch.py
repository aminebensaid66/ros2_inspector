from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ros2inspector.cli.app import app
from ros2inspector.graph.renderer import render_dot, render_mermaid
from ros2inspector.model.uam import UAM
from tests.unit.name_resolution_fixtures import make_workspace


@pytest.mark.parametrize(
    "kind, own_type, other_type, server, client",
    [
        (
            "topic",
            "std_msgs/String",
            "std_msgs/Int32",
            "self.create_publisher(String, '/wire', 10)",
            "self.create_subscription(Int32, '/wire', lambda msg: None, 10)",
        ),
        (
            "service",
            "std_srvs/Trigger",
            "std_srvs/SetBool",
            "self.create_service(Trigger, '/wire', lambda a,b: b)",
            "self.create_client(SetBool, '/wire')",
        ),
        (
            "action",
            "demo/Dock",
            "demo/Fibonacci",
            "ActionServer(self, Dock, '/wire', lambda goal: None)",
            "ActionClient(self, Fibonacci, '/wire')",
        ),
    ],
)
def test_type_conflicts_preserve_endpoint_types_and_reject_peers(
    tmp_path: Path, kind: str, own_type: str, other_type: str, server: str, client: str
) -> None:
    launch = make_workspace(tmp_path)
    launch.unlink(missing_ok=True)
    source = launch.parent.parent / "demo/node.py"
    imports = (
        "from rclpy.node import Node\nfrom std_msgs.msg import String, Int32\n"
        "from std_srvs.srv import Trigger, SetBool\n"
        "from demo.action import Dock, Fibonacci\n"
        "from rclpy.action import ActionServer, ActionClient\n"
    )
    source.write_text(
        imports + "class Provider(Node):\n def __init__(self):\n"
        "  super().__init__('provider')\n  " + server + "\n"
        "class Consumer(Node):\n def __init__(self):\n"
        "  super().__init__('consumer')\n  " + client + "\n"
    )
    result = CliRunner().invoke(
        app, ["nodes", "-C", str(tmp_path), "--no-cache", "--show-connections", "-f", "json"]
    )
    assert result.exit_code == 0, result.output
    nodes = {n["name"]: n for n in json.loads(result.stdout)}
    key = {"topic": "msg_type", "service": "srv_type", "action": "action_type"}[kind]
    for node_name, expected in [("Provider", own_type), ("Consumer", other_type)]:
        connection = nodes[node_name]["connections"][0]
        assert connection[key] == expected
        assert all(not value for name, value in connection.items() if name.startswith("other_"))
    audit = CliRunner().invoke(app, ["audit", str(tmp_path), "-f", "json"])
    assert audit.exit_code == 1
    findings = json.loads(audit.stdout)["findings"]
    assert len(findings) == 1
    assert findings[0]["rule_type"] == "interface_type_mismatch"
    assert findings[0]["severity"] == "error"


def test_type_mismatch_is_visible_in_graph_exports(tmp_path: Path) -> None:
    launch = make_workspace(tmp_path)
    launch.unlink(missing_ok=True)
    (tmp_path / "src/demo/demo/listen.py").write_text(
        "from rclpy.node import Node\nfrom std_msgs.msg import Int32\n"
        "class Listener(Node):\n def __init__(self):\n  super().__init__('listener')\n"
        "  self.create_subscription(Int32, 'chatter', lambda m: None, 10)\n"
    )
    model = UAM.build(tmp_path, use_cache=False)
    label = "interface type mismatch: std_msgs/Int32, std_msgs/String"
    assert label in render_mermaid(model, "comms")
    assert label in render_dot(model, "comms")
    result = CliRunner().invoke(
        app, ["graph", "comms", "-C", str(tmp_path), "--no-cache", "-f", "json"]
    )
    topic = next(n for n in json.loads(result.stdout)["nodes"] if n.get("kind") == "Topic")
    assert topic["interface_type_mismatch"] is True
    assert sorted(topic["observed_types"]) == ["std_msgs/Int32", "std_msgs/String"]
