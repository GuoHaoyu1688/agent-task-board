"""Acceptance test: ten real processes atomically drain a thousand tasks."""

import multiprocessing
from concurrent.futures import ProcessPoolExecutor

from agent_task_board.core import Board


def worker(path, number):
    board = Board(path)
    claimed = []
    while task := board.claim(f"worker-{number}"):
        claimed.append(task["id"])
        board.complete(task["id"], f"worker-{number}", task["lease_token"], {"worker": number})
    return claimed


def test_ten_processes_claim_1000_tasks_exactly_once(tmp_path):
    board = Board(tmp_path)
    with board.transaction() as db:
        expected = {
            board._create(db, {"title": f"Fictional task {i}"}, "test", dependencies=False)["id"]
            for i in range(1000)
        }
    with ProcessPoolExecutor(
        max_workers=10, mp_context=multiprocessing.get_context("spawn")
    ) as pool:
        results = list(pool.map(worker, [str(tmp_path)] * 10, range(10)))
    actual = [task_id for ids in results for task_id in ids]
    assert len(actual) == len(set(actual)) == 1000
    assert set(actual) == expected
    assert len(board.list_tasks(status="done")) == 1000
    assert all(task["attempts"] == 1 for task in board.list_tasks())
