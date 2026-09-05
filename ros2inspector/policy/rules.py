from __future__ import annotations

import re
from typing import Any

import networkx as nx

from ros2inspector.model.schemas import PolicyViolation, ViolationSeverity
from ros2inspector.model.uam import UnifiedArchitectureModel
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
        if not pkg.license:
            violations.append(
                PolicyViolation(
                    severity=severity,
                    rule_type="license",
                    message=f"Package '{pkg.name}' has no license declared",
                    policy_file=cfg.get("_source", "policy"),
                    affected_entities=[pkg.name],
                )
            )
        elif allowed and pkg.license not in allowed:
            violations.append(
                PolicyViolation(
                    severity=severity,
                    rule_type="license",
                    message=(
                        f"Package '{pkg.name}' uses license '{pkg.license}' "
                        f"which is not in allowed list: {allowed}"
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


def rule_topic_connectivity(
    uam: UnifiedArchitectureModel, cfg: dict[str, Any]
) -> list[PolicyViolation]:
    """Flag topics that have no publisher (orphan subscriber) or no subscriber (dead output)."""
    g = uam.graph
    source = cfg.get("_source", "policy")
    flag_no_pub: bool = cfg.get("no_publisher", True)
    flag_no_sub: bool = cfg.get("no_subscriber", True)
    sev_no_pub = _sev(cfg.get("severity_no_publisher", "warning"))
    sev_no_sub = _sev(cfg.get("severity_no_subscriber", "info"))
    exclude = _DEFAULT_SYSTEM_TOPICS | set(cfg.get("exclude", []))
    violations: list[PolicyViolation] = []

    for nid, attrs in g.nodes(data=True):
        if attrs.get("kind") != "Topic":
            continue
        if attrs.get("resolution") == "unresolved" and not cfg.get("include_unresolved", False):
            continue
        topic_name: str = attrs.get("name", "")
        if topic_name in exclude:
            continue

        in_edges = list(g.in_edges(nid, data=True))
        pub_nodes = [
            g.nodes[s].get("name", s) for s, _, d in in_edges if d.get("rel") == "publishes"
        ]
        sub_nodes = [
            g.nodes[s].get("name", s) for s, _, d in in_edges if d.get("rel") == "subscribes"
        ]

        if flag_no_pub and sub_nodes and not pub_nodes:
            violations.append(
                PolicyViolation(
                    severity=sev_no_pub,
                    rule_type="topic_connectivity",
                    message=(
                        f"Topic '{topic_name}' has no publisher "
                        f"(subscribed by: {', '.join(sub_nodes)})"
                    ),
                    policy_file=source,
                    affected_entities=[topic_name],
                )
            )

        if flag_no_sub and pub_nodes and not sub_nodes:
            violations.append(
                PolicyViolation(
                    severity=sev_no_sub,
                    rule_type="topic_connectivity",
                    message=(
                        f"Topic '{topic_name}' has no subscribers "
                        f"(published by: {', '.join(pub_nodes)})"
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
        if skip_dynamic and attrs.get("has_dynamic_names"):
            continue

        comm_edges = [d for _, _, d in g.out_edges(nid, data=True) if d.get("rel") in _COMM_RELS]
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


def rule_service_connectivity(
    uam: UnifiedArchitectureModel, cfg: dict[str, Any]
) -> list[PolicyViolation]:
    """Flag services with a provider/caller mismatch in either direction."""
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
        providers = [
            g.nodes[s].get("name", s) for s, _, d in in_edges if d.get("rel") == "provides"
        ]
        callers = [g.nodes[s].get("name", s) for s, _, d in in_edges if d.get("rel") == "calls"]
        if providers and not callers:
            violations.append(
                PolicyViolation(
                    severity=severity,
                    rule_type="service_connectivity",
                    message=(
                        f"Service '{svc_name}' has no callers (provided by: {', '.join(providers)})"
                    ),
                    policy_file=source,
                    affected_entities=[svc_name],
                )
            )
        if callers and not providers:
            violations.append(
                PolicyViolation(
                    severity=missing_provider_severity,
                    rule_type="service_connectivity",
                    message=(
                        f"Service '{svc_name}' has callers but no provider "
                        f"(called by: {', '.join(callers)})"
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
        servers = [g.nodes[s].get("name", s) for s, _, d in in_edges if d.get("rel") == "provides"]
        clients = [g.nodes[s].get("name", s) for s, _, d in in_edges if d.get("rel") == "calls"]
        if servers and not clients:
            violations.append(
                PolicyViolation(
                    severity=severity,
                    rule_type="action_connectivity",
                    message=(
                        f"Action '{action_name}' has no clients (server: {', '.join(servers)})"
                    ),
                    policy_file=source,
                    affected_entities=[action_name],
                )
            )
        if clients and not servers:
            violations.append(
                PolicyViolation(
                    severity=missing_server_severity,
                    rule_type="action_connectivity",
                    message=(
                        f"Action '{action_name}' has clients but no server "
                        f"(clients: {', '.join(clients)})"
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
    "service_connectivity": rule_service_connectivity,
    "action_connectivity": rule_action_connectivity,
}

__all__ = ["_RULE_RUNNERS"]
