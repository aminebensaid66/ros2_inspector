"""Generated packages must import, run and analyse correctly for every supported shape.

The designer's own tests check single cases. These tests run a matrix of node labels and
interface types through the generator and then *execute* the output against stub ``rclpy``,
``launch`` and interface modules, so a name that shadows another (a node called ``string``
next to ``std_msgs/String``) fails here even though ``compile()`` accepts it.
"""

from __future__ import annotations

import importlib.abc
import importlib.machinery
import itertools
import sys
import types
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from ros2inspector.design.codegen import (
    generate_package_result,
    read_manifest,
    render_node,
    render_package,
)
from ros2inspector.design.models import (
    KNOWN_INTERFACES,
    RESERVED_NODE_LABELS,
    NodeSpec,
    PackageSpec,
)
from ros2inspector.model.uam import UAM
from ros2inspector.static.package_xml import parse_package_xml

_GROUPS = ("publishers", "subscribers", "services", "clients", "action_servers", "action_clients")
_GROUP_KIND = {
    "publishers": "msg",
    "subscribers": "msg",
    "services": "srv",
    "clients": "srv",
    "action_servers": "action",
    "action_clients": "action",
}


class _InterfaceFinder(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    """Fabricate ``<pkg>``, ``<pkg>.msg``, ``<pkg>.srv`` and ``<pkg>.action`` on import."""

    def __init__(self, packages: set[str]) -> None:
        self.packages = packages

    def find_spec(self, fullname: str, path: object, target: object = None) -> Any:
        root = fullname.split(".")[0]
        if root not in self.packages:
            return None
        return importlib.machinery.ModuleSpec(fullname, self, is_package=True)

    def create_module(self, spec: Any) -> types.ModuleType:
        return _InterfaceModule(spec.name)

    def exec_module(self, module: types.ModuleType) -> None:
        return None


class _InterfaceModule(types.ModuleType):
    def __getattr__(self, name: str) -> type:
        if name.startswith("__"):
            raise AttributeError(name)
        cls = _interface_class(self.__name__, name)
        setattr(self, name, cls)
        return cls


def _interface_class(module: str, name: str) -> type:
    nested = {
        "Request": type("Request", (), {}),
        "Response": type("Response", (), {}),
        "Result": type("Result", (), {}),
        "Goal": type("Goal", (), {}),
        "Feedback": type("Feedback", (), {}),
        "__module__": module,
    }
    return type(name, (), nested)


@pytest.fixture
def ros(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Stub rclpy, launch, launch_ros and every interface package."""
    calls: dict[str, Any] = {"endpoints": [], "spun": [], "launch": [], "timers": 0}

    class FakeNode:
        def __init__(self, name: str) -> None:
            self.node_name = name
            calls["node_name"] = name

        def _record(self, kind: str, cls: type, name: str) -> object:
            calls["endpoints"].append((kind, cls, name))
            return types.SimpleNamespace(
                publish=lambda msg: calls.setdefault("sent", []).append(msg)
            )

        def create_publisher(self, cls: type, topic: str, qos: int) -> object:
            return self._record("pub", cls, topic)

        def create_subscription(self, cls: type, topic: str, cb: object, qos: int) -> object:
            assert callable(cb)
            return self._record("sub", cls, topic)

        def create_service(self, cls: type, name: str, cb: object) -> object:
            assert callable(cb)
            return self._record("srv", cls, name)

        def create_client(self, cls: type, name: str) -> object:
            return self._record("cli", cls, name)

        def create_timer(self, period: float, cb: object) -> object:
            assert period > 0 and callable(cb)
            calls["timers"] += 1
            return object()

        def get_logger(self) -> Any:
            return types.SimpleNamespace(info=lambda *_: None)

        def destroy_node(self) -> None:
            calls["destroyed"] = True

    def action_server(node: object, cls: type, name: str, cb: object) -> object:
        assert isinstance(node, FakeNode) and callable(cb)
        calls["endpoints"].append(("action_server", cls, name))
        return object()

    def action_client(node: object, cls: type, name: str) -> object:
        assert isinstance(node, FakeNode)
        calls["endpoints"].append(("action_client", cls, name))
        return object()

    def module(name: str, **attrs: object) -> None:
        mod = types.ModuleType(name)
        mod.__dict__.update(attrs)
        monkeypatch.setitem(sys.modules, name, mod)

    def spin(node: object) -> None:
        calls["spun"].append(node)
        raise KeyboardInterrupt

    module(
        "rclpy",
        init=lambda args=None: calls.__setitem__("init_args", args),
        spin=spin,
        try_shutdown=lambda: calls.__setitem__("shutdown", True),
    )
    module("rclpy.node", Node=FakeNode)
    module("rclpy.action", ActionServer=action_server, ActionClient=action_client)
    module("launch", LaunchDescription=lambda actions: ("LaunchDescription", actions))
    module("launch_ros")
    module(
        "launch_ros.actions",
        Node=lambda **kwargs: calls["launch"].append(kwargs) or kwargs,
    )
    packages = {name.split("/")[0] for names in KNOWN_INTERFACES.values() for name in names}
    packages |= {"my_msgs", "other_msgs", "custom_interfaces"}
    for name in list(sys.modules):
        if name.split(".")[0] in packages:
            monkeypatch.delitem(sys.modules, name)
    finder = _InterfaceFinder(packages)
    monkeypatch.setattr(sys, "meta_path", [finder, *sys.meta_path])
    return calls


def _endpoint(eid: str, name: str, interface: str) -> dict[str, str]:
    return {"id": eid, "name": name, "msgType": interface}


def _node(label: str, nid: str = "n1", **groups: list[dict[str, str]]) -> dict[str, Any]:
    node: dict[str, Any] = {"id": nid, "label": label}
    for group in _GROUPS:
        node[group] = groups.get(group, [])
    return node


def _execute(source: str, filename: str = "node.py") -> types.ModuleType:
    mod = types.ModuleType("generated")
    exec(compile(source, filename, "exec"), mod.__dict__)  # noqa: S102
    return mod


def _run_node(source: str) -> types.ModuleType:
    """Run the module's main(): init, construct, spin (stub raises Ctrl+C), shut down."""
    mod = _execute(source)
    mod.main()
    return mod


def _type_name(interface: str) -> tuple[str, str]:
    parts = interface.split("/")
    return parts[0], parts[-1]


def _assert_endpoints(calls: dict[str, Any], expected: list[tuple[str, str, str]]) -> None:
    got = sorted(
        (kind, cls.__module__.split(".")[0] + "/" + cls.__name__, name)
        for kind, cls, name in calls["endpoints"]
    )
    assert got == sorted(expected)


# ── every known interface binds the real interface class ────────────────────


_ALL_KNOWN = [(kind, name) for kind, names in KNOWN_INTERFACES.items() for name in names]


@pytest.mark.parametrize(("kind", "interface"), _ALL_KNOWN, ids=[n for _, n in _ALL_KNOWN])
def test_every_known_interface_runs_with_a_shadowing_node_label(
    ros: dict[str, Any], kind: str, interface: str
) -> None:
    package, type_name = _type_name(interface)
    # A label whose PascalCase equals the type name is the worst case for shadowing.
    label = "".join("_" + c.lower() if c.isupper() else c for c in type_name).lstrip("_")
    label = label.replace("2", "_2").replace("__", "_")
    if label in RESERVED_NODE_LABELS:
        # e.g. 'string' (std_msgs/String): rejected outright because string.py would shadow
        # the stdlib module; the class-shadowing case is covered by the other interfaces.
        with pytest.raises(ValidationError):
            NodeSpec.model_validate(_node(label))
        return
    groups = {
        "msg": ("publishers", "subscribers"),
        "srv": ("services", "clients"),
        "action": ("action_servers", "action_clients"),
    }[kind]
    node = NodeSpec.model_validate(
        _node(
            label,
            **{
                groups[0]: [_endpoint("a", "/out_name", interface)],
                groups[1]: [_endpoint("b", "/in_name", interface)],
            },
        )
    )
    source = render_node(node)
    _run_node(source)
    short = {
        "msg": ("pub", "sub"),
        "srv": ("srv", "cli"),
        "action": ("action_server", "action_client"),
    }
    _assert_endpoints(
        ros,
        [
            (short[kind][0], f"{package}/{type_name}", "/out_name"),
            (short[kind][1], f"{package}/{type_name}", "/in_name"),
        ],
    )
    assert ros["spun"] and ros.get("shutdown") and ros.get("destroyed")


# ── labels that collide with imports or rclpy names ─────────────────────────


@pytest.mark.parametrize(
    "label",
    ["node", "main", "action_server", "action_client", "image", "twist", "header", "odometry"],
)
def test_colliding_labels_still_bind_rclpy_and_interfaces(ros: dict[str, Any], label: str) -> None:
    node = NodeSpec.model_validate(
        _node(
            label,
            publishers=[_endpoint("p", "/chatter", "std_msgs/String")],
            subscribers=[_endpoint("s", "/image", "sensor_msgs/Image")],
            action_servers=[_endpoint("a", "/fib", "example_interfaces/Fibonacci")],
            action_clients=[_endpoint("c", "/nav", "nav2_msgs/NavigateToPose")],
        )
    )
    mod = _run_node(render_node(node))
    _assert_endpoints(
        ros,
        [
            ("pub", "std_msgs/String", "/chatter"),
            ("sub", "sensor_msgs/Image", "/image"),
            ("action_server", "example_interfaces/Fibonacci", "/fib"),
            ("action_client", "nav2_msgs/NavigateToPose", "/nav"),
        ],
    )
    assert isinstance(mod.rclpy, types.ModuleType)
    assert mod.Node is sys.modules["rclpy.node"].Node


def test_same_type_name_in_three_packages_and_two_kinds(ros: dict[str, Any]) -> None:
    node = NodeSpec.model_validate(
        _node(
            "bridge",
            publishers=[
                _endpoint("p1", "/a", "std_msgs/String"),
                _endpoint("p2", "/b", "my_msgs/String"),
                _endpoint("p3", "/c", "other_msgs/msg/String"),
            ],
            services=[_endpoint("s1", "/srv", "custom_interfaces/srv/String")],
        )
    )
    _run_node(render_node(node))
    _assert_endpoints(
        ros,
        [
            ("pub", "std_msgs/String", "/a"),
            ("pub", "my_msgs/String", "/b"),
            ("pub", "other_msgs/String", "/c"),
            ("srv", "custom_interfaces/String", "/srv"),
        ],
    )


def test_interface_named_like_rclpy_symbols_is_aliased(ros: dict[str, Any]) -> None:
    node = NodeSpec.model_validate(
        _node(
            "worker",
            publishers=[_endpoint("p1", "/n", "my_msgs/Node")],
            action_clients=[_endpoint("a1", "/x", "my_msgs/action/ActionClient")],
        )
    )
    mod = _run_node(render_node(node))
    assert mod.Node is sys.modules["rclpy.node"].Node
    _assert_endpoints(
        ros, [("pub", "my_msgs/Node", "/n"), ("action_client", "my_msgs/ActionClient", "/x")]
    )


# ── endpoint names ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "names",
    [
        ["/a/b", "/a_b", "a_b"],  # three topics that map to the same variable
        ["~/private", "/private"],
        ["relative/name", "/abs/name"],
        ["/x" * 30],  # long but valid
    ],
)
def test_endpoint_variable_names_never_collide(ros: dict[str, Any], names: list[str]) -> None:
    publishers = [_endpoint(f"p{i}", name, "std_msgs/String") for i, name in enumerate(names)]
    subscribers = [_endpoint(f"s{i}", name, "std_msgs/String") for i, name in enumerate(names)]
    node = NodeSpec.model_validate(_node("multi", publishers=publishers, subscribers=subscribers))
    mod = _execute(render_node(node))
    instance = mod.Multi()
    _assert_endpoints(
        ros,
        [("pub", "std_msgs/String", n) for n in names]
        + [("sub", "std_msgs/String", n) for n in names],
    )
    # Every subscription callback and the timer callback can be invoked.
    for attr in dir(instance):
        if attr.startswith("cb_"):
            getattr(instance, attr)(object())
    instance.timer_callback()
    assert len(ros["sent"]) == len(names)


def test_service_and_action_handlers_return_their_response_types(ros: dict[str, Any]) -> None:
    node = NodeSpec.model_validate(
        _node(
            "server",
            services=[_endpoint("s", "/add", "example_interfaces/AddTwoInts")],
            action_servers=[_endpoint("a", "/fib", "example_interfaces/Fibonacci")],
        )
    )
    mod = _execute(render_node(node))
    instance = mod.Server()
    srv_cls = sys.modules["example_interfaces.srv"].AddTwoInts
    response = srv_cls.Response()
    assert instance.handle_add(srv_cls.Request(), response) is response

    succeeded: list[bool] = []
    handle = types.SimpleNamespace(succeed=lambda: succeeded.append(True))
    result = instance.execute_fib(handle)
    assert isinstance(result, sys.modules["example_interfaces.action"].Fibonacci.Result)
    assert succeeded == [True]
    # A goal handle without succeed() (older rclpy test doubles) must not crash.
    instance.execute_fib(object())


def test_node_without_endpoints_has_no_timer(ros: dict[str, Any]) -> None:
    _run_node(render_node(NodeSpec.model_validate(_node("idle"))))
    assert ros["timers"] == 0
    assert ros["node_name"] == "idle"


# ── package-level files ─────────────────────────────────────────────────────


def _package(name: str = "demo_pkg") -> PackageSpec:
    return PackageSpec.model_validate(
        {
            "id": "p1",
            "pkg_name": name,
            "nodes": [
                _node("talker", "n1", publishers=[_endpoint("t1", "/chatter", "std_msgs/String")]),
                _node(
                    "listener",
                    "n2",
                    subscribers=[_endpoint("t2", "/chatter", "std_msgs/String")],
                    clients=[_endpoint("t3", "/trigger", "std_srvs/Trigger")],
                ),
            ],
            "edges": [{"id": "e1", "fn": "n1", "ft": "t1", "tn": "n2", "tt": "t2"}],
        }
    )


def test_setup_py_registers_every_node_and_installs_launch_file(
    ros: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, Any] = {}
    monkeypatch.setitem(
        sys.modules, "setuptools", types.SimpleNamespace(setup=lambda **kw: captured.update(kw))
    )
    files = render_package(_package())
    _execute(files["setup.py"], "setup.py")
    assert captured["name"] == "demo_pkg"
    assert captured["packages"] == ["demo_pkg"]
    assert sorted(captured["entry_points"]["console_scripts"]) == [
        "listener = demo_pkg.listener:main",
        "talker = demo_pkg.talker:main",
    ]
    installed = {dest: srcs for dest, srcs in captured["data_files"]}
    assert installed["share/demo_pkg/launch"] == ["launch/launch.py"]
    assert installed["share/ament_index/resource_index/packages"] == ["resource/demo_pkg"]
    for sources in installed.values():
        for source in sources:
            assert source in files, f"{source} is installed but not generated"


def test_launch_file_starts_every_node(ros: dict[str, Any]) -> None:
    files = render_package(_package())
    mod = _execute(files["launch/launch.py"], "launch.py")
    description = mod.generate_launch_description()
    assert description[0] == "LaunchDescription"
    assert [(n["package"], n["executable"], n["name"]) for n in ros["launch"]] == [
        ("demo_pkg", "talker", "talker"),
        ("demo_pkg", "listener", "listener"),
    ]


def test_package_xml_is_parsed_by_ros2inspector(tmp_path: Path) -> None:
    generate_package_result(_package(), tmp_path)
    meta = parse_package_xml(tmp_path / "demo_pkg" / "package.xml")
    assert meta is not None
    assert meta.name == "demo_pkg"
    assert meta.package_type.value == "ament_python"
    deps = {dep for values in meta.dependencies.values() for dep in values}
    assert {"rclpy", "std_msgs", "std_srvs", "launch", "launch_ros"} <= deps


def test_generated_package_is_analysed_like_a_hand_written_one(tmp_path: Path) -> None:
    """Dog-food: the static analyser finds every node and topic the designer generated."""
    ws = tmp_path / "ws"
    generate_package_result(_package(), ws / "src")
    uam = UAM.build(ws, use_cache=False)
    nodes = {node.declared_ros_name: node for node in uam.nodes()}
    assert set(nodes) == {"talker", "listener"}
    assert {node.package for node in nodes.values()} == {"demo_pkg"}
    assert [(e.name, e.msg_type) for e in nodes["talker"].publishers] == [
        ("/chatter", "std_msgs/String")
    ]
    assert [(e.name, e.msg_type) for e in nodes["listener"].subscriptions] == [
        ("/chatter", "std_msgs/String")
    ]
    assert [e.name for e in nodes["listener"].clients] == ["/trigger"]


def test_manifest_hashes_match_files_on_disk(tmp_path: Path) -> None:
    import hashlib

    generate_package_result(_package(), tmp_path)
    manifest = read_manifest(tmp_path / "demo_pkg")
    assert manifest
    for relative, digest in manifest.items():
        data = (tmp_path / "demo_pkg" / relative).read_bytes()
        assert hashlib.sha256(data).hexdigest() == digest


# ── combinatorial smoke: every group combination renders and runs ──────────


_COMBOS = [
    combo for size in range(len(_GROUPS) + 1) for combo in itertools.combinations(_GROUPS, size)
]


@pytest.mark.parametrize("groups", _COMBOS, ids=["+".join(c) or "empty" for c in _COMBOS])
def test_every_combination_of_endpoint_groups_runs(
    ros: dict[str, Any], groups: tuple[str, ...]
) -> None:
    sample = {
        "msg": "geometry_msgs/Twist",
        "srv": "std_srvs/SetBool",
        "action": "control_msgs/FollowJointTrajectory",
    }
    spec = _node(
        "combo",
        **{
            group: [_endpoint(f"{group}{i}", f"/{group}_{i}", sample[_GROUP_KIND[group]])]
            for i, group in enumerate(groups)
        },
    )
    _run_node(render_node(NodeSpec.model_validate(spec)))
    assert len(ros["endpoints"]) == len(groups)
    assert ros["timers"] == (1 if "publishers" in groups else 0)
