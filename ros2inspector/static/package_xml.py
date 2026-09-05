from __future__ import annotations

import sys
import xml.etree.ElementTree as ET
from pathlib import Path

from ros2inspector.model.schemas import DepType, PackageMetadata, PackageType

_DEP_TAG_MAP: dict[str, DepType] = {
    "depend": DepType.DEPEND,
    "build_depend": DepType.BUILD,
    "exec_depend": DepType.EXEC,
    "test_depend": DepType.TEST,
    "build_export_depend": DepType.BUILD_EXPORT,
}

_BUILD_SYSTEM_TYPE_MAP: dict[str, PackageType] = {
    "ament_cmake": PackageType.AMENT_CMAKE,
    "ament_python": PackageType.AMENT_PYTHON,
    "cmake": PackageType.CMAKE,
    "python": PackageType.PYTHON,
}


def parse_package_xml(path: Path) -> PackageMetadata | None:
    """Parse a ROS package manifest conservatively.

    Conditional dependencies are preserved separately instead of being treated as
    unconditional graph edges because their truth value depends on the build/runtime
    environment. Multiple license declarations are retained in ``licenses`` while
    ``license`` remains the first declaration for backwards compatibility.
    """

    try:
        tree = ET.parse(path)
    except (ET.ParseError, OSError) as exc:
        print(f"[warn] skipping {path}: {exc}", file=sys.stderr)
        return None
    root = tree.getroot()

    name = _text(root, "name") or path.parent.name
    version = _text(root, "version") or "0.0.0"
    description = _text(root, "description")
    licenses = [
        text
        for element in root.findall("license")
        if (text := _element_text(element)) is not None
    ]

    maintainers = [
        f"{text} <{maintainer.get('email', '')}>"
        for maintainer in root.findall("maintainer")
        if (text := _element_text(maintainer)) is not None
    ]

    dependencies: dict[DepType, list[str]] = {}
    conditional_dependencies: dict[DepType, list[str]] = {}
    dependency_conditions: dict[DepType, dict[str, str]] = {}
    for tag, dep_type in _DEP_TAG_MAP.items():
        for element in root.findall(tag):
            dep_name = _element_text(element)
            if not dep_name:
                continue
            condition = (element.get("condition") or "").strip()
            if condition:
                conditional_dependencies.setdefault(dep_type, []).append(dep_name)
                dependency_conditions.setdefault(dep_type, {})[dep_name] = condition
            else:
                dependencies.setdefault(dep_type, []).append(dep_name)

    return PackageMetadata(
        name=name,
        version=version,
        package_type=_detect_package_type(root),
        maintainers=maintainers,
        license=licenses[0] if licenses else None,
        licenses=licenses,
        description=description,
        path=str(path.parent),
        dependencies=dependencies,
        conditional_dependencies=conditional_dependencies,
        dependency_conditions=dependency_conditions,
    )


def _element_text(element: ET.Element) -> str | None:
    if element.text is None:
        return None
    value = element.text.strip()
    return value or None


def _text(root: ET.Element, tag: str) -> str | None:
    element = root.find(tag)
    return _element_text(element) if element is not None else None


def _detect_package_type(root: ET.Element) -> PackageType:
    # REP-149 metapackages explicitly declare <metapackage/> under <export>.
    for export in root.findall("export"):
        if export.find("metapackage") is not None:
            return PackageType.META
        for build_type in export.findall("build_type"):
            if build_type.text:
                return _BUILD_SYSTEM_TYPE_MAP.get(build_type.text.strip(), PackageType.UNKNOWN)

    # Build-tool declarations are the canonical fallback for most ROS 2 manifests.
    build_tools = [
        text
        for element in root.findall("buildtool_depend")
        if (text := _element_text(element)) is not None
        and not (element.get("condition") or "").strip()
    ]
    build_deps = [
        text
        for element in root.findall("build_depend")
        if (text := _element_text(element)) is not None
        and not (element.get("condition") or "").strip()
    ]
    declared = set(build_tools) | set(build_deps)
    if "ament_cmake" in declared:
        return PackageType.AMENT_CMAKE
    if "ament_python" in declared:
        return PackageType.AMENT_PYTHON
    return PackageType.UNKNOWN
