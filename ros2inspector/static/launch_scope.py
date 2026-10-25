from __future__ import annotations

import ast
import copy
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from ros2inspector.model.schemas import DYNAMIC_SENTINEL, UNKNOWN_SENTINEL

if TYPE_CHECKING:
    from ros2inspector.static.launch_analyzer import LaunchGraph, LaunchInclude, LaunchNode


@dataclass
class _Value:
    kind: str = "uncertain"
    expression: ast.expr | None = None
    items: list[_Value] = field(default_factory=list)
    args: list[_Value] = field(default_factory=list)
    keywords: dict[str, _Value] = field(default_factory=dict)


@dataclass
class _Scope:
    namespace: str | None = None
    absolute: bool = False
    conditions: list[str] = field(default_factory=list)


def _name(expr: ast.expr) -> str:
    if isinstance(expr, ast.Name):
        return expr.id
    if isinstance(expr, ast.Attribute):
        return _name(expr.value) + "." + expr.attr
    return ""


def _text(value: _Value) -> str:
    return ast.unparse(value.expression) if value.expression is not None else "unresolved action"


def _literal(value: _Value | None) -> object:
    if value is not None and isinstance(value.expression, ast.Constant):
        return value.expression.value
    return None


def _condition(value: _Value | None) -> bool | None:
    if value is None:
        return True
    if value.kind not in {"IfCondition", "UnlessCondition"} or not value.args:
        return None
    literal = _literal(value.args[0])
    if not isinstance(literal, str):
        return None
    literal = literal.strip().lower()
    if literal not in {"true", "1", "false", "0"}:
        return None
    result = literal in {"true", "1"}
    return result if value.kind == "IfCondition" else not result


def _compose(parent: str | None, child: str | None) -> str | None:
    if child is not None and child.startswith("/"):
        return "/" + child.strip("/")
    if parent in {DYNAMIC_SENTINEL, UNKNOWN_SENTINEL} or child in {
        DYNAMIC_SENTINEL,
        UNKNOWN_SENTINEL,
    }:
        return DYNAMIC_SENTINEL
    if not parent:
        return child
    return "/" + "/".join(p.strip("/") for p in (parent, child) if p and p.strip("/"))


class _OrderedLaunch:
    def __init__(
        self,
        graph: LaunchGraph,
        nodes: dict[int, LaunchNode],
        includes: dict[int, LaunchInclude],
        package_paths: dict[str, Path],
    ) -> None:
        self.graph = graph
        self.nodes = {key: copy.deepcopy(value) for key, value in nodes.items()}
        self.includes = {key: copy.deepcopy(value) for key, value in includes.items()}
        self.package_paths = package_paths
        self.bindings: dict[str, _Value] = {}
        self.returned: list[_Value] = []
        self.descriptions: list[_Value] = []
        self.fallback: list[_Value] = []
        self.visited_nodes: set[int] = set()
        self.visited_includes: set[int] = set()
        self.result_nodes: list[LaunchNode] = []
        self.result_includes: list[LaunchInclude] = []

    def value(self, expr: ast.expr | None) -> _Value:
        if expr is None:
            return _Value()
        if isinstance(expr, ast.Name):
            return self.bindings.get(expr.id, _Value(expression=expr))
        if isinstance(expr, ast.Constant):
            return _Value("literal", expr)
        if isinstance(expr, (ast.List, ast.Tuple)):
            return _Value("list", expr, items=[self.value(item) for item in expr.elts])
        if isinstance(expr, ast.BinOp) and isinstance(expr.op, ast.Add):
            return _Value("concat", expr, args=[self.value(expr.left), self.value(expr.right)])
        if not isinstance(expr, ast.Call):
            return _Value(expression=expr)
        kind = _name(expr.func).rsplit(".", 1)[-1]
        value = _Value(kind, expr, args=[self.value(arg) for arg in expr.args])
        value.keywords = {kw.arg: self.value(kw.value) for kw in expr.keywords if kw.arg}
        if kind in {"LaunchDescription", "GroupAction"}:
            actions = (
                value.args[0]
                if value.args
                else value.keywords.get("actions", value.keywords.get("entities"))
            )
            value.items = list(actions.items) if actions and actions.kind == "list" else []
            if actions is not None and actions.kind != "list":
                value.items.append(_Value(expression=actions.expression))
            if kind == "LaunchDescription":
                self.descriptions.append(value)
        return value

    def _uncertain_effect(self, statement: ast.AST) -> None:
        affected = {
            id(value): value
            for expr in ast.walk(statement)
            if isinstance(expr, ast.Name)
            and (value := self.bindings.get(expr.id)) is not None
            and value.kind in {"LaunchDescription", "GroupAction", "list"}
        }
        for value in affected.values():
            value.items.append(_Value())

    def statements(self, statements: list[ast.stmt]) -> None:
        for statement in statements:
            if isinstance(statement, ast.Assign):
                value = self.value(statement.value)
                for target in statement.targets:
                    if isinstance(target, ast.Name):
                        self.bindings[target.id] = value
                self.fallback.append(value)
            elif isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name):
                value = self.value(statement.value)
                self.bindings[statement.target.id] = value
                self.fallback.append(value)
            elif isinstance(statement, ast.Return):
                self.returned.append(self.value(statement.value))
                return
            elif isinstance(statement, ast.Expr):
                expr = statement.value
                if isinstance(expr, ast.Call) and isinstance(expr.func, ast.Attribute):
                    receiver = self.value(expr.func.value)
                    if receiver.kind in {"LaunchDescription", "GroupAction", "list"}:
                        if expr.func.attr in {"add_action", "add_entity", "append"}:
                            receiver.items.append(
                                self.value(expr.args[0]) if expr.args else _Value()
                            )
                            continue
                        if expr.func.attr == "extend" and expr.args:
                            items = self.value(expr.args[0])
                            receiver.items.extend(
                                items.items if items.kind == "list" else [_Value()]
                            )
                            continue
                self._uncertain_effect(statement)
                self.fallback.append(self.value(expr))
            elif isinstance(statement, ast.If) and isinstance(statement.test, ast.Constant):
                self.statements(statement.body if statement.test.value else statement.orelse)
            elif not isinstance(
                statement, (ast.Import, ast.ImportFrom, ast.FunctionDef, ast.ClassDef)
            ):
                self._uncertain_effect(statement)

    def path(self, value: _Value | None) -> str | None:
        if value is None:
            return None
        literal = _literal(value)
        if isinstance(literal, str):
            return literal
        if value.kind in {
            "PythonLaunchDescriptionSource",
            "XMLLaunchDescriptionSource",
            "AnyLaunchDescriptionSource",
            "FrontendLaunchDescriptionSource",
        }:
            return self.path(value.args[0]) if value.args else None
        if value.kind in {"FindPackageShare", "get_package_share_directory"} and value.args:
            package = _literal(value.args[0])
            return (
                str(self.package_paths[package])
                if isinstance(package, str) and package in self.package_paths
                else None
            )
        if value.kind == "PathJoinSubstitution" and value.args:
            parts = [self.path(part) for part in value.args[0].items]
            return (
                str(Path(*[p for p in parts if p is not None]))
                if parts and all(p is not None for p in parts)
                else None
            )
        if (
            value.kind == "join"
            and isinstance(value.expression, ast.Call)
            and _name(value.expression.func) == "os.path.join"
        ):
            parts = [self.path(part) for part in value.args]
            return (
                str(Path(*[p for p in parts if p is not None]))
                if parts and all(p is not None for p in parts)
                else None
            )
        if value.kind in {"concat", "list"}:
            parts = [
                self.path(part) for part in (value.args if value.kind == "concat" else value.items)
            ]
            return (
                "".join(p for p in parts if p is not None)
                if all(p is not None for p in parts)
                else None
            )
        return None

    def _mark(self, message: str) -> None:
        self.graph.unresolved_branches = True
        diagnostic = {
            "severity": "warning",
            "code": "launch_scope_unresolved",
            "file": self.graph.source_file,
            "message": message,
        }
        if diagnostic not in self.graph.diagnostics:
            self.graph.diagnostics.append(diagnostic)

    def walk(self, value: _Value, scope: _Scope, presence: list[str]) -> _Scope:
        if value.kind in {"list", "LaunchDescription"}:
            for action in value.items:
                scope = self.walk(action, scope, presence)
            return scope
        condition = value.keywords.get("condition")
        truth = _condition(condition)
        if value.kind == "PushRosNamespace":
            if truth is False:
                return scope
            argument = value.args[0] if value.args else value.keywords.get("namespace")
            namespace = _literal(argument)
            if truth is None or not isinstance(namespace, str):
                evidence = [_text(condition)] if condition is not None else []
                self._mark("A namespace push could not be resolved: " + _text(value))
                return _Scope(DYNAMIC_SENTINEL, conditions=[*scope.conditions, *evidence])
            composed = _compose(scope.namespace, namespace)
            if composed and composed != DYNAMIC_SENTINEL:
                composed = "/" + composed.strip("/")
            absolute = namespace.startswith("/")
            return _Scope(
                composed, scope.absolute or absolute, [] if absolute else list(scope.conditions)
            )
        if value.kind == "GroupAction":
            scoped_value = value.keywords.get("scoped")
            scoped = True if scoped_value is None else _literal(scoped_value)
            forwarding_value = value.keywords.get("forwarding")
            forwarding = True if forwarding_value is None else _literal(forwarding_value)
            inner = copy.deepcopy(scope)
            if scoped is not False and forwarding is False:
                inner = _Scope(absolute=True)
            elif scoped is not False and forwarding is not True:
                inner = _Scope(DYNAMIC_SENTINEL, conditions=["unresolved forwarding"])
            conditions = presence + (
                [_text(condition)] if condition is not None and truth is not True else []
            )
            for action in value.items:
                inner = self.walk(action, inner, conditions)
            if truth is False:
                return scope
            if scoped is True:
                return scope
            if scoped is False and truth is True:
                return inner
            if scoped is False and inner.namespace == scope.namespace:
                return scope
            self._mark("Group scope is unresolved: " + _text(value))
            return _Scope(DYNAMIC_SENTINEL, conditions=conditions)
        expression_id = id(value.expression)
        if expression_id in self.nodes:
            record = copy.deepcopy(self.nodes[expression_id])
            self.visited_nodes.add(expression_id)
            for name in ("package", "executable", "name", "namespace"):
                literal = _literal(value.keywords.get(name))
                if isinstance(literal, str):
                    setattr(record, name, literal)
                    if name in record.unresolved_fields:
                        record.unresolved_fields.remove(name)
            own_namespace = record.namespace
            absolute = bool(own_namespace and own_namespace.startswith("/"))
            record.namespace = _compose(scope.namespace, own_namespace)
            record.namespace_absolute = absolute or scope.absolute
            record.conditions = list(
                dict.fromkeys(
                    presence
                    + ([_text(condition)] if condition is not None and truth is not True else [])
                    + ([] if absolute else scope.conditions)
                )
            )
            record.presence = (
                "conditional"
                if presence or condition is not None and truth is not True
                else "known"
            )
            if record.namespace == DYNAMIC_SENTINEL:
                if "namespace" not in record.unresolved_fields:
                    record.unresolved_fields.append("namespace")
                self._mark("Deployment namespace is unresolved")
            self.result_nodes.append(record)
            return scope
        if expression_id in self.includes:
            include_record = copy.deepcopy(self.includes[expression_id])
            self.visited_includes.add(expression_id)
            source = (
                value.args[0] if value.args else value.keywords.get("launch_description_source")
            )
            target = self.path(source)
            if target is not None:
                include_record.target_file = target
            include_record.namespace = scope.namespace
            include_record.namespace_absolute = scope.absolute
            include_record.namespace_conditions = list(scope.conditions)
            include_record.conditions = list(
                dict.fromkeys(
                    presence
                    + ([_text(condition)] if condition is not None and truth is not True else [])
                )
            )
            include_record.arguments_unresolved = "launch_arguments" in value.keywords
            include_record.unresolved = (
                target is None
                or "$(" in (target or "")
                or scope.namespace == DYNAMIC_SENTINEL
                or include_record.arguments_unresolved
            )
            if include_record.unresolved:
                self.graph.unresolved_branches = True
            self.result_includes.append(include_record)
            return scope
        if value.kind in {"LoadComposableNodes", "ComposableNodeContainer"}:
            for argument in [*value.args, *value.keywords.values()]:
                self.walk(
                    argument,
                    scope,
                    presence + ([_text(condition)] if condition is not None else []),
                )
            return scope
        if value.kind in {"uncertain", "OpaqueFunction"} or isinstance(value.expression, ast.Call):
            self._mark("Launch actions could not be followed in source order")
            return _Scope(DYNAMIC_SENTINEL, conditions=[*scope.conditions, "unresolved action"])
        return scope

    def apply(self, tree: ast.Module) -> None:
        self.statements(tree.body)
        generate = next(
            (
                stmt
                for stmt in tree.body
                if isinstance(stmt, ast.FunctionDef) and stmt.name == "generate_launch_description"
            ),
            None,
        )
        if generate is not None:
            self.statements(generate.body)
        nested: set[int] = set()

        def collect(value: _Value) -> None:
            for child in [*value.items, *value.args, *value.keywords.values()]:
                if id(child) not in nested:
                    nested.add(id(child))
                    collect(child)

        for value in self.fallback:
            collect(value)
        roots = (
            self.returned
            or self.descriptions
            or [value for value in self.fallback if id(value) not in nested]
        )
        for root in roots:
            self.walk(root, _Scope(), [])
        for key, record in self.nodes.items():
            if key not in self.visited_nodes:
                record.namespace = DYNAMIC_SENTINEL
                record.unresolved_fields = list(
                    dict.fromkeys([*record.unresolved_fields, "namespace"])
                )
                record.presence = "conditional"
                self.result_nodes.append(record)
                self._mark("A node belongs to launch actions that could not be followed")
        for key, include_record in self.includes.items():
            if key not in self.visited_includes:
                include_record.unresolved = True
                include_record.namespace = DYNAMIC_SENTINEL
                self.result_includes.append(include_record)
        self.graph.unresolved_branches = bool(
            self.graph.diagnostics
            or any(n.is_unresolved for n in self.result_nodes)
            or any(i.unresolved for i in self.result_includes)
        )
        self.graph.nodes = self.result_nodes
        self.graph.includes = self.result_includes


def apply_python_namespaces(
    tree: ast.Module,
    graph: LaunchGraph,
    nodes: dict[int, LaunchNode],
    includes: dict[int, LaunchInclude],
    package_paths: dict[str, Path],
) -> None:
    _OrderedLaunch(graph, nodes, includes, package_paths).apply(tree)
