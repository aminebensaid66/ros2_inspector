"""Edge cases for docs, doctor, watch, the shared CLI plumbing and the analysis cache."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from ros2inspector.cache.analysis_cache import AnalysisCache
from ros2inspector.cli import cmd_docs, cmd_doctor, cmd_watch
from ros2inspector.cli.app import app
from ros2inspector.discovery import WorkspaceAccessError
from ros2inspector.model.uam import UAM

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


def _docs(*args: str) -> Any:
    return runner.invoke(app, ["-q", "docs", "--no-cache", *args])


# ── CLI registration ────────────────────────────────────────────────────────


def test_root_help_lists_every_command() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for command in ("scan", "audit", "docs", "doctor", "watch", "design"):
        assert command in result.output


@pytest.mark.parametrize("command", ["docs", "doctor", "watch", "design"])
def test_new_command_help_has_no_phantom_subcommand(command: str) -> None:
    result = runner.invoke(app, [command, "--help"])
    assert result.exit_code == 0
    assert "COMMAND [ARGS]" not in result.output


@pytest.mark.parametrize("command", ["docs", "doctor", "watch"])
def test_conflicting_positional_and_dash_c_paths(command: str, ws: Path, tmp_path: Path) -> None:
    extra = ["--audit", "--once"] if command == "watch" else []
    result = runner.invoke(app, [command, str(ws), "-C", str(tmp_path), *extra])
    assert result.exit_code == 2
    assert "two different workspace paths" in result.output


@pytest.mark.parametrize("command", ["docs", "doctor"])
def test_same_path_given_twice_is_fine(command: str, ws: Path) -> None:
    args = [command, str(ws), "-C", str(ws / "." / "src" / "..")]
    if command == "docs":
        args.insert(1, "--no-cache")
    assert runner.invoke(app, args).exit_code == 0


def test_global_analysis_options_reach_watch_and_docs(
    ws: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[dict[str, Any]] = []
    real_build = UAM.build

    def spy(root: Path, **kwargs: Any) -> Any:
        seen.append(kwargs)
        return real_build(root, **kwargs)

    monkeypatch.setattr(UAM, "build", spy)
    assert runner.invoke(
        app, ["-q", "--preset", "nav2", "--include-tests", "watch", str(ws), "--audit", "--once"]
    ).exit_code in {0, 1}
    assert runner.invoke(app, ["-q", "--preset", "nav2", "docs", str(ws)]).exit_code == 0
    assert len(seen) == 2
    assert all(call["preset"] == "nav2" for call in seen)
    assert seen[0]["include_tests"] is True


# ── docs ────────────────────────────────────────────────────────────────────


def test_docs_refuses_foreign_non_empty_directory(ws: Path, tmp_path: Path) -> None:
    site = tmp_path / "site"
    site.mkdir()
    (site / "index.md").write_text("hand written")
    result = _docs(str(ws), "-o", str(site))
    assert result.exit_code == 2
    assert "--force" in result.output
    assert (site / "index.md").read_text() == "hand written"


def test_docs_force_writes_into_foreign_directory(ws: Path, tmp_path: Path) -> None:
    site = tmp_path / "site"
    site.mkdir()
    (site / "README.md").write_text("keep me")
    assert _docs(str(ws), "-o", str(site), "--force").exit_code == 0
    assert (site / "index.md").is_file()
    assert (site / "README.md").read_text() == "keep me"


def test_docs_accepts_empty_existing_directory(ws: Path, tmp_path: Path) -> None:
    (tmp_path / "empty").mkdir()
    assert _docs(str(ws), "-o", str(tmp_path / "empty")).exit_code == 0


def test_docs_rejects_output_that_is_a_file(ws: Path, tmp_path: Path) -> None:
    target = tmp_path / "out.md"
    target.write_text("x")
    result = _docs(str(ws), "-o", str(target))
    assert result.exit_code == 2
    assert "not a directory" in result.output


def test_docs_markdown_and_html_share_a_directory(ws: Path) -> None:
    assert _docs(str(ws)).exit_code == 0
    assert _docs(str(ws), "-f", "html").exit_code == 0
    assert _docs(str(ws)).exit_code == 0
    out = ws / "ros2inspector_docs"
    assert (out / "index.md").is_file() and (out / "index.html").is_file()
    assert (out / "packages" / "pkg_a.md").is_file()
    assert (out / "packages" / "pkg_a.html").is_file()
    manifest = json.loads((out / ".ros2inspector_docs.json").read_text())["files"]
    assert "index.html" in manifest and "index.md" in manifest


def test_docs_stale_html_is_removed_only_by_an_html_run(ws: Path) -> None:
    _add_package(ws, "temp", "temp_pkg")
    assert _docs(str(ws), "-f", "html").exit_code == 0
    shutil.rmtree(ws / "src" / "temp")
    assert _docs(str(ws)).exit_code == 0
    out = ws / "ros2inspector_docs" / "packages"
    assert (out / "temp_pkg.html").is_file()  # a Markdown run never deletes HTML pages
    assert _docs(str(ws), "-f", "html").exit_code == 0
    assert not (out / "temp_pkg.html").exists()


def test_docs_tampered_manifest_cannot_delete_other_files(ws: Path, tmp_path: Path) -> None:
    out = ws / "ros2inspector_docs"
    assert _docs(str(ws)).exit_code == 0
    outside = tmp_path / "outside.md"
    outside.write_text("x")
    (out / "notes.md").write_text("mine")
    (out / "packages" / "custom.txt").write_text("mine")
    (out / ".ros2inspector_docs.json").write_text(
        json.dumps(
            {
                "files": [
                    "../../outside.md",
                    str(outside),
                    "notes.md",
                    "packages/custom.txt",
                    "packages/../notes.md",
                    "packages/sub/../../notes.md",
                ]
            }
        )
    )
    assert _docs(str(ws)).exit_code == 0
    assert outside.read_text() == "x"
    assert (out / "notes.md").read_text() == "mine"
    assert (out / "packages" / "custom.txt").read_text() == "mine"


@pytest.mark.parametrize("content", ["not json", "[]", '{"files": "index.md"}', '{"files": [1]}'])
def test_docs_survives_corrupt_manifest(ws: Path, content: str) -> None:
    out = ws / "ros2inspector_docs"
    out.mkdir()
    (out / ".ros2inspector_docs.json").write_text(content)
    assert _docs(str(ws)).exit_code == 0
    assert json.loads((out / ".ros2inspector_docs.json").read_text())["files"]


def test_docs_are_reproducible_with_source_date_epoch(
    ws: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "0")
    monkeypatch.delenv("ROS_DISTRO", raising=False)
    out = ws / "ros2inspector_docs"
    assert _docs(str(ws), "-f", "html").exit_code == 0
    first = {p.name: p.read_bytes() for p in out.rglob("*.html")}
    assert b"1970-01-01" in first["index.html"]
    assert _docs(str(ws), "-f", "html").exit_code == 0
    assert first == {p.name: p.read_bytes() for p in out.rglob("*.html")}


def test_docs_html_escapes_package_text(ws: Path) -> None:
    _add_package(
        ws, "xss", "xss_pkg", description="&lt;script&gt;alert(1)&lt;/script&gt; &amp; more"
    )
    assert _docs(str(ws), "-f", "html").exit_code == 0
    page = (ws / "ros2inspector_docs" / "packages" / "xss_pkg.html").read_text()
    assert "<script>" not in page
    assert "&lt;script&gt;" in page


def test_docs_markdown_escapes_package_text(ws: Path) -> None:
    _add_package(ws, "md", "md_pkg", description="a | b &lt;img src=x onerror=1&gt; *bold*")
    assert _docs(str(ws)).exit_code == 0
    page = (ws / "ros2inspector_docs" / "packages" / "md_pkg.md").read_text()
    assert "<img" not in page
    assert "&lt;img" in page


def test_docs_unicode_text_round_trips(ws: Path) -> None:
    _add_package(ws, "uni", "uni_pkg", description="Robôt ✓ 日本語")
    assert _docs(str(ws), "-f", "html").exit_code == 0
    page = (ws / "ros2inspector_docs" / "packages" / "uni_pkg.html").read_text(encoding="utf-8")
    assert "Robôt ✓ 日本語" in page


def test_docs_never_leak_absolute_workspace_paths(ws: Path) -> None:
    assert _docs(str(ws)).exit_code == 0
    assert _docs(str(ws), "-f", "html").exit_code == 0
    for page in (ws / "ros2inspector_docs").rglob("*.*"):
        if page.suffix in {".md", ".html"}:
            assert str(ws) not in page.read_text(), page


def test_docs_skip_huge_diagrams(ws: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cmd_docs, "_MAX_DIAGRAM_NODES", 0)
    assert _docs(str(ws)).exit_code == 0
    assert "```mermaid" not in (ws / "ros2inspector_docs" / "index.md").read_text()


def test_docs_mermaid_blocks_are_closed(ws: Path) -> None:
    assert _docs(str(ws)).exit_code == 0
    for page in (ws / "ros2inspector_docs").rglob("*.md"):
        text = page.read_text()
        assert text.count("```") % 2 == 0, page


def test_docs_empty_workspace_exits_3(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    assert _docs(str(tmp_path)).exit_code == 3


def test_docs_duplicate_packages_exit_3(ws: Path) -> None:
    _add_package(ws, "copy", "pkg_a")
    assert _docs(str(ws)).exit_code == 3


def test_docs_output_relative_to_cwd_when_given(
    ws: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    assert _docs(str(ws), "-o", "rel_docs").exit_code == 0
    assert (tmp_path / "rel_docs" / "index.md").is_file()


def test_page_names_handle_case_collisions_and_odd_names() -> None:
    from ros2inspector.model.schemas import PackageMetadata

    packages = [
        PackageMetadata(name=name, path=f"/ws/src/{i}")
        for i, name in enumerate(["Foo", "foo", "a/b", "..", "", "x" * 300, "con"])
    ]
    pages = cmd_docs.page_names(packages)
    names = list(pages.values())
    assert len({n.lower() for n in names}) == len(names)
    assert all("/" not in n and n not in {".", ".."} and n for n in names)


# ── doctor ──────────────────────────────────────────────────────────────────


def _doctor(*args: str) -> tuple[int, dict[str, Any]]:
    result = runner.invoke(app, ["doctor", *args, "--json"])
    return result.exit_code, json.loads(result.stdout)


def _checks(data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {check["check"]: check for check in data["checks"]}


def test_doctor_json_shape_is_consistent(ws: Path) -> None:
    code, data = _doctor(str(ws))
    assert code == 0
    for key in ("ros2inspector_version", "python", "platform", "checks", "cache"):
        assert key in data
    for check in data["checks"]:
        assert check["status"] in {"ok", "warn", "fail"}
        assert check["ok"] is (check["status"] != "fail")
        assert isinstance(check["hint"], str)


def test_doctor_path_that_is_a_file_fails(tmp_path: Path) -> None:
    target = tmp_path / "file"
    target.write_text("x")
    code, data = _doctor(str(target))
    assert code == 1
    assert _checks(data)["workspace_exists"]["status"] == "fail"


def test_doctor_unreadable_source_tree_is_reported(
    ws: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def denied(root: Path, **_: Any) -> list[Path]:
        raise WorkspaceAccessError(root)

    monkeypatch.setattr(cmd_doctor, "find_package_xml_files", denied)
    code, data = _doctor(str(ws))
    assert code == 1
    assert _checks(data)["workspace_readable"]["status"] == "fail"


def test_doctor_missing_runtime_module_fails(ws: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    real = cmd_doctor.importlib.util.find_spec
    monkeypatch.setattr(
        cmd_doctor.importlib.util,
        "find_spec",
        lambda name, *a: None if name == "jinja2" else real(name, *a),
    )
    code, data = _doctor(str(ws))
    assert code == 1
    assert _checks(data)["module_jinja2"]["status"] == "fail"


def test_doctor_unwritable_cache_is_only_a_warning(
    ws: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = tmp_path / "readonly-cache"
    cache.mkdir()
    real_access = os.access
    monkeypatch.setattr(cmd_doctor, "_DEFAULT_CACHE_DIR", cache)
    monkeypatch.setattr(
        cmd_doctor.os,
        "access",
        lambda path, mode, **kw: False if Path(path) == cache else real_access(path, mode, **kw),
    )
    code, data = _doctor(str(ws))
    assert code == 0
    assert _checks(data)["cache_writable"]["status"] == "warn"


def test_doctor_reports_existing_cache_size(
    ws: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "blob").write_bytes(b"x" * 1234)
    monkeypatch.setattr(cmd_doctor, "_DEFAULT_CACHE_DIR", cache)
    _, data = _doctor(str(ws))
    assert data["cache"]["size_bytes"] == 1234
    assert data["cache"]["exists"] is True


def test_doctor_from_inside_a_package_finds_the_workspace(ws: Path) -> None:
    _, data = _doctor(str(ws / "src" / "pkg_a" / "pkg_a"))
    assert data["workspace_root"] == str(ws.resolve())


def test_doctor_text_output_shows_failures(tmp_path: Path) -> None:
    result = runner.invoke(app, ["doctor", str(tmp_path / "missing")])
    assert result.exit_code == 1
    assert "FAIL" in result.output


def test_doctor_expands_user(monkeypatch: pytest.MonkeyPatch, ws: Path) -> None:
    monkeypatch.setenv("HOME", str(ws.parent))
    code, data = _doctor("~/ws")
    assert code == 0
    assert data["workspace_root"] == str(ws.resolve())


# ── watch ───────────────────────────────────────────────────────────────────


def test_watch_missing_policy_exits_3(ws: Path) -> None:
    result = runner.invoke(app, ["watch", str(ws), "--once"])
    assert result.exit_code == 3
    assert "--audit" in result.output


def test_watch_invalid_policy_exits_2(ws: Path) -> None:
    (ws / "ros2inspector_policy.yaml").write_text("rules: [unclosed\n")
    assert runner.invoke(app, ["watch", str(ws), "--once"]).exit_code == 2


def test_watch_explicit_relative_policy_is_relative_to_cwd(
    ws: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "p.yaml").write_text("version: 1\nrules: []\n")
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["watch", str(ws), "--policy", "p.yaml", "--once"])
    assert result.exit_code == 0, result.output


def test_watch_empty_workspace_exits_3(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    assert runner.invoke(app, ["watch", str(tmp_path), "--audit", "--once"]).exit_code == 3


def test_watch_once_fail_on_levels(ws: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from ros2inspector.model.schemas import PolicyViolation, ViolationSeverity

    info = PolicyViolation(
        severity=ViolationSeverity.INFO, rule_type="x", message="m", policy_file="audit"
    )
    monkeypatch.setattr(cmd_watch, "run_policy", lambda *_: [info])
    base = ["watch", str(ws), "--audit", "--once"]
    assert runner.invoke(app, base).exit_code == 0
    assert runner.invoke(app, [*base, "--fail-on", "warning"]).exit_code == 0
    assert runner.invoke(app, [*base, "--fail-on", "info"]).exit_code == 1


def test_watch_ctrl_c_before_first_run_exits_130(ws: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def interrupted(*_: Any, **__: Any) -> Any:
        raise KeyboardInterrupt

    monkeypatch.setattr(cmd_watch, "_run_once", interrupted)
    result = runner.invoke(app, ["watch", str(ws), "--audit"])
    assert result.exit_code == 130
    assert "Stopped" in result.output


def test_watch_keeps_previous_rules_when_policy_breaks(
    ws: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    policy = ws / "ros2inspector_policy.yaml"
    policy.write_text("version: 1\nrules: []\n")
    monkeypatch.setattr(cmd_watch, "_DEBOUNCE_SECONDS", 0)
    ticks = {"n": 0}

    def fake_sleep(_: float) -> None:
        ticks["n"] += 1
        if ticks["n"] == 1:
            policy.write_text("version: 1\nrules:\n  - type: no_such_rule\n")
        elif ticks["n"] > 3:
            raise KeyboardInterrupt

    monkeypatch.setattr(cmd_watch.time, "sleep", fake_sleep)
    result = runner.invoke(app, ["watch", str(ws), "--no-cache"])
    assert "keeping previous rules" in result.output
    assert "Policy remains invalid" in result.output
    assert result.exit_code == 2


def test_watch_lists_at_most_five_changed_files(ws: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cmd_watch, "_DEBOUNCE_SECONDS", 0)
    ticks = {"n": 0}

    def fake_sleep(_: float) -> None:
        ticks["n"] += 1
        if ticks["n"] == 1:
            for i in range(8):
                (ws / "src" / "pkg_a" / f"extra_{i}.py").write_text("x = 1\n")
        elif ticks["n"] > 3:
            raise KeyboardInterrupt

    monkeypatch.setattr(cmd_watch.time, "sleep", fake_sleep)
    result = runner.invoke(app, ["watch", str(ws), "--audit", "--no-cache"])
    assert "8 file(s) changed" in result.output
    assert "and 3 more" in result.output


def test_watch_ignores_changes_in_build_and_hidden_dirs(
    ws: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runs = {"n": 0}
    real = cmd_watch._run_once

    def counting(*args: Any, **kwargs: Any) -> Any:
        runs["n"] += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(cmd_watch, "_run_once", counting)
    ticks = {"n": 0}

    def fake_sleep(_: float) -> None:
        ticks["n"] += 1
        if ticks["n"] == 1:
            for folder in ("build", "install", "log", ".hidden", "__pycache__"):
                target = ws / "src" / "pkg_a" / folder
                target.mkdir(exist_ok=True)
                (target / "x.py").write_text("x")
            (ws / "src" / "pkg_a" / "notes.txt").write_text("not an input")
        elif ticks["n"] > 2:
            raise KeyboardInterrupt

    monkeypatch.setattr(cmd_watch.time, "sleep", fake_sleep)
    runner.invoke(app, ["watch", str(ws), "--audit", "--no-cache"])
    assert runs["n"] == 1  # only the initial run


def test_snapshot_notices_colcon_ignore_removal(ws: Path) -> None:
    ignored = ws / "src" / "ignored"
    ignored.mkdir()
    marker = ignored / "COLCON_IGNORE"
    marker.write_text("")
    (ignored / "a.py").write_text("")
    before = cmd_watch._snapshot(ws)
    marker.unlink()
    after = cmd_watch._snapshot(ws)
    changed = cmd_watch._changed_files(before, after)
    assert "ignored/COLCON_IGNORE" in changed
    assert "ignored/a.py" in changed


def test_snapshot_survives_vanishing_files(ws: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    real_stat = Path.stat

    def flaky(self: Path, *args: Any, **kwargs: Any) -> os.stat_result:
        if self.name == "talker.py":
            raise FileNotFoundError(self)
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", flaky)
    snap = cmd_watch._snapshot(ws)
    assert not any(key.endswith("talker.py") for key in snap)


def test_wait_until_stable_waits_for_writes_to_settle(monkeypatch: pytest.MonkeyPatch) -> None:
    snaps = iter([{"a": (1, 1, 1, 1)}, {"a": (2, 2, 2, 2)}, {"a": (2, 2, 2, 2)}])
    monkeypatch.setattr(cmd_watch, "_snapshot", lambda *_: next(snaps))
    monkeypatch.setattr(cmd_watch.time, "sleep", lambda _: None)
    result = cmd_watch._wait_until_stable(Path("."), (), {"a": (0, 0, 0, 0)})
    assert result == {"a": (2, 2, 2, 2)}


def test_violation_fingerprint_ignores_entity_order() -> None:
    from ros2inspector.model.schemas import PolicyViolation, ViolationSeverity

    def make(entities: list[str]) -> PolicyViolation:
        return PolicyViolation(
            severity=ViolationSeverity.WARNING,
            rule_type="r",
            message="m",
            policy_file="p",
            affected_entities=entities,
        )

    assert cmd_watch._fingerprint(make(["a", "b"])) == cmd_watch._fingerprint(make(["b", "a"]))


# ── analysis cache and workspace location ───────────────────────────────────


def _file_paths(uam: Any) -> set[str]:
    return {node.file_path for node in uam.nodes() if node.file_path}


def test_moved_workspace_reports_new_paths_with_cache(tmp_path: Path) -> None:
    cache_dir = tmp_path / "cache"
    first = tmp_path / "first"
    shutil.copytree(FIXTURE_WS, first)
    before = UAM.build(first, use_cache=True, cache_dir=cache_dir)
    assert all(path.startswith(str(first)) for path in _file_paths(before))

    second = tmp_path / "second"
    first.rename(second)
    after = UAM.build(second, use_cache=True, cache_dir=cache_dir)
    paths = _file_paths(after)
    assert paths and all(path.startswith(str(second)) for path in paths)
    assert not any(Path(path).exists() is False for path in paths)


def test_two_copies_of_a_workspace_keep_their_own_paths(tmp_path: Path) -> None:
    cache_dir = tmp_path / "cache"
    a, b = tmp_path / "a", tmp_path / "b"
    shutil.copytree(FIXTURE_WS, a)
    shutil.copytree(FIXTURE_WS, b)
    for root in (a, b, a, b):
        paths = _file_paths(UAM.build(root, use_cache=True, cache_dir=cache_dir))
        assert paths and all(path.startswith(str(root)) for path in paths)


def test_same_content_in_two_locations_uses_two_cache_keys(tmp_path: Path) -> None:
    a, b = tmp_path / "a" / "pkg", tmp_path / "b" / "pkg"
    for root in (a, b):
        root.mkdir(parents=True)
        (root / "package.xml").write_text("<package><name>pkg</name></package>")
    with AnalysisCache(tmp_path / "cache") as cache:
        assert cache._fingerprint(a) != cache._fingerprint(b)
        cache.set(a, [], [])
        assert cache.get(a) == ([], [])
        assert cache.get(b) is None


def test_cache_invalidates_on_content_change_with_same_size_and_mtime(tmp_path: Path) -> None:
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    source = pkg / "node.py"
    source.write_text("a = 1\n")
    stat = source.stat()
    with AnalysisCache(tmp_path / "cache") as cache:
        before = cache._fingerprint(pkg)
        source.write_text("b = 2\n")
        os.utime(source, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        cache.set_context("")  # drop the one-slot memo, as a new run would
        assert cache._fingerprint(pkg) != before


def test_docs_show_ros_node_names_and_class(ws: Path) -> None:
    assert _docs(str(ws)).exit_code == 0
    assert _docs(str(ws), "-f", "html").exit_code == 0
    out = ws / "ros2inspector_docs"
    md = (out / "packages" / "pkg_a.md").read_text()
    assert "### `talker`" in md and "**Class:** `TalkerNode`" in md
    assert "| talker | pkg\\_a |" in (out / "index.md").read_text()
    html = (out / "packages" / "pkg_a.html").read_text()
    assert "<h3><code>talker</code></h3>" in html and "<code>TalkerNode</code>" in html


def test_snapshot_watches_files_that_change_health(ws: Path) -> None:
    pkg = ws / "src" / "pkg_a"
    before = cmd_watch._snapshot(ws)
    (pkg / "README.md").write_text("# pkg_a\n")
    (pkg / "test").mkdir(exist_ok=True)
    (pkg / "test" / "fixture.json").write_text("{}\n")
    (pkg / "notes.md").write_text("not an analysis input\n")
    changed = cmd_watch._changed_files(before, cmd_watch._snapshot(ws))
    assert sorted(changed) == ["pkg_a/README.md", "pkg_a/test/fixture.json"]


def test_watch_recovers_exit_status_when_policy_is_fixed(
    ws: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    policy = ws / "ros2inspector_policy.yaml"
    policy.write_text("version: 1\nrules: []\n")
    monkeypatch.setattr(cmd_watch, "_DEBOUNCE_SECONDS", 0)
    ticks = {"n": 0}

    def fake_sleep(_: float) -> None:
        ticks["n"] += 1
        if ticks["n"] == 1:
            policy.write_text("version: 1\nrules:\n  - type: invalid_rule\n")
        elif ticks["n"] == 3:
            policy.write_text("version: 1\nrules: []\n# corrected\n")
        elif ticks["n"] > 5:
            raise KeyboardInterrupt

    monkeypatch.setattr(cmd_watch.time, "sleep", fake_sleep)
    result = runner.invoke(app, ["watch", str(ws), "--no-cache"])
    assert "keeping previous rules" in result.output
    assert "Policy reloaded." in result.output
    assert result.exit_code == 0, result.output

def test_snapshot_tracks_health_relevant_empty_launch_directories(ws: Path) -> None:
    package = ws / "src" / "pkg_a"
    before = cmd_watch._snapshot(ws)
    (package / "launch").mkdir()
    after = cmd_watch._snapshot(ws)
    assert "pkg_a/launch/" in cmd_watch._changed_files(before, after)

    (package / "bringup" / "launch").mkdir(parents=True)
    nested = cmd_watch._snapshot(ws)
    assert "pkg_a/bringup/launch/" in cmd_watch._changed_files(after, nested)

    (package / "launch").rmdir()
    removed = cmd_watch._snapshot(ws)
    assert "pkg_a/launch/" in cmd_watch._changed_files(nested, removed)


def test_watch_reruns_when_empty_launch_directory_changes(
    ws: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = ws / "src" / "pkg_a"
    monkeypatch.setattr(cmd_watch, "_DEBOUNCE_SECONDS", 0)
    real_run = cmd_watch._run_once
    runs = {"n": 0}

    def counting(*args: Any, **kwargs: Any) -> Any:
        runs["n"] += 1
        return real_run(*args, **kwargs)

    monkeypatch.setattr(cmd_watch, "_run_once", counting)
    ticks = {"n": 0}

    def fake_sleep(_: float) -> None:
        ticks["n"] += 1
        if ticks["n"] == 1:
            (package / "launch").mkdir()
        elif ticks["n"] > 3:
            raise KeyboardInterrupt

    monkeypatch.setattr(cmd_watch.time, "sleep", fake_sleep)
    runner.invoke(app, ["watch", str(ws), "--audit", "--no-cache"])
    assert runs["n"] == 2


def test_entrypoint_matches_only_its_constructed_node_class(tmp_path: Path) -> None:
    from types import SimpleNamespace

    from ros2inspector.model.uam import _entrypoint_matches_node
    from ros2inspector.static.python_entrypoints import PythonEntrypoint

    package = tmp_path / "app"
    module = package / "app" / "nodes.py"
    module.parent.mkdir(parents=True)
    module.write_text(
        "class Alpha: pass\n"
        "class Beta: pass\n"
        "def helper(): return Alpha()\n"
        "def main_alpha(): helper()\n"
        "def main_beta(): Beta()\n"
    )
    alpha = SimpleNamespace(
        language="python", file_path=str(module), source_symbol="Alpha", name="Alpha"
    )
    beta = SimpleNamespace(
        language="python", file_path=str(module), source_symbol="Beta", name="Beta"
    )
    run_alpha = PythonEntrypoint("run_alpha", "app.nodes", "main_alpha")
    run_beta = PythonEntrypoint("run_beta", "app.nodes", "main_beta")
    assert _entrypoint_matches_node(run_alpha, alpha, package)
    assert not _entrypoint_matches_node(run_alpha, beta, package)
    assert _entrypoint_matches_node(run_beta, beta, package)
    assert not _entrypoint_matches_node(run_beta, alpha, package)


def test_entrypoint_module_parsed_only_once_per_analysis_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    from ros2inspector.model.uam import _entrypoint_matches_node
    from ros2inspector.static.python_entrypoints import PythonEntrypoint

    root = tmp_path / "pkg"
    module = root / "pkg" / "nodes.py"
    module.parent.mkdir(parents=True)
    module.write_text(
        "class Alpha: pass\nclass Beta: pass\n"
        "def main_alpha(): Alpha()\ndef main_beta(): Beta()\n"
    )
    real_read = Path.read_bytes
    count = {"n": 0}

    def counted_read(path: Path) -> bytes:
        if path == module:
            count["n"] += 1
        return real_read(path)

    monkeypatch.setattr(Path, "read_bytes", counted_read)
    cache: dict[str, Any] = {}
    for symbol in ("Alpha", "Beta"):
        entry = PythonEntrypoint("launch", "pkg.nodes", "main_" + symbol.lower())
        node = SimpleNamespace(
            language="python", file_path=str(module), source_symbol=symbol, name=symbol
        )
        assert _entrypoint_matches_node(entry, node, root, cache)
    assert count["n"] == 1


def test_standalone_package_with_src_directory_is_discovered(tmp_path: Path) -> None:
    from ros2inspector.discovery.package_finder import find_package_xml_files, source_search_root

    root = tmp_path / "standalone"
    (root / "src").mkdir(parents=True)
    (root / "src" / "node.cpp").write_text("int main() { return 0; }\\n")
    (root / "package.xml").write_text(
        '<package format="3"><name>standalone</name><version>1.0.0</version>'
        '<description>standalone</description><maintainer email="a@b.c">A</maintainer>'
        '<license>MIT</license></package>'
    )
    assert source_search_root(root) == root
    assert find_package_xml_files(root) == [root / "package.xml"]


def test_tree_sitter_runtime_compatibility_requirement() -> None:
    source = (Path(__file__).parents[2] / "pyproject.toml").read_text()
    assert '"tree-sitter>=0.25"' in source
    assert '"tree-sitter-cpp>=0.23.4"' in source


def test_atomic_output_without_fchmod(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from ros2inspector.cli._output import atomic_write_text

    monkeypatch.delattr(os, "fchmod", raising=False)
    target = tmp_path / "result.txt"
    atomic_write_text(target, "hello")
    assert target.read_text() == "hello"


def test_generated_package_without_fchmod(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from ros2inspector.design.codegen import generate_package_result

    monkeypatch.delattr(os, "fchmod", raising=False)
    generate_package_result({"pkg_name": "sample_pkg", "nodes": [
        {"id": "n1", "label": "talker"}
    ]}, tmp_path)
    assert (tmp_path / "sample_pkg" / "setup.py").is_file()


def test_windows_native_writes(ws: Path, tmp_path: Path) -> None:
    """Run real installed CLI commands on native Windows Python, no API monkeypatching."""
    import subprocess
    import sys

    if sys.platform != "win32":
        pytest.skip("Windows-only native writer smoke test")

    def run(*args: str) -> None:
        result = subprocess.run(
            [
                sys.executable, "-c",
                "from ros2inspector.cli.app import app; app()",
                *args,
            ],
            text=True, capture_output=True, cwd=tmp_path, timeout=90, check=False
        )
        assert result.returncode == 0, result.stdout + result.stderr

    nodes = tmp_path / "nodes.json"
    chart = tmp_path / "graph.html"
    docs = tmp_path / "docs"
    run("nodes", "-C", str(ws), "--no-cache", "-f", "json", "-o", str(nodes))
    assert nodes.is_file() and nodes.read_text(encoding="utf-8").startswith("[")
    run("viz", "-C", str(ws), "--no-open", "--no-cache", "-o", str(chart))
    assert chart.is_file() and "<html" in chart.read_text(encoding="utf-8").lower()
    run("docs", "-C", str(ws), "--no-cache", "-o", str(docs))
    assert (docs / "index.md").is_file()

    from ros2inspector.design.codegen import generate_package_result

    generate_package_result(
        {"pkg_name": "windows_pkg", "nodes": [{"id": "n1", "label": "talker"}]},
        tmp_path,
    )
    assert (tmp_path / "windows_pkg" / "setup.py").is_file()
    assert not list(tmp_path.rglob("*.tmp"))


def test_external_manifest_dependency_is_enforced(tmp_path: Path) -> None:
    from ros2inspector.model.uam import UAM
    from ros2inspector.policy.rules import rule_dependency

    root = tmp_path / "ws"
    pkg = root / "src" / "app"
    pkg.mkdir(parents=True)
    (pkg / "package.xml").write_text(
        '<package format="3"><name>app</name><version>1.0.0</version>'
        '<description>package</description><maintainer email="a@b.c">Me</maintainer>'
        '<license>MIT</license><depend>rclpy</depend></package>'
    )
    uam = UAM.build(root, use_cache=False)
    assert len(rule_dependency(uam, {"forbidden": [{"from": "app", "to": "rclpy"}]})) == 1
    assert not rule_dependency(uam, {"required": [
        {"package": "app", "depends_on": "rclpy"}
    ]})


def test_conditional_external_dependency_is_reported_as_uncertain(tmp_path: Path) -> None:
    from ros2inspector.model.uam import UAM
    from ros2inspector.policy.rules import rule_dependency

    package = tmp_path / "ws" / "src" / "app"
    package.mkdir(parents=True)
    (package / "package.xml").write_text(
        '<package format="3"><name>app</name><version>1.0.0</version>'
        '<description>app</description><maintainer email="a@b.c">Me</maintainer>'
        '<license>MIT</license>'
        '<depend condition="$ROS_VERSION == 1">roscpp</depend></package>'
    )
    uam = UAM.build(tmp_path / "ws", use_cache=False)
    prohibited = rule_dependency(uam, {
        "forbidden": [{"from": "app", "to": "roscpp"}]
    })
    assert len(prohibited) == 1
    assert prohibited[0].confidence == "uncertain"
    assert "conditional" in prohibited[0].message
    required = rule_dependency(uam, {
        "required": [{"package": "app", "depends_on": "roscpp"}]
    })
    assert len(required) == 1
    assert required[0].confidence == "uncertain"


def test_transitive_external_dependency_respects_rule_flag(tmp_path: Path) -> None:
    from ros2inspector.model.uam import UAM
    from ros2inspector.policy.rules import rule_dependency

    root = tmp_path / "ws"
    for name, extra in (("app", "<depend>bridge</depend>"),
                        ("bridge", "<depend>rclpy</depend>")):
        pkg = root / "src" / name
        pkg.mkdir(parents=True)
        (pkg / "package.xml").write_text(
            f'<package format="3"><name>{name}</name><version>1.0.0</version>'
            '<description>pkg</description><maintainer email="a@b.c">Me</maintainer>'
            f'<license>MIT</license>{extra}</package>'
        )
    uam = UAM.build(root, use_cache=False)
    forbidden = {"forbidden": [{"from": "app", "to": "rclpy"}]}
    assert len(rule_dependency(uam, forbidden)) == 1
    assert not rule_dependency(uam, {**forbidden, "transitive": False})
    assert not rule_dependency(uam, {"required": [
        {"package": "app", "depends_on": "rclpy"}
    ]})
    assert len(rule_dependency(uam, {"transitive": False, "required": [
        {"package": "app", "depends_on": "rclpy"}
    ]})) == 1
