"""SARIF 2.1.0 export and baseline filtering for policy findings."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import orjson

from ros2inspector.model.schemas import PolicyViolation, ViolationSeverity

_SARIF_SCHEMA = "https://json.schemastore.org/sarif-2.1.0.json"
_LEVELS = {
    ViolationSeverity.ERROR: "error",
    ViolationSeverity.WARNING: "warning",
    ViolationSeverity.INFO: "note",
}


class BaselineError(ValueError):
    """The baseline file is missing or is not an audit JSON report."""


_TRAILING_DETAIL = re.compile(r"\s*\([^()]*\)\s*$")
_NUMBER = re.compile(r"\d+")


def _stable_message(violation: PolicyViolation) -> str:
    """Message with run-to-run volatile parts removed.

    Connectivity messages end with a parenthesised list of the other nodes
    involved, and health messages embed the score and threshold; neither
    changes which problem the finding describes.
    """
    message = violation.message
    if violation.rule_type == "health_threshold":
        return _NUMBER.sub("#", message)
    return _TRAILING_DETAIL.sub("", message)


def fingerprint(violation: PolicyViolation) -> str:
    """Stable identity of a finding, independent of line numbers, severity and scores."""
    entities = ",".join(sorted(violation.affected_entities))
    return f"{violation.rule_type}|{_stable_message(violation)}|{entities}"


def load_baseline(path: Path) -> set[str]:
    """Read fingerprints from a previous ``audit --format json`` report."""
    try:
        data = orjson.loads(path.read_bytes())
    except (OSError, orjson.JSONDecodeError) as exc:
        raise BaselineError(f"cannot read baseline {path}: {exc}") from exc
    findings = data.get("findings") if isinstance(data, dict) else None
    if not isinstance(findings, list):
        raise BaselineError(f"{path} is not an audit JSON report (no 'findings' list)")
    prints: set[str] = set()
    for item in findings:
        try:
            prints.add(fingerprint(PolicyViolation.model_validate(item)))
        except ValueError as exc:
            raise BaselineError(f"invalid finding in baseline {path}: {exc}") from exc
    return prints


def apply_baseline(violations: list[PolicyViolation], baseline: set[str]) -> list[PolicyViolation]:
    return [v for v in violations if fingerprint(v) not in baseline]


def _uri(file_path: str, root: Path | None) -> str:
    path = Path(file_path)
    if root is not None and path.is_absolute():
        try:
            path = path.relative_to(root.resolve())
        except ValueError:
            pass
    return path.as_posix()


def to_sarif(
    violations: list[PolicyViolation], version: str, root: Path | None = None
) -> dict[str, Any]:
    rule_ids = sorted({v.rule_type for v in violations})
    results: list[dict[str, Any]] = []
    for v in violations:
        result: dict[str, Any] = {
            "ruleId": v.rule_type,
            "level": _LEVELS.get(v.severity, "warning"),
            "message": {"text": v.message},
            "partialFingerprints": {"ros2inspector/v1": fingerprint(v)},
        }
        if v.file_path:
            physical: dict[str, Any] = {"artifactLocation": {"uri": _uri(v.file_path, root)}}
            if v.line:
                physical["region"] = {"startLine": v.line}
            result["locations"] = [{"physicalLocation": physical}]
        results.append(result)
    return {
        "$schema": _SARIF_SCHEMA,
        "version": "2.1.0",
        "runs": [
            {
                "tool": {
                    "driver": {
                        "name": "ros2inspector",
                        "version": version,
                        "informationUri": "https://github.com/aminebensaid66/ros2_inspector",
                        "rules": [{"id": rid, "name": rid} for rid in rule_ids],
                    }
                },
                "results": results,
            }
        ],
    }
