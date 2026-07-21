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
    """Return None (and emit a warning to stderr) if the file is unreadable or malformed."""
    try:
        tree = ET.parse(path)
    except (ET.ParseError, OSError) as exc:
        import sys

        print(f"[warn] skipping {path}: {exc}", file=sys.stderr)
        return None
    root = tree.getroot()

    name = _text(root, "name") or path.parent.name
    version = _text(root, "version") or "0.0.0"
    description = _text(root, "description")
    license_val = _text(root, "license")

    maintainers = [f"{m.text} <{m.get('email', '')}>" for m in root.findall("maintainer") if m.text]

    deps: dict[DepType, list[str]] = {}
    for tag, dep_type in _DEP_TAG_MAP.items():
        entries = [el.text.strip() for el in root.findall(tag) if el.text]
        if entries:
            deps[dep_type] = entries

    pkg_type = _detect_package_type(root)

    return PackageMetadata(
        name=name,
        version=version,
        package_type=pkg_type,
        maintainers=maintainers,
        license=license_val,
        description=description,
        path=str(path.parent),
        dependencies=deps,
    )


def _text(root: ET.Element, tag: str) -> str | None:
    el = root.find(tag)
    return el.text.strip() if el is not None and el.text else None


def _detect_package_type(root: ET.Element) -> PackageType:
    # REP-149 metapackages explicitly declare <metapackage/> under <export>.
    # Check this before build_type because some real-world manifests contain both.
    for export in root.findall("export"):
        if export.find("metapackage") is not None:
            return PackageType.META
        for build_type in export.findall("build_type"):
            if build_type.text:
                return _BUILD_SYSTEM_TYPE_MAP.get(build_type.text.strip(), PackageType.UNKNOWN)
    # Fallback: infer from build_depend
    build_deps = [el.text for el in root.findall("build_depend") if el.text]
    if "ament_cmake" in build_deps:
        return PackageType.AMENT_CMAKE
    if "ament_python" in build_deps:
        return PackageType.AMENT_PYTHON
    return PackageType.UNKNOWN
