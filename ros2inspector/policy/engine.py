from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

from ros2inspector.model.schemas import PolicyViolation, ViolationSeverity
from ros2inspector.model.uam import UnifiedArchitectureModel
from ros2inspector.policy.rules import _RULE_RUNNERS


class PolicyConfigError(ValueError):
    """A stable, user-facing policy validation error."""


def _validate_severity(value: object, location: str) -> None:
    if value is not None and value not in {"error", "warning", "info"}:
        raise PolicyConfigError(f"{location}: severity must be error, warning, or info")


def _validate_policy_rules(rules: object, source: Path) -> list[dict[str, Any]]:
    if not isinstance(rules, list):
        raise PolicyConfigError(f"{source}: 'rules' must be a list")
    validated: list[dict[str, Any]] = []
    for index, raw_rule in enumerate(rules, start=1):
        location = f"{source}: rule {index}"
        if not isinstance(raw_rule, dict):
            raise PolicyConfigError(f"{location} must be a mapping")
        rule = dict(raw_rule)
        rule_type = rule.get("type")
        if not isinstance(rule_type, str) or not rule_type:
            raise PolicyConfigError(f"{location}: missing non-empty 'type'")
        if rule_type not in _RULE_RUNNERS:
            supported = ", ".join(sorted(_RULE_RUNNERS))
            raise PolicyConfigError(
                f"{location}: unknown rule type '{rule_type}'. Supported types: {supported}"
            )
        for key in (
            "severity",
            "severity_no_publisher",
            "severity_no_subscriber",
            "missing_provider_severity",
            "missing_server_severity",
        ):
            _validate_severity(rule.get(key), f"{location}.{key}")
        if rule_type == "naming":
            for section in ("packages", "nodes", "topics", "services"):
                cfg = rule.get(section)
                if cfg is None:
                    continue
                if not isinstance(cfg, dict):
                    raise PolicyConfigError(f"{location}.{section} must be a mapping")
                pattern = cfg.get("pattern")
                if pattern is not None:
                    try:
                        re.compile(str(pattern))
                    except re.error as exc:
                        raise PolicyConfigError(
                            f"{location}.{section}.pattern is invalid: {exc}"
                        ) from exc
        if rule_type == "dependency":
            for section, required_keys in (
                ("forbidden", {"from", "to"}),
                ("required", {"package", "depends_on"}),
            ):
                entries = rule.get(section, [])
                if not isinstance(entries, list):
                    raise PolicyConfigError(f"{location}.{section} must be a list")
                for item_index, item in enumerate(entries, start=1):
                    if not isinstance(item, dict) or not required_keys.issubset(item):
                        needed = ", ".join(sorted(required_keys))
                        raise PolicyConfigError(
                            f"{location}.{section}[{item_index}] requires: {needed}"
                        )
        rule["_source"] = str(source)
        rule["_line"] = index
        validated.append(rule)
    return validated


def load_policy(policy_path: Path) -> list[dict[str, Any]]:
    try:
        raw = yaml.safe_load(policy_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise PolicyConfigError(f"Cannot read policy file {policy_path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise PolicyConfigError(f"Invalid YAML in {policy_path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise PolicyConfigError(f"Policy file {policy_path} must be a YAML mapping")
    version = raw.get("version", 1)
    if version != 1:
        raise PolicyConfigError(
            f"Policy file {policy_path} has unsupported version {version!r}; expected 1"
        )
    return _validate_policy_rules(raw.get("rules", []), policy_path)


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
            violations.extend(runner(uam, rule))
        except (KeyError, TypeError, ValueError, re.error) as exc:
            raise PolicyConfigError(
                f"{rule.get('_source', 'policy')}: rule {rule.get('_line', '?')} "
                f"({rule_type}) is invalid: {exc}"
            ) from exc
    return violations


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
