import os
import sys
from pathlib import Path

from .errors import WorkspaceAccessError


def resolve_overlay_paths() -> list[str]:
    """Return paths from $AMENT_PREFIX_PATH, ordered from lowest to highest priority."""
    raw = os.environ.get("AMENT_PREFIX_PATH", "")
    return [p for p in raw.split(":") if p] if raw else []


def get_ros_distro() -> str | None:
    return os.environ.get("ROS_DISTRO")


def _has_workspace_marker(path: Path) -> bool:
    return (path / "src").is_dir() or (path / "install" / "setup.bash").exists()


def find_workspace_root(start: Path) -> Path:
    """Find the nearest colcon workspace without requiring readable ancestors.

    The path supplied by the user must itself be accessible. Permission failures
    encountered only while probing ancestors are unrelated to that workspace and
    stop upward discovery rather than aborting the command.
    """
    try:
        current = start.expanduser().resolve()
        if _has_workspace_marker(current):
            return current
    except PermissionError as exc:
        raise WorkspaceAccessError(start) from exc

    for parent in current.parents:
        try:
            if _has_workspace_marker(parent):
                return parent
        except PermissionError:
            break

    print(
        f"Warning: no ROS 2 workspace root found above '{start}'; using it as root.\n"
        "  Ensure you are inside a workspace with a 'src/' directory.",
        file=sys.stderr,
    )
    return current
