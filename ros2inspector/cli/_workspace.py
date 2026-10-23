from __future__ import annotations

from pathlib import Path
from typing import NoReturn

import typer
import yaml
from rich.console import Console

from ros2inspector.cli._state import state
from ros2inspector.discovery import (
    DuplicatePackageError,
    NoPackagesFoundError,
    WorkspaceAnalysisError,
    find_package_xml_files,
)
from ros2inspector.model.schemas import PackageMetadata
from ros2inspector.model.uam import UAM, UnifiedArchitectureModel
from ros2inspector.static import parse_package_xml

_err_console = Console(stderr=True)


def _exit_workspace_error(exc: WorkspaceAnalysisError) -> NoReturn:
    _err_console.print(f"[red]Workspace error:[/red] {exc}")
    raise typer.Exit(3) from exc


def load_packages_or_exit(root: Path) -> list[PackageMetadata]:
    """Load a non-empty, unambiguous package set or terminate the CLI cleanly."""
    packages = [
        package
        for package_xml in find_package_xml_files(root)
        if (package := parse_package_xml(package_xml)) is not None
    ]
    if not packages:
        _exit_workspace_error(NoPackagesFoundError(root))

    by_name: dict[str, list[Path]] = {}
    for package in packages:
        by_name.setdefault(package.name, []).append(Path(package.path).resolve())
    duplicates = {name: paths for name, paths in by_name.items() if len(paths) > 1}
    if duplicates:
        _exit_workspace_error(DuplicatePackageError(duplicates))
    return packages


def build_uam(
    root: Path,
    *,
    use_cache: bool,
    show_progress: bool = False,
) -> UnifiedArchitectureModel:
    """Build the model with the global CLI options (tests, factory patterns, preset)."""
    return UAM.build(
        root,
        use_cache=use_cache,
        show_progress=show_progress,
        include_tests=state.include_tests,
        factory_patterns=Path(state.factory_patterns) if state.factory_patterns else None,
        preset=state.preset,
    )


def build_uam_or_exit(
    root: Path,
    *,
    use_cache: bool,
    show_progress: bool = False,
) -> UnifiedArchitectureModel:
    """Build the model and map user-actionable workspace failures to exit code 3."""
    try:
        return build_uam(root, use_cache=use_cache, show_progress=show_progress)
    except WorkspaceAnalysisError as exc:
        _exit_workspace_error(exc)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        _err_console.print(f"[red]Configuration error:[/red] {exc}")
        raise typer.Exit(2) from exc


def pick_path(argument: Path | None, option: Path | None) -> Path:
    """Accept the workspace either as a positional PATH or as ``-C/--path``.

    ``scan``/``audit``/``validate`` take a positional path while ``nodes``/``graph``/``viz``
    take ``-C``; the newer commands accept both so either habit works.
    """
    if argument is not None and option is not None:
        if argument.expanduser().resolve() != option.expanduser().resolve():
            _err_console.print(
                f"[red]Error:[/red] got two different workspace paths: '{argument}' and '{option}'"
            )
            raise typer.Exit(2)
    return (argument or option or Path(".")).expanduser()
