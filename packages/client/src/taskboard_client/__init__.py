"""Connect Taskboard tools to the shared ledger."""

from .connection import SharedActionUnavailable, SharedBoard, SharedReadError

__all__ = ["SharedActionUnavailable", "SharedBoard", "SharedReadError"]
