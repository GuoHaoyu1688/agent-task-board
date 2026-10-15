"""Explicit TOML configuration, with CLI > environment > config > default precedence."""

import os
import tomllib
from pathlib import Path

from .core import Board, BoardError


def make_board(data_dir: str | None = None, config: str | None = None) -> Board:
    settings = {}
    if config:
        with Path(config).expanduser().open("rb") as stream:
            settings = tomllib.load(stream)
    timezone = settings.get("timezone", "Asia/Shanghai")
    try:
        return Board(
            data_dir or os.environ.get("ATB_DATA_DIR") or settings.get("data_dir", "data"),
            timezone=timezone,
            lease_seconds=int(settings.get("lease_seconds", 900)),
            retry_base_seconds=int(settings.get("retry_base_seconds", 30)),
            hooks=settings.get("hooks", {}),
        )
    except (ValueError, KeyError) as exc:
        raise BoardError("invalid_config", str(exc), 2) from exc
