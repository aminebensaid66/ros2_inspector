"""HTTP-level edge cases for the designer server and the ``design`` command."""

from __future__ import annotations

import http.client
import json
import re
import socket
import threading
import time
from http.server import HTTPServer
from pathlib import Path
from typing import Any

import pytest
from rich.console import Console
from typer.testing import CliRunner

from ros2inspector.cli.app import app
from ros2inspector.design import server as design_server
from ros2inspector.design.codegen import _GENERATED_MARKER
from ros2inspector.design.server import STATE_FILE, _make_handler, run_design_server
from ros2inspector.design.ui import build_html

TOKEN = "test-token"
runner = CliRunner()


def _node(label: str = "talker", nid: str = "n1") -> dict[str, Any]:
    return {
        "id": nid,
        "label": label,
        "publishers": [{"id": "t1", "name": "/chatter", "msgType": "std_msgs/String"}],
        "subscribers": [],
        "services": [],
        "clients": [],
        "action_servers": [],
        "action_clients": [],
    }


def _state(*packages: dict[str, Any]) -> dict[str, Any]:
    return {
        "version": 1,
        "nid": 5,
        "tid": 5,
        "pid": 5,
        "activePkg": "p1",
        "packages": list(packages),
    }


def _pkg(name: str = "demo_pkg", pid: str = "p1", nodes: list[Any] | None = None) -> dict[str, Any]:
    return {
        "id": pid,
        "pkg_name": name,
        "nodes": [_node()] if nodes is None else nodes,
        "edges": [],
    }


@pytest.fixture
def server(tmp_path: Path) -> Any:
    out = tmp_path / "out"
    out.mkdir()
    handler = _make_handler(
        build_html(None, session_token=TOKEN), out, Console(quiet=True), session_token=TOKEN
    )
    httpd = HTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(
        target=httpd.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
    )
    thread.start()
    yield httpd, out
    httpd.shutdown()
    httpd.server_close()


def _call(
    httpd: HTTPServer,
    method: str,
    path: str,
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, str], bytes]:
    port = httpd.server_address[1]
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        merged = {"Host": f"127.0.0.1:{port}"}
        merged.update(headers or {})
        for key, value in merged.items():
            conn.putheader(key, value)
        if body is not None:
            conn.putheader("Content-Length", str(len(body)))
        conn.endheaders(body)
        response = conn.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        conn.close()


def _post(httpd: HTTPServer, path: str, payload: object, **headers: str) -> tuple[int, Any]:
    base = {"Content-Type": "application/json", "X-ROS2Inspector-Token": TOKEN}
    base.update(headers)
    status, _, body = _call(httpd, "POST", path, json.dumps(payload).encode(), base)
    return status, json.loads(body) if body else None


# ── GET ─────────────────────────────────────────────────────────────────────


def test_get_serves_page_with_security_headers(server: Any) -> None:
    httpd, _ = server
    status, headers, body = _call(httpd, "GET", "/")
    assert status == 200
    assert headers["Content-Type"].startswith("text/html")
    assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["Cache-Control"] == "no-store"
    assert TOKEN.encode() in body


def test_get_with_query_string_still_serves_page(server: Any) -> None:
    httpd, _ = server
    assert _call(httpd, "GET", "/?utm=1")[0] == 200


@pytest.mark.parametrize("path", ["/index.html", "/../etc/passwd", "/save", "/favicon.ico"])
def test_get_unknown_paths_are_404(server: Any, path: str) -> None:
    httpd, _ = server
    assert _call(httpd, "GET", path)[0] == 404


@pytest.mark.parametrize(
    "host", ["evil.example", "localhost.evil.example", "127.0.0.2:1", "", "[::2]:80"]
)
def test_get_rejects_non_loopback_host(server: Any, host: str) -> None:
    httpd, _ = server
    status, _, body = _call(httpd, "GET", "/", headers={"Host": host})
    assert status == 403
    assert TOKEN.encode() not in body


@pytest.mark.parametrize("host", ["localhost", "LOCALHOST:8765", "127.0.0.1", "[::1]:9"])
def test_get_accepts_loopback_host_spellings(server: Any, host: str) -> None:
    httpd, _ = server
    assert _call(httpd, "GET", "/", headers={"Host": host})[0] == 200


# ── POST guards ─────────────────────────────────────────────────────────────


def test_post_unknown_path_is_404(server: Any) -> None:
    httpd, _ = server
    assert _post(httpd, "/delete", _state(_pkg()))[0] == 404


@pytest.mark.parametrize("token", ["", "wrong", TOKEN + "x"])
def test_post_requires_exact_token(server: Any, token: str) -> None:
    httpd, out = server
    status, data = _post(httpd, "/save", _state(_pkg()), **{"X-ROS2Inspector-Token": token})
    assert status == 403 and "token" in data["error"]
    assert not (out / STATE_FILE).exists()


@pytest.mark.parametrize(
    "origin",
    ["http://evil.example", "https://127.0.0.1:1", "null", "http://127.0.0.1:1"],
)
def test_post_rejects_cross_origin(server: Any, origin: str) -> None:
    httpd, _ = server
    assert _post(httpd, "/save", _state(_pkg()), Origin=origin)[0] == 403


def test_post_accepts_same_origin(server: Any) -> None:
    httpd, _ = server
    origin = f"http://127.0.0.1:{httpd.server_address[1]}"
    assert _post(httpd, "/save", _state(_pkg()), Origin=origin)[0] == 200


@pytest.mark.parametrize("content_type", ["text/plain", "application/x-www-form-urlencoded", ""])
def test_post_requires_json_content_type(server: Any, content_type: str) -> None:
    httpd, _ = server
    assert _post(httpd, "/save", _state(_pkg()), **{"Content-Type": content_type})[0] == 415


def test_post_accepts_json_with_charset(server: Any) -> None:
    httpd, _ = server
    status, _ = _post(
        httpd, "/save", _state(_pkg()), **{"Content-Type": "application/json; charset=utf-8"}
    )
    assert status == 200


def _raw_post_status(httpd: HTTPServer, content_length: str) -> bytes:
    """Send only the headers, so the size check is seen before any body is read."""
    with socket.create_connection(("127.0.0.1", httpd.server_address[1]), timeout=5) as sock:
        sock.sendall(
            b"POST /save HTTP/1.0\r\nHost: 127.0.0.1\r\nContent-Type: application/json\r\n"
            + f"X-ROS2Inspector-Token: {TOKEN}\r\nContent-Length: {content_length}\r\n\r\n".encode()
        )
        return sock.recv(4096).split(b"\r\n", 1)[0] + b" "


@pytest.mark.parametrize("length", [str(1024 * 1024 + 1), "0", "-5", str(10**12)])
def test_post_rejects_oversized_and_empty_bodies(server: Any, length: str) -> None:
    httpd, _ = server
    assert b" 413 " in _raw_post_status(httpd, length)


def test_post_rejects_bad_content_length(server: Any) -> None:
    httpd, _ = server
    assert b" 400 " in _raw_post_status(httpd, "abc")


@pytest.mark.parametrize("body", [b"not json", b"\xff\xfe", b"[1, 2]", b'"text"', b"null"])
def test_post_rejects_non_object_json(server: Any, body: bytes) -> None:
    httpd, _ = server
    headers = {"Content-Type": "application/json", "X-ROS2Inspector-Token": TOKEN}
    assert _call(httpd, "POST", "/save", body, headers)[0] == 400


def test_validation_errors_have_paths_for_the_ui(server: Any) -> None:
    httpd, _ = server
    bad = _pkg(nodes=[_node(label="Bad-Label")])
    status, data = _post(httpd, "/generate", _state(bad))
    assert status == 422
    assert data["fields"][0]["path"].startswith("packages.0.nodes.0.label")


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity"])
def test_non_finite_coordinates_are_rejected(server: Any, value: str) -> None:
    httpd, _ = server
    body = json.dumps(_state(_pkg())).replace(
        '"label": "talker"', f'"label": "talker", "x": {value}'
    )
    headers = {"Content-Type": "application/json", "X-ROS2Inspector-Token": TOKEN}
    assert _call(httpd, "POST", "/save", body.encode(), headers)[0] == 422


def test_negative_counters_are_rejected(server: Any) -> None:
    httpd, _ = server
    state = _state(_pkg())
    state["nid"] = -1
    assert _post(httpd, "/save", state)[0] == 422


# ── generate behaviour ──────────────────────────────────────────────────────


def test_generate_skips_empty_packages_and_saves_state(server: Any) -> None:
    httpd, out = server
    status, data = _post(httpd, "/generate", _state(_pkg(), _pkg("empty_pkg", "p2", nodes=[])))
    assert status == 200
    assert (out / "demo_pkg" / _GENERATED_MARKER).is_file()
    assert not (out / "empty_pkg").exists()
    saved = json.loads((out / STATE_FILE).read_text())
    assert saved["packages"][0]["_generated_name"] == "demo_pkg"
    assert "_generated_name" not in saved["packages"][1]
    assert data["output_dir"] == str(out)


def test_generate_twice_reports_unchanged(server: Any) -> None:
    httpd, _ = server
    _post(httpd, "/generate", _state(_pkg()))
    status, data = _post(httpd, "/generate", _state(_pkg()))
    assert status == 200
    result = data["packages"][0]
    assert result["written"] == [] and result["unchanged"]


def test_save_does_not_forget_generated_name(server: Any) -> None:
    httpd, out = server
    _post(httpd, "/generate", _state(_pkg()))
    _post(httpd, "/save", _state(_pkg()))
    saved = json.loads((out / STATE_FILE).read_text())
    assert saved["packages"][0]["_generated_name"] == "demo_pkg"


def test_client_supplied_generated_name_is_ignored(server: Any) -> None:
    """A crafted '_generated_name' must not let a request rename an arbitrary directory."""
    httpd, out = server
    victim = out / "victim"
    victim.mkdir()
    (victim / "keep.txt").write_text("precious")
    package = _pkg("thief")
    package["_generated_name"] = "victim"
    status, _ = _post(httpd, "/generate", _state(package))
    assert status == 200
    assert (victim / "keep.txt").read_text() == "precious"
    assert (out / "thief" / _GENERATED_MARKER).is_file()


def test_rename_after_generate_moves_directory_and_keeps_edits(server: Any) -> None:
    httpd, out = server
    _post(httpd, "/generate", _state(_pkg("old_name")))
    node_file = out / "old_name" / "old_name" / "talker.py"
    node_file.write_text(node_file.read_text() + "\n# my edit\n")
    status, data = _post(httpd, "/generate", _state(_pkg("new_name")))
    assert status == 200, data
    assert not (out / "old_name").exists()
    # The module folder moves with the package, so the edited file is the one that is
    # installed under the new name (not stranded in an old, uninstalled module folder).
    moved = out / "new_name" / "new_name" / "talker.py"
    assert moved.read_text().endswith("# my edit\n")
    assert not (out / "new_name" / "old_name").exists()
    assert (out / "new_name" / "resource" / "new_name").is_file()
    assert not (out / "new_name" / "resource" / "old_name").exists()
    assert data["kept_modified"] == ["new_name/new_name/talker.py"]


def test_rename_twice_keeps_edits_and_leaves_no_stale_files(server: Any) -> None:
    httpd, out = server
    _post(httpd, "/generate", _state(_pkg("first_name")))
    node_file = out / "first_name" / "first_name" / "talker.py"
    node_file.write_text(node_file.read_text() + "\n# my edit\n")
    _post(httpd, "/generate", _state(_pkg("second_name")))
    status, data = _post(httpd, "/generate", _state(_pkg("third_name")))
    assert status == 200, data
    package = out / "third_name"
    assert (package / "third_name" / "talker.py").read_text().endswith("# my edit\n")
    leftovers = sorted(p.relative_to(package).as_posix() for p in package.rglob("*") if p.is_file())
    assert leftovers == [
        ".ros2inspector_generated",
        "README.md",
        "launch/launch.py",
        "package.xml",
        "resource/third_name",
        "setup.cfg",
        "setup.py",
        "test/test_copyright.py",
        "test/test_flake8.py",
        "test/test_pep257.py",
        "third_name/__init__.py",
        "third_name/talker.py",
    ]


def test_rename_updates_the_package_name_in_edited_files(server: Any) -> None:
    """Edited setup.py/package.xml/launch files must not keep pointing at the old name."""
    httpd, out = server
    _post(httpd, "/generate", _state(_pkg("old_name")))
    package = out / "old_name"
    edited = ["setup.py", "package.xml", "setup.cfg", "launch/launch.py", "old_name/talker.py"]
    for relative in edited:
        path = package / relative
        path.write_text(
            path.read_text() + "\n<!-- edit -->\n"
            if relative.endswith(".xml")
            else path.read_text() + "\n# my edit\n"
        )
    (package / "launch" / "extra.launch.py").write_text("package = 'old_name'  # old_names\n")
    status, data = _post(httpd, "/generate", _state(_pkg("new_name")))
    assert status == 200, data
    package = out / "new_name"
    # The node file has no package name in it, so only the four package files change.
    assert sorted(data["renamed_in"]) == sorted(f"new_name/{p}" for p in edited[:4])
    for path in package.rglob("*"):
        if path.is_file() and path.name not in (".ros2inspector_generated", "extra.launch.py"):
            assert re.search(r"\bold_name\b", path.read_text()) is None, path
    assert "package_name = 'new_name'" in (package / "setup.py").read_text()
    assert "<name>new_name</name>" in (package / "package.xml").read_text()
    # Files the user added are theirs and are left alone.
    extra = (package / "launch" / "extra.launch.py").read_text()
    assert extra == "package = 'old_name'  # old_names\n"
    assert (package / "setup.py").read_text().endswith("# my edit\n")
    assert "new_name/setup.py" in data["kept_modified"]


def test_rename_never_rewrites_node_code_or_launch_topics(server: Any) -> None:
    """Regression (B1): a topic like /perception/obstacles must survive renaming 'perception'.

    Rewriting node code compiles and passes colcon test, but silently cuts the publisher off
    from its subscribers at runtime, so only package entries may change.
    """
    httpd, out = server
    node = _node("detector")
    node["publishers"] = [
        {"id": "t1", "name": "/perception/obstacles", "msgType": "std_msgs/String"}
    ]
    _post(httpd, "/generate", _state(_pkg("perception", nodes=[node])))
    package = out / "perception"
    node_file = package / "perception" / "detector.py"
    node_source = node_file.read_text() + "\n# perception tuning\n"
    assert "'/perception/obstacles'" in node_source
    node_file.write_text(node_source)
    launch_file = package / "launch" / "launch.py"
    launch_source = launch_file.read_text().replace(
        "        ),",
        "            remappings=[('/perception/obstacles', '/perception/raw')],\n        ),",
    )
    assert launch_source != launch_file.read_text()
    launch_file.write_text(launch_source)

    status, data = _post(httpd, "/generate", _state(_pkg("perception_stack", nodes=[node])))
    assert status == 200, data
    package = out / "perception_stack"
    assert (package / "perception_stack" / "detector.py").read_text() == node_source
    launch = (package / "launch" / "launch.py").read_text()
    assert "package='perception_stack'" in launch
    assert "package='perception'" not in launch
    assert "remappings=[('/perception/obstacles', '/perception/raw')]" in launch
    assert data["renamed_in"] == ["perception_stack/launch/launch.py"]


def test_rename_rewrites_only_package_entries_in_launch_files(tmp_path: Path) -> None:
    from ros2inspector.design.codegen import _MARKER_FORMAT, _digest, rename_package_contents

    def check(tmp: Path, relative: str, before: str, after: str) -> None:
        package = tmp / relative.replace("/", "_")
        path = package / relative
        path.parent.mkdir(parents=True)
        path.write_text(before)
        marker = {
            "generated_by": "ros2inspector",
            "format": _MARKER_FORMAT,
            "files": {relative: _digest(b"original")},
        }
        (package / _GENERATED_MARKER).write_text(json.dumps(marker))
        rename_package_contents(package, "old", "new")
        moved = package / re.sub(r"^old/", "new/", relative)  # the module folder moves
        assert moved.read_text() == after, relative

    check(
        tmp_path,
        "launch/a.launch.py",
        "Node(package=\"old\", namespace='old', parameters=[{'old': 1}])\n",
        "Node(package=\"new\", namespace='old', parameters=[{'old': 1}])\n",
    )
    check(
        tmp_path,
        "launch/b.launch.xml",
        '<node pkg="old" exec="old" name="old_node"/>\n',
        '<node pkg="new" exec="old" name="old_node"/>\n',
    )
    check(
        tmp_path,
        "launch/c.launch.yaml",
        "- node:\n    pkg: old\n    exec: old\n    pkg: older\n",
        "- node:\n    pkg: new\n    exec: old\n    pkg: older\n",
    )
    node = "self.create_publisher(S, '/old/x', 10)\n"
    check(tmp_path, "old/node.py", node, node)
    check(tmp_path, "README.md", "# old\n", "# old\n")


def test_generated_files_are_readable_by_other_users(server: Any) -> None:
    """Regression (M2): files moved into place from a temp file must not stay 0600."""
    import os

    httpd, out = server
    _post(httpd, "/generate", _state(_pkg("perm_pkg")))
    package = out / "perm_pkg"
    umask = os.umask(0)
    os.umask(umask)
    expected = 0o666 & ~umask
    for path in package.rglob("*"):
        if path.is_file():
            assert path.stat().st_mode & 0o777 == expected, path
    # A file the user made executable keeps its mode when it is regenerated.
    launch = package / "launch" / "launch.py"
    launch.chmod(0o755)
    original = launch.read_text()
    launch.write_text(original + "# edit\n")
    from ros2inspector.design.codegen import _write_atomic

    _write_atomic(launch, original.encode())
    assert launch.stat().st_mode & 0o777 == 0o755


def test_rename_refuses_when_the_module_folder_cannot_move(server: Any) -> None:
    httpd, out = server
    _post(httpd, "/generate", _state(_pkg("old_name")))
    (out / "old_name" / "new_name").mkdir()
    status, data = _post(httpd, "/generate", _state(_pkg("new_name")))
    assert status == 409
    assert "new_name already exists inside the package" in data["error"]
    assert (out / "old_name" / "old_name" / "talker.py").is_file()
    assert not (out / "new_name").exists()


def test_unexpected_error_returns_json_500(server: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    httpd, _ = server

    def explode(*_: Any, **__: Any) -> Any:
        raise RuntimeError("template engine on fire")

    monkeypatch.setattr(design_server, "render_package", explode)
    status, data = _post(httpd, "/generate", _state(_pkg()))
    assert status == 500
    assert "RuntimeError" in data["error"]


def test_idle_connection_does_not_block_the_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(design_server, "_REQUEST_TIMEOUT_SECONDS", 0.3)
    handler = _make_handler(build_html(None), tmp_path, Console(quiet=True))
    httpd = HTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(
        target=httpd.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
    ).start()
    try:
        idle = socket.create_connection(("127.0.0.1", httpd.server_address[1]))
        time.sleep(0.05)
        started = time.monotonic()
        assert _call(httpd, "GET", "/")[0] == 200
        assert time.monotonic() - started < 5
        idle.close()
    finally:
        httpd.shutdown()
        httpd.server_close()


# ── run_design_server and the CLI ───────────────────────────────────────────


class _StopServer(HTTPServer):
    def serve_forever(self, poll_interval: float = 0.5) -> None:
        raise KeyboardInterrupt


def _fake_bind(captured: dict[str, Any]) -> Any:
    def bind(port: int, handler: Any) -> HTTPServer:
        captured["port"] = port
        captured["handler"] = handler
        return _StopServer(("127.0.0.1", 0), handler)

    return bind


def test_run_design_server_resumes_saved_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / STATE_FILE).write_text(json.dumps(_state(_pkg())))
    captured: dict[str, Any] = {}
    monkeypatch.setattr(design_server, "bind_server", _fake_bind(captured))
    console = Console(record=True, width=200)
    run_design_server(port=0, output_dir=tmp_path, open_browser=False, console=console)
    text = console.export_text()
    assert "resuming 1 package(s)" in text
    assert "Design server stopped" in text
    assert "demo_pkg" in captured["handler"]._page()
    assert captured["port"] == 0


def test_page_reload_shows_the_last_saved_design(tmp_path: Path) -> None:
    """The page is rendered per request, so reloading after Save never shows stale state."""
    from ros2inspector.design.server import _load_state

    handler = _make_handler(
        lambda: build_html(_load_state(tmp_path), session_token=TOKEN),
        tmp_path,
        Console(quiet=True),
        session_token=TOKEN,
    )
    httpd = HTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(
        target=httpd.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
    ).start()
    try:
        assert b"renamed_pkg" not in _call(httpd, "GET", "/")[2]
        assert _post(httpd, "/save", _state(_pkg("renamed_pkg")))[0] == 200
        assert b"renamed_pkg" in _call(httpd, "GET", "/")[2]
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_run_design_server_hints_at_src_for_workspace_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "src").mkdir()
    monkeypatch.setattr(design_server, "bind_server", _fake_bind({}))
    console = Console(record=True, width=200)
    run_design_server(port=0, output_dir=tmp_path, open_browser=False, console=console)
    assert "looks like a workspace root" in console.export_text()


def test_run_design_server_reports_port_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(design_server, "bind_server", _fake_bind({}))
    console = Console(record=True, width=200)
    run_design_server(port=1, output_dir=tmp_path, open_browser=False, console=console)
    assert "Port 1 is in use" in console.export_text()


def test_run_design_server_opens_browser(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    opened: list[str] = []
    monkeypatch.setattr(design_server, "bind_server", _fake_bind({}))
    monkeypatch.setattr(design_server.webbrowser, "open", opened.append)
    run_design_server(port=0, output_dir=tmp_path, open_browser=True, console=Console(quiet=True))
    deadline = time.monotonic() + 3
    while not opened and time.monotonic() < deadline:
        time.sleep(0.02)
    assert opened and opened[0].startswith("http://127.0.0.1:")


def test_cli_design_passes_resolved_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from ros2inspector.cli import cmd_design

    captured: dict[str, Any] = {}
    monkeypatch.setattr(cmd_design, "run_design_server", lambda **kw: captured.update(kw))
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["design", "-o", "out", "--port", "0", "--no-open"])
    assert result.exit_code == 0, result.output
    assert captured["output_dir"] == (tmp_path / "out").resolve()
    assert captured["port"] == 0 and captured["open_browser"] is False


@pytest.mark.parametrize("port", ["-1", "65536", "http"])
def test_cli_design_rejects_bad_port(port: str) -> None:
    assert runner.invoke(app, ["design", "--port", port, "--no-open"]).exit_code == 2


def test_cli_design_rejects_output_that_is_a_file(tmp_path: Path) -> None:
    target = tmp_path / "file.txt"
    target.write_text("x")
    result = runner.invoke(app, ["design", "-o", str(target), "--no-open", "--port", "0"])
    assert result.exit_code == 2
    assert "not a directory" in result.output
