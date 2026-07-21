from __future__ import annotations

from pathlib import Path

import pytest

FIXTURE_WS = Path(__file__).parent.parent / "fixtures" / "workspaces" / "workspace_a"


@pytest.fixture(scope="module")
def real_ws() -> Path:
    """Portable representative workspace committed with the repository."""
    return FIXTURE_WS
