from pathlib import Path

_INTERFACE_EXTENSIONS = {".msg", ".srv", ".action"}


def find_interface_files(package_path: Path) -> list[Path]:
    """Return all .msg, .srv, and .action files under a package directory."""
    return [f for f in package_path.rglob("*") if f.suffix in _INTERFACE_EXTENSIONS and f.is_file()]
