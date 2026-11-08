"""Generate must not report success when the design is not saved or a package would not run."""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import threading
from collections.abc import Iterator
from http.server import HTTPServer
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from rich.console import Console

from ros2inspector.design import server as design_server
from ros2inspector.design.codegen import (
    _GENERATED_MARKER,
    add_console_scripts,
    console_scripts,
)
from ros2inspector.design.server import STATE_FILE, _make_handler
from ros2inspector.design.ui import build_html
from tests.unit.test_design_server_http import TOKEN, _node, _pkg, _post, _state, server

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


# ── #44: an edited setup.py or launch file still registers and starts every node ───


def test_adding_a_node_registers_it_in_an_edited_setup_py(server: Any) -> None:
    httpd, out = server
    assert _post(httpd, "/generate", _state(_pkg()))[0] == 200
    setup = out / "demo_pkg/setup.py"
    setup.write_text(setup.read_text() + "# User edit\n")
    status, data = _post(httpd, "/generate", _state(_with_node(_pkg(), "auxiliary", "n2")))
    assert status == 200, data
    text = setup.read_text()
    assert text.endswith("# User edit\n")
    assert console_scripts(text) == {"talker", "auxiliary"}
    assert "'auxiliary = demo_pkg.auxiliary:main'," in text
    assert "demo_pkg/setup.py" in data["kept_modified"]
    assert data["updated_in"] == ["demo_pkg: registered auxiliary in your edited setup.py"]


def test_unreadable_console_scripts_are_refused_with_the_entry_to_add(server: Any) -> None:
    httpd, out = server
    assert _post(httpd, "/generate", _state(_pkg()))[0] == 200
    setup = out / "demo_pkg/setup.py"
    text = setup.read_text()
    start = text.index("'console_scripts': [")
    end = text.index("],", start) + 1
    setup.write_text(
        "SCRIPTS = ['talker = demo_pkg.talker:main']\n"
        + text[:start]
        + "'console_scripts': SCRIPTS"
        + text[end:]
    )
    status, data = _post(httpd, "/generate", _state(_with_node(_pkg(), "auxiliary", "n2")))
    assert status == 409
    assert "'auxiliary = demo_pkg.auxiliary:main'" in data["error"]
    assert not (out / "demo_pkg/demo_pkg/auxiliary.py").exists()


def test_renaming_a_node_updates_an_edited_launch_file_and_setup(server: Any) -> None:
    httpd, out = server
    assert _post(httpd, "/generate", _state(_pkg(nodes=[_camera()])))[0] == 200
    package = out / "demo_pkg"
    for name in ("launch/launch.py", "setup.py"):
        path = package / name
        path.write_text(path.read_text() + "# edit\n")
    status, data = _post(httpd, "/generate", _state(_pkg(nodes=[_camera("camera_renamed")])))
    assert status == 200, data
    launch = (package / "launch/launch.py").read_text()
    assert "executable='camera_renamed'" in launch and "name='camera_renamed'" in launch
    assert "camera_node" not in launch
    assert console_scripts((package / "setup.py").read_text()) == {"camera_renamed"}


def test_edited_launch_file_and_package_xml_warn_about_a_new_node(server: Any) -> None:
    httpd, out = server
    assert _post(httpd, "/generate", _state(_pkg()))[0] == 200
    package = out / "demo_pkg"
    for name in ("launch/launch.py", "package.xml"):
        path = package / name
        path.write_text(path.read_text() + "\n")
    payload = _with_node(_pkg(), "auxiliary", "n2")
    payload["nodes"][1]["publishers"][0]["msgType"] = "geometry_msgs/Twist"
    status, data = _post(httpd, "/generate", _state(payload))
    assert status == 200, data
    assert data["warnings"] == [
        "demo_pkg: launch/launch.py has your edits, so it does not start auxiliary; "
        "add the node(s) to it yourself.",
        "demo_pkg: package.xml has your edits and does not declare geometry_msgs; "
        "add <depend> entries for them.",
    ]


@pytest.mark.parametrize(
    "source",
    [
        "setup(entry_points={'console_scripts': ['a = p.a:main']})\n",
        "setup(entry_points={'console_scripts': [\n    'a = p.a:main',\n]})\n",
        "setup(entry_points={'console_scripts': []})\n",
        "setup(entry_points={\n    'console_scripts': [\n        'a = p.a:main'  # mine\n"
        "    ],\n})\n",
    ],
)
def test_console_scripts_are_appended_without_touching_the_rest(source: str) -> None:
    updated = add_console_scripts(source, ["b = p.b:main"])
    assert updated is not None
    assert console_scripts(updated) == (console_scripts(source) or set()) | {"b"}

    def tokens(text: str) -> str:
        return "".join(text.replace(",", "").split())

    assert tokens(updated.replace("'b = p.b:main'", "")) == tokens(source)


# ── #48: names ROS would reject at startup are refused on the field ────────────────


@pytest.mark.parametrize(
    "group, name, ok",
    [
        ("publishers", "/" + "a" * 246, True),
        ("publishers", "/" + "a" * 247, False),
        ("publishers", "a" * 246, True),
        ("publishers", "~/" + "a" * (247 - len("/talker/")), True),
        ("publishers", "~/" + "a" * (248 - len("/talker/")), False),
        ("services", "/" + "a" * 247, False),
        ("action_servers", "/" + "a" * 226, True),
        ("action_servers", "/" + "a" * 227, False),
        ("action_clients", "/" + "a" * 239, False),
    ],
)
def test_overlong_expanded_names_are_rejected_on_the_field(
    server: Any, group: str, name: str, ok: bool
) -> None:
    httpd, out = server
    node = _node()
    msg_type = {"publishers": "std_msgs/String", "services": "std_srvs/Trigger"}.get(
        group, "example_interfaces/Fibonacci"
    )
    node["publishers"] = []
    node[group] = [{"id": "e1", "name": name, "msgType": msg_type}]
    status, data = _post(httpd, "/generate", _state(_pkg(nodes=[node])))
    if ok:
        assert status == 200, data
        return
    assert status == 422
    assert data["fields"][0]["path"] == f"packages.0.nodes.0.{group}.0.name"
    assert "ROS 2 accepts at most" in data["fields"][0]["message"]
    assert not (out / "demo_pkg").exists()


@pytest.mark.parametrize(
    "target",
    [
        "demo_pkg.missing_module:main",
        "demo_pkg.talker:missing_callable",
        "another_pkg.talker:main",
    ],
)
def test_edited_setup_rejects_wrong_console_script_target(
    server: Any, target: str
) -> None:
    httpd, out = server
    design = _state(_pkg())
    assert _post(httpd, "/generate", design)[0] == 200
    setup = out / "demo_pkg/setup.py"
    source = setup.read_text()
    broken = source.replace("talker = demo_pkg.talker:main", f"talker = {target}")
    assert broken != source
    setup.write_text(broken)

    status, response = _post(httpd, "/generate", design)
    assert status == 409, response
    assert "console script 'talker' must point to 'demo_pkg.talker:main'" in response["error"]
    assert setup.read_text() == broken


def test_edited_setup_rejects_duplicate_managed_entry_points(server: Any) -> None:
    httpd, out = server
    design = _state(_pkg())
    assert _post(httpd, "/generate", design)[0] == 200
    setup = out / "demo_pkg/setup.py"
    source = setup.read_text()
    needle = "'talker = demo_pkg.talker:main',"
    assert needle in source
    setup.write_text(source.replace(needle, needle + "\n            " + needle))

    status, response = _post(httpd, "/generate", design)
    assert status == 409, response
    assert "console script 'talker' must point to 'demo_pkg.talker:main'" in response["error"]


def test_edited_setup_accepts_equivalent_entry_point_whitespace(server: Any) -> None:
    httpd, out = server
    design = _state(_pkg())
    assert _post(httpd, "/generate", design)[0] == 200
    setup = out / "demo_pkg/setup.py"
    setup.write_text(setup.read_text().replace(
        "talker = demo_pkg.talker:main", "talker=demo_pkg.talker:main"
    ))

    status, response = _post(httpd, "/generate", design)
    assert status == 200, response


def test_package_rename_rejects_stale_user_imports_before_writing(server: Any) -> None:
    httpd, out = server
    assert _post(httpd, "/generate", _state(_pkg()))[0] == 200
    old = out / "demo_pkg"
    (old / "demo_pkg" / "helper.py").write_text("VALUE = 42\n")
    node = old / "demo_pkg" / "talker.py"
    node.write_text(node.read_text() + "\nfrom demo_pkg.helper import VALUE\n")
    original = node.read_text()
    renamed = _pkg()
    renamed["pkg_name"] = "renamed_pkg"
    status, response = _post(httpd, "/generate", _state(renamed))
    assert status == 409, response
    assert "update its imports before renaming" in response["error"]
    assert node.read_text() == original
    assert not (out / "renamed_pkg").exists()


def test_package_rename_rejects_sibling_imports_before_writing(server: Any) -> None:
    httpd, out = server
    first = _pkg()
    second = _pkg("consumer_pkg", "p2", nodes=[_node("listener", "n2")])
    assert _post(httpd, "/generate", _state(first, second))[0] == 200
    consumer = out / "consumer_pkg" / "consumer_pkg" / "listener.py"
    consumer.write_text(consumer.read_text() + "\nfrom demo_pkg.talker import main\n")
    renamed = _pkg("new_pkg")
    status, response = _post(httpd, "/generate", _state(renamed, second))
    assert status == 409, response
    assert "imports 'demo_pkg'" in response["error"]
    assert (out / "demo_pkg").is_dir()
    assert not (out / "new_pkg").exists()


def test_rename_matches_console_script_whitespace(server: Any) -> None:
    httpd, out = server
    assert _post(httpd, "/generate", _state(_pkg()))[0] == 200
    setup = out / "demo_pkg/setup.py"
    setup.write_text(
        setup.read_text().replace("talker = demo_pkg.talker:main", "talker=demo_pkg.talker:main")
    )
    status, response = _post(
        httpd, "/generate", _state(_pkg(nodes=[_node("renamed", "n1")]))
    )
    assert status == 200, response
    text = setup.read_text()
    assert "'renamed = demo_pkg.renamed:main'" in text
    assert "talker=demo_pkg.talker:main" not in text


def test_rename_console_script_preserves_double_quoted_entry(server: Any) -> None:
    httpd, out = server
    assert _post(httpd, "/generate", _state(_pkg()))[0] == 200
    setup = out / "demo_pkg/setup.py"
    setup.write_text(
        setup.read_text().replace(
            "'talker = demo_pkg.talker:main'", '"talker=demo_pkg.talker:main"'
        )
    )
    status, response = _post(
        httpd, "/generate", _state(_pkg(nodes=[_node("renamed", "n1")]))
    )
    assert status == 200, response
    result = setup.read_text()
    assert '"renamed = demo_pkg.renamed:main"' in result
    assert "talker=demo_pkg.talker:main" not in result


def test_rename_rejects_stale_console_script_with_custom_target(server: Any) -> None:
    httpd, out = server
    assert _post(httpd, "/generate", _state(_pkg()))[0] == 200
    setup = out / "demo_pkg/setup.py"
    setup.write_text(
        setup.read_text().replace(
            "talker = demo_pkg.talker:main", "talker = demo_pkg.talker:custom_main"
        )
    )
    status, response = _post(
        httpd, "/generate", _state(_pkg(nodes=[_node("renamed", "n1")]))
    )
    assert status == 409, response
    assert "obsolete executable" in response["error"]
    assert "talker = demo_pkg.talker:custom_main" in setup.read_text()


@pytest.mark.parametrize(
    "renamed_labels",
    [("listener", "talker"), ("speaker", "talker")],
    ids=["swap", "chain"],
)
def test_rename_reusing_an_old_label_keeps_edited_setup(
    server: Any, renamed_labels: tuple[str, str]
) -> None:
    httpd, out = server
    two = _pkg(nodes=[_node("talker", "n1"), _node("listener", "n2")])
    assert _post(httpd, "/generate", _state(two))[0] == 200
    setup = out / "demo_pkg/setup.py"
    setup.write_text(setup.read_text() + "# USER_EDIT_RETAINED\n")
    first, second = renamed_labels
    status, response = _post(
        httpd, "/generate", _state(_pkg(nodes=[_node(first, "n1"), _node(second, "n2")]))
    )
    assert status == 200, response
    text = setup.read_text()
    assert f"'{first} = demo_pkg.{first}:main'" in text
    assert f"'{second} = demo_pkg.{second}:main'" in text
    assert "# USER_EDIT_RETAINED" in text


def test_rename_preserves_foreign_package_launch_entries(server: Any) -> None:
    httpd, out = server
    assert _post(httpd, "/generate", _state(_pkg()))[0] == 200
    launch = out / "demo_pkg/launch/launch.py"
    launch.write_text(launch.read_text().replace(
        "    ])", "        Node(package='other_pkg', executable='talker', name='talker'),\n    ])"
    ))
    status, response = _post(
        httpd, "/generate", _state(_pkg(nodes=[_node("renamed", "n1")]))
    )
    assert status == 200, response
    result = launch.read_text()
    assert "Node(package='other_pkg', executable='talker', name='talker')" in result
    assert "executable='renamed'" in result


def test_scoped_launch_rename_skips_dynamic_foreign_executable() -> None:
    from ros2inspector.design.source_edits import rename_launch_nodes

    text = (
        "from launch_ros.actions import Node\n"
        "from launch.substitutions import LaunchConfiguration\n"
        "entries = [\n"
        "    Node(package='foreign_pkg', executable=LaunchConfiguration('bin')),\n"
        "    Node(package='demo_pkg', executable='talker', name='runtime_custom'),\n"
        "]\n"
    )
    updated, started = rename_launch_nodes(text, {"talker": "renamed"}, "demo_pkg")
    assert "executable=LaunchConfiguration('bin')" in updated
    assert "executable='renamed', name='runtime_custom'" in updated
    assert started == {"renamed"}


def test_scoped_launch_rename_refuses_unknown_package_identity() -> None:
    from ros2inspector.design.source_edits import rename_launch_nodes

    text = (
        "from launch_ros.actions import Node\n"
        "from launch.substitutions import LaunchConfiguration\n"
        "node = Node(package=LaunchConfiguration('package'), executable='talker')\n"
    )
    with pytest.raises(ValueError, match="cannot be identified safely"):
        rename_launch_nodes(text, {"talker": "renamed"}, "demo_pkg")



def test_scoped_launch_rename_passes_unidentified_nodes_it_does_not_rename() -> None:
    from ros2inspector.design.source_edits import rename_launch_nodes

    text = (
        "from launch_ros.actions import Node\n"
        "entries = [\n"
        "    Node(package=pick(), executable='helper'),\n"
        "    Node(executable='tool'),\n"
        "    Node(package='demo_pkg', executable='talker', name='talker'),\n"
        "]\n"
    )
    updated, started = rename_launch_nodes(text, {"talker": "renamed"}, "demo_pkg")
    assert "Node(package=pick(), executable='helper')" in updated
    assert "executable='renamed', name='renamed'" in updated
    assert started == {"renamed"}


def test_scoped_launch_rename_refuses_dynamic_managed_executable() -> None:
    from ros2inspector.design.source_edits import rename_launch_nodes

    text = (
        "from launch_ros.actions import Node\n"
        "from launch.substitutions import LaunchConfiguration\n"
        "node = Node(package='demo_pkg', executable=LaunchConfiguration('bin'))\n"
    )
    with pytest.raises(ValueError, match="executable is not a literal"):
        rename_launch_nodes(text, {"talker": "renamed"}, "demo_pkg")

def test_launch_package_given_by_a_module_constant_is_recognised() -> None:
    from ros2inspector.design.source_edits import rename_launch_nodes

    text = (
        "from launch_ros.actions import Node\n"
        "PKG = 'demo_pkg'\n"
        "OTHER = 'other_pkg'\n"
        "REUSED = 'demo_pkg'\n"
        "REUSED = 'other_pkg'\n"
        "entries = [\n"
        "    Node(package=PKG, executable='talker', name='talker'),\n"
        "    Node(package=PKG, executable='listener'),\n"
        "    Node(package=OTHER, executable='talker'),\n"
        "    Node(package=REUSED, executable='helper'),\n"
        "]\n"
    )
    updated, started = rename_launch_nodes(text, {"talker": "renamed"}, "demo_pkg")
    assert "Node(package=PKG, executable='renamed', name='renamed')" in updated
    assert "Node(package=OTHER, executable='talker')" in updated
    assert started == {"renamed", "listener"}

def test_script_rename_skips_entries_it_cannot_read_before_the_target() -> None:
    from ros2inspector.design.source_edits import rename_script_entries

    source = (
        "from setuptools import setup\n"
        "EXTRA = 'tool = demo_pkg.tool:main'\n"
        "setup(entry_points={'console_scripts': [\n"
        "    EXTRA,\n"
        "    'not an entry',\n"
        "    'talker=demo_pkg.talker:main',\n"
        "]})\n"
    )
    updated = rename_script_entries(
        source, {"talker = demo_pkg.talker:main": "renamed = demo_pkg.renamed:main"}
    )
    assert "'renamed = demo_pkg.renamed:main'" in updated
    assert "EXTRA,\n    'not an entry'," in updated

_LABELS = ("alpha", "beta", "gamma", "delta")


@contextlib.contextmanager
def _serving(out: Path) -> Iterator[HTTPServer]:
    handler = _make_handler(
        build_html(None, session_token=TOKEN), out, Console(quiet=True), session_token=TOKEN
    )
    httpd = HTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(
        target=httpd.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
    )
    thread.start()
    try:
        yield httpd
    finally:
        httpd.shutdown()
        httpd.server_close()


@settings(max_examples=25, deadline=None)
@given(st.permutations(_LABELS).map(lambda labels: labels[:3]))
def test_any_rename_mapping_keeps_edited_setup_registered(renamed: list[str]) -> None:
    with tempfile.TemporaryDirectory() as tmp, _serving(Path(tmp)) as httpd:
        out = Path(tmp)
        before = [_node(label, f"n{i}") for i, label in enumerate(_LABELS[:3])]
        assert _post(httpd, "/generate", _state(_pkg(nodes=before)))[0] == 200
        setup = out / "demo_pkg/setup.py"
        setup.write_text(setup.read_text() + "# USER_EDIT_RETAINED\n")
        after = [_node(label, f"n{i}") for i, label in enumerate(renamed)]
        status, response = _post(httpd, "/generate", _state(_pkg(nodes=after)))
        assert status == 200, response
        registered = console_scripts(setup.read_text())
        assert registered is not None
        assert set(renamed) <= registered
        assert not (set(_LABELS[:3]) - set(renamed)) & registered


def test_deleting_a_node_still_started_by_edited_files_is_refused(server: Any) -> None:
    httpd, out = server
    two = _pkg(nodes=[_node("talker", "n1"), _node("listener", "n2")])
    assert _post(httpd, "/generate", _state(two))[0] == 200
    for edited in ("demo_pkg/setup.py", "demo_pkg/launch/launch.py"):
        path = out / edited
        path.write_text(path.read_text() + "# USER_EDIT_RETAINED\n")
    status, response = _post(httpd, "/generate", _state(_pkg(nodes=[_node("talker", "n1")])))
    assert status == 409, response
    assert "setup.py has your edits and still registers 'listener'" in response["error"]
    assert "starts 'listener', whose node was deleted" in response["error"]
    assert (out / "demo_pkg/demo_pkg/listener.py").is_file()


@pytest.mark.skipif(
    sys.platform == "win32" or os.geteuid() == 0, reason="needs POSIX permissions as non-root"
)
def test_package_rename_into_read_only_directory_is_refused_before_moving(server: Any) -> None:
    httpd, out = server
    assert _post(httpd, "/generate", _state(_pkg()))[0] == 200
    launch = out / "demo_pkg/launch/launch.py"
    launch.write_text(launch.read_text() + "# USER_EDIT_RETAINED\n")
    launch.parent.chmod(0o555)
    try:
        status, response = _post(httpd, "/generate", _state(_pkg(name="renamed_pkg")))
    finally:
        launch.parent.chmod(0o755)
    assert status == 400, response
    assert "demo_pkg/launch cannot be written" in response["error"]
    assert (out / "demo_pkg").is_dir() and not (out / "renamed_pkg").exists()
    assert "package='demo_pkg'" in launch.read_text()


def test_topic_rename_warns_about_an_edited_node_module(server: Any) -> None:
    httpd, out = server
    assert _post(httpd, "/generate", _state(_pkg()))[0] == 200
    module = out / "demo_pkg/demo_pkg/talker.py"
    module.write_text(module.read_text() + "# USER_EDIT_RETAINED\n")
    node = _node()
    node["publishers"][0]["name"] = "/news"
    status, response = _post(httpd, "/generate", _state(_pkg(nodes=[node])))
    assert status == 200, response
    expected = "talker.py has your edits and does not use '/news'"
    assert any(expected in warning for warning in response["warnings"])


def test_package_rename_warns_about_old_name_left_in_edited_files(server: Any) -> None:
    httpd, out = server
    assert _post(httpd, "/generate", _state(_pkg()))[0] == 200
    launch = out / "demo_pkg/launch/launch.py"
    launch.write_text(launch.read_text() + "SHARE = 'demo_pkg'\n")
    status, response = _post(httpd, "/generate", _state(_pkg(name="renamed_pkg")))
    assert status == 200, response
    assert any("still names the package 'demo_pkg'" in w for w in response["warnings"])


def test_rename_note_names_double_quoted_console_scripts(server: Any) -> None:
    httpd, out = server
    assert _post(httpd, "/generate", _state(_pkg()))[0] == 200
    setup = out / "demo_pkg/setup.py"
    setup.write_text(setup.read_text().replace("'", '"'))
    status, response = _post(httpd, "/generate", _state(_pkg(nodes=[_node("renamed", "n1")])))
    assert status == 200, response
    assert "demo_pkg: registered renamed in your edited setup.py" in response["updated_in"]


def test_regenerating_an_unchanged_design_touches_no_file(server: Any) -> None:
    httpd, out = server
    assert _post(httpd, "/generate", _state(_pkg()))[0] == 200
    files = [path for path in out.rglob("*") if path.is_file()]
    for path in files:
        os.utime(path, ns=(1_000_000_000, 1_000_000_000))
    assert _post(httpd, "/generate", _state(_pkg()))[0] == 200
    touched = [path.name for path in files if path.stat().st_mtime_ns != 1_000_000_000]
    assert touched == []
