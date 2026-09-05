from __future__ import annotations

import ast
import configparser
import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class PythonEntrypoint:
    """A statically declared Python console-script executable."""

    executable: str
    module: str
    callable: str | None = None
    source_file: str | None = None


def find_python_entrypoints(package_path: Path) -> dict[str, PythonEntrypoint]:
    """Collect Python console scripts without importing or executing package code.

    ROS 2 ``ament_python`` packages most commonly declare executables in ``setup.py``.
    ``setup.cfg`` and PEP 621 ``[project.scripts]`` declarations are also supported.
    Later declarations override earlier ones deterministically.
    """

    result: dict[str, PythonEntrypoint] = {}
    for parser, filename in (
        (_parse_setup_py, "setup.py"),
        (_parse_setup_cfg, "setup.cfg"),
        (_parse_pyproject_scripts, "pyproject.toml"),
    ):
        path = package_path / filename
        if not path.is_file():
            continue
        for executable, target in parser(path).items():
            module, callable_name = _split_target(target)
            if not module:
                continue
            result[executable] = PythonEntrypoint(
                executable=executable,
                module=module,
                callable=callable_name,
                source_file=str(path),
            )
    return result


def _split_target(target: str) -> tuple[str, str | None]:
    module, sep, callable_name = target.partition(":")
    module = module.strip()
    callable_name = callable_name.strip() if sep else ""
    return module, callable_name or None


def _parse_entry_line(value: str) -> tuple[str, str] | None:
    executable, sep, target = value.partition("=")
    executable = executable.strip()
    target = target.strip()
    if not sep or not executable or not target:
        return None
    return executable, target


def _parse_setup_py(path: Path) -> dict[str, str]:
    try:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
    except (OSError, UnicodeDecodeError, SyntaxError):
        return {}

    assignments: dict[str, ast.expr] = {}
    for stmt in tree.body:
        if isinstance(stmt, ast.Assign):
            for target in stmt.targets:
                if isinstance(target, ast.Name):
                    assignments[target.id] = stmt.value

    def _resolve(node: ast.expr) -> object:
        if isinstance(node, ast.Name) and node.id in assignments:
            return _resolve(assignments[node.id])
        try:
            return ast.literal_eval(node)
        except (ValueError, TypeError, SyntaxError):
            return None

    result: dict[str, str] = {}
    for call in (node for node in ast.walk(tree) if isinstance(node, ast.Call)):
        func_name = _attr_name(call.func).split(".")[-1]
        if func_name != "setup":
            continue
        entry_points_node = next(
            (kw.value for kw in call.keywords if kw.arg == "entry_points"),
            None,
        )
        if entry_points_node is None:
            continue
        raw = _resolve(entry_points_node)
        if not isinstance(raw, dict):
            continue
        scripts = raw.get("console_scripts")
        if not isinstance(scripts, (list, tuple)):
            continue
        for item in scripts:
            if not isinstance(item, str):
                continue
            parsed = _parse_entry_line(item)
            if parsed:
                result[parsed[0]] = parsed[1]
    return result


def _parse_setup_cfg(path: Path) -> dict[str, str]:
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read(path, encoding="utf-8")
    except (OSError, configparser.Error):
        return {}
    section = "options.entry_points"
    if not parser.has_section(section):
        return {}
    raw = parser.get(section, "console_scripts", fallback="")
    result: dict[str, str] = {}
    for line in raw.splitlines():
        parsed = _parse_entry_line(line.strip())
        if parsed:
            result[parsed[0]] = parsed[1]
    return result


_TOML_SECTION_RE = re.compile(r"^\s*\[([^]]+)\]\s*(?:#.*)?$")
_TOML_ASSIGN_RE = re.compile(
    r"^\s*(?P<key>(?:[A-Za-z0-9_.-]+)|(?:\"[^\"]+\")|(?:'[^']+'))\s*=\s*"
    r"(?P<value>(?:\"(?:[^\"\\]|\\.)*\")|(?:'[^']*'))\s*(?:#.*)?$"
)


def _parse_pyproject_scripts(path: Path) -> dict[str, str]:
    """Parse the simple string assignments allowed by ``[project.scripts]``.

    This intentionally avoids adding a TOML dependency solely for Python 3.10 support.
    Complex TOML constructs are left unresolved instead of guessed.
    """

    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return {}

    in_scripts = False
    result: dict[str, str] = {}
    for line in lines:
        section = _TOML_SECTION_RE.match(line)
        if section:
            in_scripts = section.group(1).strip() == "project.scripts"
            continue
        if not in_scripts:
            continue
        match = _TOML_ASSIGN_RE.match(line)
        if not match:
            continue
        try:
            raw_key = match.group("key")
            key = ast.literal_eval(raw_key) if raw_key[0] in "\"'" else raw_key
            value = ast.literal_eval(match.group("value"))
        except (ValueError, SyntaxError):
            continue
        if isinstance(key, str) and isinstance(value, str):
            result[key] = value
    return result


def _attr_name(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _attr_name(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    return ""
