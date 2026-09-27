from __future__ import annotations

import os
import tempfile
from pathlib import Path

from ros2inspector.utils.enums import StrEnum


class OutputFormat(StrEnum):
    TABLE = "table"
    JSON = "json"
    YAML = "yaml"


def health_bar(score: int, include_score: bool = True) -> str:
    filled = round(score / 10)
    color = "green" if score >= 70 else "yellow" if score >= 40 else "red"
    bar = "█" * filled + "░" * (10 - filled)
    suffix = f" {score}/100" if include_score else ""
    return f"[{color}]{bar}[/{color}]{suffix}"


class OutputWriteError(RuntimeError):
    """A user-facing output-file failure."""


def atomic_write_text(path: Path, text: str) -> None:
    """Atomically replace an existing output file.

    Parent directories are intentionally not created: a misspelled destination is
    treated as an invocation error.  A temporary sibling is written and fsynced
    before replacement so a failed write does not truncate a previous result.
    """
    parent = path.parent
    if not parent.is_dir():
        raise OutputWriteError(f"output directory does not exist: {parent}")
    if path.exists() and path.is_dir():
        raise OutputWriteError(f"output path is a directory: {path}")
    temp_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=parent, prefix=f".{path.name}.", delete=False
        ) as stream:
            temp_name = stream.name
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
    except OSError as exc:
        if temp_name:
            try:
                Path(temp_name).unlink(missing_ok=True)
            except OSError:
                pass
        raise OutputWriteError(f"cannot write '{path}': {exc.strerror or exc}") from exc
