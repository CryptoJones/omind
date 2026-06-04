# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Aaron K. Clark
"""File-backed CRUD over the Markdown notes in an OMI folder.

This module is deliberately framework-free: it touches the filesystem and
parses/renders the OMI memory template, nothing more. The web layer
(:mod:`omind.web.app`) wraps it, and the CLI imports it for `serve`.

Every read/write/delete routes through :meth:`OmiStore.safe_name`, which
rejects path traversal so a request can never escape the OMI directory.
"""

from __future__ import annotations

import os
import re
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from omind.seeds import (
    INDEX_FILENAME,
    INDEX_INTRO,
    INDEX_RECENT_COMMENT,
    INDEX_RECENT_HEADING,
    RESERVED_FILENAMES,
)

# \w is Unicode-aware for str patterns, so non-Latin tags (e.g. #память) round-trip.
_TAG_RE = re.compile(r"#(\w[\w/-]*)")
_WIKILINK_RE = re.compile(r"\[\[([^\]]+)\]\]")
_ACTION_RE = re.compile(r"^\s*-\s*\[([ xX])\]\s?(.*)$")
_BULLET_RE = re.compile(r"^\s*-\s+(.*)$")
_ILLEGAL_FILENAME_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


class NoteError(Exception):
    """Raised for bad note names or note content the store rejects."""


class NoteNotFoundError(NoteError):
    """Raised when a requested note does not exist."""


@dataclass
class ActionItem:
    text: str
    done: bool = False


@dataclass
class NoteFields:
    """The structured contents of a single OMI memory note."""

    title: str
    summary: str = ""
    details: str = ""
    created: str = ""
    tags: list[str] = field(default_factory=list)
    related_to: str = ""
    connections: list[str] = field(default_factory=list)
    action_items: list[ActionItem] = field(default_factory=list)
    references: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> NoteFields:
        raw_items = data.get("action_items") or []
        items: list[ActionItem] = []
        for it in raw_items:
            if isinstance(it, ActionItem):
                items.append(it)
            elif isinstance(it, dict):
                items.append(ActionItem(text=str(it.get("text", "")), done=bool(it.get("done"))))
            else:
                items.append(ActionItem(text=str(it)))
        return cls(
            title=str(data.get("title", "")).strip(),
            summary=str(data.get("summary", "")),
            details=str(data.get("details", "")),
            created=str(data.get("created", "")).strip(),
            tags=[_clean_tag(t) for t in (data.get("tags") or []) if _clean_tag(t)],
            related_to=str(data.get("related_to", "")).strip(),
            connections=[str(c).strip() for c in (data.get("connections") or []) if str(c).strip()],
            action_items=items,
            references=[str(r).strip() for r in (data.get("references") or []) if str(r).strip()],
        )


@dataclass
class NoteSummary:
    """Lightweight listing entry for the sidebar."""

    filename: str
    title: str
    tags: list[str]
    created: str
    summary: str


def _clean_tag(tag: object) -> str:
    return str(tag).lstrip("#").strip()


def today() -> str:
    return date.today().isoformat()


def parse_note(md: str) -> NoteFields:
    """Parse a note's Markdown into structured fields (best effort)."""
    title = ""
    sections: dict[str, list[str]] = {}
    current: str | None = None
    seen_title = False
    for line in md.splitlines():
        if not seen_title and line.startswith("# "):
            title = line[2:].strip()
            seen_title = True
            continue
        heading = re.match(r"^##\s+(.*)$", line)
        if heading:
            current = heading.group(1).strip()
            sections.setdefault(current, [])
            continue
        if current is not None:
            sections[current].append(line)

    def body(name: str) -> str:
        return "\n".join(sections.get(name, [])).strip()

    meta = sections.get("Metadata", [])
    created = ""
    related_to = ""
    tags: list[str] = []
    for line in meta:
        if m := re.match(r"^\s*-\s*Created:\s*(.*)$", line):
            created = m.group(1).strip()
        elif m := re.match(r"^\s*-\s*Tags:\s*(.*)$", line):
            tags = _TAG_RE.findall(m.group(1))
        elif m := re.match(r"^\s*-\s*Related to:\s*(.*)$", line):
            related_to = m.group(1).strip()

    connections = _WIKILINK_RE.findall("\n".join(sections.get("Connections", [])))

    action_items: list[ActionItem] = []
    for line in sections.get("Action Items", []):
        if m := _ACTION_RE.match(line):
            text = m.group(2).strip()
            done = m.group(1).lower() == "x"
            if text or done:
                action_items.append(ActionItem(text=text, done=done))

    references: list[str] = []
    for line in sections.get("References", []):
        if m := _BULLET_RE.match(line):
            text = m.group(1).strip()
            if text:
                references.append(text)

    return NoteFields(
        title=title,
        summary=body("Summary"),
        details=body("Details"),
        created=created,
        tags=tags,
        related_to=related_to,
        connections=[c.strip() for c in connections if c.strip()],
        action_items=action_items,
        references=references,
    )


def render_fields(f: NoteFields) -> str:
    """Render structured fields back into template-shaped Markdown."""
    out: list[str] = [f"# {f.title}".rstrip(), ""]

    out.append("## Metadata")
    out.append(f"- Created: {f.created or today()}".rstrip())
    tag_str = " ".join(f"#{_clean_tag(t)}" for t in f.tags if _clean_tag(t))
    out.append(f"- Tags: {tag_str}".rstrip())
    out.append(f"- Related to: {f.related_to}".rstrip())
    out.append("")

    out.append("## Summary")
    out.append(f.summary.strip())
    out.append("")

    out.append("## Details")
    out.append(f.details.strip())
    out.append("")

    out.append("## Connections")
    out.extend(f"[[{c}]]" for c in f.connections if c.strip())
    out.append("")

    out.append("## Action Items")
    for item in f.action_items:
        box = "x" if item.done else " "
        out.append(f"- [{box}] {item.text}".rstrip())
    out.append("")

    out.append("## References")
    out.extend(f"- {r}".rstrip() for r in f.references if r.strip())
    out.append("")

    return "\n".join(out).rstrip() + "\n"


class OmiStore:
    """CRUD over `*.md` notes in a single OMI folder."""

    def __init__(self, omi_dir: Path | str) -> None:
        self.omi_dir = Path(omi_dir).expanduser()

    # -- naming / safety ----------------------------------------------------

    def safe_name(self, name: str) -> Path:
        """Resolve a user-supplied note name to a path inside the OMI dir.

        Raises :class:`NoteError` on anything that looks like traversal: path
        separators, `..` segments, empty names, or a resolved path whose parent
        is not the OMI dir.
        """
        name = (name or "").strip()
        if not name or name in {".", ".."}:
            raise NoteError("empty or invalid note name")
        if "/" in name or "\\" in name or os.sep in name or (os.altsep and os.altsep in name):
            raise NoteError(f"note name may not contain path separators: {name!r}")
        if ".." in Path(name).parts:
            raise NoteError(f"note name may not contain '..': {name!r}")
        base = Path(name).name
        if base != name:
            raise NoteError(f"unsafe note name: {name!r}")
        if not base.endswith(".md"):
            base += ".md"
        target = (self.omi_dir / base).resolve()
        if target.parent != self.omi_dir.resolve():
            raise NoteError(f"note name escapes the OMI directory: {name!r}")
        return target

    def filename_for_title(self, title: str) -> str:
        cleaned = _ILLEGAL_FILENAME_CHARS.sub(" ", title).strip()
        cleaned = re.sub(r"\s+", " ", cleaned)
        if not cleaned:
            raise NoteError("title produces an empty filename")
        return f"{cleaned}.md"

    # -- reads --------------------------------------------------------------

    def list_notes(self) -> list[NoteSummary]:
        if not self.omi_dir.is_dir():
            return []
        summaries: list[NoteSummary] = []
        for path in self.omi_dir.glob("*.md"):
            if path.name in RESERVED_FILENAMES or path.name.startswith("."):
                continue
            fields = parse_note(path.read_text(encoding="utf-8"))
            title = fields.title or path.stem
            snippet = fields.summary or fields.details
            snippet = re.sub(r"\s+", " ", snippet).strip()
            if len(snippet) > 200:
                snippet = snippet[:197].rstrip() + "..."
            summaries.append(
                NoteSummary(
                    filename=path.name,
                    title=title,
                    tags=fields.tags,
                    created=fields.created,
                    summary=snippet,
                )
            )
        summaries.sort(key=lambda s: (s.created or "", s.title.lower()), reverse=True)
        return summaries

    def read_note(self, name: str) -> str:
        path = self.safe_name(name)
        if not path.is_file():
            raise NoteNotFoundError(f"note not found: {name!r}")
        return path.read_text(encoding="utf-8")

    def read_fields(self, name: str) -> NoteFields:
        return parse_note(self.read_note(name))

    def all_tags(self) -> list[str]:
        tags: set[str] = set()
        for summary in self.list_notes():
            tags.update(summary.tags)
        return sorted(tags, key=str.lower)

    # -- writes -------------------------------------------------------------

    def write_note(self, name: str, content: str) -> str:
        path = self.safe_name(name)
        self.omi_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        self.update_index()
        return path.name

    def create_note(self, fields: NoteFields) -> str:
        if not fields.title.strip():
            raise NoteError("a note requires a title")
        if not fields.created:
            fields.created = today()
        filename = self.filename_for_title(fields.title)
        path = self.safe_name(filename)
        if path.exists():
            raise NoteError(f"a note named {filename!r} already exists")
        return self.write_note(filename, render_fields(fields))

    def update_note(self, name: str, fields: NoteFields) -> str:
        # Validate target exists, then overwrite with rendered fields.
        self.read_note(name)
        return self.write_note(name, render_fields(fields))

    def delete_note(self, name: str) -> None:
        path = self.safe_name(name)
        if path.name in RESERVED_FILENAMES:
            raise NoteError(f"refusing to delete reserved file: {path.name}")
        if not path.is_file():
            raise NoteNotFoundError(f"note not found: {name!r}")
        path.unlink()
        self.update_index()

    # -- index --------------------------------------------------------------

    def update_index(self) -> None:
        """Regenerate the `Recent Memories` wikilink list in index.md."""
        index_path = self.omi_dir / INDEX_FILENAME
        existing = index_path.read_text(encoding="utf-8") if index_path.is_file() else ""
        if INDEX_RECENT_HEADING in existing:
            intro = existing.split(INDEX_RECENT_HEADING, 1)[0].rstrip()
        else:
            intro = existing.rstrip() or INDEX_INTRO.rstrip()

        lines = [intro, "", INDEX_RECENT_HEADING, INDEX_RECENT_COMMENT]
        for summary in self.list_notes():
            stem = summary.filename[:-3] if summary.filename.endswith(".md") else summary.filename
            lines.append(f"- [[{stem}]]")
        self.omi_dir.mkdir(parents=True, exist_ok=True)
        index_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
