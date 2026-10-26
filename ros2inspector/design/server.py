from __future__ import annotations

import errno
import json
import os
import secrets
import shutil
import tempfile
import threading
import traceback
import uuid
import webbrowser
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from pydantic import ValidationError
from pydantic_core import ErrorDetails
from rich.console import Console

from ros2inspector.design.codegen import (
    _GENERATED_MARKER,
    GenerateResult,
    package_name_edits,
    plan_package_edits,
    previous_node_labels,
    read_manifest,
    rename_blockers,
    rename_package_contents,
    render_package,
    write_package,
)
from ros2inspector.design.models import (
    DesignState,
    PackageSpec,
    known_interfaces_json,
    name_length_errors,
)
from ros2inspector.design.ui import build_html

STATE_FILE = ".ros2inspector_design.json"
_MAX_REQUEST_BYTES = 1024 * 1024
# The server is single-threaded on purpose (generation must not race), so a client that
# opens a connection and never sends a request must not block it forever.
_REQUEST_TIMEOUT_SECONDS = 30


def _rename_plan(
    packages: list[dict[str, Any]],
    output_dir: Path,
    old_state: dict[str, Any] | None,
) -> list[tuple[Path, Path]]:
    """Return (old_dir, new_dir) moves for generated packages whose name changed."""
    if not old_state:
        return []
    old_by_id: dict[str, str] = {}
    for package in old_state.get("packages", []):
        if not isinstance(package, dict) or "id" not in package:
            continue
        old_name = package.get("_generated_name") or package.get("pkg_name")
        if isinstance(old_name, str):
            old_by_id[str(package["id"])] = old_name

    moves: list[tuple[Path, Path]] = []
    for package in packages:
        package_id = str(package.get("id", ""))
        new_name = str(package.get("pkg_name", ""))
        old_name = old_by_id.get(package_id)
        if not old_name or old_name == new_name:
            continue
        old_dir = output_dir / old_name
        if not old_dir.is_dir():
            continue
        if not (old_dir / _GENERATED_MARKER).is_file():
            raise PermissionError(f"Refusing to rename '{old_dir}': it is not a generated package.")
        blockers = rename_blockers(old_dir, old_name, new_name)
        if blockers:
            raise FileExistsError(
                f"Cannot rename '{old_name}' to '{new_name}': {', '.join(blockers)} already "
                f"exists inside the package, so the Python module could not be moved. "
                "Rename or remove it first."
            )
        moves.append((old_dir, output_dir / new_name))

    sources = {old for old, _ in moves}
    for _, new_dir in moves:
        # A target is free only if nothing is there, or it is itself being renamed away.
        if new_dir.exists() and new_dir not in sources:
            raise FileExistsError(
                f"Cannot rename a package to '{new_dir.name}': '{new_dir}' already exists. "
                "Rename or remove it first; ROS 2 Inspector never deletes packages."
            )
    return moves


def _apply_renames(
    packages: list[dict[str, Any]],
    output_dir: Path,
    old_state: dict[str, Any] | None,
) -> list[str]:
    """Rename generated package directories after a validated package-name change.

    Renames are staged through a temporary directory (marked ``COLCON_IGNORE``) so that
    swaps and cycles (a -> b, b -> a) work and no package is ever deleted. If any move
    fails, every package is put back under its original name. Returns the edited files
    (as ``package/path``) whose references to the old package name were updated.
    """
    moves = _rename_plan(packages, output_dir, old_state)
    if not moves:
        return []
    for old_dir, new_dir in moves:
        package_name_edits(old_dir, old_dir.name, new_dir.name)
    staging = Path(tempfile.mkdtemp(prefix=".ros2inspector-rename-", dir=output_dir))
    (staging / "COLCON_IGNORE").write_text("", encoding="utf-8")
    staged: list[tuple[Path, Path, Path]] = []
    finished: list[tuple[Path, Path]] = []
    recovery_failed = False
    try:
        for old_dir, new_dir in moves:
            temp = staging / f"{uuid.uuid4().hex}-{old_dir.name}"
            old_dir.rename(temp)
            staged.append((old_dir, temp, new_dir))
        for old_dir, temp, new_dir in staged:
            temp.rename(new_dir)
            finished.append((old_dir, new_dir))
    except BaseException:
        # Roll the completed destinations back into their own staging slots
        # before restoring *any* original location. Direct inverse moves are
        # unsafe in rename cycles because original paths can still be occupied.
        rollback_errors: list[str] = []
        temp_by_old = {old: temp for old, temp, _ in staged}
        for old_dir, new_dir in reversed(finished):
            try:
                new_dir.rename(temp_by_old[old_dir])
            except OSError as exc:
                rollback_errors.append(f"{new_dir} -> {temp_by_old[old_dir]}: {exc}")
        if not rollback_errors:
            for old_dir, temp, _ in staged:
                if temp.exists():
                    try:
                        temp.rename(old_dir)
                    except OSError as exc:
                        rollback_errors.append(f"{temp} -> {old_dir}: {exc}")
        if rollback_errors:
            recovery_failed = True
            # Do not erase the staging directory: unresolved originals may
            # remain there. Report the exact recovery location.
            raise RuntimeError(
                f"Package rename could not be rolled back. Recovery files remain "
                f"at {staging}. Errors: {'; '.join(rollback_errors)}"
            ) from None
        raise
    finally:
        # Remove staging only when every staged original was restored or
        # every move succeeded. A failed rollback must preserve user files.
        if not recovery_failed and not any(temp.exists() for _, temp, _ in staged):
            shutil.rmtree(staging, ignore_errors=True)
    # Package names in edited files are rewritten after every move, so a swap (a <-> b) sees
    # each file once and never turns 'a' into 'b' and back again.
    renamed_in: list[str] = []
    for old_dir, new_dir in moves:
        updated = rename_package_contents(new_dir, old_dir.name, new_dir.name)
        renamed_in.extend(f"{new_dir.name}/{path}" for path in updated)
    return renamed_in


def _load_state(output_dir: Path, console: Console | None = None) -> dict[str, Any] | None:
    """Load the saved design, validated.

    Wires that no longer validate (for example from an older version that allowed mismatched
    topics) are dropped instead of discarding the whole design. A file that still does not
    validate is copied aside so the next save cannot silently destroy it.
    """
    state_path = output_dir / STATE_FILE
    if not state_path.exists():
        return None
    try:
        raw: object = json.loads(state_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
        _quarantine_state(state_path, f"unreadable ({exc})", console)
        return None
    if not isinstance(raw, dict):
        _quarantine_state(state_path, "not a JSON object", console)
        return None
    try:
        DesignState.model_validate(raw)
        return raw
    except ValidationError:
        pass
    # Only recover invalid connections. Validate the remaining design independently
    # so a malformed node or package cannot be masked by dropping all wires.
    packages = raw.get("packages")
    if not isinstance(packages, list):
        _quarantine_state(state_path, "invalid package list", console)
        return None
    original_edges: list[tuple[dict[str, Any], list[Any]]] = []
    for package in packages:
        if isinstance(package, dict):
            edges = package.get("edges", [])
            if isinstance(edges, list):
                original_edges.append((package, edges))
                package["edges"] = []
    try:
        DesignState.model_validate(raw)
    except ValidationError as exc:
        _quarantine_state(state_path, f"invalid ({exc.error_count()} errors)", console)
        return None
    dropped = 0
    for package, edges in original_edges:
        for edge in edges:
            package["edges"].append(edge)
            try:
                DesignState.model_validate(raw)
            except ValidationError:
                package["edges"].pop()
                dropped += 1
    if dropped:
        # Keep an untouched copy before any subsequent save can persist recovery.
        _quarantine_state(state_path, f"{dropped} invalid connection(s)", console)
        if console is not None:
            console.print(f"[yellow]Removed {dropped} invalid saved connection(s).[/yellow]")
    return raw


def _quarantine_state(path: Path, reason: str, console: Console | None) -> None:
    backup = path.with_name(path.name + ".invalid")
    try:
        shutil.copy2(path, backup)
    except OSError:
        return
    if console is not None:
        console.print(
            f"[yellow]Saved design {path} is {reason}; starting fresh. "
            f"A copy was kept at {backup}.[/yellow]"
        )


def _save_state(output_dir: Path, state: dict[str, Any]) -> None:
    """Write the design atomically: a failed write never truncates the previous state."""
    output_dir.mkdir(parents=True, exist_ok=True)
    state_path = output_dir / STATE_FILE
    data = json.dumps(state, indent=2, sort_keys=True).encode("utf-8")
    fd, temp_name = tempfile.mkstemp(prefix=f".{STATE_FILE}.", suffix=".tmp", dir=output_dir)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, state_path)
    except BaseException:
        Path(temp_name).unlink(missing_ok=True)
        raise


def _issue_message(issue: ErrorDetails) -> str:
    """Pydantic message without its internal 'Value error, ' / 'Assertion failed, ' prefix."""
    message = str(issue["msg"])
    for prefix in ("Value error, ", "Assertion failed, "):
        if message.startswith(prefix):
            return message[len(prefix) :]
    return message


def _validation_response(exc: ValidationError) -> dict[str, Any]:
    return {
        "error": "Validation failed",
        "fields": [
            {
                "path": ".".join(str(part) for part in issue["loc"]),
                "message": _issue_message(issue),
                "type": issue["type"],
            }
            for issue in exc.errors(include_url=False)
        ],
    }


_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def _merge_generated_names(payload: dict[str, Any], old_state: dict[str, Any] | None) -> None:
    """Carry each package's last generated directory name over from the saved state."""
    if not old_state:
        return
    generated_by_id = {
        str(package["id"]): package["_generated_name"]
        for package in old_state.get("packages", [])
        if isinstance(package, dict) and "id" in package and "_generated_name" in package
    }
    for package in payload.get("packages", []):
        package_id = str(package.get("id", ""))
        if package_id in generated_by_id:
            package["_generated_name"] = generated_by_id[package_id]


def _old_package(old_state: dict[str, Any] | None, package_id: str) -> dict[str, Any] | None:
    for package in (old_state or {}).get("packages", []):
        if isinstance(package, dict) and str(package.get("id", "")) == package_id:
            return package
    return None


def _previous_nodes(
    package_dir: Path, old_state: dict[str, Any] | None, package_id: str
) -> dict[str, str]:
    """Node id -> label at the last generation, from the package marker or the saved design."""
    labels = previous_node_labels(package_dir)
    if labels:
        return labels
    old_package = _old_package(old_state, package_id) or {}
    return {
        str(node["id"]): str(node["label"])
        for node in old_package.get("nodes", [])
        if isinstance(node, dict) and "id" in node and "label" in node
    }


def _edit_problems(
    packages: list[PackageSpec],
    rendered: dict[str, dict[str, str]],
    output_dir: Path,
    renames: list[tuple[Path, Path]],
    old_state: dict[str, Any] | None,
) -> list[str]:
    """Why regenerating would leave a package that does not run, checked before any write."""
    source_of = {new: old for old, new in renames}
    problems: list[str] = []
    for package in packages:
        if package.id not in rendered:
            continue
        target = output_dir / package.pkg_name
        source = source_of.get(target, target)
        manifest = read_manifest(source)
        if manifest is None:
            continue
        old_name, new_name = source.name, package.pkg_name
        try:
            metadata = package_name_edits(source, old_name, new_name)
        except ValueError as exc:
            problems.append(f"{package.pkg_name}: {exc}")
            continue
        # Before a pending package rename, the module folder and resource marker still use
        # the old name; view the package as it will look after the rename.
        moves = [(old_name, new_name), (f"resource/{old_name}", f"resource/{new_name}")]

        def renamed(relative: str, moves: list[tuple[str, str]] = moves) -> str:
            for old, new in moves:
                if relative == old or relative.startswith(f"{old}/"):
                    return new + relative[len(old) :]
            return relative

        def read(
            relative: str,
            source: Path = source,
            moves: list[tuple[str, str]] = moves,
            metadata: dict[str, bytes] = metadata,
        ) -> bytes | None:
            for old, new in moves:
                if relative == new or relative.startswith(f"{new}/"):
                    relative = old + relative[len(new) :]
                    break
            path = source / relative
            return metadata.get(relative, path.read_bytes()) if path.is_file() else None

        translated = {renamed(key): digest for key, digest in manifest.items()}
        plan = plan_package_edits(
            package,
            read,
            translated,
            _previous_nodes(source, old_state, package.id),
        )
        problems.extend(f"{package.pkg_name}: {problem}" for problem in plan.problems)
    return problems


def _make_handler(
    html: str | Callable[[], str],
    output_dir: Path,
    console: Console,
    session_token: str = "",
) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        # A callable re-renders the page per request, so a reload shows the last saved design.
        _page = staticmethod(html) if callable(html) else staticmethod(lambda: html)
        _out = output_dir
        _con = console
        _token = session_token
        timeout = _REQUEST_TIMEOUT_SECONDS

        def _security_headers(self) -> None:
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Cache-Control", "no-store")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self' 'unsafe-inline' data: blob:; connect-src 'self'; "
                "img-src 'self' data: blob:; frame-ancestors 'none'",
            )

        def _host_is_local(self) -> bool:
            host = self.headers.get("Host", "")
            hostname = host.rsplit(":", 1)[0].strip("[]").lower() if host else ""
            return hostname in _LOCAL_HOSTS

        def _request_is_local(self) -> bool:
            if not self._host_is_local():
                return False
            origin = self.headers.get("Origin")
            if not origin:
                return True
            parsed = urlsplit(origin)
            return (
                parsed.scheme == "http"
                and parsed.hostname in _LOCAL_HOSTS
                and parsed.netloc == self.headers.get("Host", "")
            )

        def do_GET(self) -> None:  # noqa: N802
            # The page embeds the session token and the saved design, so a DNS-rebinding
            # page must not be able to read it: require a loopback Host here too.
            if not self._host_is_local():
                self._respond(403, {"error": "Foreign Host rejected"})
                return
            if urlsplit(self.path).path not in ("/", ""):
                self.send_response(404)
                self._security_headers()
                self.end_headers()
                return
            body = self._page().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self._security_headers()
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:  # noqa: N802
            try:
                self._do_post()
            except Exception as exc:  # pragma: no cover - defensive catch-all
                self._con.print(f"[red]✗ Internal error:[/red] {exc}")
                self._con.print(traceback.format_exc(), markup=False, highlight=False)
                try:
                    self._respond(500, {"error": f"Internal error: {type(exc).__name__}: {exc}"})
                except OSError:
                    pass

        def _do_post(self) -> None:
            if urlsplit(self.path).path not in ("/generate", "/save"):
                self._respond(404, {"error": "Not found"})
                return
            if not self._request_is_local():
                self._respond(403, {"error": "Foreign Host/Origin rejected"})
                return
            if self._token and self.headers.get("X-ROS2Inspector-Token") != self._token:
                self._respond(403, {"error": "Missing or invalid session token"})
                return
            content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip()
            if content_type != "application/json":
                self._respond(415, {"error": "Content-Type must be application/json"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                self._respond(400, {"error": "Invalid Content-Length"})
                return
            if length <= 0 or length > _MAX_REQUEST_BYTES:
                self._respond(413, {"error": "Request body is empty or too large"})
                return
            try:
                raw: object = json.loads(self.rfile.read(length))
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                self._respond(400, {"error": f"Invalid JSON: {exc}"})
                return
            if not isinstance(raw, dict):
                self._respond(400, {"error": "JSON body must be an object"})
                return
            try:
                state = DesignState.model_validate(raw)
            except ValidationError as exc:
                self._respond(422, _validation_response(exc))
                return
            payload = state.model_dump(by_alias=True)
            if urlsplit(self.path).path == "/save":
                self._handle_save(payload)
            else:
                self._handle_generate(payload, state.packages)

        def _handle_save(self, payload: dict[str, Any]) -> None:
            _merge_generated_names(payload, _load_state(self._out))
            error = self._try_save(payload)
            if error is None:
                self._respond(200, {"ok": True, "state_saved": True})
            else:
                self._respond(
                    500,
                    {
                        "error": f"The design could not be saved: {error}",
                        "state_saved": False,
                        "state_error": error,
                    },
                )

        def _handle_generate(
            self,
            payload: dict[str, Any],
            packages: list[PackageSpec],
        ) -> None:
            package_by_id = {package.id: package for package in packages}
            package_payloads: list[dict[str, Any]] = payload.get("packages", [])
            too_long = name_length_errors(DesignState.model_validate(payload))
            if too_long:
                self._respond(422, {"error": "Validation failed", "fields": too_long})
                return
            to_generate = [package for package in packages if package.nodes]
            # 1. Render and compile every package before touching the disk.
            try:
                rendered = {package.id: render_package(package) for package in to_generate}
            except (SyntaxError, ValueError) as exc:
                self._respond(400, {"error": f"Generated code is invalid: {exc}"})
                return
            old_state = _load_state(self._out)
            _merge_generated_names(payload, old_state)
            # 2. Refuse up front if any target is an unrelated directory.
            try:
                renames = _rename_plan(package_payloads, self._out, old_state)
            except (OSError, ValueError) as exc:
                self._respond(409, {"error": str(exc)})
                return
            renamed_to = {new for _, new in renames}
            for package in to_generate:
                target = self._out / package.pkg_name
                if (
                    target.exists()
                    and target not in renamed_to
                    and not (target / _GENERATED_MARKER).is_file()
                ):
                    self._respond(
                        409,
                        {
                            "error": f"Refusing to overwrite '{target}': it was not "
                            "generated by ROS 2 Inspector."
                        },
                    )
                    return
            # 3. Refuse up front if a package with user edits would not run after this.
            try:
                problems = _edit_problems(to_generate, rendered, self._out, renames, old_state)
            except OSError as exc:
                problems = [str(exc)]
            if problems:
                self._respond(409, {"error": " ".join(problems), "problems": problems})
                return
            # 4. Rename, then write. Save state on the way out so names never go stale.
            results: list[GenerateResult] = []
            renamed_in: list[str] = []
            try:
                renamed_in = _apply_renames(package_payloads, self._out, old_state)
                for path in renamed_in:
                    self._con.print(f"  [yellow]updated the package name in:[/yellow] {path}")
                for package_payload in package_payloads:
                    package = package_by_id[str(package_payload.get("id", ""))]
                    if (self._out / package.pkg_name / _GENERATED_MARKER).is_file():
                        package_payload["_generated_name"] = package.pkg_name
                for package_payload in package_payloads:
                    package = package_by_id[str(package_payload.get("id", ""))]
                    if package.id not in rendered:
                        continue
                    target = self._out / package.pkg_name
                    result = write_package(
                        package,
                        self._out,
                        rendered[package.id],
                        previous_nodes=_previous_nodes(target, old_state, package.id),
                    )
                    package_payload["_generated_name"] = package.pkg_name
                    results.append(result)
                    self._report(result)
            except (OSError, ValueError, KeyError) as exc:
                self._con.print(f"[red]✗ Error:[/red] {exc}")
                state_error = self._try_save(payload)
                self._respond(
                    400,
                    {
                        "error": str(exc),
                        "packages": [result.to_dict() for result in results],
                        "state_saved": state_error is None,
                        "state_error": state_error,
                    },
                )
                return
            state_error = self._try_save(payload)
            files = [f"{r.package}/{path}" for r in results for path in r.written + r.unchanged]
            self._respond(
                200,
                {
                    "files": sorted(files),
                    "output_dir": str(self._out),
                    "packages": [result.to_dict() for result in results],
                    "kept_modified": [
                        f"{r.package}/{path}" for r in results for path in r.kept_modified
                    ],
                    "renamed_in": renamed_in,
                    "updated_in": [f"{r.package}: {note}" for r in results for note in r.notes],
                    "warnings": [f"{r.package}: {text}" for r in results for text in r.warnings],
                    "state_saved": state_error is None,
                    "state_error": state_error,
                },
            )

        def _report(self, result: GenerateResult) -> None:
            self._con.print(
                f"\n[green]✓[/green] Generated [bold cyan]{result.package}/[/bold cyan] "
                f"[dim]({len(result.written)} written, {len(result.unchanged)} unchanged, "
                f"{len(result.removed)} removed)[/dim]"
            )
            for path in result.kept_modified:
                self._con.print(f"  [yellow]kept your edits:[/yellow] {result.package}/{path}")
            for note in result.notes:
                self._con.print(f"  [yellow]{note}[/yellow]")
            for warning in result.warnings:
                self._con.print(f"  [yellow]warning:[/yellow] {warning}")

        def _try_save(self, payload: dict[str, Any]) -> str | None:
            """Save the design; return why it failed, or None. The old state is kept on failure."""
            try:
                _save_state(self._out, payload)
            except (OSError, ValueError) as exc:
                self._con.print(f"[red]✗ Could not save design state:[/red] {exc}")
                return str(exc)
            return None

        def _respond(self, code: int, data: dict[str, Any]) -> None:
            body = json.dumps(data).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self._security_headers()
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt: str, *args: object) -> None:  # noqa: ARG002
            return

    return Handler


def bind_server(port: int, handler: type[BaseHTTPRequestHandler]) -> HTTPServer:
    """Bind on 127.0.0.1, falling back to a free port if ``port`` is taken.

    Binding directly (instead of probing first) avoids a race between the probe and the bind.
    """
    try:
        return HTTPServer(("127.0.0.1", port), handler)
    except OSError as exc:
        if exc.errno not in (errno.EADDRINUSE, errno.EACCES) or port == 0:
            raise
        return HTTPServer(("127.0.0.1", 0), handler)


def create_design_server(
    port: int, output_dir: Path, console: Console, *, session_token: str | None = None
) -> HTTPServer:
    """Bind the designer on 127.0.0.1 without serving yet (the caller runs serve_forever)."""
    token = session_token or secrets.token_urlsafe(32)
    interfaces = known_interfaces_json()

    def render_page() -> str:
        return build_html(_load_state(output_dir), session_token=token, interfaces=interfaces)

    return bind_server(port, _make_handler(render_page, output_dir, console, session_token=token))


def run_design_server(
    port: int,
    output_dir: Path,
    open_browser: bool,
    console: Console,
) -> None:
    output_dir = output_dir.expanduser().resolve()
    initial_state = _load_state(output_dir, console)
    server = create_design_server(port, output_dir, console)
    actual_port = int(server.server_address[1])
    if port and actual_port != port:
        console.print(f"[yellow]Port {port} is in use, using {actual_port} instead.[/yellow]")

    url = f"http://127.0.0.1:{actual_port}/"
    if initial_state:
        package_count = len(initial_state.get("packages", []))
        console.print(
            f"\n[bold]ROS2 Inspector[/bold] — [cyan]Package Designer[/cyan] "
            f"[dim](resuming {package_count} package(s))[/dim]"
        )
    else:
        console.print("\n[bold]ROS2 Inspector[/bold] — [cyan]Package Designer[/cyan]")
    console.print(f"  [dim]Server:[/dim]     [bold]{url}[/bold]")
    console.print(f"  [dim]Output dir:[/dim] [dim]{output_dir}[/dim]")
    if (output_dir / "src").is_dir() and not (output_dir / "package.xml").exists():
        console.print(
            f"  [yellow]Hint:[/yellow] {output_dir} looks like a workspace root; colcon only "
            f"finds packages under [bold]{output_dir / 'src'}[/bold] (use -o to choose it)."
        )
    console.print("  [dim]Press [bold]Ctrl+C[/bold] to stop[/dim]\n")

    if open_browser:
        threading.Timer(0.15, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        console.print("\n[dim]Design server stopped.[/dim]")
