"""Generate must not report success when the design is not saved or a package would not run."""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest

from ros2inspector.design import server as design_server
from ros2inspector.design.codegen import _GENERATED_MARKER
from ros2inspector.design.server import STATE_FILE
from tests.unit.test_design_server_http import _node, _pkg, _post, _state, server

__all__ = ["server"]


def _camera(label: str = "camera_node") -> dict[str, Any]:
    return _node(label, "n1")


def _with_node(package: dict[str, Any], label: str, nid: str) -> dict[str, Any]:
    node = _node(label, nid)
    node["publishers"] = [{"id": f"t{nid}", "name": f"/{label}_out", "msgType": "std_msgs/String"}]
    package["nodes"].append(node)
    return package


# ── #56: a failed save is reported, and never truncates the previous state ─────────


def test_failed_state_save_is_reported_and_keeps_previous_state(
    server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    httpd, out = server
    assert _post(httpd, "/generate", _state(_pkg()))[0] == 200
    previous = (out / STATE_FILE).read_text()

    def fail(*_: object) -> None:
        raise OSError(27, "File too large")

    monkeypatch.setattr(design_server, "_save_state", fail)
    status, data = _post(httpd, "/generate", _state(_with_node(_pkg(), "extra", "n2")))
    assert status == 200
    assert data["state_saved"] is False
    assert "File too large" in data["state_error"]
    assert (out / "demo_pkg/demo_pkg/extra.py").is_file()
    assert (out / STATE_FILE).read_text() == previous

    status, data = _post(httpd, "/save", _state(_pkg()))
    assert status == 500
    assert data["state_saved"] is False
    assert "File too large" in data["error"]


def test_successful_generate_reports_the_design_as_saved(server: Any) -> None:
    httpd, _ = server
    status, data = _post(httpd, "/generate", _state(_pkg()))
    assert status == 200
    assert data["state_saved"] is True
    assert data["state_error"] is None


def test_write_limit_never_truncates_the_saved_design(tmp_path: Path) -> None:
    resource = pytest.importorskip("resource")
    del resource
    (tmp_path / STATE_FILE).write_text('{"packages": []}')
    script = textwrap.dedent(
        f"""
        import json, resource, signal, sys
        from pathlib import Path
        from ros2inspector.design.server import _save_state
        signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
        resource.setrlimit(resource.RLIMIT_FSIZE, (4096, 4096))
        try:
            _save_state(Path({str(tmp_path)!r}), {{"packages": ["x" * 100] * 200}})
        except OSError as exc:
            print("failed", exc.errno)
            sys.exit(0)
        sys.exit(1)
        """
    )
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.startswith("failed")
    assert (tmp_path / STATE_FILE).read_text() == '{"packages": []}'
    assert sorted(p.name for p in tmp_path.iterdir()) == [STATE_FILE]


# ── #43: an edited node module follows its node through a rename ───────────────────


def _rename_camera(out: Path, httpd: Any, edit: str = "# USER_EDIT_RETAINED\n") -> Any:
    assert _post(httpd, "/generate", _state(_pkg(nodes=[_camera()])))[0] == 200
    module = out / "demo_pkg/demo_pkg/camera_node.py"
    module.write_text(module.read_text() + edit)
    return _post(httpd, "/generate", _state(_pkg(nodes=[_camera("camera_renamed")])))


def test_renaming_an_edited_node_moves_its_module(server: Any) -> None:
    httpd, out = server
    status, data = _rename_camera(out, httpd)
    assert status == 200, data
    package = out / "demo_pkg"
    assert not (package / "demo_pkg/camera_node.py").exists()
    moved = (package / "demo_pkg/camera_renamed.py").read_text()
    assert moved.endswith("# USER_EDIT_RETAINED\n")
    assert "super().__init__('camera_renamed')" in moved
    setup = (package / "setup.py").read_text()
    assert "'camera_renamed = demo_pkg.camera_renamed:main'" in setup
    assert "camera_node" not in setup
    assert "executable='camera_renamed'" in (package / "launch/launch.py").read_text()
    assert data["kept_modified"] == ["demo_pkg/demo_pkg/camera_renamed.py"]
    assert data["updated_in"] == [
        "demo_pkg: moved your edited demo_pkg/camera_node.py to demo_pkg/camera_renamed.py"
    ]
    # Still counted as edited, so the next Generate keeps it.
    status, data = _post(httpd, "/generate", _state(_pkg(nodes=[_camera("camera_renamed")])))
    assert data["kept_modified"] == ["demo_pkg/demo_pkg/camera_renamed.py"]


def test_rename_uses_the_saved_design_for_older_markers(server: Any) -> None:
    httpd, out = server
    assert _post(httpd, "/generate", _state(_pkg(nodes=[_camera()])))[0] == 200
    marker = out / "demo_pkg" / _GENERATED_MARKER
    data = json.loads(marker.read_text())
    del data["nodes"]
    marker.write_text(json.dumps(data))
    module = out / "demo_pkg/demo_pkg/camera_node.py"
    module.write_text(module.read_text() + "# edit\n")
    status, _ = _post(httpd, "/generate", _state(_pkg(nodes=[_camera("camera_renamed")])))
    assert status == 200
    assert (out / "demo_pkg/demo_pkg/camera_renamed.py").read_text().endswith("# edit\n")


def test_rename_onto_an_existing_module_is_refused_without_writing(server: Any) -> None:
    httpd, out = server
    assert _post(httpd, "/generate", _state(_pkg(nodes=[_camera()])))[0] == 200
    package = out / "demo_pkg"
    (package / "demo_pkg/camera_node.py").write_text("# mine\n")
    (package / "demo_pkg/camera_renamed.py").write_text("# also mine\n")
    before = {p: p.read_bytes() for p in package.rglob("*") if p.is_file()}
    status, data = _post(httpd, "/generate", _state(_pkg(nodes=[_camera("camera_renamed")])))
    assert status == 409
    assert "camera_renamed.py already exists" in data["error"]
    assert {p: p.read_bytes() for p in package.rglob("*") if p.is_file()} == before


def test_renaming_an_unedited_node_regenerates_it(server: Any) -> None:
    httpd, out = server
    assert _post(httpd, "/generate", _state(_pkg(nodes=[_camera()])))[0] == 200
    status, data = _post(httpd, "/generate", _state(_pkg(nodes=[_camera("camera_renamed")])))
    assert status == 200
    assert data["kept_modified"] == []
    assert "demo_pkg/camera_node.py" in data["packages"][0]["removed"]
