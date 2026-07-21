from __future__ import annotations

from collections import deque
from collections.abc import Collection
from pathlib import Path

_STOP_MARKERS = frozenset({"COLCON_IGNORE", ".rosignore"})
DEFAULT_EXCLUDED_DIRS = frozenset(
    {
        "build",
        "install",
        "log",
        ".venv",
        "venv",
        "env",
        ".tox",
        ".nox",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        "__pycache__",
        "node_modules",
        "dist",
        ".git",
        ".hg",
        ".svn",
    }
)


def source_search_root(root: Path) -> Path:
    """Return the canonical source tree for a workspace.

    A normal colcon workspace is scanned through ``<workspace>/src``.  When the
    caller already points at ``src`` or at a standalone package tree, that path
    is used unchanged.
    """
    resolved = root.expanduser().resolve()
    src = resolved / "src"
    return src if resolved.name != "src" and src.is_dir() else resolved


def find_package_xml_files(
    root: Path,
    *,
    excluded_dirs: Collection[str] = DEFAULT_EXCLUDED_DIRS,
) -> list[Path]:
    """Find source ``package.xml`` files deterministically.

    Generated colcon output, virtual environments, VCS metadata, caches, and
    frontend dependencies are excluded before traversal.  Package directories
    are terminal: nested packages below a discovered package are not scanned.
    """
    scan_root = source_search_root(root)
    results: list[Path] = []
    queue: deque[Path] = deque([scan_root])
    excluded = frozenset(excluded_dirs)

    while queue:
        current = queue.popleft()
        if not current.is_dir() or current.name in excluded:
            continue
        if any((current / marker).exists() for marker in _STOP_MARKERS):
            continue

        pkg_xml = current / "package.xml"
        if pkg_xml.is_file():
            results.append(pkg_xml.resolve())
            continue

        try:
            children = sorted(current.iterdir(), key=lambda p: p.name)
        except OSError:
            continue
        for child in children:
            if child.is_dir() and not child.name.startswith(".") and child.name not in excluded:
                queue.append(child)

    return results
