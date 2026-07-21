from pathlib import Path

import networkx as nx

from ros2inspector.model.schemas import DepType, PackageMetadata

_WEIGHTS = {
    "has_description": 8,
    "has_license": 8,
    "has_maintainer": 5,
    "has_maintainer_email": 4,
    "has_version": 5,
    "has_readme": 10,
    "no_self_dependency": 15,
    "no_circular_deps": 10,
    "has_test_dir": 15,
    "test_deps_declared": 10,
    "has_launch_files": 10,
}

assert sum(_WEIGHTS.values()) == 100


def score_package(pkg: PackageMetadata) -> int:
    path = Path(pkg.path)
    score = 0

    if pkg.description and len(pkg.description.strip()) > 5:
        score += _WEIGHTS["has_description"]

    if pkg.license:
        score += _WEIGHTS["has_license"]

    maintainers_with_name = [m for m in pkg.maintainers if m.strip()]
    if maintainers_with_name:
        score += _WEIGHTS["has_maintainer"]

    if any("<" in m and "@" in m for m in pkg.maintainers):
        score += _WEIGHTS["has_maintainer_email"]

    if pkg.version and pkg.version != "0.0.0":
        score += _WEIGHTS["has_version"]

    readme_candidates = ["README.md", "README.rst", "README.txt", "README"]
    if any((path / r).exists() for r in readme_candidates):
        score += _WEIGHTS["has_readme"]

    all_deps = {d for deps in pkg.dependencies.values() for d in deps}
    # Award points when the package does not declare itself as a dependency.
    # Self-dependency is always a mistake and a reliable signal of misconfiguration.
    if pkg.name not in all_deps:
        score += _WEIGHTS["no_self_dependency"]

    # Assume no cycles initially; score_workspace deducts these points for cyclic packages.
    score += _WEIGHTS["no_circular_deps"]

    test_dir = path / "test"
    if test_dir.exists() and any(test_dir.iterdir()):
        score += _WEIGHTS["has_test_dir"]

    if pkg.dependencies.get(DepType.TEST):
        score += _WEIGHTS["test_deps_declared"]

    launch_dirs = [path / "launch", path / "bringup" / "launch"]
    if any(d.exists() for d in launch_dirs):
        score += _WEIGHTS["has_launch_files"]

    return min(score, 100)


def score_workspace(packages: list[PackageMetadata]) -> dict[str, int]:
    all_names = {p.name for p in packages}
    scores: dict[str, int] = {}
    for pkg in packages:
        s = score_package(pkg)
        pkg.health_score = s
        scores[pkg.name] = s

    # Detect real circular dependencies and deduct the reserved weight from offenders.
    dep_graph: nx.DiGraph = nx.DiGraph()
    for pkg in packages:
        dep_graph.add_node(pkg.name)
        for dep_list in pkg.dependencies.values():
            for dep in dep_list:
                if dep in all_names:
                    dep_graph.add_edge(pkg.name, dep)

    cyclic: set[str] = {node for cycle in nx.simple_cycles(dep_graph) for node in cycle}
    for pkg in packages:
        if pkg.name in cyclic:
            pkg.health_score = max(0, (pkg.health_score or 0) - _WEIGHTS["no_circular_deps"])
            scores[pkg.name] = pkg.health_score

    return scores


def workspace_aggregate_score(scores: dict[str, int]) -> int:
    if not scores:
        return 0
    return round(sum(scores.values()) / len(scores))
