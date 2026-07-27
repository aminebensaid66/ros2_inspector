from __future__ import annotations

import os
from pathlib import Path

import pytest

from ros2inspector.discovery import WorkspaceAccessError, find_workspace_root
from ros2inspector.discovery import workspace as workspace_module
from ros2inspector.discovery.package_finder import find_package_xml_files


def test_empty_directory_is_used_as_workspace_root(tmp_path: Path) -> None:
    assert find_workspace_root(tmp_path) == tmp_path.resolve()
    assert find_package_xml_files(tmp_path) == []


def test_unreadable_ancestor_does_not_abort_workspace_discovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    supplied = tmp_path / "outer" / "workspace"
    supplied.mkdir(parents=True)
    blocked_parent = supplied.parent.resolve()
    original = workspace_module._has_workspace_marker

    def fake_has_marker(path: Path) -> bool:
        if path == blocked_parent:
            raise PermissionError(13, "Permission denied", str(path))
        return original(path)

    monkeypatch.setattr(workspace_module, "_has_workspace_marker", fake_has_marker)

    assert find_workspace_root(supplied) == supplied.resolve()


def test_permission_error_on_supplied_workspace_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_iterdir = Path.iterdir

    def fake_iterdir(path: Path):  # type: ignore[no-untyped-def]
        if path == tmp_path.resolve():
            raise PermissionError(13, "Permission denied", str(path))
        return original_iterdir(path)

    monkeypatch.setattr(Path, "iterdir", fake_iterdir)

    with pytest.raises(WorkspaceAccessError, match="Cannot access workspace path"):
        find_package_xml_files(tmp_path)


@pytest.mark.skipif(
    os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="POSIX non-root permission semantics required",
)
def test_real_unreadable_workspace_is_reported(tmp_path: Path) -> None:
    blocked = tmp_path / "blocked"
    blocked.mkdir()
    blocked.chmod(0)
    try:
        with pytest.raises(WorkspaceAccessError):
            find_package_xml_files(blocked)
    finally:
        blocked.chmod(0o700)


def test_direct_src_path_with_package_is_preserved(tmp_path: Path) -> None:
    src = tmp_path / "src"
    package = src / "demo"
    package.mkdir(parents=True)
    package_xml = package / "package.xml"
    package_xml.write_text("<package/>", encoding="utf-8")

    assert find_workspace_root(src) == tmp_path.resolve()
    assert find_package_xml_files(src) == [package_xml.resolve()]
