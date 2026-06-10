# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Aaron K. Clark
"""Upsert a single OMI note through ``OmiStore`` — the one safe write path.

Used by ``omind note`` and by external writers (e.g. Hermes' memory-sync skill)
so every writer goes through the same ``.omi.lock`` flock, atomic ``os.replace``,
and ``note_version`` re-check. Writing OMI files raw bypasses all of that and
races other writers — and the memory mesh's replication daemon. See
``docs/mesh.md`` → "Node types & the single-writer rule".
"""
from __future__ import annotations

from pathlib import Path

from omind.store import NoteError, NoteFields, OmiStore


def upsert_note(omi_dir: Path | str, fields: NoteFields) -> tuple[str, str]:
    """Create the note, or update it in place if it already exists.

    Returns ``(action, filename)`` where ``action`` is ``"created"`` or
    ``"updated"``. Raises :class:`omind.store.NoteError` on an empty title.
    """
    if not fields.title.strip():
        raise NoteError("a note requires a title")
    store = OmiStore(omi_dir)
    filename = store.filename_for_title(fields.title)
    if store.safe_name(filename).exists():
        store.update_note(filename, fields)
        return "updated", filename
    store.create_note(fields)
    return "created", filename
