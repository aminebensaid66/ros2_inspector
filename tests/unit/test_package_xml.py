from pathlib import Path

from ros2inspector.model.schemas import DepType, PackageType
from ros2inspector.static.package_xml import parse_package_xml

FIXTURE_ROOT = Path(__file__).parent.parent / "fixtures" / "workspaces" / "workspace_a" / "src"


def test_parse_pkg_a():
    meta = parse_package_xml(FIXTURE_ROOT / "pkg_a" / "package.xml")
    assert meta.name == "pkg_a"
    assert meta.version == "1.0.0"
    assert meta.package_type == PackageType.AMENT_PYTHON
    assert meta.license == "MIT"
    assert "Dev User <dev@example.com>" in meta.maintainers
    assert "rclpy" in meta.dependencies.get(DepType.EXEC, [])


def test_parse_pkg_c_detects_metapackage():
    meta = parse_package_xml(FIXTURE_ROOT / "pkg_c" / "package.xml")
    assert meta.name == "pkg_c"
    assert meta.package_type == PackageType.META


def test_pkg_b_has_exec_dep_on_pkg_a():
    meta = parse_package_xml(FIXTURE_ROOT / "pkg_b" / "package.xml")
    assert "pkg_a" in meta.dependencies.get(DepType.EXEC, [])


def test_metapackage_filter_classification_is_explicit():
    """Descriptions alone must not classify packages; the XML marker is authoritative."""
    meta = parse_package_xml(FIXTURE_ROOT / "pkg_c" / "package.xml")
    assert meta is not None
    assert meta.package_type is PackageType.META
