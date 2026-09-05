import sys
from io import StringIO
from pathlib import Path
from typing import Annotated, Any

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
    table.add_column("ROS Name", style="bold cyan", no_wrap=True)
    table.add_column("Source Symbol", style="dim", no_wrap=True)
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
        ros_name = nd.declared_ros_name or nd.name
        source_symbol = nd.source_symbol or nd.name
        display_name = ros_name
        if nd.has_dynamic_names:
            display_name += " ⚠ dynamic"

        table.add_row(
            display_name,
            source_symbol,
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


def _actor_name(g: Any, actor_id: str) -> str:
    attrs = g.nodes[actor_id]
    if attrs.get("kind") == "Deployment":
        return str(attrs.get("name", actor_id))
    return str(attrs.get("declared_ros_name") or attrs.get("name", actor_id))


def _communication_actors(
    nd: NodeDefinition, uam: UnifiedArchitectureModel
) -> tuple[str, list[str]]:
    g = uam.graph
    source_id = uam.node_graph_id(nd)
    deployments = [
        target
        for _, target, data in g.out_edges(source_id, data=True)
        if data.get("rel") == "deploys_as"
    ]
    return source_id, deployments or [source_id]


def _node_with_connections(nd: NodeDefinition, uam: UnifiedArchitectureModel) -> dict[str, object]:
    g = uam.graph
    source_id, actors = _communication_actors(nd, uam)

    connections: list[dict[str, object]] = []
    for actor_id in actors:
        deployment = g.nodes[actor_id].get("name") if actor_id != source_id else None
        for _, target_id, edge_data in g.out_edges(actor_id, data=True):
            target = g.nodes.get(target_id, {})
            kind = target.get("kind", "")
            if kind not in ("Topic", "Service", "Action"):
                continue
            rel = edge_data.get("rel", "")
            name = target.get("name", target_id)

            item: dict[str, object] = {
                "kind": kind.lower(),
                "name": name,
                "role": rel,
            }
            if deployment is not None:
                item["deployment"] = deployment

            if kind == "Topic":
                item["msg_type"] = target.get("msg_type", "unknown")
                item["other_publishers"] = [
                    _actor_name(g, source)
                    for source, _, data in g.in_edges(target_id, data=True)
                    if data.get("rel") == "publishes" and source != actor_id
                ]
                item["other_subscribers"] = [
                    _actor_name(g, source)
                    for source, _, data in g.in_edges(target_id, data=True)
                    if data.get("rel") == "subscribes" and source != actor_id
                ]
            elif kind == "Service":
                item["srv_type"] = target.get("srv_type", "unknown")
                item["other_callers"] = [
                    _actor_name(g, source)
                    for source, _, data in g.in_edges(target_id, data=True)
                    if data.get("rel") == "calls" and source != actor_id
                ]
            else:
                item["action_type"] = target.get("action_type", "unknown")
                item["other_clients"] = [
                    _actor_name(g, source)
                    for source, _, data in g.in_edges(target_id, data=True)
                    if data.get("rel") == "calls" and source != actor_id
                ]
            connections.append(item)

    result = nd.model_dump(mode="json")
    source_attrs = g.nodes.get(source_id, {})
    result["deployments"] = source_attrs.get("deployments", [])
    result["connections"] = connections
    return result


def _render_connections_str(node_list: list[NodeDefinition], uam: UnifiedArchitectureModel) -> str:
    from rich.panel import Panel
    from rich.text import Text

    buf = StringIO()
    con = Console(file=buf, highlight=False)
    g = uam.graph

    for nd in sorted(node_list, key=lambda n: (n.package, n.name, n.file_path or "")):
        source_id, actors = _communication_actors(nd, uam)
        ros_name = nd.declared_ros_name or nd.name
        source_symbol = nd.source_symbol or nd.name
        title = (
            f"[bold cyan]{ros_name}[/bold cyan]  "
            f"[dim]{source_symbol} · {nd.package} · {nd.language}[/dim]"
        )
        if nd.has_dynamic_names:
            title += "  [yellow]⚠ dynamic[/yellow]"

        rows: list[str] = []
        for actor_id in actors:
            actor_name = _actor_name(g, actor_id)
            deployment_prefix = (
                f"[blue]{actor_name}[/blue]  " if actor_id != source_id else ""
            )
            for _, target_id, edge_data in g.out_edges(actor_id, data=True):
                target = g.nodes.get(target_id, {})
                kind = target.get("kind", "")
                if kind not in ("Topic", "Service", "Action"):
                    continue
                rel = edge_data.get("rel", "")
                name = target.get("name", target_id)

                if kind == "Topic":
                    msg_type = target.get("msg_type", "unknown")
                    icon = "→" if rel == "publishes" else "←"
                    other_pubs = [
                        _actor_name(g, source)
                        for source, _, data in g.in_edges(target_id, data=True)
                        if data.get("rel") == "publishes" and source != actor_id
                    ]
                    other_subs = [
                        _actor_name(g, source)
                        for source, _, data in g.in_edges(target_id, data=True)
                        if data.get("rel") == "subscribes" and source != actor_id
                    ]
                    peers = []
                    if other_pubs:
                        peers.append(f"pub: {', '.join(other_pubs)}")
                    if other_subs:
                        peers.append(f"sub: {', '.join(other_subs)}")
                    peer_text = f"  [dim]({'; '.join(peers)})[/dim]" if peers else ""
                    rows.append(
                        f"  {deployment_prefix}{icon} [green]{name}[/green]  "
                        f"[dim]{msg_type}[/dim]{peer_text}"
                    )
                elif kind == "Service":
                    service_type = target.get("srv_type", "unknown")
                    icon = "⊕" if rel == "provides" else "⊙"
                    rows.append(
                        f"  {deployment_prefix}{icon} [yellow]{name}[/yellow]  "
                        f"[dim]{service_type}[/dim]  [dim]service[/dim]"
                    )
                else:
                    action_type = target.get("action_type", "unknown")
                    icon = "▶" if rel == "provides" else "▷"
                    rows.append(
                        f"  {deployment_prefix}{icon} [magenta]{name}[/magenta]  "
                        f"[dim]{action_type}[/dim]  [dim]action[/dim]"
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
