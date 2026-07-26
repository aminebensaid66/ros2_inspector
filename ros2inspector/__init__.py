"""ROS2 Inspector — architectural analysis and governance for ROS 2 workspaces."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("ros2inspector")
except PackageNotFoundError:  # pragma: no cover - only when run from an uninstalled checkout
    __version__ = "0.0.0+unknown"

__all__ = ["__version__"]
