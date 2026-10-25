from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ros2inspector.cli.app import app
from ros2inspector.model.uam import UAM
from ros2inspector.policy.rules import rule_topic_connectivity
from ros2inspector.policy.sarif import apply_baseline, fingerprint, to_sarif
from tests.unit.name_resolution_fixtures import make_workspace, write_launch


@pytest.mark.parametrize("terms", [800, 5000])
def test_constant_expression_does_not_prove_a_missing_publisher(tmp_path: Path, terms: int) -> None:
    make_workspace(tmp_path, "/healthy")
    source = tmp_path / "src/demo/demo/node.py"
    source.write_text("x = " + " + ".join(["1"] * terms) + "\n" + source.read_text())
    (source.parent / "listener.py").write_text(
        "from rclpy.node import Node\nfrom std_msgs.msg import String\n"
        "class Listener(Node):\n def __init__(self):\n  super().__init__('listener')\n"
        "  self.create_subscription(String, '/healthy', print, 10)\n"
    )
    result = CliRunner().invoke(
        app, ["audit", str(tmp_path), "--no-cache", "--fail-on", "warning", "-f", "json"]
    )
    assert result.exit_code == 0, result.output
    report = json.loads(result.stdout)
    connectivity = [f for f in report["findings"] if f["rule_type"] == "topic_connectivity"]
    if report["diagnostics"]:
        assert all(f["severity"] == "info" and f["confidence"] == "uncertain" for f in connectivity)
    else:
        assert connectivity == []
        assert {n.name for n in UAM.build(tmp_path, use_cache=False).nodes()} == {
            "Talker",
            "Listener",
        }


def test_real_parse_failure_is_uncertain_in_json_sarif_and_baseline(tmp_path: Path) -> None:
    make_workspace(tmp_path, "/healthy")
    (tmp_path / "src/demo/demo/broken.py").write_text("def broken(")
    model = UAM.build(tmp_path, use_cache=False)
    [finding] = rule_topic_connectivity(model, {"severity_no_subscriber": "error"})
    assert finding.severity == "info"
    assert finding.confidence == "uncertain"
    assert "not proven" in finding.message
    [result] = to_sarif([finding], "0.1.5")["runs"][0]["results"]
    assert result["level"] == "note"
    assert result["properties"]["confidence"] == "uncertain"
    proven = finding.model_copy(update={"severity": "error", "confidence": "known"})
    assert apply_baseline([proven], {fingerprint(finding)}) == [proven]
    audit = CliRunner().invoke(app, ["audit", str(tmp_path), "--fail-on", "warning", "-f", "json"])
    assert audit.exit_code == 0


@pytest.mark.parametrize(
    "imports, constructor",
    [
        ("from rclpy.action import ActionClient as AC", "AC"),
        ("import rclpy.action as ra", "ra.ActionClient"),
        ("from rclpy.action.client import ActionClient as AC", "AC"),
        ("from rclpy.action.server import ActionServer as AS", "AS"),
    ],
)
def test_action_import_aliases_keep_endpoints(
    tmp_path: Path, imports: str, constructor: str
) -> None:
    make_workspace(tmp_path)
    source = tmp_path / "src/demo/demo/node.py"
    source.write_text(
        "from rclpy.node import Node\nfrom example_interfaces.action import Fibonacci\n"
        + imports
        + "\nclass Talker(Node):\n def __init__(self):\n"
        "  super().__init__('talker')\n"
        f"  self.action = {constructor}(self, Fibonacci, '/work')\n"
    )
    model = UAM.build(tmp_path, use_cache=False)
    assert len(model.actions()) == 1
    assert model.actions()[0]["name"] == "/work"
    assert model.actions()[0]["action_type"] == "example_interfaces/Fibonacci"
    assert not model.nodes()[0].analysis_incomplete
    assert not model.diagnostics()


def test_unknown_constructor_with_node_and_type_is_incomplete(tmp_path: Path) -> None:
    make_workspace(tmp_path)
    source = tmp_path / "src/demo/demo/node.py"
    source.write_text(
        source.read_text() + "  self.client = UnknownFactory(self, String, '/work')\n"
    )
    model = UAM.build(tmp_path, use_cache=False)
    assert model.nodes()[0].analysis_incomplete
    assert any(d["code"] == "source_analysis_incomplete" for d in model.diagnostics())


def _cross_package(root: Path) -> Path:
    make_workspace(root)
    base = root / "src/demo/demo/node.py"
    base.write_text(base.read_text().replace("Talker", "Base"))
    child_pkg = root / "src/child_pkg"
    module = child_pkg / "child_pkg"
    module.mkdir(parents=True)
    (child_pkg / "package.xml").write_text(
        (root / "src/demo/package.xml")
        .read_text()
        .replace("<name>demo</name>", "<name>child_pkg</name>")
    )
    (module / "child.py").write_text(
        "from demo.node import Base\nclass Child(Base):\n pass\ndef main():\n return Child()\n"
    )
    (child_pkg / "setup.py").write_text(
        "setup(entry_points={'console_scripts':['child = child_pkg.child:main']})\n"
    )
    launch = child_pkg / "launch/system.launch.py"
    launch.parent.mkdir()
    write_launch(
        launch, "Node(package='child_pkg', executable='child', name='child', namespace='robot')"
    )
    return base


def test_cross_package_inheritance_deploys_and_cache_tracks_base(tmp_path: Path) -> None:
    base = _cross_package(tmp_path)
    cache = tmp_path / "cache"
    for topic in ("chatter", "updated"):
        if topic == "updated":
            base.write_text(base.read_text().replace("chatter", "updated"))
        for cached in (False, True, True):
            model = UAM.build(tmp_path, use_cache=cached, cache_dir=cache)
            child = next(n for n in model.nodes() if n.name == "Child")
            assert child.package == "child_pkg"
            assert child.declared_ros_name == "talker"
            assert not child.analysis_incomplete
            assert child.publishers[0].name == topic
            assert model.deployments()[0]["name"] == "/robot/child"
            assert [t["name"] for t in model.topics()] == ["/robot/" + topic]
            assert not model.diagnostics()


def test_unresolved_imported_base_without_endpoints_is_reported(tmp_path: Path) -> None:
    _cross_package(tmp_path)
    child = tmp_path / "src/child_pkg/child_pkg/child.py"
    child.write_text(
        "from demo.missing import Missing\nclass Child(Missing):\n pass\n"
        "def main():\n return Child()\n"
    )
    model = UAM.build(tmp_path, use_cache=False)
    definition = next(n for n in model.nodes() if n.name == "Child")
    assert definition.analysis_incomplete
    assert model.diagnostics()


@pytest.mark.parametrize("layout", ["src", "named"])
def test_workspace_inheritance_uses_declared_python_directories(
    tmp_path: Path, layout: str
) -> None:
    _cross_package(tmp_path)
    package = tmp_path / "src/demo"
    source = package / "demo"
    if layout == "src":
        (package / "src").mkdir()
        source.rename(package / "src/demo")
        mapping = {"": "src"}
    else:
        source.rename(package / "lib")
        mapping = {"demo": "lib"}
    setup = package / "setup.py"
    setup.write_text(setup.read_text().replace("setup(", f"setup(package_dir={mapping!r}, "))
    model = UAM.build(tmp_path, use_cache=False)
    child = next(n for n in model.nodes() if n.name == "Child")
    assert not child.analysis_incomplete
    assert child.publishers[0].name == "chatter"
    assert [t["name"] for t in model.topics()] == ["/robot/chatter"]


def test_third_party_base_used_by_main_stays_incomplete(tmp_path: Path) -> None:
    make_workspace(tmp_path)
    source = tmp_path / "src/demo/demo/node.py"
    source.write_text(
        "from other_package import Base\nclass Talker(Base):\n pass\n"
        "def main():\n return Talker()\n"
    )
    model = UAM.build(tmp_path, use_cache=False)
    assert len(model.nodes()) == 1
    assert model.nodes()[0].analysis_incomplete
    assert any(d["code"] == "source_analysis_incomplete" for d in model.diagnostics())


def test_cpp_parse_gap_keeps_proven_type_conflicts_enabled(tmp_path: Path) -> None:
    make_workspace(tmp_path)
    module = tmp_path / "src/demo/demo"
    (module / "listener.py").write_text(
        "from rclpy.node import Node\nfrom std_msgs.msg import Int32\n"
        "class Listener(Node):\n def __init__(self):\n  super().__init__('listener')\n"
        "  self.create_subscription(Int32, 'chatter', print, 10)\n"
    )
    (module / "broken.cpp").write_text("class Broken : public rclcpp::Node { nonsense(")
    model = UAM.build(tmp_path, use_cache=False)
    assert any(
        d["code"] == "source_parse_failed" and d["file"].endswith("broken.cpp")
        for d in model.diagnostics()
    )
    findings = rule_topic_connectivity(model, {})
    mismatch = next(f for f in findings if f.rule_type == "interface_type_mismatch")
    assert mismatch.severity == "error"
    assert mismatch.confidence == "known"


def test_cached_inheritance_rechecks_setup_config_layout(tmp_path: Path) -> None:
    _cross_package(tmp_path)
    package = tmp_path / "src/demo"
    (package / "demo").rename(package / "lib")
    cfg = package / "setup.cfg"
    cfg.write_text("[options]\npackage_dir =\n    wrong = lib\n")
    cache = tmp_path / "cache"
    first = UAM.build(tmp_path, cache_dir=cache)
    assert next(n for n in first.nodes() if n.name == "Child").analysis_incomplete
    cfg.write_text("[options]\npackage_dir =\n    demo = lib\n")
    second = UAM.build(tmp_path, cache_dir=cache)
    child = next(n for n in second.nodes() if n.name == "Child")
    assert not child.analysis_incomplete
    assert child.publishers[0].name == "chatter"
