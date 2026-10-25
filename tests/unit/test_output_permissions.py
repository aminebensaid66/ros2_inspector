"""Files written for the user must get normal permissions, not tempfile's 0600."""

from __future__ import annotations

import os
from pathlib import Path

from ros2inspector.cli._output import atomic_write_text


def _umask() -> int:
    umask = os.umask(0)
    os.umask(umask)
    return umask


def test_new_output_file_follows_the_umask(tmp_path: Path) -> None:
    target = tmp_path / "graph.html"
    atomic_write_text(target, "<html></html>")
    assert target.stat().st_mode & 0o777 == 0o666 & ~_umask()


def test_replaced_output_file_keeps_its_mode(tmp_path: Path) -> None:
    target = tmp_path / "docs.md"
    target.write_text("old")
    target.chmod(0o640)
    atomic_write_text(target, "new")
    assert target.read_text() == "new"
    assert target.stat().st_mode & 0o777 == 0o640
