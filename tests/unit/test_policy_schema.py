from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from ros2inspector.cli.app import app
from ros2inspector.policy.engine import PolicyConfigError, load_policy
from ros2inspector.policy.rules import _RULE_RUNNERS

WORKSPACE = Path(__file__).parents[1] / "fixtures/workspaces/workspace_a"


@pytest.mark.parametrize(
    ("rule", "field"),
    [
        ({"type": "license", "allowed": "SUBMITTED"}, "allowed"),
        ({"type": "health_threshold", "min_scor": 100}, "min_scor"),
        ({"type": "license", "severity": ["error"]}, "severity"),
        ({"type": "license", "severity": {"bad": "value"}}, "severity"),
        ({"type": "topic_connectivity", "no_subscriber": "false"}, "no_subscriber"),
    ],
)
def test_invalid_policy_fields_have_config_exit(
    tmp_path: Path, rule: dict[str, object], field: str
) -> None:
    policy = tmp_path / "policy.yaml"
    policy.write_text(yaml.safe_dump({"version": 1, "rules": [rule]}))
    result = CliRunner().invoke(
        app, ["--quiet", "validate", str(WORKSPACE), "--policy", str(policy), "-f", "json"]
    )
    assert result.exit_code == 2, result.output
    assert result.stdout.strip() == ""
    assert field in result.stderr
    assert "Traceback" not in result.output
    assert len(result.stderr.strip().splitlines()) == 1


def test_policy_encoding_has_config_exit(tmp_path: Path) -> None:
    policy = tmp_path / "policy.yaml"
    policy.write_bytes(b"version: 1\n# caf\xe9\nrules: []\n")
    result = CliRunner().invoke(
        app, ["--quiet", "validate", str(WORKSPACE), "--policy", str(policy), "-f", "json"]
    )
    assert result.exit_code == 2, result.output
    assert result.stdout.strip() == ""
    assert "UTF-8" in result.stderr
    assert "Traceback" not in result.output


@pytest.mark.parametrize(
    "rule",
    [
        {"type": "license", "allowed": [""]},
        {"type": "health_threshold", "min_score": True},
        {"type": "health_threshold", "min_score": 101},
        {"type": "health_threshold", "workspace_min_score": -1},
        {"type": "maintainer_required", "require_email": "false"},
        {"type": "naming", "nodes": {"severity": ["error"]}},
        {"type": "naming", "nodes": {"patern": "abc"}},
        {"type": "naming", "topics": {"pattern": "["}},
        {"type": "dependency", "required": [{"package": "a", "depends_on": "b", "severity": {}}]},
        {"type": "dependency", "forbidden": [{"from": "a", "too": "b"}]},
    ],
)
def test_policy_schema_rejects_nested_and_wrong_type_values(
    tmp_path: Path, rule: dict[str, object]
) -> None:
    policy = tmp_path / "policy.yaml"
    policy.write_text(yaml.safe_dump({"rules": [rule]}))
    with pytest.raises(PolicyConfigError) as exc:
        load_policy(policy)
    assert "line " in str(exc.value)


def test_unknown_key_names_rule_line_and_suggestion(tmp_path: Path) -> None:
    policy = tmp_path / "policy.yaml"
    policy.write_text("version: 1\nrules:\n  - type: health_threshold\n    min_scor: 90\n")
    with pytest.raises(PolicyConfigError) as exc:
        load_policy(policy)
    message = str(exc.value)
    for expected in ["rule 1", "line 3", "health_threshold", "min_scor", "min_score"]:
        assert expected in message


def test_rule_schema_covers_runner_reads() -> None:
    from ros2inspector.policy.rules import RULE_SCHEMAS

    assert set(RULE_SCHEMAS) == set(_RULE_RUNNERS)
    for name, runner in _RULE_RUNNERS.items():
        schema = RULE_SCHEMAS[name]
        for node in ast.walk(ast.parse(inspect.getsource(runner))):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if (
                node.func.attr != "get"
                or not isinstance(node.func.value, ast.Name)
                or not node.args
            ):
                continue
            key = node.args[0]
            if not isinstance(key, ast.Constant) or not isinstance(key.value, str):
                continue
            receiver = node.func.value.id
            if receiver == "cfg":
                assert key.value in schema, (name, key.value)
            elif receiver == "sub" and name == "naming":
                for section in ("packages", "nodes", "topics", "services"):
                    assert key.value in schema[section].fields
            elif receiver in {"forbidden", "required"} and name == "dependency":
                assert schema[receiver].item is not None
                assert key.value in schema[receiver].item.fields


def test_factory_encoding_is_a_config_error(tmp_path: Path) -> None:
    from ros2inspector.static.factory_patterns import load_factory_patterns

    patterns = tmp_path / "patterns.yaml"
    patterns.write_bytes(b"# caf\xe9\nfactories: []\n")
    with pytest.raises(ValueError, match="UTF-8"):
        load_factory_patterns(patterns, None)
