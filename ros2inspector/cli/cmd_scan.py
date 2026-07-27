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
from ros2inspector.cli._state import state
from ros2inspector.cli._workspace import load_packages_or_exit
from ros2inspector.discovery import find_workspace_root, get_ros_distro
from ros2inspector.model.schemas import PackageMetadata
from ros2inspector.static import score_workspace, workspace_aggregate_score

app = typer.Typer(
    help="Discover workspace and packages.",
    context_settings={"allow_interspersed_args": True},
)
console = Console()
err_console = Console(stderr=True)


@app.callback(invoke_without_command=True)
def scan(
    path: Annotated[Path, typer.Argument(help="Workspace root (default: CWD)")] = Path("."),
    fmt: Annotated[
        OutputFormat, typer.Option("--format", "-f", help="Output format: table|json|yaml")
    ] = OutputFormat.TABLE,
) -> None:
    """Scan a ROS 2 workspace and display a summary."""
    root = find_workspace_root(path)
    distro = get_ros_distro()

    # For machine-readable formats send the diagnostic header to stderr so stdout stays clean
    if not state.quiet:
        diag = console if fmt == OutputFormat.TABLE else err_console
        diag.print(f"\n[bold cyan]ROS2 Inspector[/bold cyan] — scanning [dim]{root}[/dim]")
        if distro:
            diag.print(f"[dim]ROS distro: {distro}[/dim]\n")

    packages = load_packages_or_exit(root)

    scores = score_workspace(packages)

    if fmt == OutputFormat.TABLE:
        _render_table(packages, scores)
    elif fmt == OutputFormat.JSON:
        data = [p.model_dump(mode="json") for p in packages]
        sys.stdout.write(orjson.dumps(data, option=orjson.OPT_INDENT_2).decode() + "\n")
    else:
        data = [p.model_dump(mode="json") for p in packages]
        sys.stdout.write(yaml.dump(data, default_flow_style=False))


def _render_table(packages: list[PackageMetadata], scores: dict[str, int]) -> None:
    table = Table(title="Packages Found", box=box.ROUNDED, show_lines=True)
    table.add_column("Name", style="bold cyan", no_wrap=True)
    table.add_column("Version", style="green")
    table.add_column("Type", style="magenta")
    table.add_column("License")
    table.add_column("Maintainers")
    table.add_column("Health", no_wrap=True)

    for pkg in sorted(packages, key=lambda p: p.name):
        maintainers = ", ".join(pkg.maintainers) if pkg.maintainers else "[dim]—[/dim]"
        license_val = pkg.license or "[dim]—[/dim]"
        score = scores.get(pkg.name, 0)
        bar = health_bar(score, include_score=False)
        table.add_row(
            pkg.name,
            pkg.version,
            pkg.package_type.value,
            license_val,
            maintainers,
            f"{bar} {score}/100",
        )

    console.print(table)
    agg = workspace_aggregate_score(scores)
    agg_bar = health_bar(agg, include_score=False)
    console.print(
        f"[bold]Total:[/bold] {len(packages)} package(s)  |  "
        f"[bold]Workspace health:[/bold] {agg_bar} {agg}/100\n"
    )
