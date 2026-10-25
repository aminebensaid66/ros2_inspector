from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

from ros2inspector.model.schemas import PolicyViolation, ViolationSeverity
from ros2inspector.model.uam import UnifiedArchitectureModel
from ros2inspector.policy.rules import _RULE_RUNNERS, RULE_SCHEMAS
from ros2inspector.policy.schema import FieldSpec, PolicyConfigError, validate_mapping


def _validate_policy_rules(
    rules: object, source: Path, lines: list[int] | None = None
) -> list[dict[str, Any]]:
    if not isinstance(rules, list):
        raise PolicyConfigError(f"{source}: 'rules' must be a list")
    validated: list[dict[str, Any]] = []
    for index, raw_rule in enumerate(rules, start=1):
        line = lines[index - 1] if lines and index <= len(lines) else index
        location = f"{source}: rule {index}, line {line}"
        if not isinstance(raw_rule, dict):
            raise PolicyConfigError(f"{location} must be a mapping")
        rule_type = raw_rule.get("type")
        if not isinstance(rule_type, str) or not rule_type:
            raise PolicyConfigError(f"{location}: missing non-empty 'type'")
        if rule_type not in RULE_SCHEMAS:
            raise PolicyConfigError(f"{location}: unknown rule type '{rule_type}'")
        validate_mapping(raw_rule, RULE_SCHEMAS[rule_type], f"{location} ({rule_type})")
        rule = dict(raw_rule)
        rule["_source"] = str(source)
        rule["_line"] = line
        validated.append(rule)
    return validated


def load_policy(policy_path: Path) -> list[dict[str, Any]]:
    try:
        source_text = policy_path.read_text(encoding="utf-8")
        raw = yaml.safe_load(source_text)
        root_node = yaml.compose(source_text)
    except UnicodeDecodeError as exc:
        raise PolicyConfigError(f"policy file is not valid UTF-8: {policy_path}") from exc
    except OSError as exc:
        raise PolicyConfigError(f"Cannot read policy file {policy_path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise PolicyConfigError(f"Invalid YAML in {policy_path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise PolicyConfigError(f"Policy file {policy_path} must be a YAML mapping")
    validate_mapping(
        raw,
        {"version": FieldSpec("int", minimum=1, maximum=1), "rules": FieldSpec("list")},
        str(policy_path),
    )
    lines: list[int] = []
    if isinstance(root_node, yaml.MappingNode):
        for key, value in root_node.value:
            if isinstance(key, yaml.ScalarNode) and key.value == "rules":
                if isinstance(value, yaml.SequenceNode):
                    lines = [node.start_mark.line + 1 for node in value.value]
                break
    return _validate_policy_rules(raw.get("rules", []), policy_path, lines)


def run_policy(
    uam: UnifiedArchitectureModel,
    rules: list[dict[str, Any]],
) -> list[PolicyViolation]:
    violations: list[PolicyViolation] = []
    for rule in rules:
        rule_type = str(rule.get("type", ""))
        runner = _RULE_RUNNERS.get(rule_type)
        if runner is None:
            violations.append(
                PolicyViolation(
                    severity=ViolationSeverity.WARNING,
                    rule_type="unknown_rule",
                    message=f"Unknown rule type '{rule_type}' — skipped",
                    policy_file=str(rule.get("_source", "policy")),
                    policy_line=int(rule.get("_line", 0)) or None,
                )
            )
            continue
        try:
            produced = runner(uam, rule)
            for violation in produced:
                if violation.policy_line is None:
                    violation.policy_line = int(rule.get("_line", 0)) or None
            violations.extend(produced)
        except (KeyError, TypeError, ValueError, re.error) as exc:
            raise PolicyConfigError(
                f"{rule.get('_source', 'policy')}: rule {rule.get('_line', '?')} "
                f"({rule_type}) is invalid: {exc}"
            ) from exc
    attach_source_locations(uam, violations)
    return violations


def attach_source_locations(
    uam: UnifiedArchitectureModel, violations: list[PolicyViolation]
) -> None:
    """Attach a source location when a finding names an unambiguous graph entity."""
    graph = uam.graph
    for violation in violations:
        if violation.file_path or not violation.affected_entities:
            continue
        entity = violation.affected_entities[0]
        matches = [
            node_id for node_id, attrs in graph.nodes(data=True) if attrs.get("name") == entity
        ]
        locations: set[tuple[str, int]] = set()
        for node_id in matches:
            attrs = graph.nodes[node_id]
            if attrs.get("file_path") and attrs.get("line"):
                locations.add((str(attrs["file_path"]), int(attrs["line"])))
            for _, _, edge in graph.in_edges(node_id, data=True):
                if edge.get("file_path") and edge.get("line"):
                    locations.add((str(edge["file_path"]), int(edge["line"])))
        if len(locations) == 1:
            violation.file_path, violation.line = next(iter(locations))


def violation_summary(violations: list[PolicyViolation]) -> dict[str, int]:
    summary: dict[str, int] = {"errors": 0, "warnings": 0, "info": 0}
    for violation in violations:
        if violation.severity == ViolationSeverity.ERROR:
            summary["errors"] += 1
        elif violation.severity == ViolationSeverity.WARNING:
            summary["warnings"] += 1
        else:
            summary["info"] += 1
    return summary


__all__ = ["PolicyConfigError", "load_policy", "run_policy", "violation_summary"]
