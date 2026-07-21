import sys
from pathlib import Path
from typing import Annotated

import orjson
import typer
import yaml
from rich import box
from rich.console import Console
from rich.table import Table

from ros2inspector.cli._output import OutputFormat, health_bar
from ros2inspector.cli._workspace import load_packages_or_exit
from ros2inspector.discovery import find_workspace_root
from ros2inspector.model.schemas import PackageMetadata, PackageType
from ros2inspector.static import score_workspace

app = typer.Typer(
    help="List and inspect package details.",
    context_settings={"allow_interspersed_args": True},
)
console = Console()
err_console = Console(stderr=True)

_FILTER_MAP: dict[str, list[PackageType]] = {
    "cpp": [PackageType.AMENT_CMAKE, PackageType.CMAKE],
    "python": [PackageType.AMENT_PYTHON, PackageType.PYTHON],
    "meta": [PackageType.META],
}


_SORT_KEYS = ("name", "score", "version")


@app.callback(invoke_without_command=True)
def packages(
    path: Annotated[
        Path, typer.Option("-C", "--path", help="Workspace root (default: CWD)")
    ] = Path("."),
    filter_type: Annotated[
        str | None,
        typer.Option("--filter", "-t", help=f"Filter by type: {', '.join(_FILTER_MAP)}"),
    ] = None,
    show_deps: Annotated[bool, typer.Option("--show-deps", help="Show dependency tree")] = False,
    fmt: Annotated[
        OutputFormat, typer.Option("--format", "-f", help="Output format: table|json|yaml")
    ] = OutputFormat.TABLE,
    sort_by: Annotated[
        str, typer.Option("--sort", "-s", help=f"Sort by: {', '.join(_SORT_KEYS)}")
    ] = "name",
) -> None:
    """List all packages with metadata and optional dependency info."""
    if sort_by not in _SORT_KEYS:
        err_console.print(
            f"[red]Error:[/red] unknown sort key '{sort_by}'. Choose from: {', '.join(_SORT_KEYS)}"
        )
        raise typer.Exit(2)

    if filter_type is not None and filter_type not in _FILTER_MAP:
        err_console.print(
            f"[red]Error:[/red] unknown filter '{filter_type}'. "
            f"Choose from: {', '.join(_FILTER_MAP)}"
        )
        raise typer.Exit(2)

    root = find_workspace_root(path.resolve())
    packages_list = load_packages_or_exit(root)

    _ = score_workspace(packages_list)

    if filter_type:
        allowed = _FILTER_MAP[filter_type]
        packages_list = [p for p in packages_list if p.package_type in allowed]

    if not packages_list:
        err_console.print(f"[yellow]No packages matching filter '{filter_type}'.[/yellow]")
        return

    if sort_by == "score":
        packages_list = sorted(packages_list, key=lambda p: p.health_score or 0, reverse=True)
    elif sort_by == "version":
        packages_list = sorted(packages_list, key=lambda p: p.version)
    else:
        packages_list = sorted(packages_list, key=lambda p: p.name)

    if fmt == OutputFormat.JSON:
        data = [pkg.model_dump(mode="json") for pkg in packages_list]
        sys.stdout.write(orjson.dumps(data, option=orjson.OPT_INDENT_2).decode() + "\n")
    elif fmt == OutputFormat.YAML:
        data = [pkg.model_dump(mode="json") for pkg in packages_list]
        sys.stdout.write(yaml.dump(data, default_flow_style=False))
    else:
        for pkg in packages_list:
            _render_package(pkg, show_deps)


def _render_package(pkg: PackageMetadata, show_deps: bool) -> None:
    table = Table(
        title=f"[bold cyan]{pkg.name}[/bold cyan] [dim]v{pkg.version}[/dim]",
        box=box.SIMPLE_HEAVY,
        show_header=False,
    )
    table.add_column("Field", style="dim", width=18)
    table.add_column("Value", no_wrap=False, overflow="fold")

    table.add_row("Type", pkg.package_type.value)
    table.add_row("License", pkg.license or "[dim]missing[/dim]")
    table.add_row("Description", pkg.description or "[dim]missing[/dim]")
    table.add_row("Maintainers", ", ".join(pkg.maintainers) or "[dim]missing[/dim]")
    table.add_row("Path", pkg.path)
    if pkg.health_score is not None:
        bar = health_bar(pkg.health_score, include_score=False)
        table.add_row("Health", f"{bar} {pkg.health_score}/100")

    if show_deps:
        for dep_type, dep_list in pkg.dependencies.items():
            table.add_row(f"deps:{dep_type.value}", ", ".join(dep_list))

    console.print(table)
