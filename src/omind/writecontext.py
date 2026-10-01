# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Aaron K. Clark
"""Write-time context: what the vault already says about the names in a write
(#389, part of epic #384).

``create-note`` used to warn only about near-duplicates (title+summary cosine
>= 0.88), and ``edit-note`` checked nothing. In the 2026-09-30 incident an agent
wrote that a note about the ``As30p`` drive label was wrong, while about ten
other notes recorded that the label had moved between three drives. The write
response never showed it any of them, although write time is the cheapest point
to catch a contradiction: the agent has just stated the claim and is still in
the turn.

This module answers a write with ``related_by_entity``: for each rare name in
the incoming title/summary/tags/details, the newest few *other* notes that
mention it, with their summaries. It is advisory and never blocks the write.

* Names come from the same extractor as the name index
  (:func:`omind.entities.extract`), with the same noise rules as the tool-output
  name hints (:func:`omind.namehints._noise`) and the same rarity rules: a name
  over the index's ceiling (``OMI_ENTITY_MAX_DF``) never counts, and a name no
  note is titled after must be in at most :data:`omind.namehints.UNTITLED_MAX_DF`
  notes.
* One read-only index query (:func:`omind.searchindex.entity_lookups_readonly`):
  no model load, no refresh.
* Bounded: at most :data:`MAX_NAMES` names, :data:`PER_NAME` notes per name,
  summaries cut to :data:`SUMMARY_CHARS`, the whole field under
  :data:`MAX_CHARS`. A note shown for one name is not repeated for another.
* The note being written is excluded, and so are archived notes.
* It is **pull**: the response to the agent's own ``mcp__omi__*`` call, so the
  PostToolUse ledger records it with the rest of that result as
  ``"channel": "pull"`` (#387), and it never spends the push budget.

A name in the list whose facts changed (a superseded note, a correction) also
gets its dated history under ``name_timelines`` (:mod:`omind.timeline`, #390;
``OMIND_NAME_TIMELINES=0`` turns that part off).

``OMIND_WRITE_CONTEXT=0`` turns it off.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: Kill switch (``0``/``off``/``false``/``no``). On by default — see the #389 PR
#: for the replay numbers that decision rests on.
ENABLE_ENV = "OMIND_WRITE_CONTEXT"
#: Names reported per write.
MAX_NAMES = 3
#: Other notes listed per name, newest first (notes titled after the name first).
PER_NAME = 3
#: Each summary is cut here (``…``); ``recall-note`` has the rest.
SUMMARY_CHARS = 200
#: Each title is cut here.
TITLE_CHARS = 200
#: Ceiling on the serialized ``related_by_entity`` list. Entries past it are dropped.
MAX_CHARS = 2_400
#: Only the head of ``details`` is scanned, like a tool response (#388).
MAX_SCAN_CHARS = 16_000
#: Distinct names looked up per write.
MAX_LOOKUPS = 200

FIELD = "related_by_entity"
#: The dated history of a name in the write whose facts changed (#390).
TIMELINE_FIELD = "name_timelines"
NOTE = (
    "Advisory only; the write succeeded. Other notes already mention these names. "
    "If one says something different from what you just wrote, recall-note it and "
    "reconcile: set supersedes or conflicts_with, or fix whichever is wrong."
)

_OFF = frozenset({"0", "off", "false", "no"})

#: A sequence word plus a small number (``run4``, ``round-3``, ``ch13``,
#: ``hdd1``): identifier-shaped, but it numbers a step of whatever the note is
#: about rather than naming a thing, and every project has its own ``run4``. On
#: the #389 replay these were the largest source of unrelated notes.
_SEQUENCE_RE = re.compile(
    r"^(?:run|round|ch|chap|chapter|part|pass|step|phase|stage|day|week|item|test|"
    r"case|lane|slot|hdd|ssd|disk|gpu|cpu|node|worker|try|attempt|take|track|row|"
    r"col|line|page|sec|section|fig|table|batch|epoch|iter|trial|wave|sprint|ep|"
    r"episode|vol|volume|level|lvl|tier|option|opt|plan|draft|v)-?\d{1,2}[a-z]?$",
    re.IGNORECASE,
)


def enabled() -> bool:
    """Whether write-time context is on (default on)."""
    return os.environ.get(ENABLE_ENV, "").strip().lower() not in _OFF


@dataclass
class Related:
    """One other note that mentions a name in the write."""

    name: str
    filename: str
    title: str
    summary: str
    #: The note's date as the name index records it (``Created``, else mtime).
    updated: str
    #: Whether the note's title names the thing (it is *about* it).
    about: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "title": self.title,
            "summary": self.summary,
            "updated": self.updated,
        }


@dataclass
class _Pick:
    key: str
    spelling: str
    notes: list[Any] = field(default_factory=list)
    about: set[str] = field(default_factory=set)


def _cut(text: str, limit: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _salient(spelling: str, text: str) -> bool:
    from omind import namehints

    return namehints._salient(spelling, text)


def pick(
    omi_dir: Path | str,
    *,
    title: str = "",
    summary: str = "",
    details: str = "",
    tags: Iterable[str] = (),
    exclude: Iterable[str] = (),
    exclude_titles: Iterable[str] = (),
    as_of: str = "",
    summary_of: Callable[[str], str] | None = None,
) -> list[Related]:
    """Other notes that mention the rare names in a write, newest first.

    READ-ONLY and deterministic: shared by the MCP tools and the replay so the
    two cannot disagree. ``exclude`` lists filenames never to report (the note
    being written); ``exclude_titles`` does the same by title. ``as_of``
    (``YYYY-MM-DD``) hides notes dated after it, for replays.
    ``summary_of(filename)`` returns a note's summary; the default reads it
    from disk. Names the write puts in its title, summary or tags
    always count; a name only in ``details`` counts when it is salient there
    (a long body must mention it twice). Fails open to ``[]``.
    """
    try:
        from omind import entities, namehints, searchindex

        if not entities.enabled():
            return []
        tag_list = [str(t) for t in tags if t]
        head = "\n".join(part for part in (title, summary) if part)
        body = (details or "")[:MAX_SCAN_CHARS]
        found_head = entities.extract(head, tags=tag_list)
        found_body = entities.extract(body)
        candidates: list[tuple[str, str, bool]] = []
        for key, spelling in found_head.items():
            candidates.append((key, spelling, True))
        for key, spelling in found_body.items():
            if key not in found_head:
                candidates.append((key, spelling, False))
        written = "\n".join((head, *tag_list, body))
        candidates = [
            c
            for c in candidates
            if not namehints._noise(c[0], c[1])
            and not _SEQUENCE_RE.match(c[1])
            # The write must name the thing as a word, not only as a path
            # component: a course stored under /Volumes/As30p/ says nothing
            # about the drive.
            and namehints._names_in_title(c[1], written)
        ][:MAX_LOOKUPS]
        if not candidates:
            return []
        lookups = searchindex.entity_lookups_readonly(omi_dir, [c[0] for c in candidates])
        if not lookups:
            return []
        skip = {str(name) for name in exclude}
        skip_titles = {str(t).casefold() for t in exclude_titles if t}
        ranked: list[tuple[int, int, int, int, _Pick]] = []
        for order, (key, spelling, in_head) in enumerate(candidates):
            lookup = lookups.get(key)
            if lookup is None or lookup.common or not lookup.notes:
                continue
            if not in_head and not _salient(spelling, body):
                continue
            notes = [
                n
                for n in lookup.notes
                if n.filename not in skip and (not as_of or n.last_seen[:10] <= as_of)
            ]
            if not notes:
                continue
            about = [n for n in notes if namehints._names_in_title(spelling, n.title)]
            if not about and len(notes) > namehints.UNTITLED_MAX_DF:
                continue
            if skip_titles:
                about = [n for n in about if n.title.casefold() not in skip_titles]
                notes = [n for n in notes if n.title.casefold() not in skip_titles]
                if not notes:
                    continue
            # A name some note is titled after shows only those notes: the
            # ones that merely mention it in passing are what made the first
            # replay noisy. An untitled (rare) name shows its newest mentions.
            chosen = _Pick(
                key=key,
                spelling=spelling,
                notes=about or notes,
                about={n.filename for n in about},
            )
            # Names the vault has notes *about* first, then names the write
            # leads with, then rarer, then order of appearance.
            ranked.append((0 if about else 1, 0 if in_head else 1, len(notes), order, chosen))
        ranked.sort(key=lambda item: item[:4])
        read = summary_of or _summary_reader(omi_dir)
        out: list[Related] = []
        shown: set[str] = set()
        size = 2  # the enclosing []
        names = 0
        for *_, chosen in ranked:
            if names >= MAX_NAMES:
                break
            added = 0
            for note in chosen.notes:
                if added >= PER_NAME:
                    break
                if note.filename in shown:
                    continue
                entry = Related(
                    name=chosen.spelling,
                    filename=note.filename,
                    title=_cut(note.title, TITLE_CHARS),
                    summary=_cut(_safe(read, note.filename), SUMMARY_CHARS),
                    updated=str(note.last_seen)[:10],
                    about=note.filename in chosen.about,
                )
                cost = len(json.dumps(entry.to_dict(), ensure_ascii=False)) + 2
                if size + cost > MAX_CHARS:
                    return out
                size += cost
                shown.add(note.filename)
                out.append(entry)
                added += 1
            if added:
                names += 1
        return out
    except Exception:
        return []


def _safe(read: Callable[[str], str], filename: str) -> str:
    try:
        return str(read(filename) or "")
    except Exception:
        return ""


def _summary_reader(omi_dir: Path | str) -> Callable[[str], str]:
    from omind.store import OmiStore

    store = OmiStore(omi_dir)

    def read(filename: str) -> str:
        fields = store.read_fields(filename)
        return fields.summary or ""

    return read


def response_fields(
    omi_dir: Path | str,
    *,
    title: str = "",
    summary: str = "",
    details: str = "",
    tags: Iterable[str] = (),
    exclude: Iterable[str] = (),
    summary_of: Callable[[str], str] | None = None,
) -> dict[str, object]:
    """The fields a write tool adds to its response, or ``{}``. Never raises."""
    if not enabled():
        return {}
    # Used twice (the list and the timelines): a generator would be spent.
    exclude = tuple(exclude)
    related = pick(
        omi_dir,
        title=title,
        summary=summary,
        details=details,
        tags=tags,
        exclude=exclude,
        summary_of=summary_of,
    )
    if not related:
        return {}
    out: dict[str, object] = {FIELD: [r.to_dict() for r in related], f"{FIELD}_note": NOTE}
    # #390: a name whose facts changed also gets its dated history, oldest
    # first, superseded and corrected notes marked rather than left out.
    with contextlib.suppress(Exception):
        from omind import timeline

        timelines = timeline.for_names(
            omi_dir, [r.name for r in related], exclude=exclude, limit=MAX_NAMES
        )
        if timelines:
            out[TIMELINE_FIELD] = [t.to_dict() for t in timelines]
    return out


# -- replay (omind bench --write-context) ------------------------------------

_WRITE_TOOLS = {"create-note": "create", "edit-note": "edit"}
_OMI_PREFIXES = ("mcp__omi__", "mcp_omi_")


@dataclass
class ReplayWrite:
    """One create-note/edit-note call and what the write context would add."""

    transcript: str
    line: int
    tool: str
    title: str
    related: list[Related] = field(default_factory=list)
    #: Filenames among ``related`` the session read through OMI at any point.
    consulted: set[str] = field(default_factory=set)
    #: Filenames among ``related`` the written note links to.
    linked: set[str] = field(default_factory=set)
    milliseconds: float = 0.0

    @property
    def chars(self) -> int:
        if not self.related:
            return 0
        return len(json.dumps([r.to_dict() for r in self.related], ensure_ascii=False))


def _tool_kind(name: str) -> str:
    for prefix in _OMI_PREFIXES:
        if name.startswith(prefix):
            return _WRITE_TOOLS.get(name[len(prefix) :], "")
    return ""


_STEM_RE = re.compile(r"\.md$", re.IGNORECASE)


def _stem(filename: str) -> str:
    return _STEM_RE.sub("", filename).casefold()


def replay_transcript(path: Path | str, omi_dir: Path | str) -> list[ReplayWrite]:
    """What ``related_by_entity`` would have said for every create-note and
    edit-note call in a Claude Code ``.jsonl`` session, without writing anything.

    Each write sees only notes dated on or before the day of the call. For an
    ``edit-note`` the fields the call passed are used, with the note's current
    title when the call left the title alone. Raises ``OSError`` on an
    unreadable transcript.
    """
    import time

    from omind.store import OmiStore

    source = Path(path).expanduser()
    store = OmiStore(omi_dir)
    lines = source.read_text(encoding="utf-8", errors="replace").splitlines()
    writes: list[tuple[int, str, dict[str, Any], str]] = []
    reads: list[str] = []
    for number, raw in enumerate(lines, start=1):
        try:
            entry = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(entry, dict) or entry.get("type") != "assistant":
            continue
        stamp = str(entry.get("timestamp") or "")[:10]
        message = entry.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            name = str(block.get("name") or "")
            given = block.get("input")
            tool_input: dict[str, Any] = given if isinstance(given, dict) else {}
            if not name.startswith(_OMI_PREFIXES):
                continue
            kind = _tool_kind(name)
            if kind:
                writes.append((number, kind, tool_input, stamp))
            else:
                reads.append(json.dumps(tool_input, ensure_ascii=False).casefold())
    out: list[ReplayWrite] = []
    for number, kind, tool_input, stamp in writes:
        title = str(tool_input.get("title") or "")
        exclude: list[str] = []
        if kind == "edit":
            target = str(tool_input.get("name") or "")
            with contextlib.suppress(Exception):
                exclude.append(store.safe_name(target).name)
            if not title:
                with contextlib.suppress(Exception):
                    title = store.read_fields(target).title
        elif title:
            with contextlib.suppress(Exception):
                exclude.append(store.safe_name(title).name)
        tags = tool_input.get("tags")
        started = time.perf_counter()
        related = pick(
            omi_dir,
            title=title,
            summary=str(tool_input.get("summary") or ""),
            details=str(tool_input.get("details") or ""),
            tags=tags if isinstance(tags, list) else (),
            exclude=exclude,
            exclude_titles=[title],
            as_of=stamp,
        )
        elapsed = (time.perf_counter() - started) * 1000.0
        links = " ".join(
            json.dumps(tool_input.get(key) or "", ensure_ascii=False)
            for key in ("connections", "related_to", "supersedes", "conflicts_with",
                        "superseded_by", "details")
        ).casefold()
        write = ReplayWrite(
            transcript=str(source),
            line=number,
            tool=kind,
            title=title,
            related=related,
            milliseconds=elapsed,
        )
        for item in related:
            stem = _stem(item.filename)
            needles = {stem, item.title.casefold()[:60]}
            if any(n and any(n in r for r in reads) for n in needles):
                write.consulted.add(item.filename)
            if stem and f"[[{stem}" in links:
                write.linked.add(item.filename)
        out.append(write)
    return out
