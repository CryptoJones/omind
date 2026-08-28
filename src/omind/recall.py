# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Aaron K. Clark
"""Token-bounded memory recall shared by MCP tools and turn preflight."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from omind.store import NoteFields, OmiStore, parse_note

DEFAULT_RECALL_CHARS = 4_000
MIN_RECALL_CHARS = 500
MAX_RECALL_CHARS = 8_000
_HEADING_RE = re.compile(r"^(#{2,6})\s+(.+?)\s*$")


def bounded_chars(value: int) -> int:
    return min(MAX_RECALL_CHARS, max(MIN_RECALL_CHARS, int(value)))


def _section(raw: str, wanted: str) -> str:
    """Return one Markdown section (H2-H6), including nested subsections."""
    target = wanted.strip().casefold()
    if not target:
        return ""
    lines = raw.splitlines()
    start = -1
    level = 0
    for index, line in enumerate(lines):
        match = _HEADING_RE.match(line)
        if match and match.group(2).strip().casefold() == target:
            start, level = index + 1, len(match.group(1))
            break
    if start < 0:
        return ""
    end = len(lines)
    for index in range(start, len(lines)):
        match = _HEADING_RE.match(lines[index])
        if match and len(match.group(1)) <= level:
            end = index
            break
    return "\n".join(lines[start:end]).strip()


def _memory_text(fields: NoteFields) -> str:
    """Content that complements (rather than repeats) the summary field."""
    parts: list[str] = []
    if fields.details and fields.details.strip() != fields.summary.strip():
        parts.append(fields.details.strip())
    if fields.lead:
        parts.append(fields.lead.strip())
    if fields.connections:
        parts.append("Related: " + ", ".join(f"[[{name}]]" for name in fields.connections))
    if fields.action_items:
        items = [f"- [{'x' if item.done else ' '}] {item.text}" for item in fields.action_items]
        parts.append("Action items:\n" + "\n".join(items))
    return "\n\n".join(part for part in parts if part)


def compact_recall(
    omi_dir: Path | str,
    name: str,
    *,
    max_chars: int = DEFAULT_RECALL_CHARS,
    section: str = "",
    organic: bool = True,
    session: str = "",
) -> dict[str, Any]:
    """Read one note without returning raw/parsed duplicate representations.

    ``organic`` is the usefulness-signal gate (item #2): an agent explicitly
    recalling a note counts, but a guard/hook/turn-preflight force-recall does
    not — its position basis is not evidence the note was useful. ``session``
    dedupes a burst of re-reads to one signal.
    """
    store = OmiStore(omi_dir)
    raw = store.read_note(name)
    filename = store.safe_name(name).name
    from omind import access

    access.record(store.omi_dir, filename, organic=organic, session=session)
    fields = parse_note(raw)
    selected = _section(raw, section) if section else ""
    content = selected or _memory_text(fields)
    limit = bounded_chars(max_chars)
    truncated = len(content) > limit
    if truncated:
        # A bare "truncated" marker was a dead end the agent rarely followed —
        # the git-rules note's recurrence log records three real violations
        # caused by an overriding exception living below the fold (#239). Name
        # the note and the exact follow-up call instead.
        wanted = min(len(content) + 500, MAX_RECALL_CHARS)
        title = fields.title or Path(filename).stem
        marker = (
            f"\n…[TRUNCATED at {limit} of {len(content)} chars. Before acting "
            "on this topic, call OMI MCP recall-note "
            f'{{"name": "{title}", "max_chars": {wanted}}} '
            "or request a specific section.]"
        )
        content = content[: max(0, limit - len(marker))].rstrip() + marker
    payload: dict[str, Any] = {
        "filename": filename,
        "title": fields.title or Path(filename).stem,
        "summary": fields.summary,
        "content": content,
        "section": section if selected else "",
        "truncated": truncated,
        "version": store.note_version(name),
    }
    # Provenance, emitted only when the note declares it, so a note without
    # these fields costs exactly the tokens it did before (#195). The conflict
    # is the load-bearing one: without it the agent reads one side of a
    # disagreement and has no way to know the other side exists.
    if fields.confidence:
        payload["confidence"] = fields.confidence
    if fields.conflicts_with:
        payload["conflicts_with"] = fields.conflicts_with
        payload["warning"] = (
            f"This memory is recorded as conflicting with {fields.conflicts_with}. "
            "Read that note before acting on this one."
        )
    return payload


def filename_for_title(omi_dir: Path | str, title: str) -> str | None:
    needle = title.strip().casefold()
    if not needle:
        return None
    for note in OmiStore(omi_dir).list_notes():
        identifiers = {
            note.title.casefold(),
            note.filename.casefold(),
            Path(note.filename).stem.casefold(),
        }
        if needle in identifiers:
            return note.filename
    return None
