from __future__ import annotations

import ast
import configparser
import io
import re
import tokenize
from xml.parsers import expat

import yaml
from yaml.nodes import MappingNode, Node, ScalarNode, SequenceNode


def _tree(source: str) -> ast.Module:
    try:
        return ast.parse(source)
    except (SyntaxError, RecursionError, MemoryError) as exc:
        raise ValueError("the Python source cannot be parsed") from exc


def _position(source: str, line: int, column: int) -> int:
    lines = source.splitlines(keepends=True)
    return sum(len(s) for s in lines[: line - 1]) + len(
        lines[line - 1].encode("utf-8")[:column].decode("utf-8")
    )


def _apply(source: str, edits: list[tuple[int, int, str]]) -> str:
    previous = len(source)
    for start, end, replacement in sorted(set(edits), reverse=True):
        if end > previous:
            raise ValueError("overlapping source edits are ambiguous")
        source = source[:start] + replacement + source[end:]
        previous = start
    return source


def _quoted(original: str, value: str) -> str:
    tokens = list(tokenize.generate_tokens(io.StringIO(original).readline))
    strings = [token for token in tokens if token.type == tokenize.STRING]
    if len(strings) != 1 or strings[0].string != original:
        raise ValueError("the name is not one plain string literal")
    match = re.match(r"(?i:([ru]*))('''|\"\"\"|'|\")", original)
    if match is None:
        raise ValueError("the string literal has an unsupported prefix")
    prefix, quote = match.groups()
    return prefix + quote + value + quote


def replace_constants(source: str, replacements: list[tuple[ast.Constant, str]]) -> str:
    edits = []
    for constant, value in replacements:
        if constant.end_lineno is None or constant.end_col_offset is None:
            raise ValueError("the string literal has no source span")
        start = _position(source, constant.lineno, constant.col_offset)
        end = _position(source, constant.end_lineno, constant.end_col_offset)
        edits.append((start, end, _quoted(source[start:end], value)))
    return _apply(source, edits)


def rename_constructor(source: str, old: str, new: str, expected_class: str) -> str:
    tree = _tree(source)
    classes = [node for node in tree.body if isinstance(node, ast.ClassDef)]
    candidates = [node for node in classes if node.name == expected_class]
    if len(candidates) != 1:
        called = {
            call.func.id
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "main"
            for call in ast.walk(node)
            if isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
        }
        candidates = [node for node in classes if node.name in called]
    if len(candidates) != 1:
        raise ValueError("the node class cannot be identified uniquely")
    methods = [
        node
        for node in candidates[0].body
        if isinstance(node, ast.FunctionDef) and node.name == "__init__"
    ]
    calls = []
    pending: list[ast.AST] = [stmt for method in methods for stmt in method.body]
    while pending:
        node = pending.pop()
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "__init__"
            and isinstance(node.func.value, ast.Call)
            and isinstance(node.func.value.func, ast.Name)
            and node.func.value.func.id == "super"
        ):
            calls.append(node)
        pending.extend(ast.iter_child_nodes(node))
    if len(calls) != 1:
        raise ValueError("the constructor must have exactly one super().__init__ call")
    call = calls[0]
    name = (
        call.args[0]
        if call.args
        else next((kw.value for kw in call.keywords if kw.arg == "node_name"), None)
    )
    if not isinstance(name, ast.Constant) or name.value != old:
        raise ValueError(f"the constructor name is not the literal {old!r}")
    return replace_constants(source, [(name, new)])


def console_script_list(tree: ast.AST) -> ast.List:
    calls = [
        call
        for call in ast.walk(tree)
        if isinstance(call, ast.Call)
        and isinstance(call.func, ast.Name)
        and call.func.id == "setup"
    ]
    if len(calls) != 1:
        raise ValueError("the setup() call cannot be identified uniquely")
    listings = [
        value
        for kw in calls[0].keywords
        if kw.arg == "entry_points"
        for node in ast.walk(kw.value)
        if isinstance(node, ast.Dict)
        for key, value in zip(node.keys, node.values, strict=True)
        if isinstance(key, ast.Constant) and key.value == "console_scripts"
    ]
    if len(listings) != 1 or not isinstance(listings[0], ast.List):
        raise ValueError("the console_scripts list cannot be identified uniquely")
    return listings[0]


def rename_script_entries(source: str, renamed: dict[str, str]) -> str:
    listing = console_script_list(_tree(source))
    replacements = []
    expected = {}
    for previous, replacement in renamed.items():
        old_executable, separator, old_target = previous.partition("=")
        if not separator:
            raise ValueError("invalid old console-script registration")
        expected[(old_executable.strip(), old_target.strip())] = replacement

    for entry in listing.elts:
        if not isinstance(entry, ast.Constant) or not isinstance(entry.value, str):
            continue
        executable, separator, destination = entry.value.partition("=")
        if not separator:
            continue
        updated_entry = expected.get((executable.strip(), destination.strip()))
        if updated_entry is not None:
            replacements.append((entry, updated_entry))
    return replace_constants(source, replacements)


def _node_calls(tree: ast.AST) -> list[ast.Call]:
    aliases = {"Node": "launch_ros.actions.Node"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            for name in node.names:
                aliases[name.asname or name.name] = node.module + "." + name.name
        elif isinstance(node, ast.Import):
            for name in node.names:
                if name.asname:
                    aliases[name.asname] = name.name

    def canonical(expr: ast.expr) -> str:
        if isinstance(expr, ast.Name):
            return aliases.get(expr.id, expr.id)
        if isinstance(expr, ast.Attribute):
            return canonical(expr.value) + "." + expr.attr
        return ""

    calls = []
    for call in ast.walk(tree):
        if not isinstance(call, ast.Call):
            continue
        if canonical(call.func) == "launch_ros.actions.Node":
            calls.append(call)
        elif any(kw.arg == "executable" for kw in call.keywords):
            raise ValueError("a launch executable belongs to an unresolved action")
    return calls


def rename_launch_nodes(
    source: str, renamed: dict[str, str], package_name: str | None = None
) -> tuple[str, set[str]]:
    tree = _tree(source)
    replacements = []
    started = set()
    for call in _node_calls(tree):
        keywords = {kw.arg: kw.value for kw in call.keywords}
        executable = keywords.get("executable")
        package = keywords.get("package")
        # A foreign launch action does not belong to this managed package,
        # even when its executable is a dynamic expression.
        if (
            package_name is not None
            and isinstance(package, ast.Constant)
            and isinstance(package.value, str)
            and package.value != package_name
        ):
            continue
        if not isinstance(executable, ast.Constant) or not isinstance(executable.value, str):
            raise ValueError("the managed launch Node executable is not a literal")
        if package_name is not None and executable.value in renamed:
            if not isinstance(package, ast.Constant) or not isinstance(package.value, str):
                raise ValueError("the launch Node package cannot be identified safely")
        if package_name is not None and (
            not isinstance(package, ast.Constant) or package.value != package_name
        ):
            continue
        result = renamed.get(executable.value, executable.value)
        if result != executable.value:
            replacements.append((executable, result))
            # Preserve intentional runtime-name overrides. Only the generated
            # default name follows the renamed executable.
            runtime_name = keywords.get("name")
            if (
                isinstance(runtime_name, ast.Constant)
                and runtime_name.value == executable.value
            ):
                replacements.append((runtime_name, result))
        started.add(result)
    return replace_constants(source, replacements), started


def _setup_package(source: str, old: str, new: str) -> str:
    tree = _tree(source)
    calls = [
        call
        for call in ast.walk(tree)
        if isinstance(call, ast.Call)
        and isinstance(call.func, ast.Name)
        and call.func.id == "setup"
    ]
    if len(calls) != 1:
        raise ValueError("the setup() call cannot be identified uniquely")
    replacements = []
    identity = next((kw.value for kw in calls[0].keywords if kw.arg == "name"), None)
    if isinstance(identity, ast.Name):
        assignments = [
            stmt.value
            for stmt in tree.body
            if isinstance(stmt, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == identity.id for target in stmt.targets
            )
        ]
        identity = assignments[0] if len(assignments) == 1 else None
    if not isinstance(identity, ast.Constant) or identity.value not in {old, new}:
        raise ValueError("the setup package name is not one literal or literal assignment")
    if identity.value == old:
        replacements.append((identity, new))
    for kw in calls[0].keywords:
        if kw.arg not in {"name", "packages", "data_files", "entry_points"}:
            continue
        entry_values: ast.AST = kw.value
        if kw.arg == "entry_points":
            entry_values = console_script_list(tree)
        for constant in ast.walk(entry_values):
            if not isinstance(constant, ast.Constant) or not isinstance(constant.value, str):
                continue
            value = constant.value
            updated = value
            if kw.arg == "packages" and (value == old or value.startswith(old + ".")):
                updated = new + value[len(old) :]
            elif kw.arg == "data_files":
                for prefix in ("share/", "resource/"):
                    if value == prefix + old or value.startswith(prefix + old + "/"):
                        updated = prefix + new + value[len(prefix + old) :]
            elif kw.arg == "entry_points" and "=" in value:
                executable, separator, destination = value.partition("=")
                leading = destination[: len(destination) - len(destination.lstrip())]
                destination = destination.lstrip()
                if destination.startswith(old + "."):
                    updated = executable + separator + leading + new + destination[len(old) :]
            if updated != value:
                replacements.append((constant, updated))
    return replace_constants(source, replacements)


def _setup_config(source: str, old: str, new: str) -> str:
    try:
        config = configparser.ConfigParser(interpolation=None)
        config.read_string(source)
        for config_section, config_key in (
            ("develop", "script_dir"),
            ("install", "install_scripts"),
        ):
            if config.has_option(config_section, config_key) and config.get(
                config_section, config_key
            ).strip() not in {
                "$base/lib/" + old,
                "$base/lib/" + new,
            }:
                raise ValueError(f"the {config_section}.{config_key} path cannot be changed safely")
    except configparser.Error as exc:
        raise ValueError("the setup configuration cannot be parsed") from exc
    section = ""
    result = []
    for line in source.splitlines(keepends=True):
        header = re.match(r"\s*\[([^]]+)\]", line)
        if header:
            section = header.group(1)
        key = (
            "script_dir"
            if section == "develop"
            else "install_scripts"
            if section == "install"
            else None
        )
        if key is not None:
            match = re.match(rf"\s*{key}\s*=\s*(\$base/lib/{re.escape(old)})(?=\s|$)", line)
            if match:
                start, end = match.span(1)
                line = line[:start] + "$base/lib/" + new + line[end:]
        result.append(line)
    return "".join(result)


def _xml_package(source: str, old: str, new: str, manifest: bool) -> str:
    data = source.encode("utf-8")
    parser = expat.ParserCreate()
    edits: list[tuple[int, int, str]] = []
    depth = 0
    name_start: int | None = None

    def start(tag: str, attrs: dict[str, str]) -> None:
        nonlocal depth, name_start
        depth += 1
        index = parser.CurrentByteIndex
        end = index
        quote = 0
        while end < len(data):
            char = data[end]
            if quote:
                if char == quote:
                    quote = 0
            elif char in (34, 39):
                quote = char
            elif char == 62:
                end += 1
                break
            end += 1
        if manifest and tag == "name" and depth == 2:
            name_start = end
        if not manifest and tag in {"node", "composable_node"} and attrs.get("pkg") == old:
            opening = data[index:end].decode("utf-8")
            match = next(
                (
                    m
                    for m in re.finditer(r"([\w:.-]+)\s*=\s*(['\"])(.*?)\2", opening)
                    if m.group(1) == "pkg"
                ),
                None,
            )
            if match is None or match.group(3) != old:
                raise ValueError("the XML package attribute is ambiguous")
            begin, finish = match.span(3)
            prefix = len(data[:index].decode("utf-8"))
            edits.append((prefix + begin, prefix + finish, new))

    def end(tag: str) -> None:
        nonlocal depth, name_start
        if manifest and tag == "name" and depth == 2 and name_start is not None:
            content = data[name_start : parser.CurrentByteIndex].decode("utf-8")
            if content.strip() != old:
                raise ValueError("the package name is not a plain literal")
            prefix = len(data[:name_start].decode("utf-8"))
            start = prefix + len(content) - len(content.lstrip())
            edits.append((start, start + len(old), new))
            name_start = None
        depth -= 1

    parser.StartElementHandler = start
    parser.EndElementHandler = end
    try:
        parser.Parse(data, True)
    except expat.ExpatError as exc:
        raise ValueError("the XML source cannot be parsed") from exc
    if manifest and len(edits) != 1:
        raise ValueError("the package name cannot be identified uniquely")
    return _apply(source, edits)


def _yaml_package(source: str, old: str, new: str) -> str:
    try:
        tree = yaml.compose(source)
    except yaml.YAMLError as exc:
        raise ValueError("the YAML launch source cannot be parsed") from exc
    edits: list[tuple[int, int, str]] = []
    visited: set[int] = set()

    def walk(node: Node, in_node: bool = False) -> None:
        if id(node) in visited:
            return
        visited.add(id(node))
        if isinstance(node, MappingNode):
            for key, value in node.value:
                if not isinstance(key, ScalarNode):
                    continue
                if (
                    in_node
                    and key.value == "pkg"
                    and isinstance(value, ScalarNode)
                    and value.value == old
                ):
                    start, end = value.start_mark.index, value.end_mark.index
                    raw = source[start:end]
                    edits.append(
                        (start, end, _quoted(raw, new) if raw.startswith(("'", '"')) else new)
                    )
                else:
                    walk(value, key.value in {"node", "composable_node"})
        elif isinstance(node, SequenceNode):
            for value in node.value:
                walk(value)

    if tree is not None:
        walk(tree)
    return _apply(source, edits)


def rename_package_text(relative: str, source: str, old: str, new: str) -> str:
    if relative == "setup.py":
        return _setup_package(source, old, new)
    if relative == "setup.cfg":
        return _setup_config(source, old, new)
    if relative == "package.xml":
        return _xml_package(source, old, new, True)
    if relative.endswith(".py"):
        tree = _tree(source)
        replacements = [
            (kw.value, new)
            for call in _node_calls(tree)
            for kw in call.keywords
            if kw.arg == "package" and isinstance(kw.value, ast.Constant) and kw.value.value == old
        ]
        return replace_constants(source, replacements)
    if relative.endswith(".xml"):
        return _xml_package(source, old, new, False)
    if relative.endswith((".yaml", ".yml")):
        return _yaml_package(source, old, new)
    raise ValueError("the metadata format is unsupported")
