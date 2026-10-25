"""File helpers shared by the commands that write files for the user."""

from __future__ import annotations

import os
from pathlib import Path

# Read once at import time: os.umask() can only be read by setting it, which is not safe
# once the design server's request threads are running.
_UMASK = os.umask(0)
os.umask(_UMASK)


def default_file_mode(path: Path) -> int:
    """The mode a file written to ``path`` should get.

    Temporary files from :mod:`tempfile` are created ``0600``, so a file written through one
    and moved into place would be readable only by its owner. A file being replaced keeps its
    current mode; a new file gets the mode ``open()`` would give it (``0644`` under the usual
    umask), so installed ``package.xml`` and launch files stay readable by other users.
    """
    try:
        return path.stat().st_mode & 0o7777
    except OSError:
        return 0o666 & ~_UMASK
