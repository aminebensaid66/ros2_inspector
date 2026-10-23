from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from ros2inspector.design.server import run_design_server

console = Console(stderr=True)


def design(
    output: Annotated[
        Path,
        typer.Option("--output", "-o", help="Directory where the package will be written"),
    ] = Path("."),
    port: Annotated[
        int,
        typer.Option(
            "--port",
            "-p",
            min=0,
            max=65535,
            help="Local port for the design server (0 = any free port)",
        ),
    ] = 8765,
    open_browser: Annotated[
        bool,
        typer.Option("--open/--no-open", help="Open the browser automatically"),
    ] = True,
) -> None:
    """Launch the visual package designer in your browser.

    Design ROS2 nodes and their topics interactively, then click
    Generate to write a ready-to-build Python package to disk.

    Examples:
      ros2inspector design
      ros2inspector design -o ~/my_ws/src
      ros2inspector design --port 9000 --no-open
    """
    run_design_server(
        port=port,
        output_dir=output.resolve(),
        open_browser=open_browser,
        console=console,
    )
