"""Noninteractive command-line interface; one JSON envelope on stdout."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

from . import __version__
from .config import make_board
from .core import BoardError
from .core.board import failure, success


class Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise BoardError("invalid_arguments", message, 2)


def globals_first(argv: list[str]) -> list[str]:
    """Accept global options both before and after subcommands."""
    options, rest = [], []
    index = 0
    while index < len(argv):
        item = argv[index]
        if item == "--":
            rest.extend(argv[index:])
            break
        if item in {"--data-dir", "--config"}:
            options.append(item)
            index += 1
            if index < len(argv):
                options.append(argv[index])
        elif item in {"--json", "--quiet", "--version"} or item.startswith(
            ("--data-dir=", "--config=")
        ):
            options.append(item)
        else:
            rest.append(item)
        index += 1
    return options + rest


def parser() -> Parser:
    root = Parser(description="Local task queue with fenced worker leases")
    root.add_argument("--data-dir", help="Data directory; overrides ATB_DATA_DIR and config")
    root.add_argument("--config", help="TOML configuration file")
    root.add_argument("--json", action="store_true", help="Emit JSON (also the default)")
    root.add_argument("--quiet", action="store_true", help="Suppress informational stderr messages")
    root.add_argument("--version", action="store_true")
    commands = root.add_subparsers(dest="command")
    add = commands.add_parser("add", help="Create a task")
    add.add_argument("title")
    add.add_argument("--body", default="")
    add.add_argument("--queue", default="default")
    add.add_argument("--assignee")
    add.add_argument("--priority", type=int, default=0)
    add.add_argument("--due", dest="due_at")
    add.add_argument("--tag", dest="tags", action="append", default=[])
    add.add_argument("--dedupe", dest="dedupe_key")
    add.add_argument("--depends-on", dest="blocked_by", action="append", default=[])
    add.add_argument("--params", default="{}")
    add.add_argument("--max-attempts", type=int, default=3)
    add.add_argument("--needs-approval", action="store_true")
    add.add_argument("--actor", default="human")
    claim = commands.add_parser("claim", help="Atomically claim highest priority runnable task")
    claim.add_argument("--agent", required=True)
    claim.add_argument("--queue")
    claim.add_argument("--lease-seconds", type=int)
    for name in ("heartbeat", "done", "fail"):
        command = commands.add_parser(name)
        command.add_argument("id")
        command.add_argument("--agent", required=True)
        command.add_argument("--token", required=True, help="Lease token returned only by claim")
        if name == "heartbeat":
            command.add_argument("--lease-seconds", type=int)
        elif name == "done":
            command.add_argument("--result", default="{}")
            command.add_argument("--artifact", dest="artifacts", action="append", default=[])
        else:
            command.add_argument("--error", required=True)
    listing = commands.add_parser("list")
    listing.add_argument("--status")
    listing.add_argument("--queue")
    listing.add_argument("--agent")
    listing.add_argument("--limit", type=int, default=1000)
    show = commands.add_parser("show")
    show.add_argument("id")
    events = commands.add_parser("events")
    events.add_argument("--task", dest="task_id")
    events.add_argument("--after", type=int, default=0)
    events.add_argument("--limit", type=int, default=1000)
    comment = commands.add_parser("comment")
    comment.add_argument("id")
    comment.add_argument("text")
    comment.add_argument("--actor", default="human")
    for name in ("approve", "reject", "retry", "cancel", "reassign", "depend"):
        command = commands.add_parser(name)
        command.add_argument("id")
        command.add_argument("--actor", default="human")
        if name in {"approve", "reject"}:
            command.add_argument("--reason", default="")
        if name == "cancel":
            command.add_argument("--yes", action="store_true")
        if name == "reassign":
            command.add_argument("--assignee", help="Omit to allow any worker")
        if name == "depend":
            command.add_argument("blocked_by")
    schedule = commands.add_parser("schedule")
    sub = schedule.add_subparsers(dest="schedule_command", required=True)
    add_schedule = sub.add_parser("add")
    add_schedule.add_argument("cron")
    add_schedule.add_argument("--template", required=True)
    sub.add_parser("list")
    for name in ("pause", "resume"):
        command = sub.add_parser(name)
        command.add_argument("id")
    tick = commands.add_parser("tick")
    tick.add_argument("--max-catchup", type=int, default=100)
    export = commands.add_parser("export")
    export.add_argument("--format", choices=["csv", "json"], default="json")
    export.add_argument("--out", required=True)
    export.add_argument("--utf8", action="store_true", help="CSV without Excel BOM")
    importing = commands.add_parser("import")
    importing.add_argument("path")
    backup = commands.add_parser("backup")
    backup.add_argument("--out", help="Directory for timestamped backup ZIP")
    serve = commands.add_parser("serve")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--read-only", action="store_true")
    commands.add_parser("mcp")
    return root


def dispatch(args: argparse.Namespace) -> Any:
    board = make_board(args.data_dir, args.config)
    command = args.command
    if command == "add":
        fields = {
            key: getattr(args, key)
            for key in (
                "body",
                "queue",
                "assignee",
                "priority",
                "due_at",
                "tags",
                "dedupe_key",
                "blocked_by",
                "max_attempts",
                "needs_approval",
                "actor",
            )
        }
        return board.create_task(args.title, params=json.loads(args.params), **fields)
    if command == "claim":
        return board.claim(args.agent, args.queue, args.lease_seconds)
    if command == "heartbeat":
        return board.heartbeat(args.id, args.agent, args.token, args.lease_seconds)
    if command == "done":
        return board.complete(
            args.id, args.agent, args.token, json.loads(args.result), args.artifacts
        )
    if command == "fail":
        return board.fail(args.id, args.agent, args.token, args.error)
    if command == "list":
        return board.list_tasks(
            status=args.status, queue=args.queue, agent=args.agent, limit=args.limit
        )
    if command == "show":
        return board.get_task(args.id)
    if command == "events":
        return board.events(args.task_id, args.after, args.limit)
    if command == "comment":
        return board.comment(args.id, args.text, args.actor)
    if command in {"approve", "reject"}:
        return board.review(args.id, command == "approve", args.actor, args.reason)
    if command == "retry":
        return board.retry(args.id, args.actor)
    if command == "cancel":
        return board.cancel(args.id, confirm=args.yes, actor=args.actor)
    if command == "reassign":
        return board.reassign(args.id, args.assignee, args.actor)
    if command == "depend":
        return board.add_dependency(args.id, args.blocked_by, args.actor)
    if command == "schedule":
        if args.schedule_command == "add":
            template = json.loads(Path(args.template).read_text(encoding="utf-8"))
            return board.add_schedule(args.cron, template)
        if args.schedule_command == "list":
            return board.schedules()
        return board.set_schedule_enabled(args.id, args.schedule_command == "resume")
    if command == "tick":
        return board.tick(args.max_catchup)
    if command == "export":
        return board.export(args.out, args.format, not args.utf8)
    if command == "import":
        return board.import_file(args.path)
    if command == "backup":
        return board.backup(args.out)
    if command == "serve":
        from .web.server import serve

        serve(board, args.host, args.port, args.read_only, args.quiet)
    if command == "mcp":
        from .mcp_server import build_server

        build_server(board).run(transport="stdio")
    return None


def main(argv: list[str] | None = None) -> int:
    try:
        args = parser().parse_args(globals_first(list(sys.argv[1:] if argv is None else argv)))
        if args.version:
            print(json.dumps(success({"version": __version__})))
            return 0
        if not args.command:
            raise BoardError("invalid_arguments", "A subcommand is required; use --help", 2)
        result = dispatch(args)
        if args.command not in {"serve", "mcp"}:
            print(json.dumps(success(result), ensure_ascii=False, allow_nan=False))
        return 0
    except BoardError as exc:
        print(json.dumps(failure(exc), ensure_ascii=False))
        return exc.exit_code
    except (ValueError, TypeError, KeyError) as exc:
        print(json.dumps(failure(BoardError("invalid_input", str(exc), 2))))
        return 2
    except (OSError, sqlite3.Error) as exc:
        print(json.dumps(failure(BoardError("operation_failed", str(exc)))))
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
