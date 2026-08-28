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


def upsert_note(
    omi_dir: Path | str, fields: NoteFields, *, expected_version: str | None = None
) -> tuple[str, str]:
    """Create the note, or update it in place if it already exists.

    Returns ``(action, filename)`` where ``action`` is ``"created"`` or
    ``"updated"``. Raises :class:`omind.store.NoteError` on an empty title.

    ``expected_version`` pins the update to the version the caller built
    ``fields`` from. When omitted, the version is captured immediately before
    the existing fields are read — capturing it AFTER the read left a window
    where a concurrent writer's edit was silently reverted (2026-08-27
    review).
    """
    if not fields.title.strip():
        raise NoteError("a note requires a title")
    store = OmiStore(omi_dir)
    filename = store.filename_for_title(fields.title)
    if store.safe_name(filename).exists():
        version = expected_version if expected_version is not None else store.note_version(filename)
        _keep_existing_when_unset(fields, store.read_fields(filename))
        store.update_note(filename, fields, expected_version=version)
        return "updated", filename
    store.create_note(fields)
    return "created", filename


def _keep_existing_when_unset(fields: NoteFields, existing: NoteFields) -> None:
    """Upsert callers (CLI flags, Hermes) have no way to say "keep" — an empty
    field means unspecified, not "clear it". Without this, every `omind note`
    update wiped the note's Action Items and anything else not passed."""
    if not fields.summary.strip():
        fields.summary = existing.summary
    if not fields.details.strip():
        fields.details = existing.details
    if not fields.created:
        fields.created = existing.created
    if not fields.tags:
        fields.tags = existing.tags
    if not fields.related_to.strip():
        fields.related_to = existing.related_to
    if not fields.connections:
        fields.connections = existing.connections
    if not fields.action_items:
        fields.action_items = existing.action_items
    if not fields.references:
        fields.references = existing.references
    if not fields.extras:
        fields.extras = existing.extras
