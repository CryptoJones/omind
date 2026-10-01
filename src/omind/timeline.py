# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Aaron K. Clark
"""Name timelines: the dated history of a name whose facts change (#390, the
last child of epic #384).

Some names keep their spelling while the facts behind them change: a volume
label moves between drives, a host is rebuilt, a service changes ports. The
notes that explain the change are exactly the ones automatic recall used to
hide. The preflight never names a note carrying a CORRECTION/SUPERSEDED line
(``guard._STALE_MARKER_RE``, #321 fix 3), and ``superseded_by`` cuts a note's
rank to x0.35. The newest note often states only the current state ("As30p is
the WD Blue"), so an agent looking at an older reference had nothing to reason
with. That is the 2026-09-30 incident.

A **timeline** is the answer for such a name. It lists the dated titles of the
notes about it, oldest to newest, and marks which are superseded or carry a
correction instead of removing them:

    OMI: As30p has a dated history (118 notes; oldest first): 2026-09-25
    [[Seagate As30p …]] (has a correction) → … → 2026-09-27 [[WD Blue As30p …]]
    (newest). …

* It is built only for a name with history: at least one of the notes it would
  show is superseded (``Superseded by:``), supersedes another (``Supersedes:``),
  or carries a stale marker line. Every other name keeps its plain hint, so the
  timeline costs nothing where the facts never changed.
* It replaces the plain titles-only hint of the paths that already fire for a
  name: the preflight's rare-identifier hint (#386) and the PostToolUse name
  hint (#388). ``create-note``/``edit-note`` (#389) add it as
  ``name_timelines``. Stale-marker suppression still governs **topic** matches
  in the preflight; it does not apply to a name timeline, which shows the
  correction as part of the history.
* Bounded: the notes *about* the name (its title names it as a word) when there
  are any, otherwise all its notes; at most :data:`MAX_ENTRIES` (the newest),
  titles cut to :data:`TITLE_CHARS`, the line at most :data:`MAX_CHARS` (oldest
  entries dropped first). Push paths charge it to the push budget like the hint
  it replaces.
* Deterministic and read-only: one read-only index query
  (:func:`omind.searchindex.entity_lookups_readonly`) and at most
  :data:`MAX_ENTRIES` note heads read from disk. Fails open to ``None``.

``OMIND_NAME_TIMELINES=0`` turns it off and restores the 10.0.x behaviour of the
paths above.
"""

from __future__ import annotations

import contextlib
import os
import re
from collections.abc import Iterable, Sequence
from collections.abc import Set as AbstractSet
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: Kill switch (``0``/``off``/``false``/``no``). On by default — see the #390 PR
#: for the replay numbers that decision rests on.
ENABLE_ENV = "OMIND_NAME_TIMELINES"
#: Entries shown per name: the newest this many of its notes, oldest first.
MAX_ENTRIES = 6
#: Each title is cut here (``…``). recall-note resolves a title prefix.
TITLE_CHARS = 90
#: Ceiling on one rendered timeline line. Oldest entries are dropped to fit.
MAX_CHARS = 900
#: Only a note's head is read when looking for a stale marker.
READ_CHARS = 32_000

_OFF = frozenset({"0", "off", "false", "no"})

SUPERSEDED = "superseded"
CORRECTION = "has a correction"
SUPERSEDES = "replaces an older note"


def enabled() -> bool:
    """Whether name timelines are on (default on)."""
    return os.environ.get(ENABLE_ENV, "").strip().lower() not in _OFF


@dataclass(frozen=True)
class Entry:
    """One dated note in a name's timeline."""

    filename: str
    title: str
    date: str
    #: :data:`SUPERSEDED`, :data:`CORRECTION` or :data:`SUPERSEDES`, or ``""``.
    mark: str = ""

    def render(self, *, newest: bool = False) -> str:
        marks = [m for m in (self.mark, "newest" if newest else "") if m]
        tail = f" ({', '.join(marks)})" if marks else ""
        return f"{self.date} [[{_cut(self.title)}]]{tail}"

    def to_dict(self) -> dict[str, str]:
        return {"date": self.date, "title": _cut(self.title), "mark": self.mark}


@dataclass
class Timeline:
    """A name's history: its notes, oldest first, superseded ones marked."""

    name: str
    key: str
    #: Live notes that mention the name.
    df: int
    entries: list[Entry] = field(default_factory=list)

    @property
    def titles(self) -> list[str]:
        """Shown titles, newest first (the order a plain hint uses)."""
        return [entry.title for entry in reversed(self.entries)]

    def line(self) -> str:
        """The rendered one-line timeline, at most :data:`MAX_CHARS`."""
        entries = list(self.entries)
        while True:
            text = self._render(entries)
            if len(text) <= MAX_CHARS or len(entries) <= 1:
                return text[:MAX_CHARS]
            entries.pop(0)

    def _render(self, entries: Sequence[Entry]) -> str:
        last = len(entries) - 1
        steps = " → ".join(e.render(newest=i == last) for i, e in enumerate(entries))
        dropped = len(self.entries) - len(entries)
        more = f" (+{dropped} older)" if dropped else ""
        return (
            f"OMI: {self.name} has a dated history ({self.df} "
            f"note{'s' if self.df != 1 else ''}; oldest first{more}): {steps}. "
            f"Facts about {self.name} changed over time; recall-note the newest "
            "before asserting them, and read any correction with its claim."
        )

    def to_dict(self) -> dict[str, object]:
        return {"name": self.name, "notes": [e.to_dict() for e in self.entries]}


def _cut(title: str) -> str:
    title = " ".join(title.split())
    return title if len(title) <= TITLE_CHARS else title[: TITLE_CHARS - 1].rstrip() + "…"


def _reader(omi_dir: Path | str) -> Any:
    from omind.store import OmiStore

    store = OmiStore(omi_dir)

    def head(filename: str) -> str:
        try:
            with store.safe_name(filename).open(encoding="utf-8", errors="replace") as fh:
                return fh.read(READ_CHARS)
        except Exception:
            return ""

    return head


_LINK_RE = re.compile(r"\[\[([^\]|#]+)")


def _identity(text: str) -> str:
    text = " ".join(str(text or "").split()).casefold()
    return text[:-3] if text.endswith(".md") else text


def _superseded_targets(notes: Sequence[Any]) -> set[str]:
    """Identities (filename stems / titles, case-folded) that a note in
    ``notes`` names in its ``Supersedes:`` field. A newer note that declares
    the supersession marks the older one even when the older one was never
    stamped ``Superseded by:``."""
    targets: set[str] = set()
    for note in notes:
        raw = str(getattr(note, "supersedes", "") or "")
        if not raw.strip():
            continue
        found = _LINK_RE.findall(raw) or raw.split(",")
        targets.update(_identity(item) for item in found if item.strip())
    return targets - {""}


def _mark(note: Any, head: str, superseded: AbstractSet[str] = frozenset()) -> str:
    from omind import guard

    if str(getattr(note, "superseded_by", "") or "").strip():
        return SUPERSEDED
    if superseded and (
        _identity(note.filename) in superseded or _identity(note.title) in superseded
    ):
        return SUPERSEDED
    if head and guard.looks_stale(head):
        return CORRECTION
    if str(getattr(note, "supersedes", "") or "").strip():
        return SUPERSEDES
    return ""


def build(
    omi_dir: Path | str,
    spelling: str,
    notes: Sequence[Any],
    *,
    df: int | None = None,
    read: Any = None,
) -> Timeline | None:
    """The timeline of ``spelling`` from its notes (newest first, as the name
    index returns them), or ``None`` when it has no history to show.

    ``notes`` are :class:`omind.searchindex.EntityNote` rows, already filtered
    by the caller (``as_of``, exclusions). Never raises.
    """
    if not enabled() or not spelling or not notes:
        return None
    try:
        from omind import entities, namehints

        about = [n for n in notes if namehints._names_in_title(spelling, n.title)]
        if not about and len(notes) > namehints.UNTITLED_MAX_DF:
            return None
        recent = list(about or notes)[:MAX_ENTRIES]
        head = read or _reader(omi_dir)
        superseded = _superseded_targets(notes)
        entries = [
            Entry(
                filename=n.filename,
                title=n.title or Path(n.filename).stem,
                date=str(n.last_seen)[:10],
                mark=_mark(n, head(n.filename), superseded),
            )
            for n in recent
        ]
        if not any(entry.mark for entry in entries):
            return None
        entries.reverse()
        return Timeline(
            name=spelling,
            key=entities.normalize(spelling),
            df=len(notes) if df is None else df,
            entries=entries,
        )
    except Exception:
        return None


def for_names(
    omi_dir: Path | str,
    names: Iterable[str],
    *,
    as_of: str = "",
    exclude: Iterable[str] = (),
    limit: int = 3,
) -> list[Timeline]:
    """Timelines for the names that have one, in the order given.

    One read-only index query for all of them; a name over the rarity ceiling
    or with no history gets none. ``exclude`` lists filenames never shown (the
    note being written). Fails open to ``[]``.
    """
    if not enabled():
        return []
    try:
        from omind import entities, searchindex

        spellings = list(dict.fromkeys(str(n) for n in names if n))[: max(0, limit) * 4]
        if not spellings:
            return []
        lookups = searchindex.entity_lookups_readonly(omi_dir, spellings)
        if not lookups:
            return []
        skip = {str(f) for f in exclude}
        read = _reader(omi_dir)
        out: list[Timeline] = []
        seen: set[str] = set()
        for spelling in spellings:
            key = entities.normalize(spelling)
            lookup = lookups.get(key)
            if key in seen or lookup is None or lookup.common or not lookup.notes:
                continue
            seen.add(key)
            notes = [
                n
                for n in lookup.notes
                if n.filename not in skip and (not as_of or n.last_seen[:10] <= as_of)
            ]
            found = build(omi_dir, spelling, notes, read=read)
            if found is not None:
                out.append(found)
            if len(out) >= limit:
                break
        return out
    except Exception:
        return []


def lines(timelines: Iterable[Timeline]) -> str:
    """The rendered timelines, one per line."""
    with contextlib.suppress(Exception):
        return "\n".join(t.line() for t in timelines)
    return ""
