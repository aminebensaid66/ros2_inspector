from __future__ import annotations

import os
from collections.abc import Iterable, Iterator
from fnmatch import fnmatch
from pathlib import Path

_DEFAULT_PRUNE_DIRS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".tox",
        ".venv",
        "venv",
        "__pycache__",
        "build",
        "install",
        "log",
        "node_modules",
    }
)
_IGNORE_MARKERS = frozenset({"COLCON_IGNORE", ".rosignore"})
_IGNORE_FILE = ".ros2inspectorignore"


def _load_patterns(root: Path) -> tuple[str, ...]:
    path = root / _IGNORE_FILE
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return ()
    return tuple(
        line.strip().replace("\\", "/")
        for line in lines
        if line.strip() and not line.lstrip().startswith("#")
    )


def _matches_ignore(relative: Path, patterns: tuple[str, ...]) -> bool:
    value = relative.as_posix().lstrip("./")
    for pattern in patterns:
        pattern = pattern.lstrip("./")
        if pattern.endswith("/"):
            prefix = pattern.rstrip("/")
            if value == prefix or value.startswith(f"{prefix}/"):
                return True
        elif fnmatch(value, pattern) or fnmatch(relative.name, pattern):
            return True
    return False


def iter_package_files(
    root: Path,
    *,
    suffixes: Iterable[str] | None = None,
    names: Iterable[str] | None = None,
) -> Iterator[Path]:
    """Yield files under a package while pruning generated/ignored trees.

    ``.ros2inspectorignore`` supports simple shell-style glob patterns relative
    to the package root. Directory patterns ending in ``/`` prune the whole
    subtree before it is traversed.
    """
    suffix_set = frozenset(suffixes or ())
    name_set = frozenset(names or ())
    patterns = _load_patterns(root)

    for dirpath_str, dirnames, filenames in os.walk(root):
        dirpath = Path(dirpath_str)
        relative_dir = dirpath.relative_to(root)

        if dirpath != root and any((dirpath / marker).exists() for marker in _IGNORE_MARKERS):
            dirnames[:] = []
            continue

        kept_dirs: list[str] = []
        for dirname in dirnames:
            relative = relative_dir / dirname
            if dirname in _DEFAULT_PRUNE_DIRS or _matches_ignore(relative, patterns):
                continue
            kept_dirs.append(dirname)
        dirnames[:] = kept_dirs

        for filename in filenames:
            path = dirpath / filename
            relative = relative_dir / filename
            if _matches_ignore(relative, patterns):
                continue
            if (
                (not suffix_set and not name_set)
                or filename in name_set
                or path.suffix in suffix_set
            ):
                yield path
