from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field


class PolicyConfigError(ValueError):
    """A user-facing policy configuration error."""


@dataclass(frozen=True)
class FieldSpec:
    kind: str
    fields: dict[str, FieldSpec] = field(default_factory=dict)
    item: FieldSpec | None = None
    required: frozenset[str] = frozenset()
    minimum: int | None = None
    maximum: int | None = None


def validate_mapping(
    value: object,
    fields: dict[str, FieldSpec],
    location: str,
    required: frozenset[str] = frozenset(),
) -> None:
    if not isinstance(value, dict):
        raise PolicyConfigError(f"{location} must be a mapping")
    for key in value:
        if not isinstance(key, str) or key not in fields:
            matches = difflib.get_close_matches(str(key), fields, n=1)
            hint = f"; did you mean '{matches[0]}'?" if matches else ""
            raise PolicyConfigError(f"{location}: unknown key {key!r}{hint}")
    for key in required:
        if key not in value:
            raise PolicyConfigError(f"{location}: missing required key '{key}'")
    for key, item in value.items():
        validate_field(item, fields[key], f"{location}.{key}")


def validate_field(value: object, spec: FieldSpec, location: str) -> None:
    if spec.kind == "mapping":
        validate_mapping(value, spec.fields, location, spec.required)
    elif spec.kind == "list":
        if not isinstance(value, list):
            raise PolicyConfigError(f"{location} must be a list")
        if spec.item is not None:
            for index, item in enumerate(value, start=1):
                validate_field(item, spec.item, f"{location}[{index}]")
    elif spec.kind == "bool":
        if not isinstance(value, bool):
            raise PolicyConfigError(f"{location} must be a boolean")
    elif spec.kind == "int":
        if not isinstance(value, int) or isinstance(value, bool):
            raise PolicyConfigError(f"{location} must be an integer")
        if (spec.minimum is not None and value < spec.minimum) or (
            spec.maximum is not None and value > spec.maximum
        ):
            raise PolicyConfigError(f"{location} must be between {spec.minimum} and {spec.maximum}")
    elif spec.kind in {"string", "severity", "pattern"}:
        if not isinstance(value, str) or not value.strip():
            raise PolicyConfigError(f"{location} must be a non-empty string")
        if spec.kind == "severity" and value not in {"error", "warning", "info"}:
            raise PolicyConfigError(f"{location} must be error, warning, or info")
        if spec.kind == "pattern":
            try:
                re.compile(value)
            except re.error as exc:
                raise PolicyConfigError(f"{location} is invalid: {exc}") from exc
