from pydantic import BaseModel, Field

from ros2inspector.utils.enums import StrEnum

DYNAMIC_SENTINEL = "<dynamic>"
UNKNOWN_SENTINEL = "<unknown>"


class PackageType(StrEnum):
    AMENT_CMAKE = "ament_cmake"
    AMENT_PYTHON = "ament_python"
    CMAKE = "cmake"
    PYTHON = "python"
    META = "meta"
    UNKNOWN = "unknown"


class DepType(StrEnum):
    BUILD = "build"
    EXEC = "exec"
    TEST = "test"
    BUILD_EXPORT = "build_export"
    DEPEND = "depend"  # shorthand for build + exec + build_export


class DataSource(StrEnum):
    STATIC = "static"


class QoSProfile(BaseModel):
    reliability: str | None = None
    durability: str | None = None
    history: str | None = None
    depth: int | None = None


class CommunicationEndpoint(BaseModel):
    """A single pub/sub/service/action endpoint with its name and interface type."""

    name: str
    msg_type: str = "unknown"
    file_path: str | None = None
    line: int | None = None
    evidence: str | None = None
    type_source: str = "explicit"
    confidence: str = "high"


class NodeDefinition(BaseModel):
    name: str
    source_symbol: str | None = None
    declared_ros_name: str | None = None
    package: str
    language: str
    file_path: str | None = None
    line: int | None = None
    publishers: list[CommunicationEndpoint] = Field(default_factory=list)
    subscriptions: list[CommunicationEndpoint] = Field(default_factory=list)
    services: list[CommunicationEndpoint] = Field(default_factory=list)
    clients: list[CommunicationEndpoint] = Field(default_factory=list)
    action_servers: list[CommunicationEndpoint] = Field(default_factory=list)
    action_clients: list[CommunicationEndpoint] = Field(default_factory=list)
    source: DataSource = DataSource.STATIC
    has_dynamic_names: bool = False


class PackageMetadata(BaseModel):
    name: str
    version: str = "0.0.0"
    package_type: PackageType = PackageType.UNKNOWN
    maintainers: list[str] = Field(default_factory=list)
    license: str | None = None
    licenses: list[str] = Field(default_factory=list)
    description: str | None = None
    path: str
    is_overlay: bool = False
    health_score: int | None = None
    dependencies: dict[DepType, list[str]] = Field(default_factory=dict)
    conditional_dependencies: dict[DepType, list[str]] = Field(default_factory=dict)
    dependency_conditions: dict[DepType, dict[str, str]] = Field(default_factory=dict)


class InterfaceDefinition(BaseModel):
    name: str
    package: str
    kind: str  # msg | srv | action
    fields: list[str] = Field(default_factory=list)
    file_path: str


class ViolationSeverity(StrEnum):
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


class PolicyViolation(BaseModel):
    severity: ViolationSeverity
    rule_type: str
    message: str
    policy_file: str
    policy_line: int | None = None
    affected_entities: list[str] = Field(default_factory=list)


class WorkspaceManifest(BaseModel):
    root: str
    ros_distro: str | None = None
    packages: list[PackageMetadata] = Field(default_factory=list)
    interfaces: list[InterfaceDefinition] = Field(default_factory=list)
    overlay_paths: list[str] = Field(default_factory=list)
