from __future__ import annotations

import sys
from pathlib import Path
from typing import Annotated

import orjson
import typer
import yaml
from rich import box
from rich.console import Console
from rich.table import Table

from ros2inspector.cli._output import OutputFormat
from ros2inspector.cli._state import state
from ros2inspector.cli._workspace import build_uam_or_exit
from ros2inspector.discovery import find_workspace_root
from ros2inspector.model.schemas import PolicyViolation, ViolationSeverity
from ros2inspector.policy import PolicyConfigError, load_policy, run_policy, violation_summary
from ros2inspector.static import workspace_aggregate_score

app = typer.Typer(
    help="Validate workspace against a policy file.",
    context_settings={"allow_interspersed_args": True},
)
console = Console()
err_console = Console(stderr=True)

_SEVERITY_COLOR = {
    ViolationSeverity.ERROR: "red",
    ViolationSeverity.WARNING: "yellow",
    ViolationSeverity.INFO: "cyan",
}


@app.callback(invoke_without_command=True)
def validate(
    path: Annotated[Path, typer.Argument(help="Workspace root (default: CWD)")] = Path("."),
    policy: Annotated[
        Path,
        typer.Option("--policy", "-p", help="Path to policy YAML file"),
    ] = Path("ros2inspector_policy.yaml"),
    fail_on: Annotated[
        str,
        typer.Option("--fail-on", help="Min severity for non-zero exit: error|warning|info"),
    ] = "error",
    fmt: Annotated[
        OutputFormat, typer.Option("--format", "-f", help="Output format: table|json|yaml")
    ] = OutputFormat.TABLE,
    no_cache: Annotated[
        bool, typer.Option("--no-cache", help="Skip the incremental cache")
    ] = False,
) -> None:
    """Validate the workspace against a ros2inspector policy file.

    Exit codes:
      0 — no violations at or above --fail-on severity
      1 — violations found at or above --fail-on severity
      2 — invocation error
      3 — workspace or policy not found

    Examples:
      ros2inspector validate
      ros2inspector validate --policy my_policy.yaml
      ros2inspector validate --fail-on warning
      ros2inspector validate --format json
    """
    if fail_on not in ("error", "warning", "info"):
        err_console.print("[red]Error:[/red] --fail-on must be error, warning, or info")
        raise typer.Exit(2)

    policy_path = policy if policy.is_absolute() else Path.cwd() / policy
    if not policy_path.exists():
        err_console.print(f"[red]Error:[/red] policy file not found: {policy_path}")
        raise typer.Exit(3)

    root = find_workspace_root(path.resolve())

    if not state.quiet:
        diag = console if fmt == OutputFormat.TABLE else err_console
        diag.print(f"\n[bold cyan]ROS2 Inspector[/bold cyan] — validating [dim]{root}[/dim]")
        diag.print(f"[dim]Policy: {policy_path}[/dim]\n")

    try:
        rules = load_policy(policy_path)
    except PolicyConfigError as exc:
        err_console.print(f"[red]Policy error:[/red] {exc}")
        raise typer.Exit(2) from exc
    uam = build_uam_or_exit(root, use_cache=not no_cache, show_progress=not state.quiet)
    try:
        violations = run_policy(uam, rules)
    except PolicyConfigError as exc:
        err_console.print(f"[red]Policy error:[/red] {exc}")
        raise typer.Exit(2) from exc
    summary = violation_summary(violations)

    scores = {p.name: (p.health_score or 0) for p in uam.packages()}
    agg = workspace_aggregate_score(scores)

    if fmt == OutputFormat.TABLE:
        _render_table(violations, summary, agg)
    elif fmt == OutputFormat.JSON:
        data = {
            "summary": {
                "workspace_health": agg,
                "total_packages": len(uam.packages()),
                "violations": summary,
            },
            "violations": [v.model_dump(mode="json") for v in violations],
        }
        sys.stdout.write(orjson.dumps(data, option=orjson.OPT_INDENT_2).decode() + "\n")
    else:
        data = {
            "summary": {
                "workspace_health": agg,
                "total_packages": len(uam.packages()),
                "violations": summary,
            },
            "violations": [v.model_dump(mode="json") for v in violations],
        }
        sys.stdout.write(yaml.dump(data, default_flow_style=False))

    _fail_on_sev = {
        "error": {ViolationSeverity.ERROR},
        "warning": {ViolationSeverity.ERROR, ViolationSeverity.WARNING},
        "info": {ViolationSeverity.ERROR, ViolationSeverity.WARNING, ViolationSeverity.INFO},
    }[fail_on]

    if any(v.severity in _fail_on_sev for v in violations):
        raise typer.Exit(1)


def _render_table(
    violations: list[PolicyViolation],
    summary: dict[str, int],
    workspace_health: int,
) -> None:
    if not violations:
        console.print("[bold green]✓ No policy violations found.[/bold green]\n")
        return

    table = Table(title="Policy Violations", box=box.ROUNDED, show_lines=True)
    table.add_column("Severity", no_wrap=True)
    table.add_column("Rule", style="dim", no_wrap=True)
    table.add_column("Message", overflow="fold")
    table.add_column("Affected", overflow="fold")

    sev_order = {
        ViolationSeverity.ERROR: 0,
        ViolationSeverity.WARNING: 1,
        ViolationSeverity.INFO: 2,
    }
    for v in sorted(violations, key=lambda x: sev_order.get(x.severity, 9)):
        color = _SEVERITY_COLOR.get(v.severity, "white")
        table.add_row(
            f"[{color}]{v.severity.upper()}[/{color}]",
            v.rule_type,
            v.message,
            ", ".join(v.affected_entities) if v.affected_entities else "—",
        )

    console.print(table)

    e, w, i = summary["errors"], summary["warnings"], summary["info"]
    parts = []
    if e:
        parts.append(f"[red]{e} error(s)[/red]")
    if w:
        parts.append(f"[yellow]{w} warning(s)[/yellow]")
    if i:
        parts.append(f"[cyan]{i} info[/cyan]")
    console.print(
        f"\n[bold]Violations:[/bold] {', '.join(parts)}  |  "
        f"[bold]Workspace health:[/bold] {workspace_health}/100\n"
    )
