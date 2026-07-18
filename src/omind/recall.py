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
) -> dict[str, Any]:
    """Read one note without returning raw/parsed duplicate representations."""
    store = OmiStore(omi_dir)
    raw = store.read_note(name)
    fields = parse_note(raw)
    selected = _section(raw, section) if section else ""
    content = selected or _memory_text(fields)
    limit = bounded_chars(max_chars)
    truncated = len(content) > limit
    if truncated:
        marker = "\n…[truncated; request a section or a larger max_chars value]"
        content = content[: max(0, limit - len(marker))].rstrip() + marker
    return {
        "filename": store.safe_name(name).name,
        "title": fields.title or store.safe_name(name).stem,
        "summary": fields.summary,
        "content": content,
        "section": section if selected else "",
        "truncated": truncated,
        "version": store.note_version(name),
    }


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
