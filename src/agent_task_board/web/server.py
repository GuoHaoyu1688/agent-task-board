"""Small standard-library HTTP server with a genuinely read-only mode."""

from __future__ import annotations

import json
import secrets
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from ..core import Board, BoardError
from ..core.board import failure, success


def make_server(
    board: Board,
    host: str = "127.0.0.1",
    port: int = 8765,
    read_only: bool = False,
    quiet: bool = False,
) -> ThreadingHTTPServer:
    csrf = secrets.token_urlsafe(32)
    assets = Path(__file__).parent

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            if not quiet:
                super().log_message(format, *args)

        def valid_host(self) -> bool:
            authority = self.headers.get("Host", "")
            expected = {f"{host}:{self.server.server_port}"}
            if host in {"127.0.0.1", "localhost"}:
                expected |= {
                    f"127.0.0.1:{self.server.server_port}",
                    f"localhost:{self.server.server_port}",
                }
            return authority in expected

        def send(self, status: int, data: bytes, content_type: str = "application/json") -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type + "; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; style-src 'self'; script-src 'self'; frame-ancestors 'none'",
            )
            self.end_headers()
            self.wfile.write(data)

        def json(self, status: int, data: dict) -> None:
            self.send(status, json.dumps(data, ensure_ascii=False).encode())

        def do_GET(self) -> None:
            if not self.valid_host():
                self.json(403, failure(BoardError("forbidden_host", "Unexpected Host header")))
                return
            parsed = urlparse(self.path)
            try:
                if parsed.path == "/api/state":
                    filters = parse_qs(parsed.query)
                    tasks = board.list_tasks(agent=filters.get("agent", [None])[0], limit=10000)
                    self.json(
                        200,
                        success(
                            {
                                "tasks": tasks,
                                "read_only": read_only,
                                "csrf": None if read_only else csrf,
                                "timezone": str(board.zone),
                                "now": board.stamp(),
                            }
                        ),
                    )
                elif parsed.path == "/api/events":
                    filters = parse_qs(parsed.query)
                    self.json(
                        200,
                        success(
                            board.events(
                                filters.get("task", [None])[0], int(filters.get("after", [0])[0])
                            )
                        ),
                    )
                elif parsed.path in {"/", "/app.js", "/style.css"}:
                    name = "index.html" if parsed.path == "/" else parsed.path[1:]
                    mime = {
                        "index.html": "text/html",
                        "app.js": "application/javascript",
                        "style.css": "text/css",
                    }[name]
                    self.send(200, (assets / name).read_bytes(), mime)
                else:
                    self.json(404, failure(BoardError("not_found", "Route not found", 3)))
            except (BoardError, ValueError) as exc:
                error = (
                    exc if isinstance(exc, BoardError) else BoardError("invalid_input", str(exc), 2)
                )
                self.json(400, failure(error))

        def do_POST(self) -> None:
            if read_only:
                self.json(403, failure(BoardError("read_only", "This board is read-only")))
                return
            if (
                not self.valid_host()
                or self.headers.get("X-ATB-CSRF") != csrf
                or self.headers.get("Content-Type", "").split(";")[0] != "application/json"
            ):
                self.json(
                    403, failure(BoardError("forbidden", "Missing same-origin request token"))
                )
                return
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 1024 * 1024:
                    raise BoardError("invalid_body", "JSON body must be 1 byte..1 MiB", 2)
                payload = json.loads(self.rfile.read(size))
                if not isinstance(payload, dict):
                    raise BoardError("invalid_body", "Expected JSON object", 2)
                if self.path == "/api/tasks":
                    result = board.create_task(**payload)
                elif self.path == "/api/review":
                    if type(payload.get("approved")) is not bool:
                        raise BoardError("invalid_input", "approved must be a boolean", 2)
                    result = board.review(
                        payload["id"], payload["approved"], "human", payload.get("reason", "")
                    )
                elif self.path == "/api/comment":
                    result = board.comment(payload["id"], payload["text"], "human")
                elif self.path == "/api/reassign":
                    result = board.reassign(payload["id"], payload.get("assignee"), "human")
                else:
                    self.json(404, failure(BoardError("not_found", "Route not found", 3)))
                    return
                self.json(200, success(result))
            except (BoardError, ValueError, KeyError, TypeError) as exc:
                error = (
                    exc if isinstance(exc, BoardError) else BoardError("invalid_input", str(exc), 2)
                )
                self.json(400, failure(error))

    return ThreadingHTTPServer((host, port), Handler)


def serve(board: Board, host: str, port: int, read_only: bool, quiet: bool) -> None:
    server = make_server(board, host, port, read_only, quiet)
    if not quiet:
        print(
            f"Board: http://{host}:{server.server_port} "
            f"({'read-only' if read_only else 'editable'})",
            file=sys.stderr,
        )
    try:
        server.serve_forever()
    finally:
        server.server_close()
