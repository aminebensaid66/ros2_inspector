from __future__ import annotations

from collections.abc import Iterator

import pytest


@pytest.fixture(autouse=True, scope="session")
def _isolated_analysis_cache(tmp_path_factory: pytest.TempPathFactory) -> Iterator[None]:
    """Keep the test suite out of the developer's real ~/.cache/ros2inspector."""
    from ros2inspector.cache import analysis_cache

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(
            analysis_cache, "_DEFAULT_CACHE_DIR", tmp_path_factory.mktemp("analysis-cache")
        )
        yield
