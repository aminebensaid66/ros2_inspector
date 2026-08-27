from __future__ import annotations

from pathlib import Path
from typing import Any

import networkx as nx

from ros2inspector.cache.analysis_cache import AnalysisCache
from ros2inspector.discovery import (
    DuplicatePackageError,
    NoPackagesFoundError,
    find_interface_files,
    find_package_xml_files,
)
from ros2inspector.model.schemas import (
    CommunicationEndpoint,
    DataSource,
    DYNAMIC_SENTINEL,
    InterfaceDefinition,
    NodeDefinition,
    PackageMetadata,
)
from ros2inspector.static import (
    analyze_launch_file,
    find_launch_files,
    parse_cpp_nodes,
    parse_interface_file,
    parse_package_xml,
    parse_python_nodes,
    score_workspace,
)
from ros2inspector.static.launch_analyzer import LaunchNode


def _pkg_id(name: str) -> str:
    return f"pkg:{name}"


def _node_id(package: str, name: str) -> str:
    return f"node:{package}/{name}"


def _topic_id(name: str) -> str:
    return f"topic:{name}"


def _svc_id(name: str) -> str:
    return f"svc:{name}"


def _action_id(name: str) -> str:
    return f"action:{name}"


def _communication_id(
    kind: str,
    name: str,
    node_id: str,
    role: str,
    index: int,
) -> str:
    """Return a stable ID without merging unresolved communication endpoints."""
    builders = {"Topic": _topic_id, "Service": _svc_id, "Action": _action_id}
    if name != DYNAMIC_SENTINEL:
        return builders[kind](name)
    return f"unresolved:{kind.lower()}:{node_id}:{role}:{index}"


def _iface_id(package: str, name: str) -> str:
    return f"iface:{package}/{name}"


def _apply_remap(name: str, remaps: dict[str, str]) -> str:
    """Return the remapped topic/service/action name, normalising leading slash variants."""
    if name in remaps:
        return remaps[name]
    alt = f"/{name}" if not name.startswith("/") else name.lstrip("/")
    return remaps.get(alt, name)


def _apply_namespace(name: str, namespace: str | None) -> str:
    """Apply a launch namespace to a relative ROS name without rewriting absolute names."""
    if not namespace or namespace in {"<unknown>", "<dynamic>"} or name.startswith("/"):
        return name
    ns = namespace.strip("/")
    return f"/{ns}/{name.lstrip('/')}" if ns else name


def _normalise_symbol(name: str) -> str:
    """Normalise class/executable names for conservative launch matching."""
    chars: list[str] = []
    for index, char in enumerate(name):
        if char.isupper() and index and chars and chars[-1] != "_":
            chars.append("_")
        chars.append(char.lower() if char.isalnum() else "_")
    return "".join(chars).strip("_")


class UnifiedArchitectureModel:
    def __init__(self) -> None:
        self._graph: nx.MultiDiGraph = nx.MultiDiGraph()
        self._packages: list[PackageMetadata] = []
        self._nodes: list[NodeDefinition] = []
        self._interfaces: list[InterfaceDefinition] = []
        self._diagnostics: list[dict[str, Any]] = []
        # package_name → list of LaunchNodes found in launch files (per-node remaps)
        self._launch_remaps: dict[str, list[LaunchNode]] = {}

    # ── build ──────────────────────────────────────────────────────────────

    @classmethod
    def build(
        cls,
        workspace_root: Path,
        use_cache: bool = True,
        cache_dir: Path | None = None,
        show_progress: bool = False,
    ) -> UnifiedArchitectureModel:
        uam = cls()

        xml_files = find_package_xml_files(workspace_root)
        packages = [p for f in xml_files if (p := parse_package_xml(f)) is not None]
        if not packages:
            raise NoPackagesFoundError(workspace_root)

        package_paths: dict[str, list[Path]] = {}
        for package in packages:
            package_paths.setdefault(package.name, []).append(Path(package.path).resolve())
        duplicates = {name: paths for name, paths in package_paths.items() if len(paths) > 1}
        if duplicates:
            raise DuplicatePackageError(duplicates)

        _ = score_workspace(packages)
        uam._packages = packages

        pkg_map: dict[str, PackageMetadata] = {p.name: p for p in packages}

        all_nodes: list[NodeDefinition] = []
        all_interfaces: list[InterfaceDefinition] = []

        cache = AnalysisCache(cache_dir) if use_cache else None

        _progress = None
        _task = None
        if show_progress and packages:
            from rich.progress import (  # noqa: PLC0415
                BarColumn,
                MofNCompleteColumn,
                Progress,
                SpinnerColumn,
                TextColumn,
            )

            _progress = Progress(
                SpinnerColumn(),
                TextColumn("[cyan]{task.description}"),
                BarColumn(),
                MofNCompleteColumn(),
                transient=True,
            )
            _task = _progress.add_task("Scanning packages...", total=len(packages))
            _progress.start()

        try:
            for pkg in packages:
                if _progress is not None and _task is not None:
                    _progress.update(_task, description=f"Parsing {pkg.name}...")
                pkg_path = Path(pkg.path)

                cached = cache.get(pkg_path) if cache is not None else None
                if cached is not None:
                    pkg_nodes, pkg_ifaces = cached
                else:
                    pkg_nodes = parse_python_nodes(pkg_path, pkg.name) + parse_cpp_nodes(
                        pkg_path, pkg.name
                    )
                    pkg_ifaces = [
                        parse_interface_file(f, pkg.name) for f in find_interface_files(pkg_path)
                    ]
                    if cache is not None:
                        cache.set(pkg_path, pkg_nodes, pkg_ifaces)

                all_nodes.extend(pkg_nodes)
                all_interfaces.extend(pkg_ifaces)

                # Collect launch file remaps (not cached — fast to parse)
                pkg_launch_nodes: list[LaunchNode] = []
                for lf in find_launch_files(pkg_path):
                    try:
                        lg = analyze_launch_file(lf)
                        pkg_launch_nodes.extend(lg.nodes)
                    except Exception as exc:  # noqa: BLE001
                        uam._diagnostics.append(
                            {
                                "severity": "warning",
                                "code": "launch_parse_failed",
                                "message": str(exc),
                                "file": str(lf),
                            }
                        )
                if pkg_launch_nodes:
                    uam._launch_remaps[pkg.name] = pkg_launch_nodes

                if _progress is not None and _task is not None:
                    _progress.advance(_task)
        finally:
            if cache is not None:
                cache.close()
            if _progress is not None:
                _progress.stop()

        uam._nodes = all_nodes
        uam._interfaces = all_interfaces

        uam._build_graph(packages, all_nodes, all_interfaces, pkg_map)
        uam._resolve_interface_types(all_interfaces)

        return uam

    @staticmethod
    def _add_edge(
        graph: nx.MultiDiGraph,
        src: str,
        dst: str,
        *,
        rel: str,
        **attrs: Any,
    ) -> str:
        """Add a relationship without overwriting any existing relationship."""
        key = rel
        counter = 2
        while graph.has_edge(src, dst, key=key):
            key = f"{rel}#{counter}"
            counter += 1
        graph.add_edge(src, dst, key=key, rel=rel, **attrs)
        return key

    def _upsert_comm_node(
        self,
        node_id: str,
        *,
        kind: str,
        name: str,
        type_key: str,
        interface_type: str,
        unresolved: bool = False,
    ) -> None:
        graph = self._graph
        explicit = interface_type != "unknown"
        if node_id not in graph:
            graph.add_node(
                node_id,
                kind=kind,
                name=name,
                **{
                    type_key: interface_type,
                    "type_source": "explicit" if explicit else "unknown",
                    "confidence": "low" if unresolved else ("high" if explicit else "unknown"),
                    "resolution": "unresolved" if unresolved else "known",
                    "observed_types": [interface_type] if explicit else [],
                },
            )
            return

        attrs = graph.nodes[node_id]
        current = str(attrs.get(type_key, "unknown"))
        if not explicit:
            return
        observed = list(attrs.get("observed_types", []))
        if interface_type not in observed:
            observed.append(interface_type)
        attrs["observed_types"] = observed
        if current == "unknown":
            attrs[type_key] = interface_type
            attrs["type_source"] = "explicit"
            attrs["confidence"] = "high"
        elif current != interface_type:
            self._diagnostics.append(
                {
                    "severity": "warning",
                    "code": "interface_type_conflict",
                    "message": (
                        f"{kind} '{name}' was observed with conflicting explicit types "
                        f"'{current}' and '{interface_type}'."
                    ),
                    "entity": node_id,
                }
            )

    @staticmethod
    def _endpoint_edge_attrs(
        endpoint: CommunicationEndpoint,
        actual_name: str,
    ) -> dict[str, Any]:
        return {
            "source": DataSource.STATIC.value,
            "remapped": actual_name != endpoint.name,
            "original_name": endpoint.name,
            "file_path": endpoint.file_path,
            "line": endpoint.line,
            "evidence": endpoint.evidence,
            "confidence": endpoint.confidence,
            "resolution": "unresolved" if actual_name == DYNAMIC_SENTINEL else "known",
        }

    # ── graph construction ─────────────────────────────────────────────────

    def _build_graph(
        self,
        packages: list[PackageMetadata],
        nodes: list[NodeDefinition],
        interfaces: list[InterfaceDefinition],
        pkg_map: dict[str, PackageMetadata],
    ) -> None:
        g = self._graph

        for pkg in packages:
            g.add_node(
                _pkg_id(pkg.name),
                kind="Package",
                name=pkg.name,
                version=pkg.version,
                type=pkg.package_type.value,
                health_score=pkg.health_score,
            )

        for pkg in packages:
            src = _pkg_id(pkg.name)
            for dep_type, dep_list in pkg.dependencies.items():
                for dep_name in dep_list:
                    if dep_name in pkg_map:
                        dst = _pkg_id(dep_name)
                        self._add_edge(
                            g,
                            src,
                            dst,
                            rel="depends_on",
                            dep_type=dep_type.value,
                        )

        for nd in nodes:
            nid = _node_id(nd.package, nd.name)
            g.add_node(
                nid,
                kind="Node",
                name=nd.name,
                package=nd.package,
                language=nd.language,
                file_path=nd.file_path,
                line=nd.line,
                has_dynamic_names=nd.has_dynamic_names,
            )

            pkg_id = _pkg_id(nd.package)
            if pkg_id in g:
                self._add_edge(g, nid, pkg_id, rel="defined_in")

            # Match launch remaps by node name or executable name so we only apply
            # remaps that were explicitly declared for this specific node instance.
            pkg_remaps: dict[str, str] = {}
            namespace: str | None = None
            for ln in self._launch_remaps.get(nd.package, []):
                names = {nd.name, _normalise_symbol(nd.name)}
                if (
                    ln.name in names
                    or ln.executable in names
                    or _normalise_symbol(ln.executable) in names
                ):
                    pkg_remaps = ln.remaps
                    namespace = ln.namespace
                    g.nodes[nid]["deployment_name"] = ln.name or ln.executable
                    g.nodes[nid]["namespace"] = ln.namespace
                    break

            for index, ep in enumerate(nd.publishers):
                actual = _apply_namespace(_apply_remap(ep.name, pkg_remaps), namespace)
                tid = _communication_id("Topic", actual, nid, "publisher", index)
                self._upsert_comm_node(
                    tid,
                    kind="Topic",
                    name=actual,
                    type_key="msg_type",
                    interface_type=ep.msg_type,
                    unresolved=actual == DYNAMIC_SENTINEL,
                )
                self._add_edge(
                    g,
                    nid,
                    tid,
                    rel="publishes",
                    **self._endpoint_edge_attrs(ep, actual),
                )

            for index, ep in enumerate(nd.subscriptions):
                actual = _apply_namespace(_apply_remap(ep.name, pkg_remaps), namespace)
                tid = _communication_id("Topic", actual, nid, "subscription", index)
                self._upsert_comm_node(
                    tid,
                    kind="Topic",
                    name=actual,
                    type_key="msg_type",
                    interface_type=ep.msg_type,
                    unresolved=actual == DYNAMIC_SENTINEL,
                )
                self._add_edge(
                    g,
                    nid,
                    tid,
                    rel="subscribes",
                    **self._endpoint_edge_attrs(ep, actual),
                )

            for index, ep in enumerate(nd.services):
                actual = _apply_namespace(_apply_remap(ep.name, pkg_remaps), namespace)
                sid = _communication_id("Service", actual, nid, "service", index)
                self._upsert_comm_node(
                    sid,
                    kind="Service",
                    name=actual,
                    type_key="srv_type",
                    interface_type=ep.msg_type,
                    unresolved=actual == DYNAMIC_SENTINEL,
                )
                self._add_edge(
                    g,
                    nid,
                    sid,
                    rel="provides",
                    **self._endpoint_edge_attrs(ep, actual),
                )

            for index, ep in enumerate(nd.clients):
                actual = _apply_namespace(_apply_remap(ep.name, pkg_remaps), namespace)
                sid = _communication_id("Service", actual, nid, "client", index)
                self._upsert_comm_node(
                    sid,
                    kind="Service",
                    name=actual,
                    type_key="srv_type",
                    interface_type=ep.msg_type,
                    unresolved=actual == DYNAMIC_SENTINEL,
                )
                self._add_edge(
                    g,
                    nid,
                    sid,
                    rel="calls",
                    **self._endpoint_edge_attrs(ep, actual),
                )

            for index, ep in enumerate(nd.action_servers):
                actual = _apply_namespace(_apply_remap(ep.name, pkg_remaps), namespace)
                aid = _communication_id("Action", actual, nid, "action_server", index)
                self._upsert_comm_node(
                    aid,
                    kind="Action",
                    name=actual,
                    type_key="action_type",
                    interface_type=ep.msg_type,
                    unresolved=actual == DYNAMIC_SENTINEL,
                )
                self._add_edge(
                    g,
                    nid,
                    aid,
                    rel="provides",
                    **self._endpoint_edge_attrs(ep, actual),
                )

            for index, ep in enumerate(nd.action_clients):
                actual = _apply_namespace(_apply_remap(ep.name, pkg_remaps), namespace)
                aid = _communication_id("Action", actual, nid, "action_client", index)
                self._upsert_comm_node(
                    aid,
                    kind="Action",
                    name=actual,
                    type_key="action_type",
                    interface_type=ep.msg_type,
                    unresolved=actual == DYNAMIC_SENTINEL,
                )
                self._add_edge(
                    g,
                    nid,
                    aid,
                    rel="calls",
                    **self._endpoint_edge_attrs(ep, actual),
                )

        for iface in interfaces:
            iid = _iface_id(iface.package, iface.name)
            g.add_node(
                iid,
                kind="Interface",
                name=iface.name,
                package=iface.package,
                iface_kind=iface.kind,
                fields=iface.fields,
            )

    def _resolve_interface_types(self, interfaces: list[InterfaceDefinition]) -> None:
        # Group by lowercase name so we can detect ambiguous matches.  When two
        # packages both define e.g. Status.msg, the bare-name lookup is unreliable
        # and we leave the existing type annotation untouched.
        iface_by_name: dict[str, list[InterfaceDefinition]] = {}
        for iface in interfaces:
            iface_by_name.setdefault(iface.name.lower(), []).append(iface)

        g = self._graph
        for nid, attrs in g.nodes(data=True):
            kind = attrs.get("kind")
            name: str = attrs.get("name", "")
            bare = name.lstrip("/").split("/")[-1].lower()
            candidates = iface_by_name.get(bare)
            if not candidates or len(candidates) != 1:
                # No match, or ambiguous (multiple packages define the same
                # interface name) — skip rather than guess wrong.
                continue
            match = candidates[0]
            resolved_type = f"{match.package}/{match.name}"

            if kind == "Topic":
                type_key, current = "msg_type", attrs.get("msg_type", "unknown")
            elif kind == "Service":
                type_key, current = "srv_type", attrs.get("srv_type", "unknown")
            elif kind == "Action":
                type_key, current = "action_type", attrs.get("action_type", "unknown")
            else:
                continue

            # Heuristic name matching may fill an unknown type, but must never
            # replace an explicit type extracted from source code.
            if current != "unknown":
                continue

            g.nodes[nid][type_key] = resolved_type
            g.nodes[nid]["type_source"] = "inferred_name_match"
            g.nodes[nid]["confidence"] = "low"

        # Add uses_interface edges from nodes to interfaces they communicate through
        comm_rels = {"publishes", "subscribes", "provides", "calls"}
        for src, dst, edge_data in list(g.edges(data=True)):
            if edge_data.get("rel") not in comm_rels:
                continue
            dst_attrs = g.nodes[dst]
            dst_kind = dst_attrs.get("kind")
            resolved: str | None = None
            if dst_kind == "Topic":
                resolved = dst_attrs.get("msg_type")
            elif dst_kind == "Service":
                resolved = dst_attrs.get("srv_type")
            elif dst_kind == "Action":
                resolved = dst_attrs.get("action_type")
            if resolved and resolved != "unknown":
                # resolved is "pkg/Name" — reconstruct the iface node id
                parts = resolved.split("/", 1)
                if len(parts) == 2:
                    iid = _iface_id(parts[0], parts[1])
                    if iid in g and not any(
                        data.get("rel") == "uses_interface"
                        for data in (g.get_edge_data(src, iid) or {}).values()
                    ):
                        self._add_edge(g, src, iid, rel="uses_interface")

    # ── accessors ──────────────────────────────────────────────────────────

    def packages(self) -> list[PackageMetadata]:
        return list(self._packages)

    def nodes(self) -> list[NodeDefinition]:
        return list(self._nodes)

    def interfaces(self) -> list[InterfaceDefinition]:
        return list(self._interfaces)

    def diagnostics(self) -> list[dict[str, Any]]:
        return [dict(item) for item in self._diagnostics]

    def topics(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        g = self._graph
        for nid, attrs in g.nodes(data=True):
            if attrs.get("kind") != "Topic":
                continue
            publishers = [
                g.nodes[s]["name"]
                for s, _, d in g.in_edges(nid, data=True)
                if d.get("rel") == "publishes"
            ]
            subscribers = [
                g.nodes[s]["name"]
                for s, _, d in g.in_edges(nid, data=True)
                if d.get("rel") == "subscribes"
            ]
            result.append(
                {
                    "name": attrs["name"],
                    "msg_type": attrs.get("msg_type", "unknown"),
                    "publishers": publishers,
                    "subscribers": subscribers,
                }
            )
        return result

    def services(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        g = self._graph
        for nid, attrs in g.nodes(data=True):
            if attrs.get("kind") != "Service":
                continue
            providers = [
                g.nodes[s]["name"]
                for s, _, d in g.in_edges(nid, data=True)
                if d.get("rel") == "provides"
            ]
            callers = [
                g.nodes[s]["name"]
                for s, _, d in g.in_edges(nid, data=True)
                if d.get("rel") == "calls"
            ]
            result.append(
                {
                    "name": attrs["name"],
                    "srv_type": attrs.get("srv_type", "unknown"),
                    "providers": providers,
                    "callers": callers,
                }
            )
        return result

    def actions(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        g = self._graph
        for nid, attrs in g.nodes(data=True):
            if attrs.get("kind") != "Action":
                continue
            servers = [
                g.nodes[s]["name"]
                for s, _, d in g.in_edges(nid, data=True)
                if d.get("rel") == "provides"
            ]
            clients = [
                g.nodes[s]["name"]
                for s, _, d in g.in_edges(nid, data=True)
                if d.get("rel") == "calls"
            ]
            result.append(
                {
                    "name": attrs["name"],
                    "action_type": attrs.get("action_type", "unknown"),
                    "servers": servers,
                    "clients": clients,
                }
            )
        return result

    def launch_remaps(self) -> dict[str, list[dict[str, Any]]]:
        return {
            pkg: [
                {"executable": ln.executable, "name": ln.name or "", "remaps": ln.remaps}
                for ln in nodes
            ]
            for pkg, nodes in self._launch_remaps.items()
        }

    def summary(self) -> dict[str, int]:
        g = self._graph
        counts: dict[str, int] = {
            "packages": 0,
            "nodes": 0,
            "topics": 0,
            "services": 0,
            "actions": 0,
            "interfaces": 0,
        }
        kind_map = {
            "Package": "packages",
            "Node": "nodes",
            "Topic": "topics",
            "Service": "services",
            "Action": "actions",
            "Interface": "interfaces",
        }
        for _, attrs in g.nodes(data=True):
            key = kind_map.get(attrs.get("kind", ""), "")
            if key:
                counts[key] += 1
        return counts

    def to_dict(self) -> dict[str, Any]:
        return {
            "packages": [p.model_dump(mode="json") for p in self._packages],
            "nodes": [n.model_dump(mode="json") for n in self._nodes],
            "interfaces": [i.model_dump(mode="json") for i in self._interfaces],
            "topics": self.topics(),
            "services": self.services(),
            "actions": self.actions(),
            "launch_remaps": self.launch_remaps(),
            "diagnostics": self.diagnostics(),
            "summary": self.summary(),
            "graph": {
                "nodes": [
                    {"id": nid, **{k: v for k, v in attrs.items()}}
                    for nid, attrs in self._graph.nodes(data=True)
                ],
                "edges": [
                    {"source": s, "target": t, "key": key, **dict(d)}
                    for s, t, key, d in self._graph.edges(keys=True, data=True)
                ],
            },
        }

    @property
    def graph(self) -> nx.MultiDiGraph:
        return self._graph


# Public alias
UAM = UnifiedArchitectureModel
