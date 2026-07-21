from __future__ import annotations

from pathlib import Path


class WorkspaceAnalysisError(RuntimeError):
    """Base class for user-actionable workspace analysis failures."""


class NoPackagesFoundError(WorkspaceAnalysisError):
    def __init__(self, root: Path) -> None:
        self.root = root
        super().__init__(
            f"No valid ROS 2 packages were found under '{root}'. "
            "Check the workspace path (normally the directory containing src/)."
        )


class DuplicatePackageError(WorkspaceAnalysisError):
    def __init__(self, duplicates: dict[str, list[Path]]) -> None:
        self.duplicates = duplicates
        details = "; ".join(
            f"{name}: {', '.join(str(path) for path in paths)}"
            for name, paths in sorted(duplicates.items())
        )
        super().__init__(f"Duplicate package names make the workspace ambiguous: {details}")
