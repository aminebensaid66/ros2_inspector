import sys
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from ros2inspector.cli._state import state
from ros2inspector.cli._workspace import build_uam_or_exit
from ros2inspector.discovery import find_workspace_root
from ros2inspector.graph import render_dot, render_json, render_mermaid
from ros2inspector.utils.enums import StrEnum

app = typer.Typer(
    help="Render workspace architecture as a graph.",
    context_settings={"allow_interspersed_args": True},
)
err_console = Console(stderr=True)


class GraphType(StrEnum):
    DEPS = "deps"
    COMMS = "comms"
    FULL = "full"


class GraphFormat(StrEnum):
    MERMAID = "mermaid"
    DOT = "dot"
    JSON = "json"


@app.callback(invoke_without_command=True)
def graph(
    graph_type: Annotated[
        GraphType, typer.Argument(help="Graph type: deps | comms | full")
    ] = GraphType.DEPS,
    path: Annotated[
        Path, typer.Option("-C", "--path", help="Workspace root (default: CWD)")
    ] = Path("."),
    fmt: Annotated[
        GraphFormat, typer.Option("--format", "-f", help="Output format: mermaid|dot|json")
    ] = GraphFormat.MERMAID,
    output: Annotated[
        Path | None, typer.Option("--output", "-o", help="Write output to file")
    ] = None,
    package: Annotated[
        str | None, typer.Option("--package", "-p", help="Focus on one package and neighbors")
    ] = None,
    no_cache: Annotated[
        bool, typer.Option("--no-cache", help="Skip the incremental cache and re-parse everything")
    ] = False,
) -> None:
    """Render the workspace architecture graph.

    Examples:
      ros2inspector graph
      ros2inspector graph full
      ros2inspector graph comms --format dot
      ros2inspector graph full --format dot -o out.dot
      ros2inspector graph comms -C ~/my_ws
    """
    root = find_workspace_root(path)
    uam = build_uam_or_exit(root, use_cache=not no_cache, show_progress=not state.quiet)

    if fmt == GraphFormat.MERMAID:
        text = render_mermaid(uam, graph_type.value, package)
    elif fmt == GraphFormat.DOT:
        text = render_dot(uam, graph_type.value, package)
    else:
        text = render_json(uam, graph_type.value, package)

    if output:
        output.write_text(text, encoding="utf-8")
        err_console.print(f"[dim]Written to {output}[/dim]")
    else:
        sys.stdout.write(text)
