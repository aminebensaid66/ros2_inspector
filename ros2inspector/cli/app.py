from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from ros2inspector import __version__
from ros2inspector.cli import (
    cmd_audit,
    cmd_graph,
    cmd_nodes,
    cmd_packages,
    cmd_scan,
    cmd_validate,
    cmd_viz,
)
from ros2inspector.cli._state import state

app = typer.Typer(
    name="ros2inspector",
    help="Static ROS 2 architecture analysis, visualization, and policy checks.",
    add_completion=False,
)
_console = Console(stderr=True)


def version_callback(value: bool) -> None:
    if value:
        _console.print(f"ros2inspector v{__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: bool = typer.Option(
        False, "--version", "-v", callback=version_callback, is_eager=True
    ),
    quiet: Annotated[
        bool,
        typer.Option("--quiet", "-q", help="Suppress diagnostic headers and progress output"),
    ] = False,
    include_tests: Annotated[
        bool, typer.Option("--include-tests", help="Include nodes defined in test sources")
    ] = False,
    factory_patterns: Annotated[
        Path | None, typer.Option("--factory-patterns", help="YAML endpoint factory patterns")
    ] = None,
    preset: Annotated[
        str | None, typer.Option("--preset", help="Endpoint factory preset (nav2)")
    ] = None,
) -> None:
    if preset not in {None, "nav2"}:
        raise typer.BadParameter(
            f"unknown factory preset '{preset}'; supported: nav2", param_hint="--preset"
        )
    state.quiet = quiet
    state.include_tests = include_tests
    state.factory_patterns = str(factory_patterns) if factory_patterns else None
    state.preset = preset


app.add_typer(cmd_scan.app, name="scan")
app.add_typer(cmd_packages.app, name="packages")
app.add_typer(cmd_nodes.app, name="nodes")
app.add_typer(cmd_graph.app, name="graph")
app.add_typer(cmd_viz.app, name="viz")
app.add_typer(cmd_audit.app, name="audit")
app.add_typer(cmd_validate.app, name="validate")


def run() -> None:
    app()
