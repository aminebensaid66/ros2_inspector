from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from ros2inspector.cli.app import app
from ros2inspector.model.schemas import DepType, PackageMetadata
from ros2inspector.model.uam import UAM
from ros2inspector.policy.engine import run_policy, violation_summary

runner = CliRunner()


def _workspace(root: Path) -> Path:
    package = root / "src" / "demo"
    module = package / "demo"
    module.mkdir(parents=True)
    (package / "package.xml").write_text(
        "<package format='3'><name>demo</name><version>0.0.0</version>"
        "<description>demo package</description>"
        "<maintainer email='dev@example.com'>Dev</maintainer>"
        "<license>MIT</license><exec_depend>rclpy</exec_depend></package>",
        encoding="utf-8",
    )
    (module / "node.py").write_text(
        "from rclpy.node import Node\n"
        "from std_msgs.msg import String\n"
        "class Talker(Node):\n"
        "    def __init__(self):\n"
        "        super().__init__('talker')\n"
        "        self.create_publisher(String, '/lonely', 10)\n",
        encoding="utf-8",
    )
    return root


def test_policy_runner_covers_metadata_and_dependency_rules(tmp_path: Path) -> None:
    model = UAM()
    model._packages = [
        PackageMetadata(
            name="app",
            version="0.0.0",
            path=str(tmp_path / "app"),
            license="MIT",
            licenses=["MIT"],
            maintainers=[],
            dependencies={DepType.EXEC: ["core"]},
            health_score=50,
        ),
        PackageMetadata(
            name="core",
            version="1.0.0",
            path=str(tmp_path / "core"),
            license="Apache-2.0",
            licenses=["Apache-2.0"],
            maintainers=["Dev <dev@example.com>"],
            dependencies={DepType.EXEC: ["app"]},
            health_score=90,
        ),
    ]
    graph = model.graph
    graph.add_node("pkg:app", kind="Package", name="app")
    graph.add_node("pkg:core", kind="Package", name="core")
    graph.add_edge("pkg:app", "pkg:core", rel="depends_on")
    graph.add_edge("pkg:core", "pkg:app", rel="depends_on")

    violations = run_policy(
        model,
        [
            {"type": "health_threshold", "min_score": 70, "_source": "test"},
            {"type": "license", "allowed": ["Apache-2.0"], "_source": "test"},
            {
                "type": "dependency",
                "forbidden": [{"from": "app", "to": "core"}],
                "_source": "test",
            },
            {"type": "no_circular_deps", "_source": "test"},
            {"type": "maintainer_required", "require_email": True, "_source": "test"},
            {"type": "version_not_default", "_source": "test"},
        ],
    )
    rule_types = {item.rule_type for item in violations}
    assert {
        "health_threshold",
        "license",
        "dependency",
        "no_circular_deps",
        "maintainer_required",
        "version_not_default",
    } <= rule_types
    summary = violation_summary(violations)
    assert sum(summary.values()) == len(violations)


def test_audit_json_exit_threshold_and_validate_policy(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path / "ws")
    audit = runner.invoke(
        app,
        ["--quiet", "audit", str(workspace), "--format", "json", "--fail-on", "info"],
    )
    assert audit.exit_code == 1, audit.output
    audit_data = json.loads(audit.output)
    assert audit_data["summary"]["total_findings"] >= 1
    assert any(item["rule_type"] == "topic_connectivity" for item in audit_data["findings"])

    policy = tmp_path / "policy.yaml"
    policy.write_text(
        "version: 1\nrules:\n"
        "  - type: license\n"
        "    allowed: [Apache-2.0]\n"
        "    severity: error\n",
        encoding="utf-8",
    )
    validate = runner.invoke(
        app,
        [
            "--quiet",
            "validate",
            str(workspace),
            "--policy",
            str(policy),
            "--format",
            "json",
        ],
    )
    assert validate.exit_code == 1, validate.output
    validation_data = json.loads(validate.output)
    assert validation_data["summary"]["violations"]["errors"] == 1
    assert validation_data["violations"][0]["rule_type"] == "license"
