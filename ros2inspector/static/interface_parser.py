import logging
from pathlib import Path

from ros2inspector.model.schemas import InterfaceDefinition

_KIND_MAP = {".msg": "msg", ".srv": "srv", ".action": "action"}
_LOG = logging.getLogger(__name__)


def parse_interface_file(path: Path, package: str) -> InterfaceDefinition:
    kind = _KIND_MAP.get(path.suffix, "unknown")
    fields: list[str] = []

    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        _LOG.debug("skipping interface file %s: %s", path, exc)
        return InterfaceDefinition(
            name=path.stem,
            package=package,
            kind=kind,
            fields=fields,
            file_path=str(path),
        )

    for line in raw.splitlines():
        line = line.strip()
        # Skip comments, blank lines, and srv/action separators
        if not line or line.startswith("#") or line == "---":
            continue
        fields.append(line)

    return InterfaceDefinition(
        name=path.stem,
        package=package,
        kind=kind,
        fields=fields,
        file_path=str(path),
    )
