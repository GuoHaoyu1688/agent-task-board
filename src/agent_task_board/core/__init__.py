"""Public business API, shared by CLI, MCP and HTTP."""

from .board import Board, BoardError

__all__ = ["Board", "BoardError"]
