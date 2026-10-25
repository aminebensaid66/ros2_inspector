from __future__ import annotations

import ast
from pathlib import Path
from typing import Any


def source_failure(path: Path, error: BaseException, diagnostics: list[dict[str, Any]]) -> None:
    diagnostics.append(
        {
            "severity": "warning",
            "code": "source_parse_failed",
            "file": str(path),
            "message": f"{type(error).__name__}: {error}",
        }
    )


def read_python_tree(path: Path, diagnostics: list[dict[str, Any]]) -> ast.Module | None:
    try:
        return ast.parse(path.read_bytes(), filename=str(path))
    except (SyntaxError, UnicodeDecodeError, OSError, RecursionError, MemoryError) as exc:
        source_failure(path, exc, diagnostics)
        return None
