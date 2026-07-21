from pathlib import Path

from ros2inspector.discovery.package_finder import find_package_xml_files

WORKSPACE_A = Path(__file__).parent.parent / "fixtures" / "workspaces" / "workspace_a"


def test_finds_all_packages():
    found = find_package_xml_files(WORKSPACE_A)
    names = {f.parent.name for f in found}
    assert names == {"pkg_a", "pkg_b", "pkg_c"}


def test_respects_colcon_ignore(tmp_path):
    (tmp_path / "ignored_pkg").mkdir()
    (tmp_path / "ignored_pkg" / "COLCON_IGNORE").touch()
    (tmp_path / "ignored_pkg" / "package.xml").write_text("<package/>")
    found = find_package_xml_files(tmp_path)
    assert found == []
