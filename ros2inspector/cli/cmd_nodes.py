import sys
from io import StringIO
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
from ros2inspector.model.schemas import CommunicationEndpoint, NodeDefinition
from ros2inspector.model.uam import UnifiedArchitectureModel

app = typer.Typer(
    help="List ROS 2 nodes discovered in the workspace.",
    context_settings={"allow_interspersed_args": True},
)
console = Console()
err_console = Console(stderr=True)


@app.callback(invoke_without_command=True)
def nodes(
    fmt: Annotated[
        OutputFormat, typer.Option("--format", "-f", help="Output format: table|json|yaml")
    ] = OutputFormat.TABLE,
    path: Annotated[
        Path, typer.Option("-C", "--path", help="Workspace root (default: CWD)")
    ] = Path("."),
    package: Annotated[
        str | None, typer.Option("--package", "-p", help="Filter to a single package name")
    ] = None,
    output: Annotated[
        Path | None, typer.Option("--output", "-o", help="Write output to file")
    ] = None,
    no_cache: Annotated[
        bool, typer.Option("--no-cache", help="Skip the incremental cache and re-parse everything")
    ] = False,
    show_connections: Annotated[
        bool,
        typer.Option(
            "--show-connections",
            help="Show full topic/service/action connection details per node",
        ),
    ] = False,
) -> None:
    """Show all nodes with their publishers, subscriptions, services, and clients.

    Examples:
      ros2inspector nodes
      ros2inspector nodes --format json
      ros2inspector nodes -p euro
      ros2inspector nodes --show-connections
      ros2inspector nodes --format json -C ~/my_ws
    """
    root = find_workspace_root(path)
    uam = build_uam_or_exit(root, use_cache=not no_cache, show_progress=not state.quiet)

    node_list = uam.nodes()
    if package:
        node_list = [n for n in node_list if n.package == package]

    if fmt == OutputFormat.TABLE:
        if show_connections:
            text = _render_connections_str(node_list, uam)
        else:
            text = _render_table_str(node_list)
        _write_or_print(text, output, plain=True)
    elif fmt == OutputFormat.JSON:
        if show_connections:
            data = [_node_with_connections(n, uam) for n in node_list]
        else:
            data = [n.model_dump(mode="json") for n in node_list]
        text = orjson.dumps(data, option=orjson.OPT_INDENT_2).decode() + "\n"
        _write_or_print(text, output)
    else:
        if show_connections:
            data = [_node_with_connections(n, uam) for n in node_list]
        else:
            data = [n.model_dump(mode="json") for n in node_list]
        text = yaml.dump(data, default_flow_style=False)
        _write_or_print(text, output)


def _render_table_str(node_list: list[NodeDefinition]) -> str:
    table = Table(title="ROS 2 Nodes", box=box.ROUNDED, show_lines=True)
    table.add_column("Node Name", style="bold cyan", no_wrap=True)
    table.add_column("Package", style="magenta")
    table.add_column("Language", style="green")
    table.add_column("Publishers")
    table.add_column("Subscriptions")
    table.add_column("Services")
    table.add_column("Clients")
    table.add_column("Action Servers")
    table.add_column("Action Clients")

    def _fmt(eps: list[CommunicationEndpoint]) -> str:
        return ", ".join(ep.name for ep in eps) if eps else "—"

    for nd in sorted(node_list, key=lambda n: (n.package, n.name)):
        display_name = nd.name
        if nd.has_dynamic_names:
            display_name += " ⚠ dynamic"

        table.add_row(
            display_name,
            nd.package,
            nd.language,
            _fmt(nd.publishers),
            _fmt(nd.subscriptions),
            _fmt(nd.services),
            _fmt(nd.clients),
            _fmt(nd.action_servers),
            _fmt(nd.action_clients),
        )

    buf = StringIO()
    con = Console(file=buf, highlight=False)
    con.print(table)
    return buf.getvalue()


def _node_with_connections(nd: NodeDefinition, uam: UnifiedArchitectureModel) -> dict[str, object]:
    g = uam.graph
    nid = f"node:{nd.package}/{nd.name}"

    connections: list[dict[str, object]] = []
    for _, tid, edata in g.out_edges(nid, data=True):
        tattr = g.nodes.get(tid, {})
        kind = tattr.get("kind", "")
        if kind not in ("Topic", "Service", "Action"):
            continue
        rel = edata.get("rel", "")
        name = tattr.get("name", tid)

        if kind == "Topic":
            msg_type = tattr.get("msg_type", "unknown")
            other_pubs = [
                g.nodes[s]["name"]
                for s, _, d in g.in_edges(tid, data=True)
                if d.get("rel") == "publishes" and s != nid
            ]
            other_subs = [
                g.nodes[s]["name"]
                for s, _, d in g.in_edges(tid, data=True)
                if d.get("rel") == "subscribes" and s != nid
            ]
            connections.append(
                {
                    "kind": "topic",
                    "name": name,
                    "msg_type": msg_type,
                    "role": rel,
                    "other_publishers": other_pubs,
                    "other_subscribers": other_subs,
                }
            )
        elif kind == "Service":
            srv_type = tattr.get("srv_type", "unknown")
            callers = [
                g.nodes[s]["name"]
                for s, _, d in g.in_edges(tid, data=True)
                if d.get("rel") == "calls" and s != nid
            ]
            connections.append(
                {
                    "kind": "service",
                    "name": name,
                    "srv_type": srv_type,
                    "role": rel,
                    "other_callers": callers,
                }
            )
        elif kind == "Action":
            action_type = tattr.get("action_type", "unknown")
            clients = [
                g.nodes[s]["name"]
                for s, _, d in g.in_edges(tid, data=True)
                if d.get("rel") == "calls" and s != nid
            ]
            connections.append(
                {
                    "kind": "action",
                    "name": name,
                    "action_type": action_type,
                    "role": rel,
                    "other_clients": clients,
                }
            )

    result = nd.model_dump(mode="json")
    result["connections"] = connections
    return result


def _render_connections_str(node_list: list[NodeDefinition], uam: UnifiedArchitectureModel) -> str:
    from rich.panel import Panel
    from rich.text import Text

    buf = StringIO()
    con = Console(file=buf, highlight=False)
    g = uam.graph

    for nd in sorted(node_list, key=lambda n: (n.package, n.name)):
        nid = f"node:{nd.package}/{nd.name}"
        title = f"[bold cyan]{nd.name}[/bold cyan]  [dim]{nd.package} · {nd.language}[/dim]"
        if nd.has_dynamic_names:
            title += "  [yellow]⚠ dynamic[/yellow]"

        rows: list[str] = []
        for _, tid, edata in g.out_edges(nid, data=True):
            tattr = g.nodes.get(tid, {})
            kind = tattr.get("kind", "")
            if kind not in ("Topic", "Service", "Action"):
                continue
            rel = edata.get("rel", "")
            name = tattr.get("name", tid)

            if kind == "Topic":
                mtype = tattr.get("msg_type", "unknown")
                icon = "→" if rel == "publishes" else "←"
                other_pubs = [
                    g.nodes[s]["name"]
                    for s, _, d in g.in_edges(tid, data=True)
                    if d.get("rel") == "publishes" and s != nid
                ]
                other_subs = [
                    g.nodes[s]["name"]
                    for s, _, d in g.in_edges(tid, data=True)
                    if d.get("rel") == "subscribes" and s != nid
                ]
                conn = []
                if other_pubs:
                    conn.append(f"pub: {', '.join(other_pubs)}")
                if other_subs:
                    conn.append(f"sub: {', '.join(other_subs)}")
                conn_str = f"  [dim]({'; '.join(conn)})[/dim]" if conn else ""
                rows.append(f"  {icon} [green]{name}[/green]  [dim]{mtype}[/dim]{conn_str}")

            elif kind == "Service":
                stype = tattr.get("srv_type", "unknown")
                icon = "⊕" if rel == "provides" else "⊙"
                rows.append(
                    f"  {icon} [yellow]{name}[/yellow]  [dim]{stype}[/dim]  [dim]service[/dim]"
                )

            elif kind == "Action":
                atype = tattr.get("action_type", "unknown")
                icon = "▶" if rel == "provides" else "▷"
                rows.append(
                    f"  {icon} [magenta]{name}[/magenta]  [dim]{atype}[/dim]  [dim]action[/dim]"
                )

        body = "\n".join(rows) if rows else "  [dim]no connections[/dim]"
        con.print(Panel(Text.from_markup(body), title=Text.from_markup(title), expand=False))

    return buf.getvalue()


def _write_or_print(text: str, output: Path | None, plain: bool = False) -> None:
    if output:
        output.write_text(text, encoding="utf-8")
        err_console.print(f"[dim]Written to {output}[/dim]")
    else:
        if plain:
            console.print(text, end="")
        else:
            sys.stdout.write(text)
