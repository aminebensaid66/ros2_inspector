from __future__ import annotations

import os
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console

from ros2inspector.cli._state import state
from ros2inspector.cli._workspace import build_uam, load_packages_or_exit, pick_path
from ros2inspector.discovery import DEFAULT_EXCLUDED_DIRS, find_workspace_root, source_search_root
from ros2inspector.model.schemas import PolicyViolation, ViolationSeverity
from ros2inspector.policy import PolicyConfigError, load_policy, run_policy, violation_summary

console = Console()
err_console = Console(stderr=True)

# Every input the analysis reads: sources, interfaces, build/launch/config files.
WATCH_SUFFIXES = frozenset(
    {
        ".py",
        ".cpp",
        ".cc",
        ".cxx",
        ".c",
        ".hpp",
        ".hh",
        ".hxx",
        ".h",
        ".msg",
        ".srv",
        ".action",
        ".xml",
        ".yaml",
        ".yml",
        ".launch",
        ".cfg",
        ".toml",
    }
)
WATCH_NAMES = frozenset({"CMakeLists.txt", "COLCON_IGNORE", ".rosignore", ".ros2inspectorignore"})
_STOP_MARKERS = ("COLCON_IGNORE", ".rosignore")
_DEBOUNCE_SECONDS = 0.5
_SEVERITY_COLOR = {
    ViolationSeverity.ERROR: "red",
    ViolationSeverity.WARNING: "yellow",
    ViolationSeverity.INFO: "cyan",
}
_FAIL_LEVELS = {
    "error": {ViolationSeverity.ERROR},
    "warning": {ViolationSeverity.ERROR, ViolationSeverity.WARNING},
    "info": {ViolationSeverity.ERROR, ViolationSeverity.WARNING, ViolationSeverity.INFO},
}

Snapshot = dict[str, tuple[int, int, int, int]]


def _snapshot(root: Path, extra: tuple[Path, ...] = ()) -> Snapshot:
    """Map watched files to (mtime_ns, ctime_ns, size, inode).

    Walks the source tree with ``os.walk`` and prunes excluded and hidden directories
    *before* descending, so build/install/log/.git trees are never stat-ed.
    """
    source_root = source_search_root(root)
    snap: Snapshot = {}
    for dirpath, dirnames, filenames in os.walk(source_root):
        dirnames[:] = sorted(
            d for d in dirnames if d not in DEFAULT_EXCLUDED_DIRS and not d.startswith(".")
        )
        base = Path(dirpath)
        if base != source_root and any(marker in filenames for marker in _STOP_MARKERS):
            # Ignored subtree: only watch the marker itself, so removing it is noticed.
            dirnames[:] = []
            filenames = [name for name in filenames if name in _STOP_MARKERS]
        for filename in filenames:
            if filename not in WATCH_NAMES and Path(filename).suffix not in WATCH_SUFFIXES:
                continue
            path = base / filename
            try:
                st = path.stat()
            except OSError:
                continue
            key = path.relative_to(source_root).as_posix()
            snap[key] = (st.st_mtime_ns, st.st_ctime_ns, st.st_size, st.st_ino)
    for path in extra:
        try:
            st = path.stat()
        except OSError:
            continue
        snap[f"<policy>/{path.name}"] = (st.st_mtime_ns, st.st_ctime_ns, st.st_size, st.st_ino)
    return snap


def _changed_files(old: Snapshot, new: Snapshot) -> list[str]:
    changes = [path for path, stamp in new.items() if old.get(path) != stamp]
    changes.extend(path for path in old if path not in new)
    return sorted(changes)


def _fingerprint(violation: PolicyViolation) -> tuple[str, str, str, tuple[str, ...]]:
    return (
        str(violation.severity),
        violation.rule_type,
        violation.message,
        tuple(sorted(violation.affected_entities)),
    )


@dataclass
class RunResult:
    violations: list[PolicyViolation] = field(default_factory=list)
    error: str | None = None


def _print_violation(prefix: str, violation: PolicyViolation) -> None:
    color = _SEVERITY_COLOR.get(violation.severity, "white")
    entities = (
        f" [dim]({', '.join(violation.affected_entities)})[/dim]"
        if violation.affected_entities
        else ""
    )
    console.print(
        f"{prefix}[{color}]{violation.severity.upper():8}[/{color}]  "
        f"{violation.rule_type}: {violation.message}{entities}"
    )


def _run_once(
    root: Path,
    rules: list[dict[str, Any]],
    no_cache: bool,
    previous: RunResult | None = None,
    verbose: bool = False,
) -> RunResult:
    try:
        uam = build_uam(root, use_cache=not no_cache)
        violations = run_policy(uam, rules)
    except Exception as exc:
        console.print(f"[red]Analysis error:[/red] {type(exc).__name__}: {exc}")
        if verbose:
            console.print(traceback.format_exc(), markup=False, highlight=False)
        else:
            console.print("[dim]Run with --verbose for the full traceback.[/dim]")
        return RunResult(error=str(exc))

    summary = violation_summary(violations)
    if previous is None or previous.error is not None:
        for violation in violations:
            _print_violation("", violation)
    else:
        old = {_fingerprint(v) for v in previous.violations}
        new = {_fingerprint(v) for v in violations}
        added = [v for v in violations if _fingerprint(v) not in old]
        resolved = [v for v in previous.violations if _fingerprint(v) not in new]
        for violation in added:
            _print_violation("[bold]+ new[/bold]      ", violation)
        for violation in resolved:
            _print_violation("[green]✓ resolved[/green] ", violation)
        if not added and not resolved:
            console.print("[dim]No change in findings.[/dim]")

    if not violations:
        console.print("[bold green]✓ No violations[/bold green]")
        return RunResult(violations=violations)
    parts = []
    if summary["errors"]:
        parts.append(f"[red]{summary['errors']} error(s)[/red]")
    if summary["warnings"]:
        parts.append(f"[yellow]{summary['warnings']} warning(s)[/yellow]")
    if summary["info"]:
        parts.append(f"[cyan]{summary['info']} info[/cyan]")
    console.print(f"\n[dim]Result:[/dim] {', '.join(parts)}")
    return RunResult(violations=violations)


def _exit_code(result: RunResult, fail_on: str) -> int:
    if result.error is not None:
        return 3
    levels = _FAIL_LEVELS[fail_on]
    return 1 if any(v.severity in levels for v in result.violations) else 0


def _load_rules(audit: bool, policy_path: Path | None) -> list[dict[str, Any]]:
    if audit or policy_path is None:
        from ros2inspector.cli.cmd_audit import _DEFAULT_EXCLUDES, _build_rules

        return _build_rules(_DEFAULT_EXCLUDES)
    return load_policy(policy_path)


def _wait_until_stable(root: Path, extra: tuple[Path, ...], snap: Snapshot) -> Snapshot:
    """Debounce: editors often save in several steps, so wait for writes to settle."""
    while True:
        time.sleep(_DEBOUNCE_SECONDS)
        current = _snapshot(root, extra)
        if current == snap:
            return current
        snap = current


def watch(
    path_arg: Annotated[
        Path | None, typer.Argument(metavar="[PATH]", help="Workspace root (default: CWD)")
    ] = None,
    path: Annotated[
        Path | None, typer.Option("-C", "--path", help="Workspace root (same as PATH)")
    ] = None,
    policy: Annotated[
        Path | None,
        typer.Option(
            "--policy",
            "-p",
            help="Policy YAML file (default: <workspace>/ros2inspector_policy.yaml)",
        ),
    ] = None,
    audit: Annotated[
        bool,
        typer.Option("--audit", help="Use built-in audit rules instead of a policy file"),
    ] = False,
    interval: Annotated[
        float,
        typer.Option("--interval", "-i", min=0.5, help="Polling interval in seconds (>= 0.5)"),
    ] = 3.0,
    fail_on: Annotated[
        str,
        typer.Option("--fail-on", help="Severity that makes the exit code 1: error|warning|info"),
    ] = "error",
    once: Annotated[
        bool, typer.Option("--once", help="Run a single validation and exit (for scripts)")
    ] = False,
    no_cache: Annotated[
        bool, typer.Option("--no-cache", help="Always re-parse on each change")
    ] = False,
    verbose: Annotated[
        bool, typer.Option("--verbose", help="Show tracebacks for analysis errors")
    ] = False,
) -> None:
    """Watch the workspace for file changes and re-run policy validation.

    After the first run only new and resolved findings are printed. Editing the policy
    file reloads it. On Ctrl+C (or with --once) the exit code reflects the last run:
    0 clean, 1 findings at or above --fail-on, 2 bad policy, 3 analysis/workspace error,
    130 stopped before the first run finished.

    Examples:
      ros2inspector watch --audit
      ros2inspector watch ~/ws --policy my_policy.yaml --interval 5
      ros2inspector watch --once --fail-on warning
    """
    if fail_on not in _FAIL_LEVELS:
        err_console.print("[red]Error:[/red] --fail-on must be error, warning, or info")
        raise typer.Exit(2)

    root = find_workspace_root(pick_path(path_arg, path).resolve(), warn=not state.quiet)
    policy_path: Path | None = None
    if not audit:
        # An explicit relative --policy is relative to the CWD (like any CLI path);
        # the default policy lives in the watched workspace.
        default_policy = root / "ros2inspector_policy.yaml"
        policy_path = policy.expanduser().resolve() if policy else default_policy
        if not policy_path.is_file():
            err_console.print(
                f"[red]Error:[/red] policy file not found: {policy_path}\n"
                "[dim]Tip: use --audit to run without a policy file.[/dim]"
            )
            raise typer.Exit(3)
    try:
        rules = _load_rules(audit, policy_path)
    except PolicyConfigError as exc:
        err_console.print(f"[red]Policy error:[/red] {exc}")
        raise typer.Exit(2) from exc

    load_packages_or_exit(root)
    policy_label = "built-in audit" if policy_path is None else str(policy_path)
    extra = (policy_path,) if policy_path else ()

    if once:
        raise typer.Exit(_exit_code(_run_once(root, rules, no_cache, verbose=verbose), fail_on))

    console.print(f"[bold cyan]ROS2 Inspector — watch[/bold cyan]  [dim]{root}[/dim]")
    console.print(f"[dim]Policy: {policy_label}  |  interval: {interval:g}s[/dim]")
    console.print("[dim]Press Ctrl+C to stop.[/dim]\n")

    last: RunResult | None = None
    try:
        prev_snap = _snapshot(root, extra)
        console.print("[dim]Initial scan…[/dim]")
        last = _run_once(root, rules, no_cache, verbose=verbose)
        while True:
            time.sleep(interval)
            curr_snap = _snapshot(root, extra)
            changed = _changed_files(prev_snap, curr_snap)
            if not changed:
                continue
            curr_snap = _wait_until_stable(root, extra, curr_snap)
            changed = _changed_files(prev_snap, curr_snap)
            prev_snap = curr_snap
            if not changed:
                continue
            ts = time.strftime("%H:%M:%S")
            console.rule(f"[dim]{ts}[/dim] {len(changed)} file(s) changed")
            for f in changed[:5]:
                console.print(f"  [dim]• {f}[/dim]")
            if len(changed) > 5:
                console.print(f"  [dim]  … and {len(changed) - 5} more[/dim]")
            console.print()
            if policy_path is not None and any(f.startswith("<policy>/") for f in changed):
                try:
                    rules = load_policy(policy_path)
                    console.print("[dim]Policy reloaded.[/dim]")
                except PolicyConfigError as exc:
                    console.print(f"[red]Policy error, keeping previous rules:[/red] {exc}")
            last = _run_once(root, rules, no_cache, previous=last, verbose=verbose)
    except KeyboardInterrupt:
        console.print("\n[dim]Stopped.[/dim]")
    if last is None:
        raise typer.Exit(130)
    raise typer.Exit(_exit_code(last, fail_on))
