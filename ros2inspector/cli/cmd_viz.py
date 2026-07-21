import webbrowser
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from ros2inspector.cli._workspace import build_uam_or_exit
from ros2inspector.discovery import find_workspace_root
from ros2inspector.viz import generate_html

app = typer.Typer(
    help="Generate an interactive HTML graph of the workspace.",
    context_settings={"allow_interspersed_args": True},
)
err_console = Console(stderr=True)


@app.callback(invoke_without_command=True)
def viz(
    path: Annotated[
        Path, typer.Option("-C", "--path", help="Workspace root (default: CWD)")
    ] = Path("."),
    output: Annotated[Path, typer.Option("--output", "-o", help="Output HTML file")] = Path(
        "ros2inspector_graph.html"
    ),
    open_browser: Annotated[
        bool, typer.Option("--open/--no-open", help="Open in browser after generating")
    ] = True,
    no_cache: Annotated[
        bool, typer.Option("--no-cache", help="Skip the incremental cache and re-parse everything")
    ] = False,
) -> None:
    """Generate a self-contained interactive HTML graph.

    Examples:
      ros2inspector viz
      ros2inspector viz -C ~/my_ws -o graph.html
      ros2inspector viz --no-open
    """
    root = find_workspace_root(path.resolve())

    err_console.print(f"[dim]Scanning {root}…[/dim]")
    uam = build_uam_or_exit(root, use_cache=not no_cache)

    s = uam.summary()
    err_console.print(
        f"[dim]Found {s['packages']} packages, {s['nodes']} nodes, "
        f"{s['topics']} topics, {s['services']} services[/dim]"
    )

    html = generate_html(uam, root)
    output = output.resolve()
    output.write_text(html, encoding="utf-8")

    err_console.print(f"[green]✓[/green] Saved to [bold]{output}[/bold]")

    if open_browser:
        webbrowser.open(output.as_uri())
