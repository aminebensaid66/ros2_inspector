from __future__ import annotations

import re
from typing import Any

import networkx as nx

from ros2inspector.model.names import canonical_interface
from ros2inspector.model.schemas import PolicyViolation, ViolationSeverity
from ros2inspector.model.uam import UnifiedArchitectureModel
from ros2inspector.policy.schema import FieldSpec
from ros2inspector.static import workspace_aggregate_score


def _sev(raw: str) -> ViolationSeverity:
    try:
        return ViolationSeverity(raw.lower())
    except ValueError:
        return ViolationSeverity.WARNING


# ---------------------------------------------------------------------------
# Individual rule runners — each takes the UAM + rule config dict
# ---------------------------------------------------------------------------


def rule_health_threshold(
    uam: UnifiedArchitectureModel, cfg: dict[str, Any]
) -> list[PolicyViolation]:
    min_score: int = int(cfg.get("min_score", 70))
    severity = _sev(cfg.get("severity", "warning"))
    violations: list[PolicyViolation] = []
    scores = {p.name: (p.health_score or 0) for p in uam.packages()}
    for pkg_name, score in scores.items():
        if score < min_score:
            violations.append(
                PolicyViolation(
                    severity=severity,
                    rule_type="health_threshold",
                    message=(
                        f"Package '{pkg_name}' health score {score}/100 "
                        f"is below threshold {min_score}"
                    ),
                    policy_file=cfg.get("_source", "policy"),
                    affected_entities=[pkg_name],
                )
            )
    agg = workspace_aggregate_score(scores)
    workspace_min: int = int(cfg.get("workspace_min_score", 0))
    if workspace_min and agg < workspace_min:
        violations.append(
            PolicyViolation(
                severity=severity,
                rule_type="health_threshold",
                message=(
                    f"Workspace aggregate health score {agg}/100 is below threshold {workspace_min}"
                ),
                policy_file=cfg.get("_source", "policy"),
                affected_entities=["workspace"],
            )
        )
    return violations


def rule_license(uam: UnifiedArchitectureModel, cfg: dict[str, Any]) -> list[PolicyViolation]:
    allowed: list[str] = cfg.get("allowed", [])
    severity = _sev(cfg.get("severity", "error"))
    violations: list[PolicyViolation] = []
    for pkg in uam.packages():
        licenses = pkg.licenses or ([pkg.license] if pkg.license else [])
        if not licenses:
            violations.append(
                PolicyViolation(
                    severity=severity,
                    rule_type="license",
                    message=f"Package '{pkg.name}' has no license declared",
                    policy_file=cfg.get("_source", "policy"),
                    affected_entities=[pkg.name],
                )
            )
            continue
        disallowed = [
            license_name for license_name in licenses if allowed and license_name not in allowed
        ]
        if disallowed:
            violations.append(
                PolicyViolation(
                    severity=severity,
                    rule_type="license",
                    message=(
                        f"Package '{pkg.name}' uses disallowed license(s) {disallowed}; "
                        f"allowed: {allowed}"
                    ),
                    policy_file=cfg.get("_source", "policy"),
                    affected_entities=[pkg.name],
                )
            )
    return violations


def rule_naming(uam: UnifiedArchitectureModel, cfg: dict[str, Any]) -> list[PolicyViolation]:
    violations: list[PolicyViolation] = []
    source = cfg.get("_source", "policy")

    def _check(name: str, pattern: str, kind: str, severity: ViolationSeverity) -> None:
        if not re.match(pattern, name):
            violations.append(
                PolicyViolation(
                    severity=severity,
                    rule_type="naming",
                    message=f"{kind} '{name}' does not match naming pattern '{pattern}'",
                    policy_file=source,
                    affected_entities=[name],
                )
            )

    if "packages" in cfg:
        sub = cfg["packages"]
        pat = sub.get("pattern", r"^[a-z][a-z0-9_]*$")
        sev = _sev(sub.get("severity", "warning"))
        for pkg in uam.packages():
            _check(pkg.name, pat, "Package", sev)

    if "nodes" in cfg:
        sub = cfg["nodes"]
        pat = sub.get("pattern", r"^[a-z][a-z0-9_]*$")
        sev = _sev(sub.get("severity", "warning"))
        for nd in uam.nodes():
            _check(nd.name, pat, "Node", sev)

    if "topics" in cfg:
        sub = cfg["topics"]
        pat = sub.get("pattern", r"^/?[a-z][a-z0-9_/]*$")
        sev = _sev(sub.get("severity", "info"))
        for t in uam.topics():
            _check(t["name"], pat, "Topic", sev)

    if "services" in cfg:
        sub = cfg["services"]
        pat = sub.get("pattern", r"^/?[a-z][a-z0-9_/]*$")
        sev = _sev(sub.get("severity", "info"))
        for s in uam.services():
            _check(s["name"], pat, "Service", sev)

    return violations


def rule_dependency(uam: UnifiedArchitectureModel, cfg: dict[str, Any]) -> list[PolicyViolation]:
    violations: list[PolicyViolation] = []
    source = cfg.get("_source", "policy")
    g = uam.graph

    for forbidden in cfg.get("forbidden", []):
        frm = forbidden["from"]
        to = forbidden["to"]
        severity = _sev(forbidden.get("severity", "error"))
        src_id = f"pkg:{frm}"
        dst_id = f"pkg:{to}"
        if g.has_node(src_id) and g.has_node(dst_id):
            if g.has_edge(src_id, dst_id) or nx.has_path(g, src_id, dst_id):
                violations.append(
                    PolicyViolation(
                        severity=severity,
                        rule_type="dependency",
                        message=f"Forbidden dependency: '{frm}' must not depend on '{to}'",
                        policy_file=source,
                        affected_entities=[frm, to],
                    )
                )

    for required in cfg.get("required", []):
        pkg_name = required["package"]
        dep_name = required["depends_on"]
        severity = _sev(required.get("severity", "warning"))
        src_id = f"pkg:{pkg_name}"
        dst_id = f"pkg:{dep_name}"
        if g.has_node(src_id) and not (
            g.has_edge(src_id, dst_id) or (g.has_node(dst_id) and nx.has_path(g, src_id, dst_id))
        ):
            violations.append(
                PolicyViolation(
                    severity=severity,
                    rule_type="dependency",
                    message=(
                        f"Required dependency missing: '{pkg_name}' must depend on '{dep_name}'"
                    ),
                    policy_file=source,
                    affected_entities=[pkg_name, dep_name],
                )
            )

    return violations


def rule_no_circular_deps(
    uam: UnifiedArchitectureModel, cfg: dict[str, Any]
) -> list[PolicyViolation]:
    severity = _sev(cfg.get("severity", "error"))
    source = cfg.get("_source", "policy")
    violations: list[PolicyViolation] = []

    # Derive the dep graph directly from the UAM graph's depends_on edges to
    # avoid repeating the package-dependency extraction already done during build.
    g = uam.graph
    dep_graph: nx.DiGraph = nx.DiGraph()
    for src, dst, data in g.edges(data=True):
        if data.get("rel") == "depends_on":
            src_name = g.nodes[src].get("name", "")
            dst_name = g.nodes[dst].get("name", "")
            if src_name and dst_name:
                dep_graph.add_edge(src_name, dst_name)

    cycles = list(nx.simple_cycles(dep_graph))
    for cycle in cycles:
        cycle_str = " → ".join(cycle + [cycle[0]])
        violations.append(
            PolicyViolation(
                severity=severity,
                rule_type="no_circular_deps",
                message=f"Circular dependency detected: {cycle_str}",
                policy_file=source,
                affected_entities=cycle,
            )
        )
    return violations


def rule_maintainer_required(
    uam: UnifiedArchitectureModel, cfg: dict[str, Any]
) -> list[PolicyViolation]:
    severity = _sev(cfg.get("severity", "warning"))
    require_email: bool = cfg.get("require_email", False)
    source = cfg.get("_source", "policy")
    violations: list[PolicyViolation] = []
    for pkg in uam.packages():
        if not pkg.maintainers:
            violations.append(
                PolicyViolation(
                    severity=severity,
                    rule_type="maintainer_required",
                    message=f"Package '{pkg.name}' has no maintainer declared",
                    policy_file=source,
                    affected_entities=[pkg.name],
                )
            )
        elif require_email and not any("<" in m and "@" in m for m in pkg.maintainers):
            violations.append(
                PolicyViolation(
                    severity=severity,
                    rule_type="maintainer_required",
                    message=f"Package '{pkg.name}' maintainer has no email address",
                    policy_file=source,
                    affected_entities=[pkg.name],
                )
            )
    return violations


def rule_version_not_default(
    uam: UnifiedArchitectureModel, cfg: dict[str, Any]
) -> list[PolicyViolation]:
    severity = _sev(cfg.get("severity", "info"))
    default_version: str = cfg.get("default_version", "0.0.0")
    source = cfg.get("_source", "policy")
    violations: list[PolicyViolation] = []
    for pkg in uam.packages():
        if pkg.version == default_version:
            violations.append(
                PolicyViolation(
                    severity=severity,
                    rule_type="version_not_default",
                    message=f"Package '{pkg.name}' still uses default version '{default_version}'",
                    policy_file=source,
                    affected_entities=[pkg.name],
                )
            )
    return violations


_DEFAULT_SYSTEM_TOPICS: frozenset[str] = frozenset(
    ["/rosout", "/parameter_events", "/clock", "/tf", "/tf_static"]
)


def _unattributed_endpoints(
    uam: UnifiedArchitectureModel, kind: str
) -> list[tuple[str, str | None]]:
    """(relation, literal name or None) of endpoint calls the parser could not attach to a node."""
    return [
        (str(d.get("endpoint_rel")), d.get("endpoint_name"))
        for d in uam.diagnostics()
        if d.get("code") == "source_analysis_incomplete" and d.get("endpoint_kind") == kind
    ]


def _could_be(endpoints: list[tuple[str, str | None]], rel: str, name: str) -> bool:
    """Whether an unattributed endpoint with relation *rel* could resolve to *name*."""
    for endpoint_rel, raw in endpoints:
        if endpoint_rel != rel:
            continue
        if raw is None:
            return True
        if raw.startswith("/"):
            if raw == name:
                return True
        # A relative or private name resolves under a namespace the parser does not know.
        elif name.endswith("/" + raw.lstrip("~/")):
            return True
    return False


def _source_gap(uam: UnifiedArchitectureModel) -> bool:
    return any(
        d.get("code") == "source_parse_failed"
        or (d.get("code") == "source_analysis_incomplete" and not d.get("endpoint_kind"))
        for d in uam.diagnostics()
    )


def _parse_gap_note(uam: UnifiedArchitectureModel) -> str:
    failed = {d.get("file") for d in uam.diagnostics() if d.get("code") == "source_parse_failed"}
    if failed:
        return (
            f"; {len(failed)} source file(s) could not be parsed; missing peer is not proven "
            "while source analysis is incomplete"
        )
    if _source_gap(uam):
        return "; missing peer is not proven while source analysis is incomplete"
    return ""


def _unresolved_peers(uam: UnifiedArchitectureModel) -> dict[tuple[str, str], list[str | None]]:
    """Name suffixes of endpoints whose deployment could not be resolved, by kind and role.

    A ``None`` entry means the endpoint could take any name.
    """
    peers: dict[tuple[str, str], list[str | None]] = {}
    for _, target, data in uam.graph.edges(data=True):
        kind = uam.graph.nodes[target].get("kind")
        if kind in {"Topic", "Service", "Action"} and data.get("resolution") == "unresolved":
            suffix = data.get("name_suffix")
            peers.setdefault((str(kind), str(data.get("rel"))), []).append(
                str(suffix) if suffix else None
            )
    if any(d.get("code") == "launch_include_unresolved" for d in uam.diagnostics()):
        for kind, roles in (
            ("Topic", ("publishes", "subscribes")),
            ("Service", ("provides", "calls")),
            ("Action", ("provides", "calls")),
        ):
            for role in roles:
                peers.setdefault((kind, role), []).append(None)
    return peers


def _may_be_provided(
    peers: dict[tuple[str, str], list[str | None]], kind: str, rel: str, name: str
) -> bool:
    """True when an unresolved deployment could still be the missing peer on ``name``."""
    return any(suffix is None or name.endswith(suffix) for suffix in peers.get((kind, rel), []))


def _interface_type_mismatches(
    uam: UnifiedArchitectureModel, cfg: dict[str, Any]
) -> list[PolicyViolation]:
    findings = []
    graph = uam.graph
    for target, attrs in graph.nodes(data=True):
        if attrs.get("kind") not in {"Topic", "Service", "Action"}:
            continue
        endpoints: dict[str, set[str]] = {}
        for source, _, data in graph.in_edges(target, data=True):
            value = canonical_interface(str(data.get("msg_type", "unknown")))
            if value == "unknown" or data.get("resolution", "known") != "known":
                continue
            actor = graph.nodes[source]
            name = str(actor.get("declared_ros_name") or actor.get("name", source))
            endpoints.setdefault(value, set()).add(name)
        if len(endpoints) > 1:
            details = "; ".join(
                f"{value} ({', '.join(sorted(names))})"
                for value, names in sorted(endpoints.items())
            )
            findings.append(
                PolicyViolation(
                    severity=_sev(cfg.get("severity_type_mismatch", "error")),
                    rule_type="interface_type_mismatch",
                    policy_file=cfg.get("_source", "policy"),
                    message=(
                        f"{attrs['kind']} '{attrs['name']}' has incompatible interface types: "
                        f"{details}"
                    ),
                    affected_entities=[
                        str(attrs["name"]),
                        *sorted({name for names in endpoints.values() for name in names}),
                    ],
                )
            )
    return findings


def rule_topic_connectivity(
    uam: UnifiedArchitectureModel, cfg: dict[str, Any]
) -> list[PolicyViolation]:
    """Flag topics that have no publisher (orphan subscriber) or no subscriber (dead output)."""
    unattributed = _unattributed_endpoints(uam, "topic")
    gap = _parse_gap_note(uam)
    g = uam.graph
    source = cfg.get("_source", "policy")
    flag_no_pub: bool = cfg.get("no_publisher", True)
    flag_no_sub: bool = cfg.get("no_subscriber", True)
    sev_no_pub = _sev(cfg.get("severity_no_publisher", "warning"))
    sev_no_sub = _sev(cfg.get("severity_no_subscriber", "info"))
    # Topic names are resolved (``cmd_vel`` at the root is ``/cmd_vel``), so a relative
    # name in the policy excludes the root-namespace topic as well.
    user_exclude = {str(name) for name in cfg.get("exclude", [])}
    exclude = (
        _DEFAULT_SYSTEM_TOPICS
        | user_exclude
        | {f"/{name}" for name in user_exclude if not name.startswith(("/", "~"))}
    )
    violations = _interface_type_mismatches(uam, cfg) if cfg.get("type_mismatch", True) else []
    unresolved = _unresolved_peers(uam)

    for nid, attrs in g.nodes(data=True):
        if attrs.get("kind") != "Topic":
            continue
        if attrs.get("resolution") == "unresolved" and not cfg.get("include_unresolved", False):
            continue
        topic_name: str = attrs.get("name", "")
        if topic_name in exclude:
            continue

        in_edges = list(g.in_edges(nid, data=True))
        definite = [item for item in in_edges if item[2].get("resolution", "known") == "known"]
        if not definite and in_edges:
            continue
        possible_publishers = any(d.get("rel") == "publishes" for _, _, d in in_edges) or _could_be(
            unattributed, "publishes", topic_name
        )
        possible_subscribers = any(
            d.get("rel") == "subscribes" for _, _, d in in_edges
        ) or _could_be(unattributed, "subscribes", topic_name)
        in_edges = definite
        pub_nodes = [
            g.nodes[s].get("name", s) for s, _, d in in_edges if d.get("rel") == "publishes"
        ]
        sub_nodes = [
            g.nodes[s].get("name", s) for s, _, d in in_edges if d.get("rel") == "subscribes"
        ]

        if (
            flag_no_pub
            and sub_nodes
            and not possible_publishers
            and not _may_be_provided(unresolved, "Topic", "publishes", topic_name)
        ):
            violations.append(
                PolicyViolation(
                    severity=ViolationSeverity.INFO if gap else sev_no_pub,
                    confidence="uncertain" if gap else "known",
                    rule_type="topic_connectivity",
                    message=(
                        f"Topic '{topic_name}' has no publisher "
                        f"(subscribed by: {', '.join(sub_nodes)}{gap})"
                    ),
                    policy_file=source,
                    affected_entities=[topic_name],
                )
            )

        if (
            flag_no_sub
            and pub_nodes
            and not possible_subscribers
            and not _may_be_provided(unresolved, "Topic", "subscribes", topic_name)
        ):
            violations.append(
                PolicyViolation(
                    severity=ViolationSeverity.INFO if gap else sev_no_sub,
                    confidence="uncertain" if gap else "known",
                    rule_type="topic_connectivity",
                    message=(
                        f"Topic '{topic_name}' has no subscribers "
                        f"(published by: {', '.join(pub_nodes)}{gap})"
                    ),
                    policy_file=source,
                    affected_entities=[topic_name],
                )
            )

    return violations


def rule_node_isolation(
    uam: UnifiedArchitectureModel, cfg: dict[str, Any]
) -> list[PolicyViolation]:
    """Flag nodes with no detected communication (no pub/sub/service/action edges)."""
    g = uam.graph
    source = cfg.get("_source", "policy")
    severity = _sev(cfg.get("severity", "warning"))
    skip_dynamic: bool = cfg.get("skip_dynamic_names", True)
    violations: list[PolicyViolation] = []

    _COMM_RELS = {"publishes", "subscribes", "provides", "calls"}

    for nid, attrs in g.nodes(data=True):
        if attrs.get("kind") != "Node":
            continue
        node_name: str = attrs.get("name", "")
        if attrs.get("analysis_incomplete") or attrs.get("is_abstract"):
            continue
        if skip_dynamic and attrs.get("has_dynamic_names"):
            continue

        comm_edges = [
            d
            for _, _, d in g.out_edges(nid, data=True)
            if d.get("rel") in _COMM_RELS and d.get("resolution", "known") == "known"
        ]
        deployments = [
            target
            for _, target, data in g.out_edges(nid, data=True)
            if data.get("rel") == "deploys_as"
        ]
        for deployment_id in deployments:
            comm_edges.extend(
                data
                for _, _, data in g.out_edges(deployment_id, data=True)
                if data.get("rel") in _COMM_RELS and data.get("resolution", "known") == "known"
            )
        if (
            deployments
            and not comm_edges
            and any(g.nodes[d].get("resolution") != "known" for d in deployments)
        ):
            continue
        if not comm_edges:
            violations.append(
                PolicyViolation(
                    severity=severity,
                    rule_type="node_isolation",
                    message=(
                        f"Node '{node_name}' has no detected communication "
                        f"(no publishers, subscribers, services, or action clients/servers)"
                    ),
                    policy_file=source,
                    affected_entities=[node_name],
                )
            )

    return violations


def rule_qos_compatibility(
    uam: UnifiedArchitectureModel, cfg: dict[str, Any]
) -> list[PolicyViolation]:
    """Report proven reliability or durability incompatibility on a topic."""
    graph = uam.graph
    severity = _sev(cfg.get("severity", "warning"))
    findings: list[PolicyViolation] = []
    for topic_id, attrs in graph.nodes(data=True):
        if attrs.get("kind") != "Topic" or attrs.get("resolution") == "unresolved":
            continue
        edges = [
            (source, data)
            for source, _, data in graph.in_edges(topic_id, data=True)
            if data.get("resolution", "known") == "known" and data.get("qos")
        ]
        publishers = [(source, data) for source, data in edges if data.get("rel") == "publishes"]
        subscribers = [(source, data) for source, data in edges if data.get("rel") == "subscribes"]
        for pub_id, pub in publishers:
            for sub_id, sub in subscribers:
                offered = pub["qos"]
                requested = sub["qos"]
                failures = []
                if (
                    offered.get("reliability") == "best_effort"
                    and (requested.get("reliability") or "reliable") == "reliable"
                ):
                    failures.append("reliability")
                if (offered.get("durability") or "volatile") == "volatile" and requested.get(
                    "durability"
                ) == "transient_local":
                    failures.append("durability")
                for policy in ("deadline", "liveliness_lease_duration"):
                    offer = offered.get(policy)
                    request = requested.get(policy)
                    if isinstance(offer, (int, float)) and isinstance(request, (int, float)):
                        if offer > request:
                            failures.append(policy)
                if (
                    offered.get("liveliness") == "automatic"
                    and requested.get("liveliness") == "manual_by_topic"
                ):
                    failures.append("liveliness")
                if not failures:
                    continue
                findings.append(
                    PolicyViolation(
                        severity=severity,
                        rule_type="qos_compatibility",
                        message=(
                            f"Topic '{attrs.get('name')}' has incompatible {', '.join(failures)} "
                            f"between publisher '{graph.nodes[pub_id].get('name')}' and "
                            f"subscriber '{graph.nodes[sub_id].get('name')}'"
                        ),
                        policy_file=cfg.get("_source", "policy"),
                        affected_entities=[str(attrs.get("name"))],
                        file_path=sub.get("file_path"),
                        line=sub.get("line"),
                    )
                )
    return findings


def rule_service_connectivity(
    uam: UnifiedArchitectureModel, cfg: dict[str, Any]
) -> list[PolicyViolation]:
    """Flag services with a provider/caller mismatch in either direction."""
    unattributed = _unattributed_endpoints(uam, "service")
    gap = _parse_gap_note(uam)
    unresolved = _unresolved_peers(uam)
    g = uam.graph
    source = cfg.get("_source", "policy")
    severity = _sev(cfg.get("severity", "info"))
    missing_provider_severity = _sev(cfg.get("missing_provider_severity", "warning"))
    violations: list[PolicyViolation] = []

    for nid, attrs in g.nodes(data=True):
        if attrs.get("kind") != "Service":
            continue
        if attrs.get("resolution") == "unresolved" and not cfg.get("include_unresolved", False):
            continue
        svc_name: str = attrs.get("name", "")
        in_edges = list(g.in_edges(nid, data=True))
        possible_provider = any(d.get("rel") == "provides" for _, _, d in in_edges) or _could_be(
            unattributed, "provides", svc_name
        )
        possible_caller = any(d.get("rel") == "calls" for _, _, d in in_edges) or _could_be(
            unattributed, "calls", svc_name
        )
        in_edges = [e for e in in_edges if e[2].get("resolution", "known") == "known"]
        providers = [
            g.nodes[s].get("name", s) for s, _, d in in_edges if d.get("rel") == "provides"
        ]
        callers = [g.nodes[s].get("name", s) for s, _, d in in_edges if d.get("rel") == "calls"]
        if (
            providers
            and not possible_caller
            and not _may_be_provided(unresolved, "Service", "calls", svc_name)
        ):
            violations.append(
                PolicyViolation(
                    severity=ViolationSeverity.INFO if gap else severity,
                    confidence="uncertain" if gap else "known",
                    rule_type="service_connectivity",
                    message=(
                        f"Service '{svc_name}' has no callers "
                        f"(provided by: {', '.join(providers)}{gap})"
                    ),
                    policy_file=source,
                    affected_entities=[svc_name],
                )
            )
        if (
            callers
            and not possible_provider
            and not _may_be_provided(unresolved, "Service", "provides", svc_name)
        ):
            violations.append(
                PolicyViolation(
                    severity=ViolationSeverity.INFO
                    if _source_gap(uam)
                    else missing_provider_severity,
                    confidence="uncertain" if gap else "known",
                    rule_type="service_connectivity",
                    message=(
                        f"Service '{svc_name}' has callers but no provider "
                        f"(called by: {', '.join(callers)}{gap})"
                    ),
                    policy_file=source,
                    affected_entities=[svc_name],
                )
            )

    return violations


def rule_action_connectivity(
    uam: UnifiedArchitectureModel, cfg: dict[str, Any]
) -> list[PolicyViolation]:
    """Flag actions with a server/client mismatch in either direction."""
    unattributed = _unattributed_endpoints(uam, "action")
    gap = _parse_gap_note(uam)
    unresolved = _unresolved_peers(uam)
    g = uam.graph
    source = cfg.get("_source", "policy")
    severity = _sev(cfg.get("severity", "warning"))
    missing_server_severity = _sev(cfg.get("missing_server_severity", "warning"))
    violations: list[PolicyViolation] = []

    for nid, attrs in g.nodes(data=True):
        if attrs.get("kind") != "Action":
            continue
        if attrs.get("resolution") == "unresolved" and not cfg.get("include_unresolved", False):
            continue
        action_name: str = attrs.get("name", "")
        in_edges = list(g.in_edges(nid, data=True))
        definite = [item for item in in_edges if item[2].get("resolution", "known") == "known"]
        if not definite and in_edges:
            continue
        possible_server = any(d.get("rel") == "provides" for _, _, d in in_edges) or _could_be(
            unattributed, "provides", action_name
        )
        possible_client = any(d.get("rel") == "calls" for _, _, d in in_edges) or _could_be(
            unattributed, "calls", action_name
        )
        in_edges = definite
        servers = [g.nodes[s].get("name", s) for s, _, d in in_edges if d.get("rel") == "provides"]
        clients = [g.nodes[s].get("name", s) for s, _, d in in_edges if d.get("rel") == "calls"]
        if (
            servers
            and not possible_client
            and not _may_be_provided(unresolved, "Action", "calls", action_name)
        ):
            violations.append(
                PolicyViolation(
                    severity=ViolationSeverity.INFO if gap else severity,
                    confidence="uncertain" if gap else "known",
                    rule_type="action_connectivity",
                    message=(
                        f"Action '{action_name}' has no clients (server: {', '.join(servers)}{gap})"
                    ),
                    policy_file=source,
                    affected_entities=[action_name],
                )
            )
        if (
            clients
            and not possible_server
            and not _may_be_provided(unresolved, "Action", "provides", action_name)
        ):
            violations.append(
                PolicyViolation(
                    severity=ViolationSeverity.INFO
                    if _source_gap(uam)
                    else missing_server_severity,
                    confidence="uncertain" if gap else "known",
                    rule_type="action_connectivity",
                    message=(
                        f"Action '{action_name}' has clients but no server "
                        f"(clients: {', '.join(clients)}{gap})"
                    ),
                    policy_file=source,
                    affected_entities=[action_name],
                )
            )

    return violations


_RULE_RUNNERS = {
    "health_threshold": rule_health_threshold,
    "license": rule_license,
    "naming": rule_naming,
    "dependency": rule_dependency,
    "no_circular_deps": rule_no_circular_deps,
    "maintainer_required": rule_maintainer_required,
    "version_not_default": rule_version_not_default,
    "topic_connectivity": rule_topic_connectivity,
    "node_isolation": rule_node_isolation,
    "qos_compatibility": rule_qos_compatibility,
    "service_connectivity": rule_service_connectivity,
    "action_connectivity": rule_action_connectivity,
}

_STRING = FieldSpec("string")
_BOOL = FieldSpec("bool")
_SEVERITY = FieldSpec("severity")
_SCORE = FieldSpec("int", minimum=0, maximum=100)
_STRINGS = FieldSpec("list", item=_STRING)
_COMMON = {"type": _STRING, "_source": _STRING, "_line": FieldSpec("int", minimum=0)}
_NAMING = FieldSpec("mapping", fields={"pattern": FieldSpec("pattern"), "severity": _SEVERITY})

RULE_SCHEMAS: dict[str, dict[str, FieldSpec]] = {
    "health_threshold": {"min_score": _SCORE, "workspace_min_score": _SCORE},
    "license": {"allowed": _STRINGS},
    "naming": dict.fromkeys(("packages", "nodes", "topics", "services"), _NAMING),
    "dependency": {
        "forbidden": FieldSpec(
            "list",
            item=FieldSpec(
                "mapping",
                fields={"from": _STRING, "to": _STRING, "severity": _SEVERITY},
                required=frozenset({"from", "to"}),
            ),
        ),
        "required": FieldSpec(
            "list",
            item=FieldSpec(
                "mapping",
                fields={"package": _STRING, "depends_on": _STRING, "severity": _SEVERITY},
                required=frozenset({"package", "depends_on"}),
            ),
        ),
    },
    "no_circular_deps": {},
    "maintainer_required": {"require_email": _BOOL},
    "version_not_default": {"default_version": _STRING},
    "topic_connectivity": {
        "no_publisher": _BOOL,
        "no_subscriber": _BOOL,
        "include_unresolved": _BOOL,
        "exclude": _STRINGS,
        "severity_no_publisher": _SEVERITY,
        "severity_no_subscriber": _SEVERITY,
        "type_mismatch": _BOOL,
        "severity_type_mismatch": _SEVERITY,
    },
    "node_isolation": {"skip_dynamic_names": _BOOL},
    "qos_compatibility": {},
    "service_connectivity": {"missing_provider_severity": _SEVERITY, "include_unresolved": _BOOL},
    "action_connectivity": {"missing_server_severity": _SEVERITY, "include_unresolved": _BOOL},
}
for _fields in RULE_SCHEMAS.values():
    _fields.update(_COMMON)
    _fields["severity"] = _SEVERITY

__all__ = ["_RULE_RUNNERS"]
