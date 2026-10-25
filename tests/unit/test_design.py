"""Tests for the validated package designer, code generator, and local HTTP API."""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from http.server import HTTPServer
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from rich.console import Console

from ros2inspector.design.codegen import _GENERATED_MARKER, generate_package
from ros2inspector.design.models import DesignState, PackageSpec
from ros2inspector.design.server import (
    STATE_FILE,
    _apply_renames,
    _load_state,
    _make_handler,
    _save_state,
)
from ros2inspector.design.ui import build_html


def _package_payload(name: str = "demo_pkg") -> dict[str, Any]:
    return {"id": "p1", "pkg_name": name, "nodes": [], "edges": []}


def _node_payload(name: str = "demo_node") -> dict[str, Any]:
    return {
        "id": "n1",
        "label": name,
        "publishers": [],
        "subscribers": [],
        "services": [],
        "clients": [],
        "action_servers": [],
        "action_clients": [],
    }


def _state_payload(package: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "version": 1,
        "nid": 2,
        "tid": 2,
        "pid": 2,
        "activePkg": "p1",
        "packages": [package or _package_payload()],
    }


def _start_server(
    output_dir: Path,
    *,
    token: str = "",
    initial_state: dict[str, Any] | None = None,
) -> tuple[HTTPServer, int]:
    html = build_html(initial_state, session_token=token)
    handler = _make_handler(
        html,
        output_dir,
        Console(stderr=True, quiet=True),
        session_token=token,
    )
    server = HTTPServer(("127.0.0.1", 0), handler)
    port = int(server.server_address[1])
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    time.sleep(0.03)
    return server, port


def _request(
    url: str,
    payload: object,
    *,
    token: str = "",
    content_type: str = "application/json",
    origin: str | None = None,
) -> tuple[int, dict[str, Any]]:
    data = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": content_type}
    if token:
        headers["X-ROS2Inspector-Token"] = token
    if origin:
        headers["Origin"] = origin
    request = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request) as response:
            return int(response.status), json.loads(response.read())
    except urllib.error.HTTPError as exc:
        body = exc.read()
        return exc.code, json.loads(body) if body else {}


def test_generate_scaffold_and_marker(tmp_path: Path) -> None:
    files = generate_package(_package_payload("empty_pkg"), tmp_path)
    names = {Path(item).name for item in files}
    assert {"package.xml", "setup.py", "setup.cfg", "launch.py", "__init__.py"} <= names
    assert (tmp_path / "empty_pkg" / _GENERATED_MARKER).is_file()
    assert (tmp_path / "empty_pkg" / "resource" / "empty_pkg").is_file()


def test_generate_topics_services_and_actions(tmp_path: Path) -> None:
    package = _package_payload("robot_pkg")
    node = _node_payload("controller_node")
    node.update(
        {
            "publishers": [{"id": "pub", "name": "/status", "msgType": "std_msgs/String"}],
            "subscribers": [{"id": "sub", "name": "/cmd", "msgType": "geometry_msgs/Twist"}],
            "services": [{"id": "srv", "name": "/reset", "msgType": "std_srvs/Trigger"}],
            "clients": [{"id": "client", "name": "/set_bool", "msgType": "std_srvs/SetBool"}],
            "action_servers": [
                {"id": "as", "name": "/navigate", "msgType": "nav2_msgs/NavigateToPose"}
            ],
            "action_clients": [{"id": "ac", "name": "/dock", "msgType": "nav2_msgs/DockRobot"}],
        }
    )
    package["nodes"] = [node]

    generate_package(package, tmp_path)
    source_path = tmp_path / "robot_pkg" / "robot_pkg" / "controller_node.py"
    source = source_path.read_text(encoding="utf-8")
    compile(source, str(source_path), "exec")
    assert "create_publisher" in source
    assert "create_subscription" in source
    assert "create_service" in source
    assert "create_client" in source
    assert "ActionServer" in source
    assert "ActionClient" in source
    assert "from std_srvs.srv import SetBool" in source
    assert "from nav2_msgs.action import NavigateToPose" in source


def test_generate_deduplicates_imports_and_dependencies(tmp_path: Path) -> None:
    package = _package_payload("deps_pkg")
    node = _node_payload()
    node["publishers"] = [
        {"id": "p1", "name": "/one", "msgType": "std_msgs/String"},
        {"id": "p2", "name": "/two", "msgType": "std_msgs/String"},
    ]
    package["nodes"] = [node]
    generate_package(package, tmp_path)
    source = (tmp_path / "deps_pkg" / "deps_pkg" / "demo_node.py").read_text()
    package_xml = (tmp_path / "deps_pkg" / "package.xml").read_text()
    assert source.count("from std_msgs.msg import String") == 1
    assert package_xml.count("<depend>std_msgs</depend>") == 1


@pytest.mark.parametrize(
    "payload",
    [
        {"id": "p1", "pkg_name": "bad-name", "nodes": []},
        {"id": "p1", "pkg_name": "BadName", "nodes": []},
        {"id": "p1", "pkg_name": "../escape", "nodes": []},
    ],
)
def test_invalid_package_names_are_rejected_without_writes(
    tmp_path: Path, payload: dict[str, Any]
) -> None:
    with pytest.raises(ValidationError):
        generate_package(payload, tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_invalid_node_and_quoted_topic_are_rejected(tmp_path: Path) -> None:
    package = _package_payload("safe_pkg")
    node = _node_payload("not-valid")
    node["publishers"] = [
        {"id": "p1", "name": "/topic'\nraise SystemExit", "msgType": "std_msgs/String"}
    ]
    package["nodes"] = [node]
    with pytest.raises(ValidationError):
        generate_package(package, tmp_path)
    assert not (tmp_path / "safe_pkg").exists()


def test_duplicate_nodes_and_endpoint_ids_are_rejected() -> None:
    duplicate_nodes = _package_payload()
    duplicate_nodes["nodes"] = [_node_payload("same_node"), _node_payload("same_node")]
    with pytest.raises(ValidationError, match="node names must be unique"):
        PackageSpec.model_validate(duplicate_nodes)

    duplicate_endpoints = _package_payload()
    node = _node_payload()
    node["publishers"] = [{"id": "dup", "name": "/one", "msgType": "std_msgs/String"}]
    node["subscribers"] = [{"id": "dup", "name": "/two", "msgType": "std_msgs/String"}]
    duplicate_endpoints["nodes"] = [node]
    with pytest.raises(ValidationError, match="endpoint ids must be unique"):
        PackageSpec.model_validate(duplicate_endpoints)


def test_unrelated_existing_directory_is_never_overwritten(tmp_path: Path) -> None:
    target = tmp_path / "demo_pkg"
    target.mkdir()
    sentinel = target / "valuable.txt"
    sentinel.write_text("keep me")
    with pytest.raises(FileExistsError, match="not generated"):
        generate_package(_package_payload(), tmp_path)
    assert sentinel.read_text() == "keep me"


def test_regeneration_is_deterministic(tmp_path: Path) -> None:
    package = _package_payload("stable_pkg")
    package["nodes"] = [_node_payload("stable_node")]
    first = generate_package(package, tmp_path)
    first_content = {item: (tmp_path / item).read_bytes() for item in first}
    second = generate_package(package, tmp_path)
    second_content = {item: (tmp_path / item).read_bytes() for item in second}
    assert first_content == second_content


def test_save_and_load_state_roundtrip(tmp_path: Path) -> None:
    state = _state_payload()
    _save_state(tmp_path, state)
    assert _load_state(tmp_path) == state
    assert (tmp_path / STATE_FILE).is_file()


def test_load_state_rejects_corrupt_or_invalid_state(tmp_path: Path) -> None:
    (tmp_path / STATE_FILE).write_text("not-json{{", encoding="utf-8")
    assert _load_state(tmp_path) is None
    (tmp_path / STATE_FILE).write_text(json.dumps({"packages": [{"pkg_name": "bad-name"}]}))
    assert _load_state(tmp_path) is None


def test_apply_renames_only_moves_generated_directories(tmp_path: Path) -> None:
    old_dir = tmp_path / "old_pkg"
    old_dir.mkdir()
    (old_dir / _GENERATED_MARKER).write_text("generated")
    old_state = {"packages": [{"id": "p1", "pkg_name": "old_pkg", "_generated_name": "old_pkg"}]}
    _apply_renames([_package_payload("new_pkg")], tmp_path, old_state)
    assert not old_dir.exists()
    assert (tmp_path / "new_pkg" / _GENERATED_MARKER).is_file()


def test_apply_renames_refuses_unmarked_directory(tmp_path: Path) -> None:
    (tmp_path / "old_pkg").mkdir()
    old_state = {"packages": [{"id": "p1", "pkg_name": "old_pkg", "_generated_name": "old_pkg"}]}
    with pytest.raises(PermissionError, match="not a generated package"):
        _apply_renames([_package_payload("new_pkg")], tmp_path, old_state)


def test_server_returns_field_level_validation_errors(tmp_path: Path) -> None:
    server, port = _start_server(tmp_path)
    try:
        package = _package_payload("bad-name")
        code, response = _request(f"http://127.0.0.1:{port}/generate", _state_payload(package))
        assert code == 422
        assert response["error"] == "Validation failed"
        assert any(field["path"].endswith("pkg_name") for field in response["fields"])
        assert not (tmp_path / "bad-name").exists()
    finally:
        server.shutdown()
        server.server_close()


def test_server_enforces_token_content_type_and_origin(tmp_path: Path) -> None:
    token = "test-session-token"
    server, port = _start_server(tmp_path, token=token)
    url = f"http://127.0.0.1:{port}/save"
    try:
        code, _ = _request(url, _state_payload())
        assert code == 403
        code, _ = _request(url, _state_payload(), token=token, content_type="text/plain")
        assert code == 415
        code, _ = _request(
            url,
            _state_payload(),
            token=token,
            origin="https://malicious.example",
        )
        assert code == 403
        code, response = _request(url, _state_payload(), token=token)
        assert code == 200
        assert response == {"ok": True, "state_saved": True}
    finally:
        server.shutdown()
        server.server_close()


def test_server_generates_and_safely_renames_package(tmp_path: Path) -> None:
    server, port = _start_server(tmp_path)
    try:
        alpha = _package_payload("alpha_pkg")
        alpha["nodes"] = [_node_payload()]
        code, _ = _request(
            f"http://127.0.0.1:{port}/generate",
            _state_payload(alpha),
        )
        assert code == 200
        assert (tmp_path / "alpha_pkg").is_dir()

        beta = _package_payload("beta_pkg")
        beta["nodes"] = [_node_payload()]
        code, _ = _request(
            f"http://127.0.0.1:{port}/generate",
            _state_payload(beta),
        )
        assert code == 200
        assert (tmp_path / "beta_pkg").is_dir()
        assert not (tmp_path / "alpha_pkg").exists()
    finally:
        server.shutdown()
        server.server_close()


def test_design_state_rejects_duplicate_package_names() -> None:
    first = _package_payload("same_pkg")
    second = _package_payload("same_pkg")
    second["id"] = "p2"
    with pytest.raises(ValidationError, match="package names must be unique"):
        DesignState.model_validate({"packages": [first, second]})


def test_ui_contains_all_endpoint_editors_and_security_token() -> None:
    html = build_html(session_token="secret-token")
    assert html.startswith("<!DOCTYPE html>")
    assert "Publishers" in html
    assert "Subscribers" in html
    assert "Service Providers" in html
    assert "Service Clients" in html
    assert "Action Servers" in html
    assert "Action Clients" in html
    assert "X-ROS2Inspector-Token" in html
    assert "secret-token" in html


def test_ui_initial_state_escapes_script_end_tag() -> None:
    state = _state_payload()
    state["malicious"] = "</script><script>alert(1)</script>"
    html = build_html(state)
    assert "</script><script>" not in html
    assert "__INITIAL_STATE_SLOT__" not in html


def _wired_package(pub_type: str, sub_type: str) -> dict[str, Any]:
    talker = _node_payload("talker")
    talker["publishers"] = [{"id": "t1", "name": "/chatter", "msgType": pub_type}]
    listener = {**_node_payload("listener"), "id": "n2"}
    listener["subscribers"] = [{"id": "t2", "name": "/chatter", "msgType": sub_type}]
    return {
        **_package_payload(),
        "nodes": [talker, listener],
        "edges": [{"id": "e1", "fn": "n1", "ft": "t1", "tn": "n2", "tt": "t2"}],
    }


def test_wire_accepts_both_spellings_of_one_type() -> None:
    PackageSpec.model_validate(_wired_package("std_msgs/String", "std_msgs/msg/String"))
    with pytest.raises(ValidationError, match="types differ"):
        PackageSpec.model_validate(_wired_package("std_msgs/String", "std_msgs/msg/Int32"))


def test_validation_messages_hide_pydantic_prefixes(tmp_path: Path) -> None:
    server, port = _start_server(tmp_path)
    package = _package_payload()
    package["nodes"] = [_node_payload("Bad Name")]
    try:
        status, body = _request(f"http://127.0.0.1:{port}/save", _state_payload(package))
    finally:
        server.shutdown()
        server.server_close()
    assert status == 422
    messages = [field["message"] for field in body["fields"]]
    assert messages and all(not m.startswith("Value error") for m in messages)
    assert any(m.startswith("node name must be a lowercase identifier") for m in messages)
