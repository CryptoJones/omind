# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Aaron K. Clark
"""Notice when a session did work and wrote nothing down (#221).

Every write to the vault requires the agent to *decide* to call ``create-note``.
A forgotten call loses the memory silently: no error, no warning, no degraded
result — just a fact that never entered the vault, discovered later or never. A
nine-model review named this omind's largest gap, more than any other finding.

**This is a detector, not an ingest path.** BACKLOG.md's *Not planned* section
rejected capturing external documents on the grounds that omind's notes are
"written by an agent about its own work". That reasoning still holds and this
does not challenge it: nothing here reads a transcript, a conversation, or any
file outside the vault. It reads omind's OWN journal — the record it already
keeps of what the session did — and compares "work happened" against "a note was
written". Both halves already live in `Journal/`.

The output is a nudge on stderr, never a block. A memory layer that refuses to
let you stop working would be a worse failure than the one it is preventing.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

#: Tools that constitute *writing something down*. A session that called any of
#: these has recorded its work and needs no nudge.
_WRITE_TOOLS = (
    "create-note",
    "edit-note",
    "restore-note",
    "delete-note",  # a deliberate retraction is also a decision worth crediting
)

#: Tools that are pure reading. A session that ONLY read has not necessarily
#: done work worth recording, so these do not count toward the "did work" side.
_READ_TOOLS = (
    "recall-note",
    "read-note",
    "search-vault",
    "list-notes",
    "list-tags",
    "backlinks",
    "graph",
)

#: Below this many journalled actions a session is too small to be worth a
#: nudge. Answering one question and stopping is not a lost memory, and a
#: detector that fires on every trivial session is one people learn to ignore.
DEFAULT_MIN_ACTIONS = 12

_SESSION_RE = re.compile(r"\[session ([^\]]+)\]")


def _entry_session(line: str) -> str:
    match = _SESSION_RE.search(line)
    return match.group(1).strip() if match else ""


def summarize(journal_text: str, session: str) -> tuple[int, int]:
    """Return ``(actions, writes)`` for ``session`` within ``journal_text``.

    ``actions`` counts journalled tool calls that are not pure reads and not the
    turn-ended marker; ``writes`` counts note-writing tool calls.
    """
    actions = 0
    writes = 0
    for raw in journal_text.splitlines():
        line = raw.strip()
        if not line.startswith("- ") or _entry_session(line) != session:
            continue
        if "Stop -> turn ended" in line:
            continue
        if any(tool in line for tool in _WRITE_TOOLS):
            writes += 1
            actions += 1
            continue
        if any(tool in line for tool in _READ_TOOLS):
            continue  # reading is not work worth recording
        actions += 1
    return actions, writes


def notice(
    journal_text: str,
    session: str,
    *,
    min_actions: int = DEFAULT_MIN_ACTIONS,
) -> str | None:
    """The nudge for this session, or ``None`` when nothing is worth saying.

    Silent when the session wrote something, when it was too small to matter, or
    when it only read. Deliberately conservative: a false nudge trains people to
    ignore the real one.
    """
    actions, writes = summarize(journal_text, session)
    if writes or actions < min_actions:
        return None
    return (
        f"omind: {actions} actions this session and no note written. "
        "If anything here is worth remembering, record it now — a forgotten "
        "create-note loses it silently. (OMIND_NO_UNWRITTEN=1 to disable.)"
    )


def disabled() -> bool:
    """Opt-out switch. Any non-empty value turns the detector off."""
    return bool(os.environ.get("OMIND_NO_UNWRITTEN", "").strip())


def check(omi_dir: Path | str, session: str, *, now: object = None) -> str | None:
    """Read today's journal and return the nudge, or ``None``.

    Never raises: this runs on the Stop path, and a detector that can break the
    ability to stop is strictly worse than the omission it reports.
    """
    if disabled() or not session:
        return None
    try:
        from omind import hooks

        path = Path(hooks.journal_dir(omi_dir)) / hooks.journal_name(now)  # type: ignore[arg-type]
        if not path.is_file():
            return None
        return notice(path.read_text(encoding="utf-8"), session)
    except Exception:
        return None
