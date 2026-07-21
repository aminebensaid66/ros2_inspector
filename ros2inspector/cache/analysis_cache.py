from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Any

import diskcache

from ros2inspector.model.schemas import InterfaceDefinition, NodeDefinition

_LOG = logging.getLogger(__name__)

_DEFAULT_CACHE_DIR = Path.home() / ".cache" / "ros2inspector"

_SOURCE_SUFFIXES = frozenset((".py", ".cpp", ".hpp", ".h", ".msg", ".srv", ".action"))

_CACHE_VERSION = "v3-content-sha256"
_CACHE_SIZE_LIMIT = 256 * 1024 * 1024  # 256 MB


def _pkg_fingerprint(pkg_path: Path) -> str:
    """SHA-256 of relative paths and file contents for all package source inputs."""
    h = hashlib.sha256()
    h.update(_CACHE_VERSION.encode())
    files: list[Path] = sorted(
        f
        for f in pkg_path.rglob("*")
        if f.is_file() and (f.suffix in _SOURCE_SUFFIXES or f.name == "package.xml")
    )
    for f in files:
        try:
            h.update(str(f.relative_to(pkg_path)).encode("utf-8", errors="surrogateescape"))
            h.update(b"\0")
            with f.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    h.update(chunk)
            h.update(b"\0")
        except OSError:
            pass
    return h.hexdigest()


class AnalysisCache:
    """Disk-backed cache for per-package parse results.

    Keys are SHA-256 fingerprints of package source paths and contents.
    Values are serialised lists of NodeDefinition and InterfaceDefinition.
    A stale entry is never served — any file change invalidates the key.
    """

    def __init__(self, cache_dir: Path | None = None) -> None:
        self._dir = cache_dir or _DEFAULT_CACHE_DIR
        self._cache: diskcache.Cache = diskcache.Cache(str(self._dir), size_limit=_CACHE_SIZE_LIMIT)
        # One-slot memoisation so a get() + set() pair on the same path only
        # runs the rglob/stat fingerprint scan once instead of twice.
        self._fp_cache: tuple[Path, str] | None = None

    def _fingerprint(self, pkg_path: Path) -> str:
        if self._fp_cache is not None and self._fp_cache[0] == pkg_path:
            return self._fp_cache[1]
        fp = _pkg_fingerprint(pkg_path)
        self._fp_cache = (pkg_path, fp)
        return fp

    # ── public API ─────────────────────────────────────────────────────────────

    def get(self, pkg_path: Path) -> tuple[list[NodeDefinition], list[InterfaceDefinition]] | None:
        key = self._fingerprint(pkg_path)
        result: Any = self._cache.get(key)
        if result is None:
            _LOG.debug("cache miss: %s", pkg_path)
            return None
        _LOG.debug("cache hit: %s", pkg_path)
        nodes_raw, ifaces_raw = result
        nodes = [NodeDefinition.model_validate(n) for n in nodes_raw]
        ifaces = [InterfaceDefinition.model_validate(i) for i in ifaces_raw]
        return nodes, ifaces

    def set(
        self,
        pkg_path: Path,
        nodes: list[NodeDefinition],
        ifaces: list[InterfaceDefinition],
    ) -> None:
        key = self._fingerprint(pkg_path)  # reuses the value computed by get()
        self._cache.set(
            key,
            (
                [n.model_dump(mode="json") for n in nodes],
                [i.model_dump(mode="json") for i in ifaces],
            ),
        )

    def clear(self) -> None:
        self._cache.clear()

    def close(self) -> None:
        self._cache.close()

    def stats(self) -> dict[str, Any]:
        return {"size": len(self._cache), "directory": str(self._dir)}

    # ── context manager ────────────────────────────────────────────────────────

    def __enter__(self) -> AnalysisCache:
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()
