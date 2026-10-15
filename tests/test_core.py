import json
import re
import sqlite3
import sys
import zipfile

import pytest

from agent_task_board.core import Board, BoardError


def done(board, agent="worker"):
    task = board.claim(agent)
    return board.complete(task["id"], agent, task["lease_token"], {"answer": 42})


def test_priority_assignment_dedupe_and_unicode(board):
    low = board.create_task("Example A", dedupe_key="a", priority=1, tags=["example"])
    high = board.create_task("Unicode: 水", priority=8, assignee="alice", params={"input": 4})
    assert re.fullmatch(r"[0-9A-HJKMNP-TV-Z]{26}", low["id"])
    assert board.create_task("Repeated", dedupe_key="a")["id"] == low["id"]
    assert board.claim("bob")["id"] == low["id"]
    assert board.claim("alice")["id"] == high["id"]
    assert board.claim("idle") is None
    assert "lease_token" not in board.get_task(low["id"])
    assert all("lease_token" not in t for t in board.list_tasks())


def test_lease_expiry_fences_even_the_same_agent(board, clock):
    task = board.create_task("Fence")
    first = board.claim("worker")
    clock.advance(11)
    second = board.claim("worker")
    assert first["id"] == second["id"] == task["id"]
    assert first["lease_token"] != second["lease_token"]
    for fn, args in ((board.heartbeat, ()), (board.complete, ({}, [])), (board.fail, ("old",))):
        with pytest.raises(BoardError, match="Lease"):
            fn(task["id"], "worker", first["lease_token"], *args)
    assert board.complete(task["id"], "worker", second["lease_token"])["status"] == "done"


def test_expired_lease_cannot_be_renewed_even_before_tick(board, clock):
    board.create_task("Expired")
    task = board.claim("worker")
    clock.advance(10)
    with pytest.raises(BoardError):
        board.heartbeat(task["id"], "worker", task["lease_token"])
    assert board.tick()["expired_leases"] == 1


def test_heartbeat_renewal_and_owner(board, clock):
    board.create_task("Long operation")
    task = board.claim("alice")
    clock.advance(8)
    with pytest.raises(BoardError):
        board.heartbeat(task["id"], "bob", task["lease_token"])
    renewed = board.heartbeat(task["id"], "alice", task["lease_token"])
    assert renewed["status"] == "running"
    clock.advance(5)
    assert board.claim("bob") is None
    assert board.complete(task["id"], "alice", task["lease_token"])["status"] == "done"


def test_exponential_retry_and_max_attempts(board, clock):
    board.create_task("Retries", max_attempts=3)
    for attempt, delay in [(1, 2), (2, 4), (3, 8)]:
        task = board.claim("w")
        assert task["attempts"] == attempt
        failed = board.fail(task["id"], "w", task["lease_token"], "Example error")
        assert failed["status"] == ("todo" if attempt < 3 else "failed")
        assert board.claim("w") is None
        clock.advance(delay)
    assert board.claim("w") is None
    board.retry(task["id"])
    assert board.claim("w")["attempts"] == 1


def test_expiry_exhausts_attempts(board, clock):
    original = board.create_task("Only once", max_attempts=1)
    board.claim("w")
    clock.advance(11)
    assert board.tick()["expired_leases"] == 1
    assert board.get_task(original["id"])["status"] == "failed"


def test_dependencies_cycles_and_approval(board):
    parent = board.create_task("Needs approval", needs_approval=True)
    child = board.create_task("After approval", blocked_by=[parent["id"]])
    assert child["status"] == "blocked"
    with pytest.raises(BoardError, match="acyclic"):
        board.add_dependency(parent["id"], child["id"])
    with pytest.raises(BoardError, match="acyclic"):
        board.add_dependency(parent["id"], parent["id"])
    assert done(board)["status"] == "review"
    assert board.claim("w") is None
    board.review(parent["id"], False, reason="Please revise")
    assert done(board)["status"] == "review"
    assert board.review(parent["id"], True)["status"] == "done"
    assert board.get_task(child["id"])["status"] == "todo"
    assert done(board)["id"] == child["id"]


def test_reassign_invalidates_lease_and_comments(board):
    task = board.create_task("Transfer")
    lease = board.claim("alice")
    board.comment(task["id"], "Prepared input", "alice")
    board.reassign(task["id"], "bob")
    with pytest.raises(BoardError):
        board.complete(task["id"], "alice", lease["lease_token"])
    assert board.claim("alice") is None
    assert board.claim("bob")["id"] == task["id"]
    assert [e["type"] for e in board.events(task["id"])] == [
        "created",
        "claimed",
        "comment",
        "reassigned",
        "claimed",
    ]


def test_cancel_requires_confirmation_and_retry_dedupe_conflict(board):
    task = board.create_task("Cancel", dedupe_key="stable")
    with pytest.raises(BoardError):
        board.cancel(task["id"])
    assert board.cancel(task["id"], confirm=True)["status"] == "cancelled"
    board.create_task("Replacement", dedupe_key="stable")
    with pytest.raises(BoardError, match="dedupe"):
        board.retry(task["id"])


def test_cron_timezone_catchup_and_no_duplicates(board, clock):
    # 00:00 UTC is 08:00 Asia/Shanghai; first scheduled occurrence is 08:01.
    schedule = board.add_schedule("1 8 * * *", {"title": "Daily example"})
    assert board.tick()["generated"] == []
    clock.advance(60)
    first = board.tick()
    assert len(first["generated"]) == 1
    assert board.tick()["generated"] == []
    clock.advance(3 * 86400)
    assert board.tick(max_catchup=2)["catchup_pending"]
    assert len(board.tick(max_catchup=2)["generated"]) == 1
    assert board.tick()["generated"] == []
    board.set_schedule_enabled(schedule["id"], False)
    clock.advance(86400)
    assert board.tick()["generated"] == []
    board.set_schedule_enabled(schedule["id"], True)
    assert board.schedules()[0]["enabled"]


@pytest.mark.parametrize("format", ["json", "csv"])
def test_roundtrip_import_is_idempotent_and_preserves_dependencies(board, tmp_path, format):
    parent = board.create_task("Parent", tags=["a", "b"], params={"n": 3})
    child = board.create_task("Child", blocked_by=[parent["id"]], needs_approval=True)
    board.comment(parent["id"], "Example note")
    done(board)
    path = tmp_path / ("export." + format)
    board.export(path, format)
    if format == "csv":
        assert path.read_bytes().startswith(b"\xef\xbb\xbf")
    restored = Board(tmp_path / "restored")
    assert restored.import_file(path) == {"imported": 2, "skipped": 0}
    assert restored.import_file(path) == {"imported": 0, "skipped": 2}
    assert restored.get_task(child["id"])["blocked_by"] == [parent["id"]]
    assert restored.get_task(parent["id"])["status"] == "done"
    assert restored.get_task(parent["id"])["result"] == {"answer": 42}
    if format == "json":
        assert any(e["type"] == "comment" for e in restored.events(parent["id"]))


def test_import_cycle_rolls_back_everything(board, tmp_path):
    path = tmp_path / "bad.json"
    path.write_text(
        json.dumps(
            [
                {"id": "a", "title": "A", "blocked_by": ["b"]},
                {"id": "b", "title": "B", "blocked_by": ["a"]},
            ]
        )
    )
    with pytest.raises(BoardError):
        board.import_file(path)
    assert board.list_tasks() == []


def test_import_active_lease_is_not_restored(board, tmp_path):
    task = board.create_task("Running")
    board.claim("worker")
    path = tmp_path / "snapshot.json"
    board.export(path)
    restored = Board(tmp_path / "restored")
    restored.import_file(path)
    assert restored.get_task(task["id"])["status"] == "todo"
    assert restored.claim("new")["id"] == task["id"]


def test_backup_captures_wal_and_integrity(board, tmp_path):
    board.create_task("Durable")
    archive = board.backup()["path"]
    with zipfile.ZipFile(archive) as zipped:
        zipped.extract("tasks.db", tmp_path / "restore")
    with sqlite3.connect(tmp_path / "restore/tasks.db") as db:
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert db.execute("SELECT count(*) FROM tasks").fetchone()[0] == 1


def test_completion_hooks_disabled_and_explicitly_enabled(tmp_path):
    output = tmp_path / "hook-output.json"
    command = [
        sys.executable,
        "-c",
        "import pathlib,sys; pathlib.Path(sys.argv[1]).write_text(sys.stdin.read())",
        str(output),
    ]
    board = Board(tmp_path / "board", hooks={"command": command})
    board.create_task("Disabled")
    done(board)
    assert not output.exists()
    board.hooks["enabled"] = True
    board.create_task("Enabled")
    task = done(board)
    assert json.loads(output.read_text())["id"] == task["id"]
    assert board.events(task["id"])[-1]["type"] == "hook_delivered"
    board.hooks["command"] = ["/nonexistent-example-hook"]
    board.create_task("Hook failure")
    task = done(board)
    assert task["status"] == "done"
    assert board.events(task["id"])[-1]["type"] == "hook_failed"


@pytest.mark.parametrize(
    "fields",
    [
        {"title": ""},
        {"title": "x", "max_attempts": 0},
        {"title": "x", "priority": "high"},
        {"title": "x", "tags": "tag"},
        {"title": "x", "params": []},
        {"title": "x", "due_at": "yesterday"},
        {"title": "x", "needs_approval": 1},
    ],
)
def test_input_validation(board, fields):
    with pytest.raises(BoardError):
        board.create_task(**fields)
    assert board.list_tasks() == []


def test_date_defaults_to_shanghai_and_missing_errors(board):
    task = board.create_task("Deadline", due_at="2026-01-01T09:00")
    assert task["due_at"] == "2026-01-01T01:00:00.000000+00:00"
    with pytest.raises(BoardError) as error:
        board.get_task("missing")
    assert error.value.exit_code == 3
    with pytest.raises(BoardError):
        board.add_schedule("invalid", {"title": "Bad"})
    with pytest.raises(BoardError):
        board.list_tasks(status="other")
