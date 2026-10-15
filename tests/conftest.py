from datetime import UTC, datetime, timedelta

import pytest

from agent_task_board.core import Board


class Clock:
    def __init__(self):
        self.value = datetime(2026, 1, 1, tzinfo=UTC)

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += timedelta(seconds=seconds)


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def board(tmp_path, clock):
    return Board(tmp_path, clock=clock, lease_seconds=10, retry_base_seconds=2)
