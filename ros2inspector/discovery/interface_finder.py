from pathlib import Path

from ros2inspector.discovery.file_walker import iter_package_files

_INTERFACE_EXTENSIONS = {".msg", ".srv", ".action"}


def find_interface_files(package_path: Path) -> list[Path]:
    """Return all .msg, .srv, and .action files under a package directory."""
    return list(iter_package_files(package_path, suffixes=_INTERFACE_EXTENSIONS))
