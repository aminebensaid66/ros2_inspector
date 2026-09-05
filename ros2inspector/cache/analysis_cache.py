from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Any

import diskcache

from ros2inspector.discovery.file_walker import iter_package_files
from ros2inspector.model.schemas import InterfaceDefinition, NodeDefinition

_LOG = logging.getLogger(__name__)

_DEFAULT_CACHE_DIR = Path.home() / ".cache" / "ros2inspector"

_SOURCE_SUFFIXES = frozenset(
    (".py", ".cpp", ".cxx", ".cc", ".hpp", ".h", ".msg", ".srv", ".action")
)

_CACHE_VERSION = "v5-pruned-content-sha256-metadata-digest-cache"
_CACHE_SIZE_LIMIT = 256 * 1024 * 1024
_DIGEST_CACHE_SIZE_LIMIT = 128 * 1024 * 1024


def _content_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _cached_content_digest(path: Path, digest_cache: diskcache.Cache | None) -> str:
    """Return a content hash, reusing it only while strong file metadata is unchanged."""
    if digest_cache is None:
        return _content_digest(path)
    stat = path.stat()
    metadata_key = (
        "file-digest-v1",
        str(path.resolve()),
        stat.st_size,
        stat.st_mtime_ns,
        stat.st_ctime_ns,
    )
    cached: Any = digest_cache.get(metadata_key)
    if isinstance(cached, str):
        return cached
    digest = _content_digest(path)
    digest_cache.set(metadata_key, digest)
    return digest


def _pkg_fingerprint(pkg_path: Path, digest_cache: diskcache.Cache | None = None) -> str:
    """SHA-256 of paths and content digests for all package source inputs.

    Package traversal is still deterministic and content-based. ``AnalysisCache`` adds a
    persistent per-file digest cache keyed by size + nanosecond mtime/ctime, so unchanged
    large source files are not reread on every CLI invocation.
    """
    digest = hashlib.sha256()
    digest.update(_CACHE_VERSION.encode())
    files = sorted(iter_package_files(pkg_path, suffixes=_SOURCE_SUFFIXES, names={"package.xml"}))
    for file_path in files:
        try:
            relative = str(file_path.relative_to(pkg_path))
            content_digest = _cached_content_digest(file_path, digest_cache)
        except OSError:
            continue
        digest.update(relative.encode("utf-8", errors="surrogateescape"))
        digest.update(b"\0")
        digest.update(content_digest.encode("ascii"))
        digest.update(b"\0")
    return digest.hexdigest()


class AnalysisCache:
    """Disk-backed cache for per-package static parse results."""

    def __init__(self, cache_dir: Path | None = None) -> None:
        self._dir = cache_dir or _DEFAULT_CACHE_DIR
        self._cache: diskcache.Cache = diskcache.Cache(str(self._dir), size_limit=_CACHE_SIZE_LIMIT)
        self._digest_cache: diskcache.Cache = diskcache.Cache(
            str(self._dir / "file-digests"), size_limit=_DIGEST_CACHE_SIZE_LIMIT
        )
        self._fp_cache: tuple[Path, str] | None = None

    def _fingerprint(self, pkg_path: Path) -> str:
        if self._fp_cache is not None and self._fp_cache[0] == pkg_path:
            return self._fp_cache[1]
        fingerprint = _pkg_fingerprint(pkg_path, self._digest_cache)
        self._fp_cache = (pkg_path, fingerprint)
        return fingerprint

    def get(self, pkg_path: Path) -> tuple[list[NodeDefinition], list[InterfaceDefinition]] | None:
        key = self._fingerprint(pkg_path)
        result: Any = self._cache.get(key)
        if result is None:
            _LOG.debug("cache miss: %s", pkg_path)
            return None
        _LOG.debug("cache hit: %s", pkg_path)
        nodes_raw, ifaces_raw = result
        nodes = [NodeDefinition.model_validate(item) for item in nodes_raw]
        ifaces = [InterfaceDefinition.model_validate(item) for item in ifaces_raw]
        return nodes, ifaces

    def set(
        self,
        pkg_path: Path,
        nodes: list[NodeDefinition],
        ifaces: list[InterfaceDefinition],
    ) -> None:
        key = self._fingerprint(pkg_path)
        self._cache.set(
            key,
            (
                [node.model_dump(mode="json") for node in nodes],
                [iface.model_dump(mode="json") for iface in ifaces],
            ),
        )

    def clear(self) -> None:
        self._cache.clear()
        self._digest_cache.clear()
        self._fp_cache = None

    def close(self) -> None:
        self._cache.close()
        self._digest_cache.close()

    def stats(self) -> dict[str, Any]:
        return {
            "size": len(self._cache),
            "file_digest_entries": len(self._digest_cache),
            "directory": str(self._dir),
        }

    def __enter__(self) -> AnalysisCache:
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()
