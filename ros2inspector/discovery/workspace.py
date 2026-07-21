import os
import sys
from pathlib import Path


def resolve_overlay_paths() -> list[str]:
    """Return paths from $AMENT_PREFIX_PATH, ordered from lowest to highest priority."""
    raw = os.environ.get("AMENT_PREFIX_PATH", "")
    return [p for p in raw.split(":") if p] if raw else []


def get_ros_distro() -> str | None:
    return os.environ.get("ROS_DISTRO")


def find_workspace_root(start: Path) -> Path:
    """
    Walk up from start looking for a colcon workspace marker (src/ sibling or
    install/setup.bash). Falls back to start with a warning if none found.
    """
    current = start.resolve()
    for parent in [current, *current.parents]:
        if (parent / "src").is_dir() or (parent / "install" / "setup.bash").exists():
            return parent
    print(
        f"Warning: no ROS 2 workspace root found above '{start}'; using it as root.\n"
        "  Ensure you are inside a workspace with a 'src/' directory.",
        file=sys.stderr,
    )
    return current
