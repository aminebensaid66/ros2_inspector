from __future__ import annotations

import hashlib
from collections import Counter
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
    DYNAMIC_SENTINEL,
    CommunicationEndpoint,
    DataSource,
    InterfaceDefinition,
    NodeDefinition,
    PackageMetadata,
)
from ros2inspector.static import (
    analyze_launch_file,
    find_launch_files,
    find_python_entrypoints,
    parse_cpp_nodes,
    parse_interface_file,
    parse_package_xml,
    parse_python_nodes,
    score_workspace,
)
from ros2inspector.static.launch_analyzer import LaunchGraph, LaunchNode
from ros2inspector.static.python_entrypoints import PythonEntrypoint


def _pkg_id(name: str) -> str:
    return f"pkg:{name}"


def _node_id(package: str, name: str) -> str:
    """Legacy source-node ID used when a package/symbol pair is unique."""
    return f"node:{package}/{name}"


def _definition_key(nd: NodeDefinition) -> tuple[str, str, str, int]:
    return (nd.package, nd.name, nd.file_path or "", nd.line or 0)


def _definition_id(nd: NodeDefinition, duplicate_symbols: set[tuple[str, str]]) -> str:
    """Return a stable source-definition ID without breaking unique legacy IDs."""
    base = _node_id(nd.package, nd.name)
    if (nd.package, nd.name) not in duplicate_symbols:
        return base
    provenance = f"{nd.file_path or ''}:{nd.line or 0}:{nd.source_symbol or nd.name}"
    suffix = hashlib.sha1(provenance.encode("utf-8"), usedforsecurity=False).hexdigest()[:10]
    return f"{base}@{suffix}"


def _deployment_id(source_id: str, launch_node: LaunchNode, index: int) -> str:
    raw = "|".join(
        (
            source_id,
            launch_node.source_file or "",
            launch_node.executable,
            launch_node.name or "",
            launch_node.namespace or "",
            str(index),
        )
    )
    suffix = hashlib.sha1(raw.encode("utf-8"), usedforsecurity=False).hexdigest()[:12]
    return f"deployment:{suffix}"


def _actor_display_name(graph: nx.MultiDiGraph, actor_id: str) -> str:
    attrs = graph.nodes[actor_id]
    if attrs.get("kind") == "Deployment":
        return str(attrs.get("name", actor_id))
    # Preserve the 0.1.x accessor contract for source-only nodes: callers of
    # topics()/services()/actions() historically receive the source symbol.
    return str(attrs.get("name", actor_id))


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
    if name == DYNAMIC_SENTINEL:
        return name
    if name in remaps:
        return remaps[name]
    alt = f"/{name}" if not name.startswith("/") else name.lstrip("/")
    return remaps.get(alt, name)


def _apply_namespace(name: str, namespace: str | None) -> str:
    """Apply a launch namespace to a relative ROS name without rewriting absolute names."""
    # Preserve unresolved names as unresolved. Turning ``<dynamic>`` into
    # ``/robot/<dynamic>`` would make it look resolved and could merge unrelated
    # endpoints that merely share the same launch namespace.
    if (
        name == DYNAMIC_SENTINEL
        or not namespace
        or namespace in {"<unknown>", "<dynamic>"}
        or name.startswith("/")
    ):
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


def _python_modules_for_file(file_path: str | None, package_path: Path) -> set[str]:
    if not file_path:
        return set()
    try:
        relative = Path(file_path).resolve().relative_to(package_path.resolve())
    except (OSError, ValueError):
        return set()
    if relative.suffix != ".py":
        return set()
    parts = list(relative.with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    candidates: set[str] = set()
    if parts:
        candidates.add(".".join(parts))
    if parts and parts[0] == "src" and len(parts) > 1:
        candidates.add(".".join(parts[1:]))
    return candidates


def _entrypoint_matches_node(
    entrypoint: PythonEntrypoint, nd: NodeDefinition, package_path: Path
) -> bool:
    if nd.language != "python":
        return False
    return entrypoint.module in _python_modules_for_file(nd.file_path, package_path)


def _launch_matches_definition(
    nd: NodeDefinition,
    launch_node: LaunchNode,
    package_path: Path,
    entrypoints: dict[str, PythonEntrypoint],
) -> bool:
    # A discovered Python console-script is stronger evidence than either the
    # source class name or the launch-time ROS name. If it exists, do not fall
    # through to weaker heuristics for a different source module.
    entrypoint = entrypoints.get(launch_node.executable)
    if entrypoint is not None:
        return _entrypoint_matches_node(entrypoint, nd, package_path)

    # C++ executables and legacy Python packages without discoverable console-script
    # metadata still benefit from conservative executable/symbol matching.
    source_names = {nd.name, nd.source_symbol or nd.name, _normalise_symbol(nd.name)}
    if (
        launch_node.executable in source_names
        or _normalise_symbol(launch_node.executable) in source_names
    ):
        return True

    # Launch ``name=`` is runtime identity, not source identity. Use it only as
    # a final fallback when no executable mapping was available, and compare it
    # solely with a ROS name explicitly declared in the source.
    return bool(
        launch_node.name
        and launch_node.name not in {"<unknown>", "<dynamic>"}
        and nd.declared_ros_name
        and launch_node.name == nd.declared_ros_name
    )


def _effective_deployment_name(nd: NodeDefinition, launch_node: LaunchNode) -> str:
    if launch_node.namespace == "<dynamic>" or launch_node.name == "<dynamic>":
        return DYNAMIC_SENTINEL
    base = launch_node.name
    if not base or base == "<unknown>":
        base = nd.declared_ros_name
    if not base:
        return DYNAMIC_SENTINEL
    return _apply_namespace(base, launch_node.namespace)


class UnifiedArchitectureModel:
    def __init__(self) -> None:
        self._graph: nx.MultiDiGraph = nx.MultiDiGraph()
        self._packages: list[PackageMetadata] = []
        self._nodes: list[NodeDefinition] = []
        self._interfaces: list[InterfaceDefinition] = []
        self._diagnostics: list[dict[str, Any]] = []
        # package_name → list of LaunchNodes found in launch files (per-node remaps)
        self._launch_remaps: dict[str, list[LaunchNode]] = {}
        self._launch_includes: list[dict[str, Any]] = []
        self._entrypoints: dict[str, dict[str, PythonEntrypoint]] = {}
        self._node_graph_ids: dict[tuple[str, str, str, int], str] = {}

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
                uam._entrypoints[pkg.name] = find_python_entrypoints(pkg_path)

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

                # Collect launch records (not cached — fast to parse). Index by
                # target package because bringup files commonly live elsewhere.
                for lf in find_launch_files(pkg_path):
                    try:
                        lg = analyze_launch_file(lf)
                        uam._record_launch_uncertainty(lg)
                        for launch_node in lg.nodes:
                            launch_node.source_file = lg.source_file
                            target_package = launch_node.package
                            if target_package not in {"<unknown>", "<dynamic>"}:
                                uam._launch_remaps.setdefault(target_package, []).append(
                                    launch_node
                                )
                    except Exception as exc:  # noqa: BLE001
                        uam._diagnostics.append(
                            {
                                "severity": "warning",
                                "code": "launch_parse_failed",
                                "message": str(exc),
                                "file": str(lf),
                            }
                        )
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

    def _record_launch_uncertainty(self, launch_graph: object) -> None:
        """Keep launch constructs that static analysis cannot safely execute."""
        if not isinstance(launch_graph, LaunchGraph):
            return
        self._launch_includes.extend(
            {
                "source_file": include.source_file,
                "target_file": include.target_file,
                "unresolved": include.unresolved,
            }
            for include in launch_graph.includes
        )
        if launch_graph.unresolved_branches:
            self._diagnostics.append(
                {
                    "severity": "warning",
                    "code": "launch_branch_unresolved",
                    "message": (
                        "Launch file contains substitutions, conditions, groups, or other "
                        "constructs that were not resolved statically."
                    ),
                    "file": launch_graph.source_file,
                }
            )

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

        symbol_counts = Counter((nd.package, nd.name) for nd in nodes)
        duplicate_symbols = {key for key, count in symbol_counts.items() if count > 1}

        for nd in nodes:
            nid = _definition_id(nd, duplicate_symbols)
            self._node_graph_ids[_definition_key(nd)] = nid
            g.add_node(
                nid,
                kind="Node",
                name=nd.name,
                source_symbol=nd.source_symbol or nd.name,
                declared_ros_name=nd.declared_ros_name,
                package=nd.package,
                language=nd.language,
                file_path=nd.file_path,
                line=nd.line,
                has_dynamic_names=nd.has_dynamic_names,
            )

            pkg_id = _pkg_id(nd.package)
            if pkg_id in g:
                self._add_edge(g, nid, pkg_id, rel="defined_in")

            package_path = Path(pkg_map[nd.package].path)
            matches = [
                launch_node
                for launch_node in self._launch_remaps.get(nd.package, [])
                if _launch_matches_definition(
                    nd,
                    launch_node,
                    package_path,
                    self._entrypoints.get(nd.package, {}),
                )
            ]

            actors: list[tuple[str, dict[str, str], str | None, dict[str, Any]]] = []
            if matches:
                deployments: list[dict[str, Any]] = []
                for deployment_index, launch_node in enumerate(matches):
                    deployment_id = _deployment_id(nid, launch_node, deployment_index)
                    effective_name = _effective_deployment_name(nd, launch_node)
                    unresolved = effective_name == DYNAMIC_SENTINEL or launch_node.is_unresolved
                    g.add_node(
                        deployment_id,
                        kind="Deployment",
                        name=effective_name,
                        package=nd.package,
                        executable=launch_node.executable,
                        deployment_name=launch_node.name or nd.declared_ros_name,
                        namespace=launch_node.namespace,
                        launch_file=launch_node.source_file,
                        source_node_id=nid,
                        source_symbol=nd.source_symbol or nd.name,
                        resolution="unresolved" if unresolved else "known",
                        unresolved_fields=list(launch_node.unresolved_fields),
                    )
                    self._add_edge(g, nid, deployment_id, rel="deploys_as")
                    deployment_attrs = {
                        "deployment_id": deployment_id,
                        "deployment_name": effective_name,
                        "namespace": launch_node.namespace,
                        "launch_file": launch_node.source_file,
                    }
                    actors.append(
                        (
                            deployment_id,
                            launch_node.remaps,
                            launch_node.namespace,
                            deployment_attrs,
                        )
                    )
                    deployments.append(
                        {
                            "id": deployment_id,
                            "name": effective_name,
                            "launch_name": launch_node.name or "",
                            "executable": launch_node.executable,
                            "namespace": launch_node.namespace,
                            "launch_file": launch_node.source_file,
                            "remaps": dict(launch_node.remaps),
                            "resolution": "unresolved" if unresolved else "known",
                            "unresolved_fields": list(launch_node.unresolved_fields),
                        }
                    )
                g.nodes[nid]["deployments"] = deployments
                first = deployments[0]
                g.nodes[nid]["deployment_name"] = first["launch_name"] or first["executable"]
                g.nodes[nid]["namespace"] = first["namespace"]
                g.nodes[nid]["launch_file"] = first["launch_file"]
            else:
                actors.append((nid, {}, None, {}))

            endpoint_groups = (
                (nd.publishers, "Topic", "publisher", "publishes", "msg_type"),
                (nd.subscriptions, "Topic", "subscription", "subscribes", "msg_type"),
                (nd.services, "Service", "service", "provides", "srv_type"),
                (nd.clients, "Service", "client", "calls", "srv_type"),
                (nd.action_servers, "Action", "action_server", "provides", "action_type"),
                (nd.action_clients, "Action", "action_client", "calls", "action_type"),
            )
            for endpoints, kind, role, rel, type_key in endpoint_groups:
                for index, endpoint in enumerate(endpoints):
                    for actor_id, remaps, namespace, deployment_attrs in actors:
                        actual = _apply_namespace(_apply_remap(endpoint.name, remaps), namespace)
                        comm_id = _communication_id(kind, actual, actor_id, role, index)
                        self._upsert_comm_node(
                            comm_id,
                            kind=kind,
                            name=actual,
                            type_key=type_key,
                            interface_type=endpoint.msg_type,
                            unresolved=actual == DYNAMIC_SENTINEL,
                        )
                        edge_attrs = self._endpoint_edge_attrs(endpoint, actual)
                        edge_attrs.update(deployment_attrs)
                        self._add_edge(g, actor_id, comm_id, rel=rel, **edge_attrs)

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
        """Link only source-proven interface types; never infer a type from an endpoint name."""
        g = self._graph
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
            if not resolved or resolved == "unknown":
                continue

            parts = resolved.split("/", 1)
            if len(parts) != 2:
                continue
            iid = _iface_id(parts[0], parts[1])
            if iid not in g:
                continue

            source_id = src
            if g.nodes[src].get("kind") == "Deployment":
                candidate = g.nodes[src].get("source_node_id")
                if isinstance(candidate, str) and candidate in g:
                    source_id = candidate
            if not any(
                data.get("rel") == "uses_interface"
                for data in (g.get_edge_data(source_id, iid) or {}).values()
            ):
                self._add_edge(g, source_id, iid, rel="uses_interface")

    # ── accessors ──────────────────────────────────────────────────────────

    def packages(self) -> list[PackageMetadata]:
        return list(self._packages)

    def nodes(self) -> list[NodeDefinition]:
        return list(self._nodes)

    def node_graph_id(self, node: NodeDefinition) -> str:
        """Return the graph ID for one source definition, including collision disambiguation."""
        return self._node_graph_ids.get(_definition_key(node), _node_id(node.package, node.name))

    def deployments(self) -> list[dict[str, Any]]:
        return [
            {"id": node_id, **dict(attrs)}
            for node_id, attrs in self._graph.nodes(data=True)
            if attrs.get("kind") == "Deployment"
        ]

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
                _actor_display_name(g, s)
                for s, _, d in g.in_edges(nid, data=True)
                if d.get("rel") == "publishes"
            ]
            subscribers = [
                _actor_display_name(g, s)
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
                _actor_display_name(g, s)
                for s, _, d in g.in_edges(nid, data=True)
                if d.get("rel") == "provides"
            ]
            callers = [
                _actor_display_name(g, s)
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
                _actor_display_name(g, s)
                for s, _, d in g.in_edges(nid, data=True)
                if d.get("rel") == "provides"
            ]
            clients = [
                _actor_display_name(g, s)
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
                {
                    "executable": ln.executable,
                    "name": ln.name or "",
                    "remaps": ln.remaps,
                    "namespace": ln.namespace,
                    "source_file": ln.source_file,
                    "unresolved_fields": list(ln.unresolved_fields),
                }
                for ln in nodes
            ]
            for pkg, nodes in self._launch_remaps.items()
        }

    def launch_includes(self) -> list[dict[str, Any]]:
        return [dict(include) for include in self._launch_includes]

    def summary(self) -> dict[str, int]:
        g = self._graph
        counts: dict[str, int] = {
            "packages": 0,
            "nodes": 0,
            "deployments": 0,
            "topics": 0,
            "services": 0,
            "actions": 0,
            "interfaces": 0,
        }
        kind_map = {
            "Package": "packages",
            "Node": "nodes",
            "Deployment": "deployments",
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
            "deployments": self.deployments(),
            "interfaces": [i.model_dump(mode="json") for i in self._interfaces],
            "topics": self.topics(),
            "services": self.services(),
            "actions": self.actions(),
            "launch_remaps": self.launch_remaps(),
            "launch_includes": self.launch_includes(),
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
