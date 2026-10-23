from __future__ import annotations

import importlib.util
import json
import os
import platform
import sys
from importlib.resources import files
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.table import Table

from ros2inspector import __version__
from ros2inspector.cache.analysis_cache import _DEFAULT_CACHE_DIR
from ros2inspector.cli._workspace import pick_path
from ros2inspector.discovery import (
    DEFAULT_EXCLUDED_DIRS,
    WorkspaceAccessError,
    find_package_xml_files,
    get_ros_distro,
    source_search_root,
)
from ros2inspector.static.package_xml import parse_package_xml

console = Console()

_OK, _WARN, _FAIL = "ok", "warn", "fail"
_STATUS_STYLE = {_OK: "[green]OK[/green]", _WARN: "[yellow]WARN[/yellow]", _FAIL: "[red]FAIL[/red]"}
# Optional modules that individual commands need at runtime.
_RUNTIME_MODULES = {"jinja2": "design, docs", "tree_sitter_cpp": "C++ parsing"}


def _check(name: str, status: str, value: object, hint: str = "") -> dict[str, Any]:
    return {"check": name, "status": status, "ok": status != _FAIL, "value": value, "hint": hint}


def _find_root_quietly(start: Path) -> Path | None:
    for parent in [start, *start.parents]:
        try:
            if (parent / "src").is_dir() or (parent / "install" / "setup.bash").exists():
                return parent
        except OSError:
            return None
    return None


def _nearest_existing(path: Path) -> Path:
    for candidate in [path, *path.parents]:
        if candidate.exists():
            return candidate
    return path


def _cache_info(cache_dir: Path) -> dict[str, Any]:
    """Describe the cache without creating it (a diagnostic must be read-only)."""
    existing = _nearest_existing(cache_dir)
    writable = os.access(existing, os.W_OK | os.X_OK) if existing.is_dir() else False
    info: dict[str, Any] = {
        "directory": str(cache_dir),
        "exists": cache_dir.is_dir(),
        "writable": writable,
    }
    if cache_dir.is_dir():
        info["size_bytes"] = sum(
            p.stat().st_size for p in cache_dir.rglob("*") if p.is_file() and not p.is_symlink()
        )
    return info


def collect(requested: Path) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    exists = requested.is_dir()
    checks.append(
        _check(
            "workspace_exists",
            _OK if exists else _FAIL,
            str(requested),
            "" if exists else "Pass the workspace directory (the one containing src/).",
        )
    )

    root: Path | None = None
    source_root: Path | None = None
    if exists:
        root = _find_root_quietly(requested)
        checks.append(
            _check(
                "workspace_root",
                _OK if root else _WARN,
                str(root or requested),
                "" if root else "No src/ or install/setup.bash above this path; using it as root.",
            )
        )
        root = root or requested
        try:
            source_root = source_search_root(root)
            package_files = find_package_xml_files(root)
        except WorkspaceAccessError as exc:
            checks.append(_check("workspace_readable", _FAIL, str(exc), "Fix the permissions."))
            source_root, package_files = None, []
        parsed = [(path, parse_package_xml(path)) for path in package_files]
        broken = [str(path) for path, package in parsed if package is None]
        by_name: dict[str, list[str]] = {}
        for path, package in parsed:
            if package is not None:
                by_name.setdefault(package.name, []).append(str(path.parent))
        duplicates = {name: paths for name, paths in by_name.items() if len(paths) > 1}
        checks.append(
            _check(
                "packages",
                _OK if by_name else _FAIL,
                len(by_name),
                "" if by_name else f"No package.xml found under {source_root}.",
            )
        )
        checks.append(
            _check(
                "unparsable_package_xml",
                _WARN if broken else _OK,
                broken or 0,
                "These packages are skipped by every command." if broken else "",
            )
        )
        checks.append(
            _check(
                "duplicate_package_names",
                _FAIL if duplicates else _OK,
                duplicates or 0,
                "Analysis commands refuse ambiguous workspaces; remove or COLCON_IGNORE one copy."
                if duplicates
                else "",
            )
        )

    distro = get_ros_distro()
    checks.append(
        _check(
            "ros_distro",
            _OK if distro else _WARN,
            distro or "not set",
            "" if distro else "Not required for static analysis; source ROS 2 for runtime tools.",
        )
    )

    try:
        asset = files("ros2inspector.viz").joinpath("_cytoscape.min.js")
        asset_ok = asset.is_file() and len(asset.read_bytes()) > 1000
    except OSError:
        asset_ok = False
    checks.append(
        _check(
            "offline_visualization_asset",
            _OK if asset_ok else _FAIL,
            asset_ok,
            "" if asset_ok else "Reinstall ros2inspector; the viz command needs this file.",
        )
    )
    for module, used_by in _RUNTIME_MODULES.items():
        present = importlib.util.find_spec(module) is not None
        checks.append(
            _check(
                f"module_{module}",
                _OK if present else _FAIL,
                present,
                "" if present else f"Needed by {used_by}; reinstall ros2inspector.",
            )
        )

    cache = _cache_info(_DEFAULT_CACHE_DIR)
    checks.append(
        _check(
            "cache_writable",
            _OK if cache["writable"] else _WARN,
            cache["directory"],
            "" if cache["writable"] else "Use --no-cache or fix permissions on the cache dir.",
        )
    )

    return {
        "ros2inspector_version": __version__,
        "python": platform.python_version(),
        "python_executable": sys.executable,
        "platform": platform.platform(),
        "ros_distro": distro,
        "workspace_root": str(root) if root else None,
        "source_root": str(source_root) if source_root else None,
        "ignored_directories": sorted(DEFAULT_EXCLUDED_DIRS),
        "cache": cache,
        "checks": checks,
    }


def doctor(
    path_arg: Annotated[
        Path | None, typer.Argument(metavar="[PATH]", help="Workspace root (default: CWD)")
    ] = None,
    path: Annotated[
        Path | None, typer.Option("-C", "--path", help="Workspace root (same as PATH)")
    ] = None,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit machine-readable JSON"),
    ] = False,
) -> None:
    """Report versions, workspace layout, cache, and installation health.

    Exits 1 when any check FAILs. WARN results are informational.
    """
    requested = pick_path(path_arg, path).expanduser().resolve()
    data = collect(requested)
    checks: list[dict[str, Any]] = data["checks"]
    if json_output:
        sys.stdout.write(json.dumps(data, indent=2, sort_keys=True) + "\n")
    else:
        table = Table(title="ROS 2 Inspector Diagnostics")
        table.add_column("Check")
        table.add_column("Status")
        table.add_column("Value", overflow="fold")
        table.add_column("Hint", overflow="fold")
        for check in checks:
            table.add_row(
                str(check["check"]),
                _STATUS_STYLE[check["status"]],
                str(check["value"]),
                check["hint"],
            )
        console.print(table)
        console.print(f"Version: {__version__}  Python: {data['python']} ({sys.executable})")
        if data["source_root"]:
            console.print(f"Source root: {data['source_root']}")
        console.print(f"Ignored by default: {', '.join(data['ignored_directories'])}")
    if any(check["status"] == _FAIL for check in checks):
        raise typer.Exit(1)
