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

import contextlib
import os
import re
import tempfile
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from omind import filelock
from omind.seeds import (
    INDEX_FILENAME,
    INDEX_INTRO,
    INDEX_RECENT_COMMENT,
    INDEX_RECENT_HEADING,
    RESERVED_FILENAMES,
)

# Inter-process write lock for an OMI folder. Concurrent Claude Code sessions
# (and the web app, cron) are separate processes, so an advisory ``flock`` on a
# shared file is what serializes their writes. Readers never take it — atomic
# renames (see :func:`_atomic_write`) keep every read consistent.
LOCK_FILENAME = ".omi.lock"

# index.md is the primary SessionStart priming payload (16k char cap in
# omind.hooks), so the Recent Memories list is capped rather than unbounded.
RECENT_LIMIT = 25
# Per-entry description budget for the Recent Memories list.
_INDEX_DESC_LIMIT = 100
# Written into the regenerated region so we can tell a new-format index (entry
# descriptions generated from note summaries) from an old bare-link one whose
# `— description` annotations were written by hand and need migrating.
_INDEX_GENERATED_MARKER = "<!-- entry descriptions are generated from note Summary sections -->"


def _atomic_write(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` atomically: same-dir temp file + ``os.replace``.

    On POSIX ``os.replace`` is an atomic rename, so a concurrent reader sees
    either the old file or the new one in full — never a half-written file.
    """
    directory = path.parent
    directory.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".tmp-", suffix=".md")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise

# \w is Unicode-aware for str patterns, so non-Latin tags (e.g. #память) round-trip.
_TAG_RE = re.compile(r"#(\w[\w/-]*)")
_WIKILINK_RE = re.compile(r"\[\[([^\]]+)\]\]")
_ACTION_RE = re.compile(r"^\s*-\s*\[([ xX])\]\s?(.*)$")
_BULLET_RE = re.compile(r"^\s*-\s+(.*)$")
_ILLEGAL_FILENAME_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
# A Recent Memories entry: `- [[stem]]`, optionally annotated `— description`.
_INDEX_ENTRY_RE = re.compile(r"^-\s*\[\[([^\]]+)\]\](?:\s+[—–-]+\s+(.+))?\s*$")
_SUMMARY_HEADING_RE = re.compile(r"^##\s+Summary\s*$")
# Per-day journal notes written by omind.hooks; auto-recorded noise that would
# otherwise crowd hand-curated memories out of the capped index list.
_JOURNAL_NOTE_RE = re.compile(r"^Session Journal .*\.md$")


class NoteError(Exception):
    """Raised for bad note names or note content the store rejects."""


class NoteNotFoundError(NoteError):
    """Raised when a requested note does not exist."""


class NoteConflictError(Exception):
    """Raised when a note changed on disk since the caller last read it.

    Deliberately NOT a :class:`NoteError`: the web layer maps it to HTTP 409
    (the write is valid, the *base version* is stale), whereas NoteError maps
    to 400.
    """


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


def _collapse(text: str, limit: int) -> str:
    """Collapse whitespace to one line and truncate to ``limit`` characters."""
    collapsed = re.sub(r"\s+", " ", text).strip()
    if len(collapsed) > limit:
        collapsed = collapsed[: limit - 3].rstrip() + "..."
    return collapsed


def _with_summary(md: str, summary: str) -> str:
    """Return ``md`` with ``summary`` inserted into its (empty) Summary section.

    Surgical line edit rather than a parse/render round-trip so a hand-curated
    note keeps any sections the template doesn't know about. Appends a fresh
    ``## Summary`` section when the note has none.
    """
    lines = md.splitlines()
    for i, line in enumerate(lines):
        if _SUMMARY_HEADING_RE.match(line):
            return "\n".join([*lines[: i + 1], summary, *lines[i + 1 :]]).rstrip() + "\n"
    return md.rstrip() + f"\n\n## Summary\n{summary}\n"


class OmiStore:
    """CRUD over `*.md` notes in a single OMI folder."""

    def __init__(self, omi_dir: Path | str) -> None:
        self.omi_dir = Path(omi_dir).expanduser()

    # -- concurrency --------------------------------------------------------

    @contextlib.contextmanager
    def _write_lock(self) -> Iterator[None]:
        """Hold the OMI folder's inter-process exclusive write lock.

        Serializes writers across separate processes — concurrent Claude Code
        sessions, the web UI, cron — so two saves can't interleave a note write
        with another's ``index.md`` regeneration. Held once per public write
        operation; the unlocked ``_write_index`` body runs inside it.
        """
        self.omi_dir.mkdir(parents=True, exist_ok=True)
        lock_path = self.omi_dir / LOCK_FILENAME
        fd = os.open(lock_path, os.O_WRONLY | os.O_CREAT, 0o644)
        try:
            filelock.lock_fd(fd)
            yield
        finally:
            filelock.unlock_fd(fd)
            os.close(fd)

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

    def _note_paths(self) -> Iterator[Path]:
        """Yield the user-visible note files (skips reserved + dotfiles)."""
        if not self.omi_dir.is_dir():
            return
        for path in self.omi_dir.glob("*.md"):
            if path.name in RESERVED_FILENAMES or path.name.startswith("."):
                continue
            yield path

    def _summarize(self, path: Path, text: str | None = None) -> NoteSummary:
        if text is None:
            text = path.read_text(encoding="utf-8")
        fields = parse_note(text)
        snippet = re.sub(r"\s+", " ", fields.summary or fields.details).strip()
        if len(snippet) > 200:
            snippet = snippet[:197].rstrip() + "..."
        return NoteSummary(
            filename=path.name,
            title=fields.title or path.stem,
            tags=fields.tags,
            created=fields.created,
            summary=snippet,
        )

    def list_notes(self) -> list[NoteSummary]:
        summaries = [self._summarize(p) for p in self._note_paths()]
        summaries.sort(key=lambda s: (s.created or "", s.title.lower()), reverse=True)
        return summaries

    def backlinks(self, name: str) -> list[NoteSummary]:
        """Notes that ``[[wikilink]]`` to the given note (by title or stem)."""
        target = self.safe_name(name)
        if not target.is_file():
            raise NoteNotFoundError(f"note not found: {name!r}")
        target_text = target.read_text(encoding="utf-8")
        stem = target.name[:-3] if target.name.endswith(".md") else target.name
        identifiers = {stem.strip().lower()}
        title = parse_note(target_text).title.strip().lower()
        if title:
            identifiers.add(title)

        results: list[NoteSummary] = []
        for path in self._note_paths():
            if path.resolve() == target.resolve():
                continue
            text = path.read_text(encoding="utf-8")
            link_targets = {t.strip().lower() for t in _WIKILINK_RE.findall(text)}
            if link_targets & identifiers:
                results.append(self._summarize(path, text))
        results.sort(key=lambda s: (s.created or "", s.title.lower()), reverse=True)
        return results

    def read_note(self, name: str) -> str:
        path = self.safe_name(name)
        if not path.is_file():
            raise NoteNotFoundError(f"note not found: {name!r}")
        return path.read_text(encoding="utf-8")

    def read_fields(self, name: str) -> NoteFields:
        return parse_note(self.read_note(name))

    def note_version(self, name: str) -> str:
        """An opaque token for a note's on-disk state (mtime + size).

        Empty string when the note does not exist yet. Callers pass the token
        they last saw back to :meth:`write_note`; a mismatch means someone else
        (Claude Code's MCP, Hermes' cron, another tab) wrote in the meantime.
        """
        path = self.safe_name(name)
        if not path.is_file():
            return ""
        st = path.stat()
        return f"{st.st_mtime_ns}-{st.st_size}"

    def all_tags(self) -> list[str]:
        tags: set[str] = set()
        for summary in self.list_notes():
            tags.update(summary.tags)
        return sorted(tags, key=str.lower)

    # -- writes -------------------------------------------------------------

    def write_note(self, name: str, content: str, expected_version: str | None = None) -> str:
        path = self.safe_name(name)
        with self._write_lock():
            # Re-check the optimistic-concurrency token *inside* the lock so the
            # check-then-write is atomic against another process's save.
            if expected_version is not None and path.is_file():
                current = self.note_version(name)
                if current != expected_version:
                    raise NoteConflictError(
                        f"note {name!r} changed on disk (expected {expected_version!r}, "
                        f"found {current!r})"
                    )
            _atomic_write(path, content)
            self._write_index()
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

    def update_note(
        self, name: str, fields: NoteFields, expected_version: str | None = None
    ) -> str:
        # Validate target exists, then overwrite with rendered fields.
        self.read_note(name)
        return self.write_note(name, render_fields(fields), expected_version=expected_version)

    def delete_note(self, name: str) -> None:
        path = self.safe_name(name)
        if path.name in RESERVED_FILENAMES:
            raise NoteError(f"refusing to delete reserved file: {path.name}")
        if not path.is_file():
            raise NoteNotFoundError(f"note not found: {name!r}")
        with self._write_lock():
            path.unlink()
            self._write_index()

    # -- index --------------------------------------------------------------

    def update_index(self) -> None:
        """Regenerate the `Recent Memories` wikilink list in index.md (locked)."""
        with self._write_lock():
            self._write_index()

    def _write_index(self) -> None:
        """Regenerate index.md. Caller MUST hold :meth:`_write_lock`.

        Read-modify-write on the shared index.md, so it only runs under the
        write lock; the standalone entry point is :meth:`update_index`.

        Each Recent Memories entry is rendered as ``- [[stem]] — summary``
        (summary collapsed to one line, ≤ :data:`_INDEX_DESC_LIMIT` chars), the
        list is capped at :data:`RECENT_LIMIT` newest-first entries, and
        journal notes are excluded. Hand-written ``— description`` annotations
        on the old bare-link format are migrated into the notes themselves by
        :meth:`_migrate_index_descriptions` before the list is regenerated.
        """
        index_path = self.omi_dir / INDEX_FILENAME
        existing = index_path.read_text(encoding="utf-8") if index_path.is_file() else ""
        self._migrate_index_descriptions(existing)
        if INDEX_RECENT_HEADING in existing:
            intro = existing.split(INDEX_RECENT_HEADING, 1)[0].rstrip()
        else:
            intro = existing.rstrip() or INDEX_INTRO.rstrip()

        notes = [s for s in self.list_notes() if not _JOURNAL_NOTE_RE.match(s.filename)]
        lines = [intro, "", INDEX_RECENT_HEADING, INDEX_RECENT_COMMENT, _INDEX_GENERATED_MARKER]
        for summary in notes[:RECENT_LIMIT]:
            stem = summary.filename[:-3] if summary.filename.endswith(".md") else summary.filename
            description = _collapse(summary.summary, _INDEX_DESC_LIMIT)
            lines.append(f"- [[{stem}]] — {description}" if description else f"- [[{stem}]]")
        if len(notes) > RECENT_LIMIT:
            lines.extend(["", f"*({len(notes)} notes total)*"])
        _atomic_write(index_path, "\n".join(lines).rstrip() + "\n")

    def _migrate_index_descriptions(self, existing: str) -> None:
        """Copy hand-written index descriptions into empty note Summaries.

        One-time migration for the old bare-link index format: a Recent
        Memories line carrying a hand-written ``— description`` whose note has
        an empty ``## Summary`` gets the description copied into that section,
        so regeneration renders it instead of destroying it. Notes that already
        have a summary are left alone. A new-format index (marked with
        :data:`_INDEX_GENERATED_MARKER`) carries only generated descriptions,
        so it is never migrated — that is what makes the migration one-time and
        idempotent. Caller MUST hold :meth:`_write_lock`.
        """
        if INDEX_RECENT_HEADING not in existing or _INDEX_GENERATED_MARKER in existing:
            return
        recent = existing.split(INDEX_RECENT_HEADING, 1)[1]
        for line in recent.splitlines():
            entry = _INDEX_ENTRY_RE.match(line.strip())
            if not entry or not entry.group(2):
                continue
            description = entry.group(2).strip()
            try:
                path = self.safe_name(entry.group(1).strip())
            except NoteError:
                continue
            if path.name in RESERVED_FILENAMES or not path.is_file():
                continue
            text = path.read_text(encoding="utf-8")
            if parse_note(text).summary.strip():
                continue
            _atomic_write(path, _with_summary(text, description))
