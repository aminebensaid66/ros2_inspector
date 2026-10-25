from __future__ import annotations

import keyword
import re
import sys
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_PACKAGE_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_IDENTIFIER_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
# ROS 2 topic/service/action names: tokens of [A-Za-z0-9_] that do not start with a digit,
# separated by single '/', optionally absolute ('/') or private ('~/'), no trailing '/'.
_ROS_NAME_RE = re.compile(r"^(?:/|~/)?[A-Za-z_][A-Za-z0-9_]*(?:/[A-Za-z_][A-Za-z0-9_]*)*$")
# 'pkg/Type' or the canonical 'pkg/msg/Type', 'pkg/srv/Type', 'pkg/action/Type'.
_INTERFACE_RE = re.compile(r"^([a-z][a-z0-9_]*)/(?:(msg|srv|action)/)?([A-Z][A-Za-z0-9_]*)$")

InterfaceKind = Literal["msg", "srv", "action"]

# Interfaces offered in the designer. The UI also accepts any other ``pkg/Type``.
KNOWN_INTERFACES: dict[InterfaceKind, tuple[str, ...]] = {
    "msg": (
        "std_msgs/String",
        "std_msgs/Bool",
        "std_msgs/Int32",
        "std_msgs/Int64",
        "std_msgs/Float32",
        "std_msgs/Float64",
        "std_msgs/Header",
        "sensor_msgs/Image",
        "sensor_msgs/CompressedImage",
        "sensor_msgs/CameraInfo",
        "sensor_msgs/LaserScan",
        "sensor_msgs/Imu",
        "sensor_msgs/JointState",
        "sensor_msgs/NavSatFix",
        "sensor_msgs/PointCloud2",
        "geometry_msgs/Twist",
        "geometry_msgs/TwistStamped",
        "geometry_msgs/Pose",
        "geometry_msgs/PoseStamped",
        "geometry_msgs/Vector3",
        "geometry_msgs/TransformStamped",
        "nav_msgs/Odometry",
        "nav_msgs/Path",
        "nav_msgs/OccupancyGrid",
        "diagnostic_msgs/DiagnosticArray",
    ),
    "srv": (
        "std_srvs/Trigger",
        "std_srvs/SetBool",
        "std_srvs/Empty",
        "example_interfaces/AddTwoInts",
        "nav_msgs/GetPlan",
    ),
    "action": (
        "example_interfaces/Fibonacci",
        "nav2_msgs/NavigateToPose",
        "control_msgs/FollowJointTrajectory",
    ),
}
_KNOWN_KIND = {name: kind for kind, names in KNOWN_INTERFACES.items() for name in names}

# Package names that would shadow a module the generated code (or Python itself) imports,
# or that setuptools/colcon treat specially.
RESERVED_PACKAGE_NAMES = frozenset(
    {
        "test",
        "tests",
        "launch",
        "launch_ros",
        "rclpy",
        "rclcpp",
        "rcl",
        "rmw",
        "ament_index_python",
        "setuptools",
        "std_msgs",
        "std_srvs",
        "builtin_interfaces",
        "rosidl_runtime_py",
    }
    | {name for names in KNOWN_INTERFACES.values() for name in (n.split("/")[0] for n in names)}
    | set(sys.stdlib_module_names)
)


# A node becomes <label>.py next to its siblings. Running any node file directly puts that
# directory first on sys.path, so a label equal to a module Python or rclpy imports
# (string.py, logging.py, ...) breaks every node in the package.
RESERVED_NODE_LABELS = frozenset(
    {"rclpy", "rclcpp", "launch", "launch_ros", "setuptools", "ament_index_python"}
    | set(sys.stdlib_module_names)
)


def split_interface(value: str) -> tuple[str, InterfaceKind | None, str]:
    """Return (package, explicit kind or None, type name) for a validated interface string."""
    match = _INTERFACE_RE.fullmatch(value)
    if match is None:
        raise ValueError(f"invalid interface type {value!r}")
    kind = match.group(2)
    return match.group(1), kind, match.group(3)  # type: ignore[return-value]


def canonical_interface(value: str) -> str:
    """'pkg/msg/Type' and 'pkg/Type' name the same interface; return the short form."""
    package, _, type_name = split_interface(value)
    return f"{package}/{type_name}"


def _check_identifier(value: str, what: str) -> str:
    if not _IDENTIFIER_RE.fullmatch(value):
        raise ValueError(f"{what} must be a lowercase identifier (letters, digits, '_')")
    if keyword.iskeyword(value) or keyword.issoftkeyword(value):
        raise ValueError(f"{what} {value!r} is a Python keyword")
    if "__" in value or value.endswith("_"):
        raise ValueError(f"{what} must not contain '__' or end with '_' (ROS naming rules)")
    return value


class EndpointSpec(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    id: str = Field(min_length=1, max_length=128, pattern=_ID_RE.pattern)
    name: str = Field(min_length=1, max_length=256)
    interface_type: str = Field(alias="msgType", min_length=3, max_length=256)

    @field_validator("name")
    @classmethod
    def validate_ros_name(cls, value: str) -> str:
        if not _ROS_NAME_RE.fullmatch(value) or "__" in value:
            raise ValueError(
                "must be a valid ROS name: tokens of letters, digits and '_' that do not "
                "start with a digit, separated by '/', with no trailing '/' or '__'"
            )
        return value

    @field_validator("interface_type")
    @classmethod
    def validate_interface_type(cls, value: str) -> str:
        if not _INTERFACE_RE.fullmatch(value):
            raise ValueError("must use package/Type form, for example std_msgs/String")
        return value


def _check_kind(endpoints: list[EndpointSpec], expected: InterfaceKind, group: str) -> None:
    for endpoint in endpoints:
        package, kind, type_name = split_interface(endpoint.interface_type)
        actual = kind or _KNOWN_KIND.get(f"{package}/{type_name}")
        if actual is not None and actual != expected:
            raise ValueError(
                f"{group} endpoint {endpoint.name!r} uses {endpoint.interface_type!r}, "
                f"which is a .{actual} type; expected a .{expected} type"
            )


class NodeSpec(BaseModel):
    model_config = ConfigDict(extra="ignore", allow_inf_nan=False)

    id: str = Field(min_length=1, max_length=128, pattern=_ID_RE.pattern)
    label: str = Field(min_length=1, max_length=128)
    x: float = 0
    y: float = 0
    publishers: list[EndpointSpec] = Field(default_factory=list)
    subscribers: list[EndpointSpec] = Field(default_factory=list)
    services: list[EndpointSpec] = Field(default_factory=list)
    clients: list[EndpointSpec] = Field(default_factory=list)
    action_servers: list[EndpointSpec] = Field(default_factory=list)
    action_clients: list[EndpointSpec] = Field(default_factory=list)

    @field_validator("label")
    @classmethod
    def validate_label(cls, value: str) -> str:
        _check_identifier(value, "node name")
        if value in RESERVED_NODE_LABELS:
            raise ValueError(
                f"node name {value!r} would create {value}.py, which shadows the Python module "
                f"'{value}' when a node file is run directly; choose another name"
            )
        return value

    @model_validator(mode="after")
    def validate_endpoints(self) -> NodeSpec:
        endpoints = (
            self.publishers
            + self.subscribers
            + self.services
            + self.clients
            + self.action_servers
            + self.action_clients
        )
        ids = [endpoint.id for endpoint in endpoints]
        if len(ids) != len(set(ids)):
            raise ValueError("endpoint ids must be unique within a node")
        _check_kind(self.publishers, "msg", "publisher")
        _check_kind(self.subscribers, "msg", "subscriber")
        _check_kind(self.services, "srv", "service")
        _check_kind(self.clients, "srv", "client")
        _check_kind(self.action_servers, "action", "action server")
        _check_kind(self.action_clients, "action", "action client")
        return self


class EdgeSpec(BaseModel):
    """A designer wire from a publisher (fn/ft) to a subscriber (tn/tt)."""

    model_config = ConfigDict(extra="ignore")

    id: str = Field(min_length=1, max_length=128, pattern=_ID_RE.pattern)
    fn: str = Field(min_length=1, max_length=128)
    ft: str = Field(min_length=1, max_length=128)
    tn: str = Field(min_length=1, max_length=128)
    tt: str = Field(min_length=1, max_length=128)


class PackageSpec(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str = Field(default="package", min_length=1, max_length=128, pattern=_ID_RE.pattern)
    pkg_name: str = Field(min_length=1, max_length=128)
    nodes: list[NodeSpec] = Field(default_factory=list)
    edges: list[EdgeSpec] = Field(default_factory=list)

    @field_validator("pkg_name")
    @classmethod
    def validate_package_name(cls, value: str) -> str:
        if not _PACKAGE_RE.fullmatch(value):
            raise ValueError("must match ^[a-z][a-z0-9_]*$")
        _check_identifier(value, "package name")
        if value in RESERVED_PACKAGE_NAMES:
            raise ValueError(
                f"{value!r} would shadow a Python or ROS module that generated code imports"
            )
        return value

    @model_validator(mode="after")
    def validate_nodes(self) -> PackageSpec:
        names = [node.label for node in self.nodes]
        if len(names) != len(set(names)):
            raise ValueError("node names must be unique within a package")
        ids = [node.id for node in self.nodes]
        if len(ids) != len(set(ids)):
            raise ValueError("node ids must be unique within a package")
        if self.pkg_name in names:
            raise ValueError("a node must not have the same name as its package")
        interface_packages = {
            split_interface(endpoint.interface_type)[0]
            for node in self.nodes
            for group in (
                node.publishers,
                node.subscribers,
                node.services,
                node.clients,
                node.action_servers,
                node.action_clients,
            )
            for endpoint in group
        }
        for label in names:
            if label in interface_packages:
                raise ValueError(
                    f"node name {label!r} is also an interface package used here; "
                    f"{label}.py would shadow it"
                )
        self._validate_edges()
        return self

    def _validate_edges(self) -> None:
        nodes = {node.id: node for node in self.nodes}
        for edge in self.edges:
            source = nodes.get(edge.fn)
            target = nodes.get(edge.tn)
            if source is None or target is None:
                raise ValueError(f"edge {edge.id!r} references a node that does not exist")
            publisher = next((p for p in source.publishers if p.id == edge.ft), None)
            subscriber = next((s for s in target.subscribers if s.id == edge.tt), None)
            if publisher is None or subscriber is None:
                raise ValueError(
                    f"edge {edge.id!r} must connect an existing publisher to an existing subscriber"
                )
            if publisher.name != subscriber.name:
                raise ValueError(
                    f"edge {source.label} -> {target.label}: topic names differ "
                    f"({publisher.name!r} vs {subscriber.name!r}), so they would not connect"
                )
            if canonical_interface(publisher.interface_type) != canonical_interface(
                subscriber.interface_type
            ):
                raise ValueError(
                    f"edge {source.label} -> {target.label} on {publisher.name!r}: "
                    f"types differ ({publisher.interface_type} vs {subscriber.interface_type})"
                )


class DesignState(BaseModel):
    model_config = ConfigDict(extra="ignore")

    version: int = 1
    nid: int = Field(default=1, ge=0)
    tid: int = Field(default=1, ge=0)
    pid: int = Field(default=1, ge=0)
    activePkg: str | None = None
    packages: list[PackageSpec] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_packages(self) -> DesignState:
        names = [package.pkg_name for package in self.packages]
        if len(names) != len(set(names)):
            raise ValueError("package names must be unique")
        ids = [package.id for package in self.packages]
        if len(ids) != len(set(ids)):
            raise ValueError("package ids must be unique")
        return self


# rcl rejects a fully qualified topic or service name longer than RMW_TOPIC_MAX_NAME_LENGTH
# (255 - 8). An action creates its topics and services under the action name, and the longest
# of them adds this suffix, so the action name itself must leave room for it. Both limits were
# measured with rclpy on Jazzy (tests/integration/test_design_ros2_runtime.py).
MAX_EXPANDED_NAME_LENGTH = 247
_LONGEST_ACTION_SUFFIX = "/_action/cancel_goal"
_ENDPOINT_ROLES = (
    ("publishers", "a topic"),
    ("subscribers", "a topic"),
    ("services", "a service"),
    ("clients", "a service"),
    ("action_servers", "an action"),
    ("action_clients", "an action"),
)


def expanded_endpoint_name(name: str, node_label: str) -> str:
    """The fully qualified name of a generated node's endpoint.

    The generated launch file starts every node in the root namespace under its own label, so
    a relative name resolves under ``/`` and a private ``~/`` name under ``/<label>``.
    """
    if name.startswith("/"):
        return name
    if name.startswith("~/"):
        return f"/{node_label}/{name[2:]}"
    return f"/{name}"


def name_length_errors(state: DesignState) -> list[dict[str, str]]:
    """Endpoint names that ROS 2 would reject at startup because they are too long.

    Returned in the field-error form of a validation response, so the designer shows each
    one on the offending field.
    """
    errors = []
    for package_index, package in enumerate(state.packages):
        for node_index, node in enumerate(package.nodes):
            for group, what in _ENDPOINT_ROLES:
                for index, endpoint in enumerate(getattr(node, group)):
                    expanded = expanded_endpoint_name(endpoint.name, node.label)
                    limit = MAX_EXPANDED_NAME_LENGTH
                    if group.startswith("action_"):
                        limit -= len(_LONGEST_ACTION_SUFFIX)
                    if len(expanded) <= limit:
                        continue
                    reason = (
                        f", because ROS adds '{_LONGEST_ACTION_SUFFIX}' to it"
                        if group.startswith("action_")
                        else ""
                    )
                    errors.append(
                        {
                            "path": f"packages.{package_index}.nodes.{node_index}."
                            f"{group}.{index}.name",
                            "message": (
                                f"is {len(expanded)} characters once expanded; ROS 2 accepts "
                                f"at most {limit} for {what} name{reason}. Shorten it by "
                                f"{len(expanded) - limit}."
                            ),
                            "type": "name_too_long",
                        }
                    )
    return errors


def known_interfaces_json() -> dict[str, Any]:
    return {kind: list(names) for kind, names in KNOWN_INTERFACES.items()}
