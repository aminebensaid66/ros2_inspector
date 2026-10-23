"""Tests for the docs, doctor and watch commands (issues X1-X8, R1-R4, W1-W8)."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from ros2inspector.cli import cmd_watch
from ros2inspector.cli.app import app
from ros2inspector.cli.cmd_docs import page_names
from ros2inspector.model.schemas import PackageMetadata, PolicyViolation, ViolationSeverity

runner = CliRunner()
FIXTURE_WS = Path(__file__).parent.parent / "fixtures" / "workspaces" / "workspace_a"


@pytest.fixture
def ws(tmp_path: Path) -> Path:
    root = tmp_path / "ws"
    shutil.copytree(FIXTURE_WS, root)
    return root


def _add_package(root: Path, folder: str, name: str, description: str = "d") -> Path:
    pkg = root / "src" / folder
    pkg.mkdir(parents=True)
    (pkg / "package.xml").write_text(
        f"""<?xml version="1.0"?>
<package format="3"><name>{name}</name><version>0.1.0</version>
<description>{description}</description>
<maintainer email="a@b.c">x</maintainer><license>MIT</license></package>
""",
        encoding="utf-8",
    )
    return pkg


def _docs(root: Path, *extra: str) -> Any:
    result = runner.invoke(app, ["-q", "docs", str(root), "--no-cache", *extra])
    assert result.exit_code == 0, result.output
    return result


# ── docs ────────────────────────────────────────────────────────────────────


def test_docs_markdown_golden_content(ws: Path) -> None:
    _docs(ws)
    out = ws / "ros2inspector_docs"
    index = (out / "index.md").read_text()
    assert "| [pkg\\_a](packages/pkg_a.md) |" in index
    assert "```mermaid" in index
    page = (out / "packages" / "pkg_a.md").read_text()
    assert "`/chatter` (`std_msgs/String`)" in page
    assert "`src/pkg_a/pkg_a/talker.py`" in page
    assert str(ws) not in page


def test_docs_html_shows_names_not_object_reprs(ws: Path) -> None:
    _docs(ws, "--format", "html")
    page = (ws / "ros2inspector_docs" / "packages" / "pkg_a.html").read_text()
    assert "<code>/chatter</code> (std_msgs/String)" in page
    assert "msg_type=" not in page
    assert "evidence=" not in page
    assert str(ws) not in page


def test_docs_package_name_cannot_escape_output_dir(ws: Path) -> None:
    _add_package(ws, "evil", "../../escaped")
    _docs(ws)
    assert not list(ws.parent.glob("escaped*"))
    assert not list(ws.glob("escaped*"))
    pages = sorted(p.name for p in (ws / "ros2inspector_docs" / "packages").iterdir())
    assert "______escaped.md" in pages


def test_page_names_are_unique_and_safe() -> None:
    packages = [
        PackageMetadata(name="dup", path="/a/dup"),
        PackageMetadata(name="dup", path="/b/dup"),
        PackageMetadata(name="Dup", path="/c/dup"),
        PackageMetadata(name="a/b", path="/d"),
        PackageMetadata(name="..", path="/e"),
    ]
    pages = page_names(packages)
    assert len(set(pages.values())) == len(packages)
    assert all("/" not in name and name not in {".", ".."} for name in pages.values())


def test_docs_markdown_has_action_sections(ws: Path) -> None:
    node = ws / "src" / "pkg_a" / "pkg_a" / "fib.py"
    node.write_text(
        """from rclpy.node import Node
from rclpy.action import ActionServer, ActionClient
from example_interfaces.action import Fibonacci


class FibNode(Node):
    def __init__(self):
        super().__init__("fib")
        ActionServer(self, Fibonacci, "fibonacci", self.cb)
        ActionClient(self, Fibonacci, "other_fib")
""",
        encoding="utf-8",
    )
    _docs(ws)
    page = (ws / "ros2inspector_docs" / "packages" / "pkg_a.md").read_text()
    assert "**Action Servers:**" in page
    assert "**Action Clients:**" in page


def test_docs_escapes_markdown_tables_and_html(ws: Path) -> None:
    _add_package(ws, "weird", "weird_pkg", "a | b &lt;script&gt;x&lt;/script&gt;")
    _docs(ws)
    page = (ws / "ros2inspector_docs" / "packages" / "weird_pkg.md").read_text()
    assert "<script>" not in page
    assert "&lt;script&gt;" in page


def test_docs_removes_stale_pages_and_keeps_foreign_files(ws: Path) -> None:
    extra = _add_package(ws, "temp", "temp_pkg")
    _docs(ws)
    pkg_dir = ws / "ros2inspector_docs" / "packages"
    assert (pkg_dir / "temp_pkg.md").is_file()
    (pkg_dir / "notes.md").write_text("mine")
    shutil.rmtree(extra)
    _docs(ws)
    assert not (pkg_dir / "temp_pkg.md").exists()
    assert (pkg_dir / "notes.md").read_text() == "mine"


def test_docs_health_none_is_a_dash(ws: Path) -> None:
    from ros2inspector.cli.cmd_docs import _health, _health_class

    assert _health(None) == "—"
    assert _health_class(None) == "health-none"


def test_docs_default_output_is_in_workspace(ws: Path, tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.chdir(tmp_path)
    _docs(ws)
    assert (ws / "ros2inspector_docs" / "index.md").is_file()
    assert not (tmp_path / "ros2inspector_docs").exists()


def test_docs_rejects_unknown_format(ws: Path) -> None:
    result = runner.invoke(app, ["docs", str(ws), "--format", "pdf"])
    assert result.exit_code == 2


def test_docs_accepts_dash_c(ws: Path) -> None:
    result = runner.invoke(app, ["-q", "docs", "-C", str(ws), "--no-cache"])
    assert result.exit_code == 0, result.output


# ── doctor ──────────────────────────────────────────────────────────────────


def _doctor(*args: str) -> tuple[int, dict[str, Any]]:
    result = runner.invoke(app, ["doctor", *args, "--json"])
    return result.exit_code, json.loads(result.stdout)


def _checks(data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {check["check"]: check for check in data["checks"]}


def test_doctor_missing_path_fails_without_fake_roots(tmp_path: Path) -> None:
    code, data = _doctor(str(tmp_path / "missing"))
    assert code == 1
    assert _checks(data)["workspace_exists"]["status"] == "fail"
    assert data["workspace_root"] is None
    assert data["source_root"] is None


def test_doctor_empty_workspace_fails(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    code, data = _doctor(str(tmp_path))
    assert code == 1
    assert _checks(data)["packages"]["status"] == "fail"


def test_doctor_happy_path_and_text_output(ws: Path) -> None:
    code, data = _doctor(str(ws))
    assert code == 0
    assert _checks(data)["packages"]["value"] == 3
    text = runner.invoke(app, ["doctor", "-C", str(ws)])
    assert text.exit_code == 0
    assert "Diagnostics" in text.output


def test_doctor_flags_duplicates_and_broken_xml(ws: Path) -> None:
    _add_package(ws, "copy_a", "pkg_a")
    (ws / "src" / "broken").mkdir()
    (ws / "src" / "broken" / "package.xml").write_text("<package><name>x")
    code, data = _doctor(str(ws))
    checks = _checks(data)
    assert code == 1
    assert "pkg_a" in checks["duplicate_package_names"]["value"]
    assert checks["unparsable_package_xml"]["status"] == "warn"


def test_doctor_ros_distro_is_warning_only(ws: Path, monkeypatch: Any) -> None:
    monkeypatch.delenv("ROS_DISTRO", raising=False)
    code, data = _doctor(str(ws))
    assert code == 0
    assert _checks(data)["ros_distro"]["status"] == "warn"


def test_doctor_does_not_create_cache_dir(ws: Path, tmp_path: Path, monkeypatch: Any) -> None:
    cache = tmp_path / "nocache" / "ros2inspector"
    monkeypatch.setattr("ros2inspector.cli.cmd_doctor._DEFAULT_CACHE_DIR", cache)
    _doctor(str(ws))
    assert not cache.exists()


def test_doctor_missing_asset_fails(ws: Path, monkeypatch: Any) -> None:
    class Missing:
        def joinpath(self, _: str) -> Missing:
            return self

        def is_file(self) -> bool:
            return False

    monkeypatch.setattr("ros2inspector.cli.cmd_doctor.files", lambda _: Missing())
    code, data = _doctor(str(ws))
    assert code == 1
    assert _checks(data)["offline_visualization_asset"]["status"] == "fail"


# ── watch ───────────────────────────────────────────────────────────────────


def test_snapshot_watches_all_inputs_and_prunes_excluded(ws: Path) -> None:
    pkg = ws / "src" / "pkg_a"
    (pkg / "msg").mkdir(exist_ok=True)
    (pkg / "msg" / "New.msg").write_text("int32 x\n")
    (pkg / "CMakeLists.txt").write_text("project(x)\n")
    (pkg / "config.yml").write_text("a: 1\n")
    (pkg / "build").mkdir()
    (pkg / "build" / "junk.py").write_text("")
    (pkg / ".git").mkdir()
    (pkg / ".git" / "x.py").write_text("")
    ignored = ws / "src" / "ignored"
    ignored.mkdir()
    (ignored / "COLCON_IGNORE").write_text("")
    (ignored / "skip.py").write_text("")
    snap = cmd_watch._snapshot(ws)
    assert "pkg_a/msg/New.msg" in snap
    assert "pkg_a/CMakeLists.txt" in snap
    assert "pkg_a/config.yml" in snap
    assert "ignored/COLCON_IGNORE" in snap
    assert not any("build/" in key or ".git/" in key or "skip.py" in key for key in snap)


def test_snapshot_does_not_walk_excluded_trees(ws: Path, monkeypatch: Any) -> None:
    (ws / "src" / "pkg_a" / "build" / "deep").mkdir(parents=True)
    visited: list[str] = []
    real_walk = cmd_watch.os.walk

    def spy(top: Any, *args: Any, **kwargs: Any) -> Any:
        for item in real_walk(top, *args, **kwargs):
            visited.append(item[0])
            yield item

    monkeypatch.setattr(cmd_watch.os, "walk", spy)
    cmd_watch._snapshot(ws)
    assert not any("build" in Path(path).parts for path in visited)


def test_changed_files_add_modify_delete() -> None:
    old = {"a": (1, 1, 1, 1), "b": (1, 1, 1, 1)}
    new = {"a": (2, 2, 1, 1), "c": (1, 1, 1, 1)}
    assert cmd_watch._changed_files(old, new) == ["a", "b", "c"]


@pytest.mark.parametrize("value", ["0", "-1", "0.1"])
def test_watch_rejects_bad_interval(ws: Path, value: str) -> None:
    result = runner.invoke(app, ["watch", str(ws), "--audit", "--interval", value])
    assert result.exit_code == 2


def test_watch_default_policy_is_relative_to_workspace(
    ws: Path, tmp_path: Path, monkeypatch: Any
) -> None:
    (ws / "ros2inspector_policy.yaml").write_text("version: 1\nrules: []\n")
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["watch", str(ws), "--once", "--no-cache"])
    assert result.exit_code == 0, result.output


def test_watch_once_exit_codes(ws: Path) -> None:
    clean = runner.invoke(app, ["watch", str(ws), "--audit", "--once", "--no-cache"])
    assert clean.exit_code == 0, clean.output
    strict = runner.invoke(
        app, ["watch", str(ws), "--audit", "--once", "--no-cache", "--fail-on", "info"]
    )
    assert strict.exit_code in {0, 1}
    bad = runner.invoke(app, ["watch", str(ws), "--audit", "--once", "--fail-on", "nope"])
    assert bad.exit_code == 2


def _violation(message: str) -> PolicyViolation:
    return PolicyViolation(
        severity=ViolationSeverity.WARNING,
        rule_type="node_isolation",
        message=message,
        policy_file="audit",
    )


def test_watch_loop_reports_new_and_resolved(ws: Path, monkeypatch: Any) -> None:
    runs = iter(
        [
            [_violation("old finding")],
            [_violation("new finding")],
        ]
    )
    monkeypatch.setattr(cmd_watch, "run_policy", lambda *_: next(runs))
    monkeypatch.setattr(cmd_watch, "_DEBOUNCE_SECONDS", 0)
    sleeps = {"n": 0}

    def fake_sleep(_: float) -> None:
        sleeps["n"] += 1
        if sleeps["n"] == 1:
            (ws / "src" / "pkg_a" / "pkg_a" / "talker.py").write_text("# changed\n")
        elif sleeps["n"] > 3:
            raise KeyboardInterrupt

    monkeypatch.setattr(cmd_watch.time, "sleep", fake_sleep)
    result = runner.invoke(app, ["watch", str(ws), "--audit", "--no-cache", "--fail-on", "warning"])
    assert "+ new" in result.output
    assert "new finding" in result.output
    assert "resolved" in result.output
    assert "old finding" in result.output
    assert result.exit_code == 1


def test_watch_reloads_policy_on_change(ws: Path, monkeypatch: Any) -> None:
    policy = ws / "ros2inspector_policy.yaml"
    policy.write_text("version: 1\nrules: []\n")
    monkeypatch.setattr(cmd_watch, "_DEBOUNCE_SECONDS", 0)
    sleeps = {"n": 0}

    def fake_sleep(_: float) -> None:
        sleeps["n"] += 1
        if sleeps["n"] == 1:
            policy.write_text("version: 1\nrules:\n  - type: node_isolation\n")
        elif sleeps["n"] > 3:
            raise KeyboardInterrupt

    monkeypatch.setattr(cmd_watch.time, "sleep", fake_sleep)
    result = runner.invoke(app, ["watch", str(ws), "--no-cache"])
    assert "Policy reloaded." in result.output


def test_watch_analysis_error_exit_code(ws: Path, monkeypatch: Any) -> None:
    def boom(*_: Any, **__: Any) -> Any:
        raise RuntimeError("parser exploded")

    monkeypatch.setattr(cmd_watch, "build_uam", boom)
    result = runner.invoke(app, ["watch", str(ws), "--audit", "--once", "--verbose"])
    assert result.exit_code == 3
    assert "RuntimeError: parser exploded" in result.output
    assert "Traceback" in result.output
