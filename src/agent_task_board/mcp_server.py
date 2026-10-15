"""Official MCP SDK stdio adapter; all task logic belongs to core.Board."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from mcp.server.fastmcp import FastMCP

from .config import make_board
from .core import Board, BoardError
from .core.board import failure, success


def invoke(fn: Callable, *args: Any, **kwargs: Any) -> dict:
    try:
        return success(fn(*args, **kwargs))
    except BoardError as exc:
        return failure(exc)
    except (ValueError, TypeError) as exc:
        return failure(BoardError("invalid_input", str(exc), 2))


def build_server(board: Board) -> FastMCP:
    mcp = FastMCP("agent-task-board")

    @mcp.tool()
    def create_task(
        title: str,
        body: str = "",
        queue: str = "default",
        assignee: str | None = None,
        priority: int = 0,
        due_at: str | None = None,
        tags: list[str] | None = None,
        dedupe_key: str | None = None,
        blocked_by: list[str] | None = None,
        params: dict | None = None,
        max_attempts: int = 3,
        needs_approval: bool = False,
        actor: str = "agent",
    ) -> dict:
        """Write: create a task, or return the unfinished task with the same dedupe key."""
        return invoke(
            board.create_task,
            title,
            body=body,
            queue=queue,
            assignee=assignee,
            priority=priority,
            due_at=due_at,
            tags=tags or [],
            dedupe_key=dedupe_key,
            blocked_by=blocked_by or [],
            params=params or {},
            max_attempts=max_attempts,
            needs_approval=needs_approval,
            actor=actor,
        )

    @mcp.tool()
    def claim_task(agent: str, queue: str | None = None, lease_seconds: int = 900) -> dict:
        """Write: atomically claim work. Keep lease_token; no task returns data=null."""
        return invoke(board.claim, agent, queue, lease_seconds)

    @mcp.tool()
    def heartbeat(id: str, agent: str, token: str, lease_seconds: int = 900) -> dict:
        """Write: renew a valid lease and mark running. Expired/replaced tokens are rejected."""
        return invoke(board.heartbeat, id, agent, token, lease_seconds)

    @mcp.tool()
    def complete_task(
        id: str, agent: str, token: str, result: dict, artifacts: list[str] | None = None
    ) -> dict:
        """Write: finish your leased task, or request human review; may fire configured hooks."""
        return invoke(board.complete, id, agent, token, result, artifacts)

    @mcp.tool()
    def fail_task(id: str, agent: str, token: str, error: str) -> dict:
        """Write: record failure; schedule exponential retry or mark terminal failed."""
        return invoke(board.fail, id, agent, token, error)

    @mcp.tool()
    def list_tasks(filters: dict | None = None) -> dict:
        """Read tasks. filters: status, queue, agent (optional strings), limit (1..100000)."""
        filters = filters or {}
        if set(filters) - {"status", "queue", "agent", "limit"}:
            return failure(BoardError("invalid_filter", "Unsupported filter", 2))
        return invoke(board.list_tasks, **filters)

    @mcp.tool()
    def add_comment(id: str, text: str, actor: str = "agent") -> dict:
        """Write: append a hand-off note to the immutable event timeline."""
        return invoke(board.comment, id, text, actor)

    @mcp.tool()
    def get_task(id: str) -> dict:
        """Read task metadata and dependencies; lease secrets are never exposed."""
        return invoke(board.get_task, id)

    @mcp.tool()
    def cancel_task(id: str, confirm: bool = False, actor: str = "agent") -> dict:
        """Dangerous write: cancel work and invalidate its lease. Requires confirm=true."""
        return invoke(board.cancel, id, confirm=confirm, actor=actor)

    return mcp


def main() -> None:
    build_server(make_board()).run(transport="stdio")


if __name__ == "__main__":
    main()
