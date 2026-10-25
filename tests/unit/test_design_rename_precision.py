from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest

from ros2inspector.design.codegen import (
    plan_package_edits,
    read_manifest,
    render_package,
    write_package,
)
from ros2inspector.design.models import PackageSpec
from tests.unit.test_design_server_http import _node, _pkg, _post, _state, server

__all__ = ["server"]


@pytest.mark.parametrize("quote, keyword", [("'", False), ('"', False), ("'", True)])
def test_node_rename_changes_only_its_constructor(
    tmp_path: Path, quote: str, keyword: bool
) -> None:
    package = PackageSpec.model_validate(_pkg(nodes=[_node("camera_node")]))
    write_package(package, tmp_path, render_package(package))
    root = tmp_path / "demo_pkg"
    module = root / "demo_pkg/camera_node.py"
    original = module.read_text().replace(
        "super().__init__('camera_node')",
        f"super().__init__({'node_name=' if keyword else ''}{quote}camera_node{quote})",
    )
    extra = (
        "\nUSER_TEXT = \"super().__init__('camera_node')\"\n"
        'USER_DOC = """super().__init__(\'camera_node\')"""\n'
        "# super().__init__('camera_node')\n"
        "class Other:\n    def __init__(self):\n        super().__init__('camera_node')\n"
    )
    original += extra
    module.write_text(original)
    package.nodes[0].label = "camera_renamed"
    plan = plan_package_edits(
        package,
        lambda relative: (root / relative).read_bytes() if (root / relative).is_file() else None,
        read_manifest(root) or {},
        {"n1": "camera_node"},
    )
    assert plan.problems == []
    updated = plan.patched["demo_pkg/camera_renamed.py"]
    expected = original.replace(f"{quote}camera_node{quote}", f"{quote}camera_renamed{quote}", 1)
    assert updated == expected
    assert updated.endswith(extra)


@pytest.mark.parametrize("edit", ["syntax", "zero", "multiple", "dynamic", "concatenated"])
def test_ambiguous_node_rename_refuses_without_writes(server: Any, edit: str) -> None:
    httpd, out = server
    payload = _state(_pkg(nodes=[_node("camera_node")]))
    assert _post(httpd, "/generate", payload)[0] == 200
    root = out / "demo_pkg"
    module = root / "demo_pkg/camera_node.py"
    source = module.read_text()
    if edit == "syntax":
        source += "\ndef broken(\n"
    elif edit == "zero":
        source = source.replace("super().__init__('camera_node')", "pass")
    elif edit == "multiple":
        source = source.replace(
            "super().__init__('camera_node')",
            "super().__init__('camera_node')\n        super().__init__('camera_node')",
        )
    else:
        source = source.replace(
            "super().__init__('camera_node')", "super().__init__(self.node_name)"
        )
    module.write_text(source)
    before = {p.relative_to(out): p.read_bytes() for p in out.rglob("*") if p.is_file()}
    payload["packages"][0]["nodes"][0]["label"] = "camera_renamed"
    status, data = _post(httpd, "/generate", payload)
    assert status == 409
    assert "constructor" in data["error"]
    assert "update the node name" in data["error"]
    assert {p.relative_to(out): p.read_bytes() for p in out.rglob("*") if p.is_file()} == before


def test_http_rename_preserves_user_text_in_node_setup_and_launch(server: Any) -> None:
    httpd, out = server
    assert _post(httpd, "/generate", _state(_pkg(nodes=[_node("camera_node")])))[0] == 200
    root = out / "demo_pkg"
    values = {
        "demo_pkg/camera_node.py": "super().__init__('camera_node')",
        "setup.py": "camera_node = demo_pkg.camera_node:main",
        "launch/launch.py": "executable='camera_node' name='camera_node' package='demo_pkg'",
    }
    for relative, value in values.items():
        file = root / relative
        file.write_text(file.read_text() + f"\nUSER_TEXT = {value!r}\n")
    status, data = _post(httpd, "/generate", _state(_pkg(nodes=[_node("camera_renamed")])))
    assert status == 200, data
    for relative, value in values.items():
        file = root / (
            "demo_pkg/camera_renamed.py" if relative.endswith("camera_node.py") else relative
        )
        constants = [
            n.value.value
            for n in ast.parse(file.read_text()).body
            if isinstance(n, ast.Assign)
            and isinstance(n.value, ast.Constant)
            and any(isinstance(t, ast.Name) and t.id == "USER_TEXT" for t in n.targets)
        ]
        assert constants == [value]


def test_package_rename_preserves_unrelated_metadata_text(server: Any) -> None:
    httpd, out = server
    assert _post(httpd, "/generate", _state(_pkg("old_name")))[0] == 200
    package = out / "old_name"
    setup = package / "setup.py"
    setup.write_text(setup.read_text() + "\nUSER_TEXT = 'old_name'\n# old_name tuning\n")
    manifest = package / "package.xml"
    manifest.write_text(
        manifest.read_text().replace("</package>", "<!-- old_name tuning --></package>")
    )
    cfg = package / "setup.cfg"
    cfg.write_text(cfg.read_text() + "\n# old_name tuning\n")
    status, data = _post(httpd, "/generate", _state(_pkg("new_name")))
    assert status == 200, data
    package = out / "new_name"
    assert "USER_TEXT = 'old_name'\n# old_name tuning" in (package / "setup.py").read_text()
    assert "<!-- old_name tuning -->" in (package / "package.xml").read_text()
    assert "# old_name tuning" in (package / "setup.cfg").read_text()
    assert "package_name = 'new_name'" in (package / "setup.py").read_text()
    assert "<name>new_name</name>" in (package / "package.xml").read_text()


def test_invalid_edited_metadata_blocks_package_move(server: Any) -> None:
    httpd, out = server
    assert _post(httpd, "/generate", _state(_pkg("old_name")))[0] == 200
    setup = out / "old_name/setup.py"
    setup.write_text(setup.read_text() + "\ndef broken(\n")
    before = {p.relative_to(out): p.read_bytes() for p in out.rglob("*") if p.is_file()}
    status, data = _post(httpd, "/generate", _state(_pkg("new_name")))
    assert status == 409, data
    assert "setup.py" in data["error"]
    assert {p.relative_to(out): p.read_bytes() for p in out.rglob("*") if p.is_file()} == before


def test_package_rename_follows_only_the_setup_name_binding(server: Any) -> None:
    httpd, out = server
    assert _post(httpd, "/generate", _state(_pkg("old_name")))[0] == 200
    setup = out / "old_name/setup.py"
    source = setup.read_text().replace("package_name", "configured_package")
    source += "\npackage_name = 'old_name'\n"
    setup.write_text(source)
    status, data = _post(httpd, "/generate", _state(_pkg("new_name")))
    assert status == 200, data
    updated = (out / "new_name/setup.py").read_text()
    assert "configured_package = 'new_name'" in updated
    assert updated.endswith("package_name = 'old_name'\n")


def test_dynamic_setup_package_identity_refuses_before_move(server: Any) -> None:
    httpd, out = server
    assert _post(httpd, "/generate", _state(_pkg("old_name")))[0] == 200
    setup = out / "old_name/setup.py"
    source = setup.read_text().replace("name=package_name", "name=choose_package()")
    setup.write_text(source)
    status, data = _post(httpd, "/generate", _state(_pkg("new_name")))
    assert status == 409
    assert "setup package name" in data["error"]
    assert setup.read_text() == source
    assert not (out / "new_name").exists()
