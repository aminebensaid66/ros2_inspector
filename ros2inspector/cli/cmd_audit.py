from __future__ import annotations

import sys
from pathlib import Path
from typing import Annotated, Any

import orjson
import typer
import yaml
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ros2inspector.cli._output import OutputFormat, health_bar
from ros2inspector.cli._state import state
from ros2inspector.cli._workspace import build_uam_or_exit
from ros2inspector.discovery import find_workspace_root
from ros2inspector.model.schemas import PolicyViolation, ViolationSeverity
from ros2inspector.policy.rules import (
    rule_action_connectivity,
    rule_node_isolation,
    rule_service_connectivity,
    rule_topic_connectivity,
)
from ros2inspector.static import workspace_aggregate_score

app = typer.Typer(
    help="Run architecture quality audit without a policy file.",
    context_settings={"allow_interspersed_args": True},
)
console = Console()
err_console = Console(stderr=True)

_SEVERITY_COLOR = {
    ViolationSeverity.ERROR: "red",
    ViolationSeverity.WARNING: "yellow",
    ViolationSeverity.INFO: "cyan",
}

_SEVERITY_ICON = {
    ViolationSeverity.ERROR: "✖",
    ViolationSeverity.WARNING: "⚠",
    ViolationSeverity.INFO: "●",
}

_DEFAULT_EXCLUDES = ["/rosout", "/parameter_events", "/clock", "/tf", "/tf_static"]


def _build_rules(exclude: list[str]) -> list[dict[str, Any]]:
    return [
        {
            "type": "topic_connectivity",
            "no_publisher": True,
            "no_subscriber": True,
            "severity_no_publisher": "warning",
            "severity_no_subscriber": "info",
            "exclude": exclude,
            "_source": "audit",
        },
        {
            "type": "node_isolation",
            "severity": "warning",
            "skip_dynamic_names": True,
            "_source": "audit",
        },
        {
            "type": "service_connectivity",
            "severity": "info",
            "_source": "audit",
        },
        {
            "type": "action_connectivity",
            "severity": "warning",
            "_source": "audit",
        },
    ]


_RUNNERS = {
    "topic_connectivity": rule_topic_connectivity,
    "node_isolation": rule_node_isolation,
    "service_connectivity": rule_service_connectivity,
    "action_connectivity": rule_action_connectivity,
}

_SECTION_TITLES = {
    "topic_connectivity": "Topic Connectivity",
    "node_isolation": "Node Isolation",
    "service_connectivity": "Service Connectivity",
    "action_connectivity": "Action Connectivity",
}

_SECTION_DESCRIPTIONS = {
    "topic_connectivity": (
        "Topics that are published but never subscribed (silent output) "
        "or subscribed but never published (missing publisher)."
    ),
    "node_isolation": (
        "Nodes with no detected publishers, subscribers, services, or action connections. "
        "May indicate misconfiguration or purely dynamic topic names."
    ),
    "service_connectivity": (
        "Services whose provider/client counterpart is missing in the analysed workspace. "
        "The counterpart may be external or not yet implemented."
    ),
    "action_connectivity": (
        "Actions whose server/client counterpart is missing in the analysed workspace."
    ),
}


@app.callback(invoke_without_command=True)
def audit(
    path: Annotated[Path, typer.Argument(help="Workspace root (default: CWD)")] = Path("."),
    fmt: Annotated[
        OutputFormat, typer.Option("--format", "-f", help="Output format: table|json|yaml")
    ] = OutputFormat.TABLE,
    fail_on: Annotated[
        str,
        typer.Option("--fail-on", help="Min severity for non-zero exit: error|warning|info"),
    ] = "error",
    strict: Annotated[
        bool,
        typer.Option("--strict", help="Treat warnings as errors for exit code"),
    ] = False,
    exclude: Annotated[
        str,
        typer.Option(
            "--exclude",
            help="Comma-separated extra topics to exclude (e.g. /my_topic,/other)",
        ),
    ] = "",
    no_cache: Annotated[
        bool, typer.Option("--no-cache", help="Skip the incremental cache")
    ] = False,
) -> None:
    """Run an architecture quality audit on the workspace.

    Checks topic connectivity, node isolation, unused services, and
    action servers without clients — no policy file required.

    Exit codes:
      0 — no findings at or above --fail-on severity
      1 — findings found at or above --fail-on severity
      2 — invocation error
      3 — workspace not found

    Examples:
      ros2inspector audit
      ros2inspector audit --strict
      ros2inspector audit --fail-on warning
      ros2inspector audit --exclude /my_debug_topic,/legacy_cmd
      ros2inspector audit --format json
    """
    if fail_on not in ("error", "warning", "info"):
        err_console.print("[red]Error:[/red] --fail-on must be error, warning, or info")
        raise typer.Exit(2)

    extra_excludes = [t.strip() for t in exclude.split(",") if t.strip()]
    all_excludes = _DEFAULT_EXCLUDES + extra_excludes

    root = find_workspace_root(path)

    if not state.quiet:
        diag = console if fmt == OutputFormat.TABLE else err_console
        diag.print(
            f"\n[bold cyan]ROS2 Inspector[/bold cyan] — architecture audit [dim]{root}[/dim]\n"
        )

    uam = build_uam_or_exit(root, use_cache=not no_cache, show_progress=not state.quiet)

    rules = _build_rules(all_excludes)
    violations: list[PolicyViolation] = []
    for rule in rules:
        runner = _RUNNERS[rule["type"]]
        violations.extend(runner(uam, rule))

    scores = {p.name: (p.health_score or 0) for p in uam.packages()}
    agg = workspace_aggregate_score(scores)
    summary_counts = uam.summary()

    if fmt == OutputFormat.TABLE:
        _render_table(violations, agg, summary_counts)
    elif fmt == OutputFormat.JSON:
        data = _build_data(violations, agg, summary_counts)
        sys.stdout.write(orjson.dumps(data, option=orjson.OPT_INDENT_2).decode() + "\n")
    else:
        data = _build_data(violations, agg, summary_counts)
        sys.stdout.write(yaml.dump(data, default_flow_style=False))

    effective_fail = "warning" if strict else fail_on
    _fail_on_sev = {
        "error": {ViolationSeverity.ERROR},
        "warning": {ViolationSeverity.ERROR, ViolationSeverity.WARNING},
        "info": {ViolationSeverity.ERROR, ViolationSeverity.WARNING, ViolationSeverity.INFO},
    }[effective_fail]

    if any(v.severity in _fail_on_sev for v in violations):
        raise typer.Exit(1)


def _build_data(
    violations: list[PolicyViolation],
    workspace_health: int,
    summary_counts: dict[str, int],
) -> dict[str, object]:
    by_rule: dict[str, int] = {}
    for v in violations:
        by_rule[v.rule_type] = by_rule.get(v.rule_type, 0) + 1

    return {
        "summary": {
            "workspace_health": workspace_health,
            "total_findings": len(violations),
            "by_rule": by_rule,
            "workspace": summary_counts,
        },
        "findings": [v.model_dump(mode="json") for v in violations],
    }


def _render_table(
    violations: list[PolicyViolation],
    workspace_health: int,
    summary_counts: dict[str, int],
) -> None:
    # Group violations by rule type, preserving order
    by_rule: dict[str, list[PolicyViolation]] = {}
    for v in violations:
        by_rule.setdefault(v.rule_type, []).append(v)

    rule_order = [
        "topic_connectivity",
        "node_isolation",
        "service_connectivity",
        "action_connectivity",
    ]

    if not violations:
        console.print(
            Panel(
                "[bold green]✓ No architecture issues found.[/bold green]\n"
                "[dim]All topics are connected, nodes communicate, "
                "services and actions have counterparts.[/dim]",
                title="[bold]Audit Result[/bold]",
                border_style="green",
            )
        )
    else:
        for rule_type in rule_order:
            rule_violations = by_rule.get(rule_type, [])
            if not rule_violations:
                continue

            title = _SECTION_TITLES.get(rule_type, rule_type)
            desc = _SECTION_DESCRIPTIONS.get(rule_type, "")

            table = Table(
                title=f"[bold]{title}[/bold]",
                caption=f"[dim]{desc}[/dim]",
                box=box.ROUNDED,
                show_lines=True,
                title_justify="left",
                caption_justify="left",
                min_width=70,
            )
            table.add_column("Sev", no_wrap=True, width=8)
            table.add_column("Finding", overflow="fold")
            table.add_column("Affected", overflow="fold", style="dim")

            sev_order = {
                ViolationSeverity.ERROR: 0,
                ViolationSeverity.WARNING: 1,
                ViolationSeverity.INFO: 2,
            }
            for v in sorted(rule_violations, key=lambda x: sev_order.get(x.severity, 9)):
                color = _SEVERITY_COLOR.get(v.severity, "white")
                icon = _SEVERITY_ICON.get(v.severity, "·")
                table.add_row(
                    f"[{color}]{icon} {v.severity.upper()[:4]}[/{color}]",
                    v.message,
                    ", ".join(v.affected_entities) if v.affected_entities else "—",
                )

            console.print(table)
            console.print()

    _render_summary(violations, workspace_health, summary_counts, by_rule)


def _render_summary(
    violations: list[PolicyViolation],
    workspace_health: int,
    summary_counts: dict[str, int],
    by_rule: dict[str, list[PolicyViolation]],
) -> None:
    errors = sum(1 for v in violations if v.severity == ViolationSeverity.ERROR)
    warnings = sum(1 for v in violations if v.severity == ViolationSeverity.WARNING)
    infos = sum(1 for v in violations if v.severity == ViolationSeverity.INFO)

    counts_line = Text()
    counts_line.append(
        f"{summary_counts.get('packages', 0)} pkg  "
        f"{summary_counts.get('nodes', 0)} nodes  "
        f"{summary_counts.get('topics', 0)} topics  "
        f"{summary_counts.get('services', 0)} svc  "
        f"{summary_counts.get('actions', 0)} actions",
        style="dim",
    )

    findings_line = Text()
    if not violations:
        findings_line.append("✓ Clean", style="bold green")
    else:
        parts = []
        if errors:
            parts.append((f"{errors} error(s)", "bold red"))
        if warnings:
            parts.append((f"{warnings} warning(s)", "bold yellow"))
        if infos:
            parts.append((f"{infos} info", "bold cyan"))
        for i, (text, style) in enumerate(parts):
            if i:
                findings_line.append("  ", style="dim")
            findings_line.append(text, style=style)

    rule_breakdown = Text()
    for rule_type, rule_violations in by_rule.items():
        label = _SECTION_TITLES.get(rule_type, rule_type)
        count = len(rule_violations)
        rule_breakdown.append(f"  {label}: ", style="dim")
        rule_breakdown.append(str(count), style="bold")
        rule_breakdown.append("\n")

    body = Text()
    body.append("Workspace  ", style="dim")
    body.append(counts_line)
    body.append("\n")
    body.append("Health     ", style="dim")
    body.append(Text.from_markup(health_bar(workspace_health)))
    body.append("\n")
    if violations:
        body.append("Findings   ", style="dim")
        body.append(findings_line)
        body.append("\n\n")
        body.append(rule_breakdown)
    else:
        body.append("Findings   ", style="dim")
        body.append(findings_line)

    border = (
        "red"
        if any(v.severity == ViolationSeverity.ERROR for v in violations)
        else "yellow"
        if violations
        else "green"
    )

    console.print(Panel(body, title="[bold]Audit Summary[/bold]", border_style=border))
    console.print()
