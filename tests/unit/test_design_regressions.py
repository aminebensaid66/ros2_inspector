"""Regression tests for designer bugs D1-D15 (see analysis/ros2inspector-dev-features.md)."""

from __future__ import annotations

import errno
import json
import socket
import sys
import types
import urllib.error
import urllib.request
from http.server import HTTPServer
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from rich.console import Console

from ros2inspector.design.codegen import (
    _GENERATED_MARKER,
    generate_package,
    generate_package_result,
    read_manifest,
    render_node,
    render_package,
)
from ros2inspector.design.models import DesignState, EndpointSpec, NodeSpec, PackageSpec
from ros2inspector.design.server import (
    STATE_FILE,
    _apply_renames,
    _load_state,
    _make_handler,
    _save_state,
    bind_server,
)
from ros2inspector.design.ui import build_html

from .test_design import _request, _start_server


def _endpoint(eid: str, name: str, msg_type: str) -> dict[str, str]:
    return {"id": eid, "name": name, "msgType": msg_type}


def _node(nid: str, label: str, **groups: list[dict[str, str]]) -> dict[str, Any]:
    node: dict[str, Any] = {
        "id": nid,
        "label": label,
        "publishers": [],
        "subscribers": [],
        "services": [],
        "clients": [],
        "action_servers": [],
        "action_clients": [],
    }
    node.update(groups)
    return node


def _pkg(pid: str, name: str, nodes: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "id": pid,
        "pkg_name": name,
        "nodes": nodes if nodes is not None else [_node("n1", "talker")],
        "edges": [],
    }


def _state(*packages: dict[str, Any]) -> dict[str, Any]:
    return {"version": 1, "nid": 9, "tid": 9, "pid": 9, "packages": list(packages)}


# ── D1: regeneration keeps user edits ───────────────────────────────────────


def test_regenerate_keeps_edited_and_user_added_files(tmp_path: Path) -> None:
    generate_package(_pkg("p1", "demo_pkg"), tmp_path)
    node_file = tmp_path / "demo_pkg" / "demo_pkg" / "talker.py"
    node_file.write_text(node_file.read_text() + "\n# my edit\n")
    readme = tmp_path / "demo_pkg" / "README.md"
    readme.write_text("mine")

    updated = _pkg(
        "p1",
        "demo_pkg",
        [
            _node("n1", "talker", publishers=[_endpoint("t1", "/chatter", "std_msgs/String")]),
            _node("n2", "listener"),
        ],
    )
    result = generate_package_result(updated, tmp_path)

    assert "# my edit" in node_file.read_text()
    assert readme.read_text() == "mine"
    assert "demo_pkg/talker.py" in result.kept_modified
    assert "demo_pkg/listener.py" in result.written
    assert "listener = demo_pkg.listener:main" in (tmp_path / "demo_pkg" / "setup.py").read_text()


def test_regenerate_updates_unedited_files_and_removes_stale_ones(tmp_path: Path) -> None:
    generate_package(_pkg("p1", "demo_pkg", [_node("n1", "talker"), _node("n2", "old")]), tmp_path)
    stale = tmp_path / "demo_pkg" / "demo_pkg" / "old.py"
    assert stale.is_file()

    with_pub = _node("n1", "talker", publishers=[_endpoint("t1", "/chatter", "std_msgs/String")])
    result = generate_package_result(_pkg("p1", "demo_pkg", [with_pub]), tmp_path)

    assert "create_publisher" in (tmp_path / "demo_pkg" / "demo_pkg" / "talker.py").read_text()
    assert "demo_pkg/old.py" in result.removed
    assert not stale.exists()
    manifest = read_manifest(tmp_path / "demo_pkg")
    assert manifest is not None and "demo_pkg/old.py" not in manifest


def test_edited_file_that_left_the_design_is_kept(tmp_path: Path) -> None:
    generate_package(_pkg("p1", "demo_pkg", [_node("n1", "talker"), _node("n2", "old")]), tmp_path)
    old = tmp_path / "demo_pkg" / "demo_pkg" / "old.py"
    old.write_text("# hand written\n")
    result = generate_package_result(_pkg("p1", "demo_pkg"), tmp_path)
    assert old.read_text() == "# hand written\n"
    assert "demo_pkg/old.py" in result.kept_modified


def test_legacy_marker_treats_differing_files_as_user_owned(tmp_path: Path) -> None:
    target = tmp_path / "demo_pkg"
    (target / "demo_pkg").mkdir(parents=True)
    (target / _GENERATED_MARKER).write_text("generated-by=ros2inspector\nformat=1\n")
    (target / "demo_pkg" / "talker.py").write_text("# edited under v1\n")
    result = generate_package_result(_pkg("p1", "demo_pkg"), tmp_path)
    assert (target / "demo_pkg" / "talker.py").read_text() == "# edited under v1\n"
    assert "demo_pkg/talker.py" in result.kept_modified
    assert json.loads((target / _GENERATED_MARKER).read_text())["format"] == 2


def test_generation_leaves_no_temp_or_backup_dirs(tmp_path: Path) -> None:
    generate_package(_pkg("p1", "demo_pkg"), tmp_path)
    generate_package(_pkg("p1", "demo_pkg"), tmp_path)
    leftovers = [p.name for p in tmp_path.iterdir() if p.name.startswith(".")]
    assert leftovers == []


# ── D2: renames never delete a package ──────────────────────────────────────


def _generated(tmp_path: Path, name: str, content: str) -> None:
    generate_package(_pkg("x", name), tmp_path)
    (tmp_path / name / "USER.txt").write_text(content)


def test_swapping_two_package_names_keeps_both(tmp_path: Path) -> None:
    _generated(tmp_path, "alpha", "alpha code")
    _generated(tmp_path, "beta", "beta code")
    old = {
        "packages": [
            {"id": "a", "pkg_name": "alpha", "_generated_name": "alpha"},
            {"id": "b", "pkg_name": "beta", "_generated_name": "beta"},
        ]
    }
    _apply_renames([_pkg("a", "beta"), _pkg("b", "alpha")], tmp_path, old)
    assert (tmp_path / "beta" / "USER.txt").read_text() == "alpha code"
    assert (tmp_path / "alpha" / "USER.txt").read_text() == "beta code"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["alpha", "beta"]


def test_three_way_rename_cycle(tmp_path: Path) -> None:
    for name in ("one", "two", "three"):
        _generated(tmp_path, name, name)
    old = {
        "packages": [
            {"id": n, "pkg_name": n, "_generated_name": n} for n in ("one", "two", "three")
        ]
    }
    _apply_renames([_pkg("one", "two"), _pkg("two", "three"), _pkg("three", "one")], tmp_path, old)
    assert (tmp_path / "two" / "USER.txt").read_text() == "one"
    assert (tmp_path / "three" / "USER.txt").read_text() == "two"
    assert (tmp_path / "one" / "USER.txt").read_text() == "three"


def test_rename_onto_other_generated_package_is_refused(tmp_path: Path) -> None:
    _generated(tmp_path, "alpha", "alpha code")
    _generated(tmp_path, "beta", "beta code")
    old = {"packages": [{"id": "a", "pkg_name": "alpha", "_generated_name": "alpha"}]}
    with pytest.raises(FileExistsError, match="never deletes"):
        _apply_renames([_pkg("a", "beta")], tmp_path, old)
    assert (tmp_path / "alpha" / "USER.txt").read_text() == "alpha code"
    assert (tmp_path / "beta" / "USER.txt").read_text() == "beta code"


def test_server_swap_rename_and_regenerate(tmp_path: Path) -> None:
    server, port = _start_server(tmp_path)
    url = f"http://127.0.0.1:{port}/generate"
    try:
        code, _ = _request(url, _state(_pkg("a", "alpha"), _pkg("b", "beta")))
        assert code == 200
        (tmp_path / "alpha" / "USER.txt").write_text("alpha code")
        code, body = _request(url, _state(_pkg("a", "beta"), _pkg("b", "alpha")))
        assert code == 200, body
        assert (tmp_path / "beta" / "USER.txt").read_text() == "alpha code"
        saved = json.loads((tmp_path / STATE_FILE).read_text())
        assert {p["id"]: p["_generated_name"] for p in saved["packages"]} == {
            "a": "beta",
            "b": "alpha",
        }
    finally:
        server.shutdown()
        server.server_close()


# ── D3 / D4: generated code binds the right names ───────────────────────────


@pytest.fixture
def ros_stubs(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Minimal rclpy / interface modules so generated nodes can be imported and run."""
    created: dict[str, Any] = {}

    class FakeNode:
        def __init__(self, name: str) -> None:
            self.name = name
            created["node"] = self
            created.setdefault("publishers", [])
            created.setdefault("subscriptions", [])

        def create_publisher(self, cls: type, topic: str, qos: int) -> object:
            created["publishers"].append((cls, topic))
            return object()

        def create_subscription(self, cls: type, topic: str, cb: object, qos: int) -> object:
            created["subscriptions"].append((cls, topic))
            return object()

        def create_service(self, cls: type, name: str, cb: object) -> object:
            created.setdefault("services", []).append((cls, name))
            return object()

        def create_client(self, cls: type, name: str) -> object:
            return object()

        def create_timer(self, period: float, cb: object) -> object:
            return object()

        def get_logger(self) -> Any:
            return types.SimpleNamespace(info=lambda *_: None)

    def module(name: str, **attrs: object) -> types.ModuleType:
        mod = types.ModuleType(name)
        for key, value in attrs.items():
            setattr(mod, key, value)
        monkeypatch.setitem(sys.modules, name, mod)
        return mod

    module("rclpy", init=lambda **_: None, spin=lambda _: None, try_shutdown=lambda: None)
    module("rclpy.node", Node=FakeNode)
    module(
        "rclpy.action",
        ActionClient=lambda *a, **k: object(),
        ActionServer=lambda *a, **k: object(),
    )
    for package in ("std_msgs", "my_msgs", "example_interfaces"):
        module(package)
        for kind in ("msg", "srv", "action"):
            module(
                f"{package}.{kind}",
                **{
                    name: type(name, (), {"__module__": f"{package}.{kind}"})
                    for name in ("String", "Image", "Fibonacci", "AddTwoInts")
                },
            )
    return created


def _instantiate(source: str) -> types.ModuleType:
    mod = types.ModuleType("generated_node")
    exec(compile(source, "generated_node.py", "exec"), mod.__dict__)  # noqa: S102
    classes = [
        v for v in mod.__dict__.values() if isinstance(v, type) and v.__module__ == mod.__name__
    ]
    assert len(classes) == 1
    classes[0]()
    return mod


def test_node_named_like_message_does_not_shadow_it(ros_stubs: dict[str, Any]) -> None:
    node = NodeSpec.model_validate(
        _node("n1", "image", publishers=[_endpoint("t1", "/camera", "my_msgs/Image")])
    )
    source = render_node(node)
    assert "class ImageNode(Node)" in source
    _instantiate(source)
    cls, topic = ros_stubs["publishers"][0]
    assert (cls.__module__, cls.__name__, topic) == ("my_msgs.msg", "Image", "/camera")


@pytest.mark.parametrize("label", ["string", "logging", "time", "rclpy", "launch", "typing"])
def test_node_labels_that_shadow_python_modules_are_rejected(label: str) -> None:
    # string.py next to the other node files breaks 'import logging' inside rclpy as soon
    # as any node file of the package is run directly (seen on ROS 2 Jazzy).
    with pytest.raises(ValidationError, match="shadows the Python module"):
        NodeSpec.model_validate(_node("n1", label))


def test_node_label_equal_to_used_interface_package_is_rejected() -> None:
    package = _pkg(
        "p1",
        "demo_pkg",
        [
            _node("n1", "my_msgs"),
            _node("n2", "talker", publishers=[_endpoint("t1", "/a", "my_msgs/Image")]),
        ],
    )
    with pytest.raises(ValidationError, match="interface package"):
        PackageSpec.model_validate(package)


def test_node_named_node_does_not_shadow_rclpy_node(ros_stubs: dict[str, Any]) -> None:
    source = render_node(NodeSpec.model_validate(_node("n1", "node")))
    assert "class NodeNode(Node)" in source
    _instantiate(source)


def test_same_type_name_from_two_packages_is_aliased(ros_stubs: dict[str, Any]) -> None:
    node = NodeSpec.model_validate(
        _node(
            "n1",
            "bridge",
            publishers=[_endpoint("t1", "/a", "std_msgs/String")],
            subscribers=[_endpoint("t2", "/b", "my_msgs/String")],
        )
    )
    source = render_node(node)
    assert "from std_msgs.msg import String as StdMsgsString" in source
    assert "from my_msgs.msg import String as MyMsgsString" in source
    _instantiate(source)
    assert ros_stubs["publishers"][0][0].__module__ == "std_msgs.msg"
    assert ros_stubs["subscriptions"][0][0].__module__ == "my_msgs.msg"


def test_canonical_interface_form_is_accepted(ros_stubs: dict[str, Any]) -> None:
    node = NodeSpec.model_validate(
        _node(
            "n1", "adder", services=[_endpoint("s1", "/add", "example_interfaces/srv/AddTwoInts")]
        )
    )
    source = render_node(node)
    assert "from example_interfaces.srv import AddTwoInts" in source
    _instantiate(source)
    assert ros_stubs["services"][0][1] == "/add"


# ── D5 / D6: package metadata and names ─────────────────────────────────────


def test_package_xml_declares_launch_dependencies() -> None:
    files = render_package(PackageSpec.model_validate(_pkg("p1", "demo_pkg")))
    assert "<exec_depend>launch</exec_depend>" in files["package.xml"]
    assert "<exec_depend>launch_ros</exec_depend>" in files["package.xml"]
    assert "packages=[package_name]" in files["setup.py"]


@pytest.mark.parametrize("name", ["test", "tests", "rclpy", "launch", "std_msgs", "json", "class"])
def test_reserved_package_names_are_rejected(name: str) -> None:
    with pytest.raises(ValidationError):
        PackageSpec.model_validate(_pkg("p1", name))


@pytest.mark.parametrize("label", ["import", "my__node", "trailing_"])
def test_invalid_node_labels_are_rejected(label: str) -> None:
    with pytest.raises(ValidationError):
        NodeSpec.model_validate(_node("n1", label))


def test_node_with_package_name_is_rejected() -> None:
    with pytest.raises(ValidationError, match="same name as its package"):
        PackageSpec.model_validate(_pkg("p1", "demo_pkg", [_node("n1", "demo_pkg")]))


# ── D7: wires must be real connections ──────────────────────────────────────


def _wired(pub_name: str, pub_type: str, sub_name: str, sub_type: str) -> dict[str, Any]:
    package = _pkg(
        "p1",
        "demo_pkg",
        [
            _node("n1", "talker", publishers=[_endpoint("t1", pub_name, pub_type)]),
            _node("n2", "listener", subscribers=[_endpoint("t2", sub_name, sub_type)]),
        ],
    )
    package["edges"] = [{"id": "e1", "fn": "n1", "ft": "t1", "tn": "n2", "tt": "t2"}]
    return package


def test_matching_wire_is_accepted() -> None:
    PackageSpec.model_validate(_wired("/chatter", "std_msgs/String", "/chatter", "std_msgs/String"))


def test_wire_with_different_topic_names_is_rejected() -> None:
    with pytest.raises(ValidationError, match="topic names differ"):
        PackageSpec.model_validate(
            _wired("/image_raw", "std_msgs/String", "/detections", "std_msgs/String")
        )


def test_wire_with_different_types_is_rejected() -> None:
    with pytest.raises(ValidationError, match="types differ"):
        PackageSpec.model_validate(_wired("/x", "sensor_msgs/Image", "/x", "std_msgs/String"))


def test_dangling_wire_is_rejected() -> None:
    package = _pkg("p1", "demo_pkg")
    package["edges"] = [{"id": "e1", "fn": "n1", "ft": "t1", "tn": "n9", "tt": "t2"}]
    with pytest.raises(ValidationError, match="does not exist"):
        PackageSpec.model_validate(package)


def test_saved_state_with_invalid_wires_keeps_the_design(tmp_path: Path) -> None:
    state = _state(_wired("/a", "std_msgs/String", "/b", "std_msgs/String"))
    (tmp_path / STATE_FILE).write_text(json.dumps(state))
    loaded = _load_state(tmp_path)
    assert loaded is not None
    assert loaded["packages"][0]["edges"] == []
    assert len(loaded["packages"][0]["nodes"]) == 2


def test_unloadable_state_is_copied_aside(tmp_path: Path) -> None:
    (tmp_path / STATE_FILE).write_text("{broken")
    assert _load_state(tmp_path) is None
    assert (tmp_path / f"{STATE_FILE}.invalid").read_text() == "{broken"


# ── D8 / D9: interface kinds and ROS names ──────────────────────────────────


@pytest.mark.parametrize(
    ("group", "msg_type"),
    [
        ("services", "std_msgs/String"),
        ("publishers", "std_srvs/Trigger"),
        ("action_clients", "std_msgs/String"),
        ("clients", "my_pkg/msg/Thing"),
    ],
)
def test_interface_kind_must_match_endpoint_kind(group: str, msg_type: str) -> None:
    with pytest.raises(ValidationError, match="expected a"):
        NodeSpec.model_validate(_node("n1", "talker", **{group: [_endpoint("e1", "/x", msg_type)]}))


def test_unknown_custom_types_are_allowed() -> None:
    NodeSpec.model_validate(
        _node("n1", "talker", publishers=[_endpoint("e1", "/x", "my_robot_msgs/Status")])
    )


@pytest.mark.parametrize("name", ["a/", "/a/1b", "1abc", "/a//b", "a__b", "/a b"])
def test_invalid_ros_names_are_rejected(name: str) -> None:
    with pytest.raises(ValidationError):
        EndpointSpec.model_validate(_endpoint("e1", name, "std_msgs/String"))


@pytest.mark.parametrize("name", ["chatter", "/chatter", "~/status", "/robot1/cmd_vel", "_hidden"])
def test_valid_ros_names_are_accepted(name: str) -> None:
    EndpointSpec.model_validate(_endpoint("e1", name, "std_msgs/String"))


def test_private_name_produces_valid_variable(ros_stubs: dict[str, Any]) -> None:
    node = NodeSpec.model_validate(
        _node("n1", "talker", publishers=[_endpoint("t1", "~/status", "std_msgs/String")])
    )
    source = render_node(node)
    assert "self.pub_status" in source
    _instantiate(source)


# ── D11: validation happens before any write ────────────────────────────────


def test_unrelated_directory_blocks_whole_generate_before_writes(tmp_path: Path) -> None:
    (tmp_path / "beta").mkdir()
    server, port = _start_server(tmp_path)
    try:
        code, body = _request(
            f"http://127.0.0.1:{port}/generate", _state(_pkg("a", "alpha"), _pkg("b", "beta"))
        )
        assert code == 409
        assert "not generated" in body["error"]
        assert not (tmp_path / "alpha").exists()
    finally:
        server.shutdown()
        server.server_close()


def test_generate_response_reports_kept_files(tmp_path: Path) -> None:
    server, port = _start_server(tmp_path)
    url = f"http://127.0.0.1:{port}/generate"
    try:
        assert _request(url, _state(_pkg("a", "alpha")))[0] == 200
        (tmp_path / "alpha" / "alpha" / "talker.py").write_text("# mine\n")
        code, body = _request(url, _state(_pkg("a", "alpha")))
        assert code == 200
        assert body["kept_modified"] == ["alpha/alpha/talker.py"]
    finally:
        server.shutdown()
        server.server_close()


# ── D12 / D13: binding and Host checks ──────────────────────────────────────


def test_bind_server_falls_back_when_port_is_taken() -> None:
    handler = _make_handler("", Path("."), Console(quiet=True))
    blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    blocker.bind(("127.0.0.1", 0))
    blocker.listen()
    taken = blocker.getsockname()[1]
    try:
        server = bind_server(taken, handler)
        try:
            assert server.server_address[1] != taken
        finally:
            server.server_close()
    finally:
        blocker.close()


def test_bind_server_reraises_other_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_: object, **__: object) -> HTTPServer:
        raise OSError(errno.EADDRNOTAVAIL, "nope")

    monkeypatch.setattr("ros2inspector.design.server.HTTPServer", boom)
    with pytest.raises(OSError):
        bind_server(1234, _make_handler("", Path("."), Console(quiet=True)))


def test_get_rejects_foreign_host(tmp_path: Path) -> None:
    server, port = _start_server(tmp_path, token="secret-token")
    try:
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/", headers={"Host": "evil.example"}
        )
        with pytest.raises(urllib.error.HTTPError) as info:
            urllib.request.urlopen(request)
        assert info.value.code == 403
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/") as response:
            assert "secret-token" in response.read().decode()
    finally:
        server.shutdown()
        server.server_close()


# ── D15 and HTML embedding ──────────────────────────────────────────────────


def test_ui_escapes_script_breakers_in_saved_state() -> None:
    state = _state(_pkg("p1", "demo_pkg"))
    state["packages"][0]["nodes"][0]["x"] = 0
    state["note"] = "<!--<script> </script> __SESSION_TOKEN_SLOT__"
    html = build_html(state, session_token="tok")
    body = html.split("const SESSION_TOKEN=", 1)[1]
    assert "<!--<script>" not in html
    assert body.startswith('"tok"')
    # Single-pass substitution: the slot name inside user data is left as data.
    assert html.count("__SESSION_TOKEN_SLOT__") == 1
    assert html.count('"tok"') == 1
    assert "\\u003c!--\\u003cscript\\u003e" in html


def test_ui_has_free_text_type_inputs_and_safe_handlers() -> None:
    html = build_html(None)
    assert '<datalist id="dl-msg">' in html
    assert "function jsa(v)" in html
    assert "onclick=\"remT('${side}'" not in html
    assert "sensor_msgs/CameraInfo" in html


def test_ids_with_quotes_are_rejected_server_side() -> None:
    package = _pkg("p1", "demo_pkg")
    package["id"] = "p1');alert(1);('"
    with pytest.raises(ValidationError):
        DesignState.model_validate(_state(package))


def test_save_state_roundtrip_keeps_active_package(tmp_path: Path) -> None:
    state = _state(_pkg("a", "alpha"), _pkg("b", "beta"))
    state["activePkg"] = "b"
    _save_state(tmp_path, DesignState.model_validate(state).model_dump(by_alias=True))
    loaded = _load_state(tmp_path)
    assert loaded is not None and loaded["activePkg"] == "b"


# ── generated packages are healthy and follow ament_flake8 style (#16) ──────


def _full_package() -> dict[str, Any]:
    def ep(eid: str, name: str, msg_type: str) -> dict[str, str]:
        return {"id": eid, "name": name, "msgType": msg_type}

    return {
        "id": "p",
        "pkg_name": "healthy_pkg",
        "nodes": [
            {
                "id": "a",
                "label": "talker",
                "publishers": [ep("1", "/chatter", "std_msgs/String")],
                "services": [ep("2", "/reset", "std_srvs/Trigger")],
                "action_servers": [ep("3", "/fib", "example_interfaces/Fibonacci")],
            },
            {
                "id": "b",
                "label": "listener",
                "subscribers": [
                    ep("4", "/robot/camera/front_left/image_rect", "sensor_msgs/Image")
                ],
            },
        ],
    }


def test_generated_package_scores_full_health(tmp_path: Path) -> None:
    from ros2inspector.static.health_scorer import score_package
    from ros2inspector.static.package_xml import parse_package_xml

    generate_package(_full_package(), tmp_path)
    metadata = parse_package_xml(tmp_path / "healthy_pkg" / "package.xml")
    assert metadata is not None
    assert score_package(metadata) == 100


def test_generated_sources_follow_ament_flake8_style(tmp_path: Path) -> None:
    generate_package(_full_package(), tmp_path)
    package = tmp_path / "healthy_pkg"
    talker = (package / "healthy_pkg" / "talker.py").read_text()
    imports = [line for line in talker.splitlines() if line.startswith(("import ", "from "))]
    assert imports == sorted(imports, key=lambda line: line.split()[1].lower())
    assert "from rclpy.action import ActionServer" in imports  # ActionClient is unused here
    assert "\n\n\n    def " not in talker
    for path in package.rglob("*.py"):
        if path.parent.name != "test":
            source = path.read_text()
            assert '"' not in source, f"double quotes in {path.name}"
            assert all(len(line) <= 99 for line in source.splitlines()), path.name


# ── nodes --show-connections names service and action peers (#29) ───────────


def test_show_connections_names_service_and_action_peers(tmp_path: Path) -> None:
    from typer.testing import CliRunner

    from ros2inspector.cli.app import app

    def ep(eid: str, name: str, msg_type: str) -> dict[str, str]:
        return {"id": eid, "name": name, "msgType": msg_type}

    src = tmp_path / "ws" / "src"
    generate_package(
        {
            "id": "p",
            "pkg_name": "peers_pkg",
            "nodes": [
                {
                    "id": "a",
                    "label": "server_node",
                    "services": [ep("1", "/reset", "std_srvs/Trigger")],
                    "action_servers": [ep("2", "/fib", "example_interfaces/Fibonacci")],
                },
                {
                    "id": "b",
                    "label": "client_node",
                    "clients": [ep("3", "/reset", "std_srvs/Trigger")],
                    "action_clients": [ep("4", "/fib", "example_interfaces/Fibonacci")],
                },
            ],
        },
        src,
    )
    result = CliRunner().invoke(
        app,
        ["-q", "nodes", "-C", str(tmp_path / "ws"), "--show-connections", "--no-cache"],
        env={"COLUMNS": "200"},
    )
    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    reset_lines = [line for line in lines if "/reset" in line]
    fib_lines = [line for line in lines if "/fib" in line]
    assert any("(client: client_node)" in line for line in reset_lines), reset_lines
    assert any("(server: server_node)" in line for line in reset_lines), reset_lines
    assert any("(client: client_node)" in line for line in fib_lines), fib_lines
    assert any("(server: server_node)" in line for line in fib_lines), fib_lines


@pytest.mark.parametrize("mask", range(64))
def test_every_endpoint_combination_has_flake8_blank_lines(mask: int) -> None:
    """Each subset of endpoint kinds renders with PEP 8 blank lines (E301-E303)."""
    from ros2inspector.design.codegen import render_node
    from ros2inspector.design.models import NodeSpec

    groups = {
        "publishers": ("/out", "std_msgs/String"),
        "subscribers": ("/in", "std_msgs/String"),
        "services": ("/srv", "std_srvs/Trigger"),
        "clients": ("/cli", "std_srvs/Trigger"),
        "action_servers": ("/act", "example_interfaces/Fibonacci"),
        "action_clients": ("/goal", "example_interfaces/Fibonacci"),
    }
    node: dict[str, Any] = {"id": "n", "label": "combo"}
    for bit, (group, (name, msg_type)) in enumerate(groups.items()):
        if mask & (1 << bit):
            node[group] = [{"id": f"e{bit}", "name": name, "msgType": msg_type}]
    source = render_node(NodeSpec.model_validate(node))
    assert "\n\n\n\n" not in source  # E303: never more than two blank lines
    assert "\n\n\n    " not in source  # E303: one blank line between methods
    assert "\n\nclass " not in source.replace("\n\n\nclass ", "")  # E302 before class
    assert "\n\ndef main" not in source.replace("\n\n\ndef main", "")  # E302 before main
    compile(source, "combo.py", "exec")


@pytest.mark.parametrize(
    "topic",
    [
        "/robot/front_left/wheel/joint_states_filtered",  # the name from #9
        "/" + "/".join(["very_long_segment_name"] * 6),  # 138 characters
    ],
)
def test_long_names_stay_within_ament_flake8_line_length(tmp_path: Path, topic: str) -> None:
    """Every generated line fits in 99 characters or is explicitly exempted (E501)."""

    def ep(eid: str, msg_type: str) -> dict[str, str]:
        return {"id": eid, "name": topic, "msgType": msg_type}

    label = "front_left_wheel_joint_state_filter_node_with_a_long_name"
    generate_package(
        {
            "id": "p",
            "pkg_name": "a_rather_long_package_name_for_the_wheel_controllers",
            "nodes": [
                {
                    "id": "a",
                    "label": label,
                    "publishers": [ep("1", "sensor_msgs/JointState")],
                    "subscribers": [ep("2", "sensor_msgs/JointState")],
                    "services": [ep("3", "std_srvs/Trigger")],
                    "clients": [ep("4", "std_srvs/Trigger")],
                    "action_servers": [ep("5", "example_interfaces/Fibonacci")],
                    "action_clients": [ep("6", "example_interfaces/Fibonacci")],
                }
            ],
        },
        tmp_path,
    )
    sources = [p for p in tmp_path.rglob("*.py") if p.parent.name != "test"]
    assert len(sources) == 4  # node, __init__, setup.py, launch.py
    for path in sources:
        source = path.read_text()
        compile(source, path.name, "exec")
        for line in source.splitlines():
            assert len(line) <= 99 or line.endswith(("# noqa: E501", ",E501")), (path.name, line)
    node = next(p for p in sources if p.stem == label).read_text()
    assert "(Node):\n\n    def __init__" in node  # flake8-class-newline (CNL100)
    if len(topic) < 50:
        assert "E501" not in node  # realistic names need no exemption at all


def test_saved_state_recovers_only_invalid_edges_across_packages(tmp_path: Path) -> None:
    def wired(pid: str, name: str) -> dict[str, Any]:
        package = _pkg(pid, name, [
            _node("sender", "sender", publishers=[_endpoint("pub", "/events", "std_msgs/String")]),
            _node(
                "receiver",
                "receiver",
                subscribers=[_endpoint("sub", "/events", "std_msgs/String")],
            ),
        ])
        package["edges"] = [{"id": "valid", "fn": "sender", "ft": "pub",
                             "tn": "receiver", "tt": "sub"}]
        return package

    first = wired("p1", "first_pkg")
    first["edges"].append({"id": "broken", "fn": "sender", "ft": "pub",
                           "tn": "receiver", "tt": "missing"})
    second = wired("p2", "second_pkg")
    source = _state(first, second)
    (tmp_path / STATE_FILE).write_text(json.dumps(source))
    recovered = _load_state(tmp_path)

    assert recovered is not None
    assert [edge["id"] for edge in recovered["packages"][0]["edges"]] == ["valid"]
    assert [edge["id"] for edge in recovered["packages"][1]["edges"]] == ["valid"]
    assert json.loads((tmp_path / f"{STATE_FILE}.invalid").read_text()) == source


def test_saved_state_does_not_hide_invalid_nodes_by_dropping_wires(tmp_path: Path) -> None:
    invalid = _state(_pkg("p1", "demo_pkg", [_node("n1", "Invalid Name")]))
    (tmp_path / STATE_FILE).write_text(json.dumps(invalid))
    assert _load_state(tmp_path) is None
    assert json.loads((tmp_path / f"{STATE_FILE}.invalid").read_text()) == invalid
