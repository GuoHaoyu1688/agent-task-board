"""SQLite coordination: serialized state transitions and fenced worker leases."""

from __future__ import annotations

import csv
import io
import json
import secrets
import sqlite3
import subprocess
import tempfile
import urllib.request
import zipfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from croniter import croniter

STATUSES = {"todo", "claimed", "running", "done", "failed", "blocked", "cancelled", "review"}
ACTIVE = {"todo", "claimed", "running", "blocked", "review"}
SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
 id TEXT PRIMARY KEY, title TEXT NOT NULL, body TEXT NOT NULL DEFAULT '',
 queue TEXT NOT NULL DEFAULT 'default', assignee TEXT, priority INTEGER NOT NULL DEFAULT 0,
 due_at TEXT, tags_json TEXT NOT NULL DEFAULT '[]', dedupe_key TEXT,
 params_json TEXT NOT NULL DEFAULT '{}', result_json TEXT NOT NULL DEFAULT '{}',
 artifacts_json TEXT NOT NULL DEFAULT '[]', status TEXT NOT NULL,
 attempts INTEGER NOT NULL DEFAULT 0, max_attempts INTEGER NOT NULL DEFAULT 3,
 lease_owner TEXT, lease_token TEXT, lease_until TEXT, available_at TEXT NOT NULL,
 needs_approval INTEGER NOT NULL DEFAULT 0, error TEXT, created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS active_dedupe ON tasks(dedupe_key)
 WHERE dedupe_key IS NOT NULL AND status IN ('todo','claimed','running','blocked','review');
CREATE INDEX IF NOT EXISTS claim_order ON tasks(status, available_at, priority DESC, created_at);
CREATE TABLE IF NOT EXISTS deps (
 task_id TEXT NOT NULL REFERENCES tasks(id), blocked_by TEXT NOT NULL REFERENCES tasks(id),
 PRIMARY KEY(task_id, blocked_by), CHECK(task_id != blocked_by)
);
CREATE TABLE IF NOT EXISTS events (
 id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT REFERENCES tasks(id), actor TEXT NOT NULL,
 type TEXT NOT NULL, payload_json TEXT NOT NULL, at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS schedules (
 id TEXT PRIMARY KEY, cron TEXT NOT NULL, template_json TEXT NOT NULL,
 last_run_at TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL
);
PRAGMA user_version=1;
"""


class BoardError(Exception):
    """A stable public error with a machine-readable code and CLI exit status."""

    def __init__(self, code: str, message: str, exit_code: int = 1):
        super().__init__(message)
        self.code, self.exit_code = code, exit_code


def success(data: Any) -> dict:
    return {"ok": True, "data": data, "warnings": []}


def failure(error: BoardError) -> dict:
    return {"ok": False, "error": {"code": error.code, "message": str(error)}}


def uid(now: datetime) -> str:
    """Generate a 26-character ULID using timestamp plus 80 random bits."""
    value = (int(now.timestamp() * 1000) << 80) | secrets.randbits(80)
    alphabet = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
    chars = []
    for _ in range(26):
        chars.append(alphabet[value & 31])
        value >>= 5
    return "".join(reversed(chars))


def encode(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


class Board:
    """One-machine queue; each mutation obtains BEGIN IMMEDIATE before reading state."""

    def __init__(
        self,
        data_dir: str | Path = "data",
        *,
        timezone: str = "Asia/Shanghai",
        clock: Callable[[], datetime] | None = None,
        lease_seconds: int = 900,
        retry_base_seconds: int = 30,
        hooks: dict | None = None,
    ):
        self.data_dir = Path(data_dir).expanduser().resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.data_dir / "tasks.db"
        self.zone = ZoneInfo(timezone)
        self.clock = clock or (lambda: datetime.now(UTC))
        self.lease_seconds = lease_seconds
        self.retry_base_seconds = retry_base_seconds
        self.hooks = hooks or {}
        if lease_seconds <= 0 or retry_base_seconds < 0:
            raise BoardError(
                "invalid_config", "Lease must be positive; retry base cannot be negative", 2
            )
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(SCHEMA)

    def now(self) -> datetime:
        return self.clock().astimezone(UTC)

    def stamp(self, value: datetime | None = None) -> str:
        return (value or self.now()).astimezone(UTC).isoformat(timespec="microseconds")

    def parse_date(self, value: str | None) -> str | None:
        if not value:
            return None
        try:
            dt = datetime.fromisoformat(value)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=self.zone)
            return self.stamp(dt)
        except (ValueError, TypeError) as exc:
            raise BoardError("invalid_date", f"Invalid ISO date: {value}", 2) from exc

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=60, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=60000")
        try:
            yield db
        finally:
            db.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
                db.commit()
            except Exception:
                db.rollback()
                raise

    def _event(
        self,
        db: sqlite3.Connection,
        task_id: str | None,
        actor: str,
        kind: str,
        payload: Any = None,
    ) -> None:
        db.execute(
            "INSERT INTO events(task_id,actor,type,payload_json,at) VALUES(?,?,?,?,?)",
            (task_id, actor, kind, encode(payload or {}), self.stamp()),
        )

    def _row(self, db: sqlite3.Connection, task_id: str) -> sqlite3.Row:
        row = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            raise BoardError("not_found", f"Task not found: {task_id}", 3)
        return row

    def _task(self, db: sqlite3.Connection, row: sqlite3.Row, *, lease: bool = False) -> dict:
        result = dict(row)
        for key in ("tags", "params", "result", "artifacts"):
            result[key] = json.loads(result.pop(f"{key}_json"))
        result["needs_approval"] = bool(result["needs_approval"])
        result["blocked_by"] = [
            r[0]
            for r in db.execute(
                "SELECT blocked_by FROM deps WHERE task_id=? ORDER BY blocked_by", (row["id"],)
            )
        ]
        if not lease:
            result.pop("lease_token", None)
        return result

    def _validate_template(self, task: dict) -> dict:
        if not isinstance(task, dict):
            raise BoardError("invalid_task", "Task must be an object", 2)
        title = task.get("title")
        if not isinstance(title, str) or not title.strip():
            raise BoardError("invalid_task", "A nonempty title is required", 2)
        for key in ("body", "queue"):
            if key in task and not isinstance(task[key], str):
                raise BoardError("invalid_task", f"{key} must be a string", 2)
        for key in ("assignee", "dedupe_key"):
            if task.get(key) is not None and not isinstance(task[key], str):
                raise BoardError("invalid_task", f"{key} must be string or null", 2)
        for key in ("tags", "blocked_by", "artifacts"):
            if key in task and (
                not isinstance(task[key], list) or not all(isinstance(x, str) for x in task[key])
            ):
                raise BoardError("invalid_task", f"{key} must be a string array", 2)
        if not isinstance(task.get("params", {}), dict):
            raise BoardError("invalid_task", "params must be an object", 2)
        for key, default in (("priority", 0), ("max_attempts", 3)):
            if type(task.get(key, default)) is not int:
                raise BoardError("invalid_task", f"{key} must be an integer", 2)
        if task.get("max_attempts", 3) < 1:
            raise BoardError("invalid_task", "max_attempts must be at least 1", 2)
        if type(task.get("needs_approval", False)) is not bool:
            raise BoardError("invalid_task", "needs_approval must be boolean", 2)
        task = dict(task)
        task["due_at"] = self.parse_date(task.get("due_at"))
        encode(task)
        return task

    def _dependencies(self, db: sqlite3.Connection, task_id: str, blocked_by: list[str]) -> None:
        for dep in blocked_by:
            self._row(db, dep)
            cycle = db.execute(
                """WITH RECURSIVE ancestors(id) AS (
                SELECT ? UNION SELECT d.blocked_by FROM deps d JOIN ancestors a ON d.task_id=a.id
                ) SELECT 1 FROM ancestors WHERE id=? LIMIT 1""",
                (dep, task_id),
            ).fetchone()
            if cycle:
                raise BoardError("dependency_cycle", "Dependencies must form an acyclic graph", 2)
            db.execute("INSERT OR IGNORE INTO deps VALUES(?,?)", (task_id, dep))

    def _refresh_blocked(self, db: sqlite3.Connection) -> None:
        rows = db.execute("""SELECT t.id, t.status, EXISTS(
            SELECT 1 FROM deps d JOIN tasks p ON p.id=d.blocked_by
            WHERE d.task_id=t.id AND p.status!='done') AS waiting
            FROM tasks t WHERE t.status IN ('todo','blocked')""").fetchall()
        for row in rows:
            status = "blocked" if row["waiting"] else "todo"
            if status != row["status"]:
                db.execute(
                    "UPDATE tasks SET status=?,updated_at=? WHERE id=?",
                    (status, self.stamp(), row["id"]),
                )
                self._event(db, row["id"], "system", status)

    def _create(
        self,
        db: sqlite3.Connection,
        task: dict,
        actor: str,
        *,
        task_id: str | None = None,
        dependencies: bool = True,
    ) -> dict:
        task = self._validate_template(task)
        if task.get("dedupe_key"):
            found = db.execute(
                """SELECT * FROM tasks WHERE dedupe_key=?
                AND status IN ('todo','claimed','running','blocked','review')""",
                (task["dedupe_key"],),
            ).fetchone()
            if found:
                result = self._task(db, found)
                result["deduplicated"] = True
                return result
        task_id = task_id or uid(self.now())
        now = self.stamp()
        db.execute(
            """INSERT INTO tasks(id,title,body,queue,assignee,priority,due_at,tags_json,
            dedupe_key,params_json,status,max_attempts,available_at,needs_approval,created_at,
            updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,'todo',?,?,?,?,?)""",
            (
                task_id,
                task["title"].strip(),
                task.get("body", ""),
                task.get("queue", "default"),
                task.get("assignee"),
                task.get("priority", 0),
                task.get("due_at"),
                encode(task.get("tags", [])),
                task.get("dedupe_key") or None,
                encode(task.get("params", {})),
                task.get("max_attempts", 3),
                now,
                int(task.get("needs_approval", False)),
                now,
                now,
            ),
        )
        if dependencies:
            self._dependencies(db, task_id, task.get("blocked_by", []))
            self._refresh_blocked(db)
        self._event(db, task_id, actor, "created")
        return self._task(db, self._row(db, task_id))

    def create_task(self, title: str, *, actor: str = "human", **fields: Any) -> dict:
        with self.transaction() as db:
            return self._create(db, {"title": title, **fields}, actor)

    def add_dependency(self, task_id: str, blocked_by: str, actor: str = "human") -> dict:
        with self.transaction() as db:
            task = self._row(db, task_id)
            if task["status"] not in {"todo", "blocked"}:
                raise BoardError("invalid_state", "Only unclaimed tasks accept dependencies")
            self._dependencies(db, task_id, [blocked_by])
            self._refresh_blocked(db)
            self._event(db, task_id, actor, "dependency_added", {"blocked_by": blocked_by})
            return self._task(db, self._row(db, task_id))

    def _expire(self, db: sqlite3.Connection) -> int:
        rows = db.execute(
            "SELECT * FROM tasks WHERE status IN ('claimed','running') AND lease_until<=?",
            (self.stamp(),),
        ).fetchall()
        for row in rows:
            status = "failed" if row["attempts"] >= row["max_attempts"] else "todo"
            db.execute(
                """UPDATE tasks SET status=?, lease_owner=NULL, lease_token=NULL,
                lease_until=NULL, available_at=?,error='Lease expired', updated_at=? WHERE id=?""",
                (status, self.stamp(), self.stamp(), row["id"]),
            )
            self._event(
                db,
                row["id"],
                "system",
                "lease_expired",
                {"former_owner": row["lease_owner"], "status": status},
            )
        self._refresh_blocked(db)
        return len(rows)

    def claim(
        self, agent: str, queue: str | None = None, lease_seconds: int | None = None
    ) -> dict | None:
        if not agent.strip():
            raise BoardError("invalid_agent", "agent cannot be empty", 2)
        seconds = self.lease_seconds if lease_seconds is None else lease_seconds
        if seconds <= 0:
            raise BoardError("invalid_lease", "lease_seconds must be positive", 2)
        with self.transaction() as db:
            self._expire(db)
            row = db.execute(
                """SELECT * FROM tasks WHERE status='todo' AND available_at<=?
                AND (assignee IS NULL OR assignee=?) AND (? IS NULL OR queue=?)
                ORDER BY priority DESC,created_at,id LIMIT 1""",
                (self.stamp(), agent, queue, queue),
            ).fetchone()
            if row is None:
                return None
            token = secrets.token_urlsafe(32)
            db.execute(
                """UPDATE tasks SET status='claimed',attempts=attempts+1,
                lease_owner=?,lease_token=?,lease_until=?,updated_at=? WHERE id=?""",
                (
                    agent,
                    token,
                    self.stamp(self.now() + timedelta(seconds=seconds)),
                    self.stamp(),
                    row["id"],
                ),
            )
            self._event(db, row["id"], agent, "claimed", {"attempt": row["attempts"] + 1})
            return self._task(db, self._row(db, row["id"]), lease=True)

    def _owned(self, db: sqlite3.Connection, task_id: str, agent: str, token: str) -> sqlite3.Row:
        row = self._row(db, task_id)
        if (
            row["status"] not in {"claimed", "running"}
            or row["lease_owner"] != agent
            or not token
            or not secrets.compare_digest(row["lease_token"] or "", token)
            or row["lease_until"] <= self.stamp()
        ):
            raise BoardError(
                "lease_conflict", "Lease is expired, replaced, or owned by another worker"
            )
        return row

    def heartbeat(
        self, task_id: str, agent: str, token: str, lease_seconds: int | None = None
    ) -> dict:
        seconds = self.lease_seconds if lease_seconds is None else lease_seconds
        if seconds <= 0:
            raise BoardError("invalid_lease", "lease_seconds must be positive", 2)
        with self.transaction() as db:
            self._owned(db, task_id, agent, token)
            db.execute(
                "UPDATE tasks SET status='running',lease_until=?,updated_at=? WHERE id=?",
                (self.stamp(self.now() + timedelta(seconds=seconds)), self.stamp(), task_id),
            )
            self._event(db, task_id, agent, "heartbeat")
            return self._task(db, self._row(db, task_id), lease=True)

    def complete(
        self,
        task_id: str,
        agent: str,
        token: str,
        result: dict | None = None,
        artifacts: list[str] | None = None,
    ) -> dict:
        if not isinstance(result or {}, dict) or not isinstance(artifacts or [], list):
            raise BoardError("invalid_result", "result must be object; artifacts must be array", 2)
        if not all(isinstance(p, str) for p in (artifacts or [])):
            raise BoardError("invalid_result", "Artifacts must be path strings", 2)
        with self.transaction() as db:
            row = self._owned(db, task_id, agent, token)
            status = "review" if row["needs_approval"] else "done"
            db.execute(
                """UPDATE tasks SET status=?,result_json=?,artifacts_json=?,lease_owner=NULL,
                lease_token=NULL,lease_until=NULL,error=NULL,updated_at=? WHERE id=?""",
                (status, encode(result or {}), encode(artifacts or []), self.stamp(), task_id),
            )
            self._event(db, task_id, agent, status, {"artifacts": artifacts or []})
            self._refresh_blocked(db)
            task = self._task(db, self._row(db, task_id))
        if status == "done":
            self._dispatch_hooks(task)
        return task

    def fail(self, task_id: str, agent: str, token: str, error: str) -> dict:
        with self.transaction() as db:
            row = self._owned(db, task_id, agent, token)
            status = "todo" if row["attempts"] < row["max_attempts"] else "failed"
            delay = min(self.retry_base_seconds * 2 ** min(row["attempts"] - 1, 20), 86400)
            db.execute(
                """UPDATE tasks SET status=?,error=?,available_at=?,lease_owner=NULL,
                lease_token=NULL,lease_until=NULL,updated_at=? WHERE id=?""",
                (
                    status,
                    error,
                    self.stamp(self.now() + timedelta(seconds=delay)),
                    self.stamp(),
                    task_id,
                ),
            )
            self._event(
                db,
                task_id,
                agent,
                "failed_attempt",
                {"error": error, "next_status": status, "retry_in_seconds": delay},
            )
            return self._task(db, self._row(db, task_id))

    def review(self, task_id: str, approved: bool, actor: str = "human", reason: str = "") -> dict:
        with self.transaction() as db:
            row = self._row(db, task_id)
            if row["status"] != "review":
                raise BoardError("invalid_state", "Only review tasks can be approved or rejected")
            status = "done" if approved else "todo"
            db.execute(
                "UPDATE tasks SET status=?,attempts=?,available_at=?,updated_at=? WHERE id=?",
                (status, row["attempts"] if approved else 0, self.stamp(), self.stamp(), task_id),
            )
            self._event(
                db, task_id, actor, "approved" if approved else "rejected", {"reason": reason}
            )
            self._refresh_blocked(db)
            task = self._task(db, self._row(db, task_id))
        if approved:
            self._dispatch_hooks(task)
        return task

    def comment(self, task_id: str, text: str, actor: str = "human") -> dict:
        if not text.strip():
            raise BoardError("invalid_comment", "Comment cannot be empty", 2)
        with self.transaction() as db:
            self._row(db, task_id)
            self._event(db, task_id, actor, "comment", {"text": text})
        return {"task_id": task_id, "text": text}

    def reassign(self, task_id: str, assignee: str | None, actor: str = "human") -> dict:
        with self.transaction() as db:
            row = self._row(db, task_id)
            if row["status"] not in ACTIVE:
                raise BoardError("invalid_state", "Only unfinished tasks can be reassigned")
            status = "todo" if row["status"] in {"claimed", "running"} else row["status"]
            db.execute(
                """UPDATE tasks SET assignee=?,status=?,lease_owner=NULL,lease_token=NULL,
                lease_until=NULL,available_at=?,updated_at=? WHERE id=?""",
                (assignee or None, status, self.stamp(), self.stamp(), task_id),
            )
            self._refresh_blocked(db)
            self._event(db, task_id, actor, "reassigned", {"assignee": assignee})
            return self._task(db, self._row(db, task_id))

    def cancel(self, task_id: str, *, confirm: bool = False, actor: str = "human") -> dict:
        if not confirm:
            raise BoardError(
                "confirmation_required", "Cancellation requires --yes / confirm=true", 2
            )
        with self.transaction() as db:
            row = self._row(db, task_id)
            if row["status"] == "done":
                raise BoardError("invalid_state", "Completed tasks cannot be cancelled")
            db.execute(
                """UPDATE tasks SET status='cancelled',lease_owner=NULL,lease_token=NULL,
                lease_until=NULL,updated_at=? WHERE id=?""",
                (self.stamp(), task_id),
            )
            self._event(db, task_id, actor, "cancelled")
            self._refresh_blocked(db)
            return self._task(db, self._row(db, task_id))

    def retry(self, task_id: str, actor: str = "human") -> dict:
        with self.transaction() as db:
            row = self._row(db, task_id)
            if row["status"] not in {"failed", "cancelled"}:
                raise BoardError("invalid_state", "Only failed/cancelled tasks can be retried")
            try:
                db.execute(
                    "UPDATE tasks SET status='todo',attempts=0,available_at=?,updated_at=? "
                    "WHERE id=?",
                    (self.stamp(), self.stamp(), task_id),
                )
            except sqlite3.IntegrityError as exc:
                raise BoardError(
                    "dedupe_conflict", "Another unfinished task owns this dedupe key"
                ) from exc
            self._refresh_blocked(db)
            self._event(db, task_id, actor, "retried")
            return self._task(db, self._row(db, task_id))

    def get_task(self, task_id: str) -> dict:
        with self.connect() as db:
            return self._task(db, self._row(db, task_id))

    def list_tasks(
        self,
        *,
        status: str | None = None,
        queue: str | None = None,
        agent: str | None = None,
        limit: int = 1000,
    ) -> list[dict]:
        if status is not None and status not in STATUSES:
            raise BoardError("invalid_status", f"Unknown status: {status}", 2)
        if not 1 <= limit <= 100000:
            raise BoardError("invalid_limit", "limit must be 1..100000", 2)
        with self.connect() as db:
            rows = db.execute(
                """SELECT * FROM tasks WHERE (? IS NULL OR status=?)
                AND (? IS NULL OR queue=?) AND (? IS NULL OR assignee=? OR lease_owner=?)
                ORDER BY priority DESC,created_at,id LIMIT ?""",
                (status, status, queue, queue, agent, agent, agent, limit),
            ).fetchall()
            return [self._task(db, row) for row in rows]

    def events(self, task_id: str | None = None, after: int = 0, limit: int = 1000) -> list[dict]:
        with self.connect() as db:
            if task_id:
                self._row(db, task_id)
            rows = db.execute(
                """SELECT * FROM events WHERE (? IS NULL OR task_id=?)
                AND id>? ORDER BY id LIMIT ?""",
                (task_id, task_id, after, limit),
            ).fetchall()
            results = []
            for row in rows:
                result = dict(row)
                result["payload"] = json.loads(result.pop("payload_json"))
                results.append(result)
            return results

    def add_schedule(self, expression: str, template: dict, actor: str = "human") -> dict:
        if len(expression.split()) != 5 or not croniter.is_valid(expression):
            raise BoardError("invalid_cron", "Expected a valid five-field cron expression", 2)
        template = self._validate_template(template)
        with self.transaction() as db:
            for dep in template.get("blocked_by", []):
                self._row(db, dep)
            schedule_id = uid(self.now())
            db.execute(
                "INSERT INTO schedules VALUES(?,?,?,?,1,?)",
                (schedule_id, expression, encode(template), self.stamp(), self.stamp()),
            )
            self._event(db, None, actor, "schedule_created", {"id": schedule_id})
        return {"id": schedule_id, "cron": expression, "template": template}

    def schedules(self) -> list[dict]:
        with self.connect() as db:
            result = []
            for row in db.execute("SELECT * FROM schedules ORDER BY created_at,id"):
                item = dict(row)
                item["template"] = json.loads(item.pop("template_json"))
                item["enabled"] = bool(item["enabled"])
                result.append(item)
            return result

    def set_schedule_enabled(self, schedule_id: str, enabled: bool) -> dict:
        with self.transaction() as db:
            if not db.execute("SELECT 1 FROM schedules WHERE id=?", (schedule_id,)).fetchone():
                raise BoardError("not_found", "Schedule not found", 3)
            # Resuming starts now, avoiding an unexpected burst of missed work.
            db.execute(
                "UPDATE schedules SET enabled=?,last_run_at=? WHERE id=?",
                (int(enabled), self.stamp(), schedule_id),
            )
            self._event(
                db, None, "human", "schedule_toggled", {"id": schedule_id, "enabled": enabled}
            )
        return {"id": schedule_id, "enabled": enabled}

    def tick(self, max_catchup: int = 100) -> dict:
        if not 1 <= max_catchup <= 10000:
            raise BoardError("invalid_limit", "max_catchup must be 1..10000", 2)
        with self.transaction() as db:
            expired = self._expire(db)
            generated, more = [], False
            for row in db.execute("SELECT * FROM schedules WHERE enabled=1").fetchall():
                base = datetime.fromisoformat(row["last_run_at"]).astimezone(self.zone)
                iterator = croniter(row["cron"], base)
                due = iterator.get_next(datetime)
                count = 0
                while due <= self.now() and count < max_catchup:
                    template = json.loads(row["template_json"])
                    template["dedupe_key"] = f"schedule:{row['id']}:{self.stamp(due)}"
                    task = self._create(db, template, "scheduler")
                    self._event(
                        db,
                        task["id"],
                        "scheduler",
                        "scheduled",
                        {"schedule_id": row["id"], "scheduled_for": self.stamp(due)},
                    )
                    generated.append(task["id"])
                    db.execute(
                        "UPDATE schedules SET last_run_at=? WHERE id=?",
                        (self.stamp(due), row["id"]),
                    )
                    due = iterator.get_next(datetime)
                    count += 1
                more = more or due <= self.now()
            return {"expired_leases": expired, "generated": generated, "catchup_pending": more}

    def _dispatch_hooks(self, task: dict) -> None:
        """Explicitly enabled, best-effort post-commit hooks; never execute task-supplied code."""
        if not self.hooks.get("enabled", False):
            return
        hooks = []
        if self.hooks.get("command"):
            hooks.append("command")
        if self.hooks.get("webhook") and self.hooks.get("allow_network", False):
            hooks.append("webhook")
        for kind in hooks:
            try:
                if kind == "command":
                    command = self.hooks["command"]
                    if not isinstance(command, list) or not all(
                        isinstance(x, str) for x in command
                    ):
                        raise ValueError("hooks.command must be an argv array")
                    subprocess.run(
                        command,
                        input=encode(task),
                        text=True,
                        capture_output=True,
                        timeout=10,
                        check=True,
                        shell=False,
                    )
                else:
                    request = urllib.request.Request(
                        self.hooks["webhook"],
                        data=encode(task).encode(),
                        headers={"Content-Type": "application/json"},
                        method="POST",
                    )
                    with urllib.request.urlopen(request, timeout=5) as response:
                        response.read(1024)
                with self.transaction() as db:
                    self._event(db, task["id"], "hook", "hook_delivered", {"kind": kind})
            except Exception as exc:
                with self.transaction() as db:
                    self._event(
                        db, task["id"], "hook", "hook_failed", {"kind": kind, "error": str(exc)}
                    )

    def snapshot(self) -> dict:
        with self.transaction() as db:
            tasks = [
                self._task(db, r) for r in db.execute("SELECT * FROM tasks ORDER BY created_at,id")
            ]
            schedules = [dict(r) for r in db.execute("SELECT * FROM schedules")]
            events = [dict(r) for r in db.execute("SELECT * FROM events ORDER BY id")]
            return {
                "schema_version": 1,
                "exported_at": self.stamp(),
                "tasks": tasks,
                "schedules": schedules,
                "events": events,
            }

    def export(self, path: str | Path, format: str = "json", bom: bool = True) -> dict:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        snapshot = self.snapshot()
        if format == "json":
            path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
        elif format == "csv":
            fields = [
                "id",
                "title",
                "body",
                "queue",
                "assignee",
                "priority",
                "due_at",
                "tags",
                "dedupe_key",
                "params",
                "result",
                "artifacts",
                "status",
                "attempts",
                "max_attempts",
                "needs_approval",
                "blocked_by",
                "created_at",
                "updated_at",
            ]
            with path.open("w", newline="", encoding="utf-8-sig" if bom else "utf-8") as file:
                writer = csv.DictWriter(file, fields, extrasaction="ignore")
                writer.writeheader()
                for task in snapshot["tasks"]:
                    writer.writerow(
                        {
                            key: encode(value) if isinstance(value, (dict, list, bool)) else value
                            for key, value in task.items()
                        }
                    )
        else:
            raise BoardError("invalid_format", "format must be json or csv", 2)
        return {"path": str(path.resolve()), "count": len(snapshot["tasks"]), "format": format}

    def import_file(self, path: str | Path) -> dict:
        path = Path(path)
        if path.suffix.lower() == ".csv":
            tasks = list(csv.DictReader(io.StringIO(path.read_text(encoding="utf-8-sig"))))
            for task in tasks:
                for key in ("tags", "params", "result", "artifacts", "blocked_by"):
                    if task.get(key):
                        task[key] = json.loads(task[key])
                    else:
                        task.pop(key, None)
                for key in ("priority", "max_attempts", "attempts"):
                    if task.get(key):
                        task[key] = int(task[key])
                    else:
                        task.pop(key, None)
                task["needs_approval"] = str(task.get("needs_approval", "false")).lower() in {
                    "true",
                    "1",
                }
                for key in ("assignee", "dedupe_key", "due_at"):
                    task[key] = task.get(key) or None
            payload = {"tasks": tasks}
        else:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
            if isinstance(payload, list):
                payload = {"tasks": payload}
        if not isinstance(payload, dict) or not isinstance(payload.get("tasks"), list):
            raise BoardError("invalid_import", "Expected task array or object with tasks array", 2)
        tasks = [self._validate_template(t) for t in payload["tasks"]]
        created, skipped, id_map = [], [], {}
        with self.transaction() as db:
            for task in tasks:
                task_id = task.get("id") or uid(self.now())
                if not isinstance(task_id, str):
                    raise BoardError("invalid_import", "Task id must be a string", 2)
                if db.execute("SELECT 1 FROM tasks WHERE id=?", (task_id,)).fetchone():
                    skipped.append(task_id)
                    id_map[task_id] = task_id
                    continue
                result = self._create(db, task, "import", task_id=task_id, dependencies=False)
                id_map[task_id] = result["id"]
                if result.get("deduplicated"):
                    skipped.append(result["id"])
                    continue
                status = task.get("status", "todo")
                if status not in STATUSES:
                    raise BoardError("invalid_status", f"Invalid imported status: {status}", 2)
                # Workers must acquire fresh leases in this database.
                status = "todo" if status in {"claimed", "running", "blocked"} else status
                db.execute(
                    """UPDATE tasks SET status=?,result_json=?,artifacts_json=?,
                    attempts=?,created_at=?,updated_at=? WHERE id=?""",
                    (
                        status,
                        encode(task.get("result", {})),
                        encode(task.get("artifacts", [])),
                        int(task.get("attempts", 0)),
                        self.parse_date(task.get("created_at")) or self.stamp(),
                        self.parse_date(task.get("updated_at")) or self.stamp(),
                        task_id,
                    ),
                )
                created.append((task_id, task))
            for task_id, task in created:
                self._dependencies(
                    db, task_id, [id_map.get(dep, dep) for dep in task.get("blocked_by", [])]
                )
            self._refresh_blocked(db)
            for schedule in payload.get("schedules", []):
                if not croniter.is_valid(schedule["cron"]):
                    raise BoardError("invalid_cron", "Invalid imported schedule", 2)
                template = schedule.get("template") or json.loads(schedule["template_json"])
                self._validate_template(template)
                db.execute(
                    "INSERT OR IGNORE INTO schedules VALUES(?,?,?,?,?,?)",
                    (
                        schedule["id"],
                        schedule["cron"],
                        encode(template),
                        self.parse_date(schedule["last_run_at"]),
                        int(schedule.get("enabled", 1)),
                        self.parse_date(schedule["created_at"]),
                    ),
                )
            imported_ids = {task_id for task_id, _ in created}
            for event in payload.get("events", []):
                if event.get("task_id") in imported_ids:
                    db.execute(
                        "INSERT INTO events(task_id,actor,type,payload_json,at) VALUES(?,?,?,?,?)",
                        (
                            event["task_id"],
                            event["actor"],
                            event["type"],
                            event["payload_json"],
                            self.parse_date(event["at"]),
                        ),
                    )
        return {"imported": len(created), "skipped": len(skipped)}

    def backup(self, out: str | Path | None = None) -> dict:
        directory = Path(out) if out else self.data_dir / "backups"
        directory.mkdir(parents=True, exist_ok=True)
        name = self.now().astimezone(self.zone).strftime("agent-task-board_%Y%m%dT%H%M%S_%f.zip")
        path = directory / name
        with tempfile.TemporaryDirectory() as tmp:
            copy = Path(tmp) / "tasks.db"
            with self.connect() as source, sqlite3.connect(copy) as target:
                source.backup(target)
            with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
                archive.write(copy, "tasks.db")
                archive.writestr(
                    "backup.json", encode({"schema_version": 1, "created_at": self.stamp()})
                )
        return {"path": str(path.resolve())}
