"""Generate must not report success when the design is not saved or a package would not run."""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest

from ros2inspector.design import server as design_server
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
