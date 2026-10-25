from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ros2inspector.cache.analysis_cache import _SOURCE_SUFFIXES, _pkg_fingerprint
from ros2inspector.cli.app import app
from ros2inspector.cli.cmd_watch import WATCH_SUFFIXES
from ros2inspector.model.uam import UAM
from ros2inspector.static.launch_analyzer import analyze_launch_file
from ros2inspector.static.python_entrypoints import find_python_entrypoints


def _package(root: Path) -> Path:
    package = root / "src/demo"
    module = package / "demo"
    module.mkdir(parents=True)
    (package / "package.xml").write_text(
        '<package format="3"><name>demo</name><version>0.1.0</version'
        '><description>fixture</description><maintainer email="dev@ex'
        'ample.com">Dev</maintainer><license>MIT</license></package>'
    )
    return module


_NODE = (
    "from rclpy.node import Node\nfrom std_msgs.msg import String\n"
    "class Speaker(Node):\n def __init__(self):\n  super().__init__"
    "('speaker')\n  self.create_publisher(String, '/chatter', 10)\n"
)


@pytest.mark.parametrize(
    "bad",
    ["x = " + " + ".join(["1"] * 60000), "def broken(", "# coding: missing-codec\n"],
    ids=["recursion", "syntax", "encoding"],
)
def test_source_failure_keeps_other_nodes_and_cached_diagnostic(tmp_path: Path, bad: str) -> None:
    module = _package(tmp_path)
    (module / "good.py").write_text(_NODE)
    (module / "bad.py").write_text(bad)
    for cached in (False, True, True):
        model = UAM.build(tmp_path, use_cache=cached)
        assert [n.name for n in model.nodes()] == ["Speaker"]
        assert any(
            d["code"] == "source_parse_failed" and d["file"].endswith("bad.py")
            for d in model.diagnostics()
        )
    result = CliRunner().invoke(app, ["nodes", "-C", str(tmp_path), "-f", "json", "--no-cache"])
    assert result.exit_code == 0, result.output
    assert len(json.loads(result.stdout)) == 1
    assert "source_parse_failed" in result.stderr
    assert "Traceback" not in result.output
    audit = CliRunner().invoke(app, ["audit", str(tmp_path), "-f", "json", "--no-cache"])
    report = json.loads(audit.stdout)
    assert any(d["code"] == "source_parse_failed" for d in report["diagnostics"])
    # An unreadable file must not switch the check off; the finding says what was not seen.
    [finding] = [f for f in report["findings"] if f["rule_type"] == "topic_connectivity"]
    assert "/chatter" in finding["message"]
    assert "1 source file(s) could not be parsed" in finding["message"]


def _connectivity(root: Path) -> list[str]:
    audit = CliRunner().invoke(app, ["audit", str(root), "-f", "json", "--no-cache"])
    report = json.loads(audit.stdout)
    return sorted(
        f["message"].split(" (")[0]
        for f in report["findings"]
        if f["rule_type"].endswith("_connectivity")
    )


def test_unrelated_create_calls_keep_connectivity_findings(tmp_path: Path) -> None:
    module = _package(tmp_path)
    (module / "good.py").write_text(_NODE)
    (module / "util.py").write_text(
        "import asyncio\nimport boto3\n"
        "def go(loop):\n    return loop.create_task(asyncio.sleep(1))\n"
        "def s3():\n    return boto3.session.Session().create_client('s3')\n"
    )
    assert _connectivity(tmp_path) == ["Topic '/chatter' has no subscribers"]


def test_unattributed_endpoint_hides_only_the_peer_it_could_be(tmp_path: Path) -> None:
    module = _package(tmp_path)
    (module / "good.py").write_text(
        _NODE + "  self.create_publisher(String, '/status', 10)\n"
        "  self.create_subscription(String, '/cmd', print, 10)\n"
    )
    (module / "helper.py").write_text(
        "from std_msgs.msg import String\nclass Helper:\n def __init__(self, node, topic):\n"
        "  self.node = node\n"
        "  self.node.create_subscription(String, 'chatter', print, 10)\n"
        "  self.node.create_publisher(String, topic, 10)\n"
    )
    # 'chatter' may be /chatter's subscriber, and the dynamic publisher may feed /cmd;
    # nothing could subscribe to /status.
    assert _connectivity(tmp_path) == ["Topic '/status' has no subscribers"]


def test_encoding_cookie_applies_to_nodes_launch_and_setup(tmp_path: Path) -> None:
    module = _package(tmp_path)
    prefix = b"# coding: latin-1\n# caf\xe9\n"
    (module / "node.py").write_bytes(prefix + _NODE.encode())
    launch = module.parent / "launch/demo.launch.py"
    launch.parent.mkdir()
    launch.write_bytes(
        prefix
        + (
            b"from launch_ros.actions import Node\ndef generate_launch_desc"
            b"ription():\n return [Node(package='demo', executable='speaker"
            b"')]\n"
        )
    )
    (module.parent / "setup.py").write_bytes(
        prefix
        + (
            b"from setuptools import setup\nsetup(entry_points={'console_sc"
            b"ripts':['speaker = demo.node:main']})\n"
        )
    )
    model = UAM.build(tmp_path, use_cache=False)
    assert model.nodes()[0].publishers[0].msg_type == "std_msgs/String"
    assert analyze_launch_file(launch).nodes[0].executable == "speaker"
    assert find_python_entrypoints(module.parent)["speaker"].module == "demo.node"


@pytest.mark.parametrize("suffix", [".hh", ".hxx", ".c++", ".C", ".h++", ".ipp", ".tpp"])
def test_cpp_header_suffixes_reach_model_cache_and_watch(tmp_path: Path, suffix: str) -> None:
    module = _package(tmp_path)
    header = module / ("node" + suffix)
    header.write_text(
        "class Speaker : public rclcpp::Node { public: Speaker() : No"
        'de("speaker") { pub_ = create_publisher<std_msgs::msg::Strin'
        'g>("/chatter", 10); } };'
    )
    assert suffix in WATCH_SUFFIXES
    assert suffix in _SOURCE_SUFFIXES
    model = UAM.build(tmp_path, use_cache=False)
    assert model.nodes()[0].publishers[0].msg_type == "std_msgs/String"
    before = _pkg_fingerprint(module.parent)
    header.write_text(header.read_text().replace("chatter", "changed"))
    assert _pkg_fingerprint(module.parent) != before


@pytest.mark.parametrize(
    "imports,ctor",
    [
        ("import rclpy", "rclpy.create_node"),
        ("import rclpy as rp", "rp.create_node"),
        ("from rclpy.node import Node", "Node"),
        ("from rclpy.node import Node as N", "N"),
        ("import rclpy.node as rn", "rn.Node"),
    ],
)
def test_procedural_nodes_keep_endpoints_and_entrypoint(
    tmp_path: Path, imports: str, ctor: str
) -> None:
    module = _package(tmp_path)
    (module / "node.py").write_text(
        imports
        + (
            "\nfrom std_msgs.msg import String\nfrom std_srvs.srv import Tr"
            "igger\nfrom rclpy.action import ActionServer, ActionClient\nfr"
            "om demo.action import Dock\ndef main():\n node = "
        )
        + ctor
        + (
            "('factory')\n node.create_publisher(String, '/out', 10)\n node"
            ".create_subscription(String, '/in', lambda msg: None, 10)\n n"
            "ode.create_service(Trigger, '/srv', lambda a,b: b)\n node.cre"
            "ate_client(Trigger, '/client')\n ActionServer(node, Dock, '/d"
            "ock', lambda g: None)\n ActionClient(node, Dock, '/remote')\n"
        )
    )
    (module.parent / "setup.py").write_text(
        "setup(entry_points={'console_scripts':['factory = demo.node:main']})\n"
    )
    launch = module.parent / "launch/forms.launch.py"
    launch.parent.mkdir()
    launch.write_text("Node(package='demo', executable='factory', name='deployed')\n")
    model = UAM.build(tmp_path, use_cache=False)
    assert [deployment["name"] for deployment in model.deployments()] == ["deployed"]
    node = model.nodes()[0]
    assert node.declared_ros_name == "factory"
    for attr, name in [
        ("publishers", "/out"),
        ("subscriptions", "/in"),
        ("services", "/srv"),
        ("clients", "/client"),
        ("action_servers", "/dock"),
        ("action_clients", "/remote"),
    ]:
        assert getattr(node, attr)[0].name == name
    assert not node.analysis_incomplete


def test_imported_subclass_inherits_name_transitively(tmp_path: Path) -> None:
    module = _package(tmp_path)
    (module / "base.py").write_text(
        "from rclpy.node import Node\nclass Base(Node):\n def __init__("
        "self):\n  super().__init__('child')\n"
    )
    (module / "middle.py").write_text("from demo.base import Base as B\nclass Middle(B):\n pass\n")
    (module / "child.py").write_text(
        "from .middle import Middle\nfrom std_msgs.msg import String\nc"
        "lass Child(Middle):\n def __init__(self):\n  super().__init__("
        ")\n  self.create_publisher(String, '/child_topic', 10)\n"
    )
    model = UAM.build(tmp_path, use_cache=False)
    assert next(n for n in model.nodes() if n.name == "Base").is_abstract
    child = next(n for n in model.nodes() if n.name == "Child")
    assert child.declared_ros_name == "child"
    assert child.publishers[0].msg_type == "std_msgs/String"
    assert not child.analysis_incomplete


def test_unresolved_receiver_and_base_are_reported(tmp_path: Path) -> None:
    module = _package(tmp_path)
    (module / "unknown.py").write_text(
        "from external import Base\nfrom std_msgs.msg import String\ncl"
        "ass Child(Base):\n def run(self):\n  self.create_publisher(Str"
        "ing, '/maybe', 10)\n"
    )
    model = UAM.build(tmp_path, use_cache=False)
    assert model.diagnostics()
    assert model.nodes()[0].analysis_incomplete


@pytest.mark.parametrize("failure", [RecursionError, MemoryError])
def test_visitor_resource_failure_keeps_other_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: type[Exception]
) -> None:
    from ros2inspector.static.python_parser import _NodeVisitor

    module = _package(tmp_path)
    (module / "good.py").write_text(_NODE)
    (module / "limited.py").write_text(_NODE)
    original = _NodeVisitor.visit

    def visit(self: _NodeVisitor, node: object) -> object:
        if self.file_path.name == "limited.py":
            raise failure("visitor limit")
        return original(self, node)

    monkeypatch.setattr(_NodeVisitor, "visit", visit)
    model = UAM.build(tmp_path, use_cache=False)
    assert len(model.nodes()) == 1
    assert any("visitor limit" in d["message"] for d in model.diagnostics())


def test_launch_and_entrypoint_recursion_is_a_diagnostic(tmp_path: Path) -> None:
    source = tmp_path / "deep.launch.py"
    source.write_text("x = " + " + ".join(["1"] * 60000))
    graph = analyze_launch_file(source)
    assert graph.unresolved_branches
    assert graph.diagnostics[0]["code"] == "source_parse_failed"
    (tmp_path / "setup.py").write_text("entries = entries\nsetup(entry_points=entries)\n")
    diagnostics: list = []
    assert find_python_entrypoints(tmp_path, diagnostics) == {}
    assert diagnostics[0]["code"] == "source_parse_failed"


def test_cpp_suffix_registry_is_shared() -> None:
    from ros2inspector.static.cpp_parser import _CPP_SUFFIXES
    from ros2inspector.static.source_kinds import CPP_SUFFIXES

    assert _CPP_SUFFIXES is CPP_SUFFIXES
    assert CPP_SUFFIXES <= WATCH_SUFFIXES
    assert CPP_SUFFIXES <= _SOURCE_SUFFIXES
