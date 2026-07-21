from pathlib import Path

from ros2inspector.model.schemas import PackageMetadata, PackageType
from ros2inspector.static.health_scorer import (
    score_package,
    score_workspace,
    workspace_aggregate_score,
)

FIXTURE_ROOT = Path(__file__).parent.parent / "fixtures" / "workspaces" / "workspace_a"


def _make_pkg(name="test", **kwargs) -> PackageMetadata:
    defaults = dict(
        name=name,
        version="1.0.0",
        package_type=PackageType.AMENT_PYTHON,
        maintainers=["Dev <dev@example.com>"],
        license="MIT",
        description="A well-documented package.",
        path=str(FIXTURE_ROOT / "pkg_a"),
        dependencies={},
    )
    defaults.update(kwargs)
    return PackageMetadata(**defaults)


def test_full_score_well_documented_package():
    pkg = _make_pkg()
    score = score_package(pkg)
    # Should score high: has description, license, maintainer, email, version, no circ deps
    assert score >= 50


def test_missing_license_reduces_score():
    pkg_with = _make_pkg(license="MIT")
    pkg_without = _make_pkg(license=None)
    assert score_package(pkg_with) > score_package(pkg_without)


def test_missing_description_reduces_score():
    pkg_with = _make_pkg(description="Does something useful.")
    pkg_without = _make_pkg(description=None)
    assert score_package(pkg_with) > score_package(pkg_without)


def test_workspace_aggregate():
    pkgs = [_make_pkg(name=f"pkg_{i}") for i in range(3)]
    scores = score_workspace(pkgs)
    assert len(scores) == 3
    agg = workspace_aggregate_score(scores)
    assert 0 <= agg <= 100
    # health_score should be set on the model
    assert all(p.health_score is not None for p in pkgs)
