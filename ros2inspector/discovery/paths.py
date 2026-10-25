from pathlib import Path

DirectoryKeys = set[tuple[int, int]]


def visit_directory(path: Path, visited: DirectoryKeys) -> bool:
    """Claim a physical directory once, including paths reached through symlinks."""
    stat = path.stat()
    key = (stat.st_dev, stat.st_ino)
    if key in visited:
        return False
    visited.add(key)
    return True
