from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from ros2inspector.cli.app import app
from ros2inspector.model.schemas import PolicyViolation, ViolationSeverity
from ros2inspector.policy.sarif import apply_baseline, fingerprint, to_sarif

runner = CliRunner()
WORKSPACE_A = Path(__file__).parent.parent / "fixtures" / "workspaces" / "workspace_a"


def _violation(message: str, **kwargs: object) -> PolicyViolation:
    return PolicyViolation(
        severity=ViolationSeverity.WARNING,
        rule_type="topic_connectivity",
        message=message,
        policy_file="audit",
        **kwargs,  # type: ignore[arg-type]
    )


def test_sarif_shape_and_locations(tmp_path: Path) -> None:
    located = _violation("a", file_path=str(tmp_path / "pkg" / "n.py"), line=7)
    bare = _violation("b")
    sarif = to_sarif([located, bare], "1.2.3", tmp_path)
    assert sarif["version"] == "2.1.0"
    run = sarif["runs"][0]
    assert run["tool"]["driver"]["name"] == "ros2inspector"
    assert run["tool"]["driver"]["rules"] == [
        {"id": "topic_connectivity", "name": "topic_connectivity"}
    ]
    first, second = run["results"]
    assert first["level"] == "warning"
    physical = first["locations"][0]["physicalLocation"]
    assert physical["artifactLocation"]["uri"] == "pkg/n.py"
    assert physical["region"]["startLine"] == 7
    assert "locations" not in second


def test_baseline_ignores_line_and_severity_changes() -> None:
    old = _violation("same", affected_entities=["/t"], line=1)
    moved = _violation("same", affected_entities=["/t"], line=99)
    new = _violation("different", affected_entities=["/t"])
    assert apply_baseline([moved, new], {fingerprint(old)}) == [new]


def test_fingerprint_survives_volatile_message_parts() -> None:
    def v(rule: str, message: str, entity: str) -> PolicyViolation:
        return PolicyViolation(
            severity=ViolationSeverity.WARNING,
            rule_type=rule,
            message=message,
            policy_file="audit",
            affected_entities=[entity],
        )

    health_old = v("health_threshold", "Package 'p' health score 60/100 is below threshold 70", "p")
    health_new = v("health_threshold", "Package 'p' health score 55/100 is below threshold 70", "p")
    assert fingerprint(health_old) == fingerprint(health_new)

    no_sub = v("topic_connectivity", "Topic '/t' has no subscribers (published by: a)", "/t")
    more_pubs = v("topic_connectivity", "Topic '/t' has no subscribers (published by: a, b)", "/t")
    no_pub = v("topic_connectivity", "Topic '/t' has no publisher (subscribed by: c)", "/t")
    assert fingerprint(no_sub) == fingerprint(more_pubs)
    assert fingerprint(no_sub) != fingerprint(no_pub)
    assert apply_baseline([more_pubs, no_pub], {fingerprint(no_sub)}) == [no_pub]


def test_cli_sarif_and_baseline_roundtrip(tmp_path: Path) -> None:
    result = runner.invoke(
        app, ["--quiet", "audit", str(WORKSPACE_A), "--format", "sarif", "--fail-on", "info"]
    )
    sarif = json.loads(result.stdout)
    assert sarif["version"] == "2.1.0"

    report = runner.invoke(
        app, ["--quiet", "audit", str(WORKSPACE_A), "--format", "json", "--fail-on", "info"]
    )
    baseline = tmp_path / "audit.json"
    baseline.write_text(report.stdout)
    assert json.loads(report.stdout)["findings"], "fixture should produce findings"

    again = runner.invoke(
        app,
        [
            "--quiet",
            "audit",
            str(WORKSPACE_A),
            "--format",
            "json",
            "--fail-on",
            "info",
            "--baseline",
            str(baseline),
        ],
    )
    assert again.exit_code == 0
    assert json.loads(again.stdout)["findings"] == []


def test_bad_baseline_exits_2(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text("not json")
    result = runner.invoke(app, ["--quiet", "audit", str(WORKSPACE_A), "--baseline", str(bad)])
    assert result.exit_code == 2
    missing = runner.invoke(
        app, ["--quiet", "audit", str(WORKSPACE_A), "--baseline", str(tmp_path / "nope.json")]
    )
    assert missing.exit_code == 2
