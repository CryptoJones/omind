# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Aaron K. Clark
"""Machine-local access statistics for dynamic core-memory selection."""

from __future__ import annotations

import math
import sqlite3
import time
from pathlib import Path

from omind import paths

_MAX_AGE_DAYS = 90


def record(omi_dir: Path | str, filename: str, *, now: float | None = None) -> None:
    """Record one actual note read. Advisory and fail-open."""
    path = paths.access_state_path(Path(omi_dir))
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(path, timeout=2.0) as db:
            db.execute("PRAGMA busy_timeout=2000")
            db.execute(
                "CREATE TABLE IF NOT EXISTS access ("
                "filename TEXT PRIMARY KEY, count INTEGER NOT NULL, last_access REAL NOT NULL)"
            )
            db.execute(
                "INSERT INTO access(filename, count, last_access) VALUES (?, 1, ?)"
                " ON CONFLICT(filename) DO UPDATE SET"
                " count = count + 1, last_access = excluded.last_access",
                (filename, now if now is not None else time.time()),
            )
    except (OSError, sqlite3.Error):
        return


def core_members(
    omi_dir: Path | str,
    *,
    limit: int = 3,
    now: float | None = None,
    max_age_days: int = _MAX_AGE_DAYS,
) -> list[str]:
    """Best recently/frequently read notes, newest score first."""
    path = paths.access_state_path(Path(omi_dir))
    if limit <= 0 or not path.is_file():
        return []
    current = now if now is not None else time.time()
    cutoff = current - max_age_days * 86_400
    try:
        with sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=1.0) as db:
            rows = db.execute(
                "SELECT filename, count, last_access FROM access WHERE last_access >= ?",
                (cutoff,),
            )
            scored = [
                (
                    math.log1p(int(count))
                    + 1.0 / (1.0 + max(0.0, current - float(last_access)) / 2_592_000),
                    float(last_access),
                    str(filename),
                )
                for filename, count, last_access in rows
            ]
    except (OSError, sqlite3.Error, TypeError, ValueError):
        return []
    scored.sort(key=lambda item: (-item[0], -item[1], item[2]))
    return [filename for _score, _last_access, filename in scored[:limit]]
