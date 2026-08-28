# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Aaron K. Clark
"""Machine-local access statistics for dynamic core-memory selection.

Two independent consumers share the one ``access`` table (2026-08-27 roundtable,
item #2): the #173 tiered-core scorer (:func:`core_members`) and the usefulness
feedback leg (:func:`usefulness_weights`). They *share the table and keep their
own scorer* — the two effects are never blended, or a note would be counted
twice.

The split that keeps them honest lives in the schema:

* ``count`` / ``last_access`` — every read, no dedupe. Feeds #173 unchanged; the
  usefulness leg never looks at these.
* ``organic_count`` / ``organic_last`` — only *organic* reads (an agent
  explicitly calling read-note/recall-note), deduped once per session. This is
  the clean usefulness signal. Capsule/hook/turn-preflight reads are excluded
  (their position basis is not evidence a human found the note useful — the
  Cortana/Wintermute concern the panel adopted as mandatory).
* ``filtered_count`` — reads the usefulness leg deliberately dropped
  (non-organic, or an organic re-read already seen this session). Kept only so
  ``search --explain`` can show *counted and filtered* reads side by side.
"""

from __future__ import annotations

import contextlib
import math
import sqlite3
import time
from pathlib import Path

from omind import paths

_MAX_AGE_DAYS = 90

#: Usefulness decay (item #2). Decay-only, no boost: a term that only subtracts
#: cannot self-reinforce, so this can only ever *demote* an organically-read
#: note that has since gone stale — never push a popular one up. The floor and
#: the 30-day onset are HAL9000's dissent, adopted: a note read recently (or
#: never read at all — new/never-surfaced) is untouched, and even a long-stale
#: note bottoms out at the floor rather than being buried.
_USEFUL_ONSET_DAYS = 30.0
_USEFUL_FLOOR = 0.7


def _ensure_schema(db: sqlite3.Connection) -> None:
    db.execute(
        "CREATE TABLE IF NOT EXISTS access ("
        "filename TEXT PRIMARY KEY, count INTEGER NOT NULL, last_access REAL NOT NULL,"
        " organic_count INTEGER NOT NULL DEFAULT 0,"
        " organic_last REAL NOT NULL DEFAULT 0,"
        " filtered_count INTEGER NOT NULL DEFAULT 0)"
    )
    # Migrate installs created before the usefulness columns existed. A duplicate
    # column raises OperationalError, which is the "already migrated" signal.
    for column, decl in (
        ("organic_count", "INTEGER NOT NULL DEFAULT 0"),
        ("organic_last", "REAL NOT NULL DEFAULT 0"),
        ("filtered_count", "INTEGER NOT NULL DEFAULT 0"),
    ):
        with contextlib.suppress(sqlite3.OperationalError):
            db.execute(f"ALTER TABLE access ADD COLUMN {column} {decl}")
    db.execute(
        "CREATE TABLE IF NOT EXISTS read_session ("
        "filename TEXT NOT NULL, session TEXT NOT NULL, PRIMARY KEY(filename, session))"
    )


def record(
    omi_dir: Path | str,
    filename: str,
    *,
    session: str = "",
    organic: bool = True,
    now: float | None = None,
) -> None:
    """Record one note read. Advisory and fail-open.

    ``count``/``last_access`` always advance (the #173 scorer's inputs are
    unchanged from before this table was shared). The usefulness columns advance
    only for an ``organic`` read not already seen this ``session``; every other
    read lands in ``filtered_count`` instead so ``--explain`` can account for it.
    """
    path = paths.access_state_path(Path(omi_dir))
    stamp = now if now is not None else time.time()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(path, timeout=2.0) as db:
            db.execute("PRAGMA busy_timeout=2000")
            _ensure_schema(db)
            db.execute(
                "INSERT INTO access(filename, count, last_access) VALUES (?, 1, ?)"
                " ON CONFLICT(filename) DO UPDATE SET"
                " count = count + 1, last_access = excluded.last_access",
                (filename, stamp),
            )
            first_this_session = False
            if organic:
                # A session id lets a burst of re-reads count once; an unknown
                # session ("") cannot be deduped, so each such read counts (rare
                # — only non-MCP entry points omit it).
                if session:
                    cur = db.execute(
                        "INSERT OR IGNORE INTO read_session(filename, session)"
                        " VALUES (?, ?)",
                        (filename, session),
                    )
                    first_this_session = cur.rowcount > 0
                else:
                    first_this_session = True
            if first_this_session:
                db.execute(
                    "UPDATE access SET organic_count = organic_count + 1,"
                    " organic_last = ? WHERE filename = ?",
                    (stamp, filename),
                )
            else:
                db.execute(
                    "UPDATE access SET filtered_count = filtered_count + 1"
                    " WHERE filename = ?",
                    (filename,),
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
    """Best recently/frequently read notes, newest score first (#173 scorer)."""
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


class Usefulness:
    """One note's usefulness signal, as ``search --explain`` reports it."""

    __slots__ = ("weight", "useful_reads", "filtered_reads")

    def __init__(self, weight: float, useful_reads: int, filtered_reads: int) -> None:
        #: Multiplicative demotion in ``[floor, 1.0]``; 1.0 means untouched.
        self.weight = weight
        #: Organic, per-session-deduped reads (the counted signal).
        self.useful_reads = useful_reads
        #: Reads excluded as non-organic or same-session re-reads.
        self.filtered_reads = filtered_reads


def _decay_weight(organic_last: float, organic_count: int, current: float) -> float:
    """Demotion for one note. Decay-only: never above 1.0.

    A note never read organically, or last read within the onset window, is
    untouched (1.0). Past the onset it decays linearly toward the floor, reached
    at ``_MAX_AGE_DAYS`` — a gentle reorder nudge, never a bury.
    """
    if organic_count <= 0 or organic_last <= 0:
        return 1.0
    age_days = max(0.0, (current - organic_last) / 86_400)
    if age_days <= _USEFUL_ONSET_DAYS:
        return 1.0
    span = _MAX_AGE_DAYS - _USEFUL_ONSET_DAYS
    frac = 1.0 if span <= 0 else min(1.0, (age_days - _USEFUL_ONSET_DAYS) / span)
    return 1.0 - (1.0 - _USEFUL_FLOOR) * frac


def usefulness_weights(
    omi_dir: Path | str,
    filenames: set[str] | None = None,
    *,
    now: float | None = None,
) -> dict[str, Usefulness]:
    """Usefulness demotion for each note (item #2 scorer), fail-open to ``{}``.

    Reads only the ``organic_*``/``filtered_count`` columns — never ``count``,
    which belongs to #173. Restricted to ``filenames`` when given so the search
    path never scores notes it did not already match (reorder-never-add)."""
    path = paths.access_state_path(Path(omi_dir))
    if not path.is_file():
        return {}
    current = now if now is not None else time.time()
    try:
        with sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=1.0) as db:
            rows = db.execute(
                "SELECT filename, organic_count, organic_last, filtered_count FROM access"
            ).fetchall()
    except (OSError, sqlite3.Error):
        return {}
    out: dict[str, Usefulness] = {}
    for filename, organic_count, organic_last, filtered_count in rows:
        name = str(filename)
        if filenames is not None and name not in filenames:
            continue
        with contextlib.suppress(TypeError, ValueError):
            out[name] = Usefulness(
                weight=_decay_weight(float(organic_last), int(organic_count), current),
                useful_reads=int(organic_count),
                filtered_reads=int(filtered_count),
            )
    return out
