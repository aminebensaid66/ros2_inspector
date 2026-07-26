"""Enum compatibility helpers."""

from enum import Enum


class StrEnum(str, Enum):
    """Python 3.10-compatible equivalent of :class:`enum.StrEnum`."""

    def __str__(self) -> str:
        return str(self.value)
