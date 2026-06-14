# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Aaron K. Clark
"""Field-level 3-way merge for OMI notes — the mesh's git merge driver.

Two nodes can edit the same note while partitioned; this driver converges
them **without losing data** (docs/mesh.md, "The conflict model"). Notes
round-trip through :class:`~omind.store.NoteFields`, so the merge operates
field by field instead of diffing raw text:

- ``tags`` / ``connections`` / ``references``: 3-way set union — an element
  removed by one side and untouched by the other stays removed.
- ``action_items``: union by text; ``done`` is OR.
- ``title`` / ``summary`` / ``related_to`` / ``created`` / ``disabled``:
  last-writer-wins by the note's Lamport rev, tie-broken by node-id.
- ``details`` (free text): 3-way line merge; disjoint edits both apply,
  same-point additions concatenate, and only a truly diverging region emits
  conflict markers (plus a ``merge-conflict`` tag so doctor and humans see it).
- Sections the template doesn't know (`## Anything Else`) are preserved and
  merged as text blocks — the driver must never eat hand-curated content.

Every rule is **symmetric**: merging (ours, theirs) and (theirs, ours)
produces byte-identical output, ordered by revision rather than by side, so
two nodes that merge each other's work converge even when a region conflicts.

The driver exits 0 whenever it produced a structured merge — markers
included — because an unattended sync daemon must keep flowing; loudness
comes from the tag, the messages on stderr, and ``omind doctor``. Exit 1
(fall back to git's default conflict) only when an input cannot be parsed.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path

from omind.clock import Rev
from omind.store import (
    TEMPLATE_SECTIONS,
    ActionItem,
    NoteFields,
    parse_note,
    render_fields,
    split_sections,
)

#: Tag stamped onto a note whose Details carry conflict markers.
CONFLICT_TAG = "merge-conflict"

#: ``TEMPLATE_SECTIONS`` (the headings the NoteFields template owns; anything
#: else is an "extra" section preserved verbatim-ish through the merge) lives in
#: :mod:`omind.store` so parse_note's extra-capture and this driver agree.

@dataclass
class MergeResult:
    fields: NoteFields
    extras: dict[str, list[str]]
    clean: bool
    messages: list[str] = field(default_factory=list)


# -- 3-way line merge ---------------------------------------------------------


@dataclass
class _Hunk:
    lo: int
    hi: int
    repl: list[str]
    side: int  # 0 = ours, 1 = theirs

    @property
    def is_insertion(self) -> bool:
        return self.lo == self.hi


def _hunks(base: list[str], side_lines: list[str], side: int) -> list[_Hunk]:
    matcher = SequenceMatcher(a=base, b=side_lines, autojunk=False)
    out: list[_Hunk] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag != "equal":
            out.append(_Hunk(lo=i1, hi=i2, repl=side_lines[j1:j2], side=side))
    return out


def _apply(base: list[str], hunks: list[_Hunk], lo: int, hi: int) -> list[str]:
    """Apply one side's hunks to the base region [lo, hi)."""
    result: list[str] = []
    pos = lo
    for h in sorted(hunks, key=lambda h: h.lo):
        result.extend(base[pos : h.lo])
        result.extend(h.repl)
        pos = h.hi
    result.extend(base[pos:hi])
    return result


def _rev_order_key(rev: Rev | None, text: list[str]) -> tuple[int, str, str]:
    """Deterministic, side-independent ordering for concatenation/markers."""
    if rev is None:
        return (-1, "", "\n".join(text))
    return (rev.counter, rev.node_id, "\n".join(text))


def _label(rev: Rev | None) -> str:
    return str(rev) if rev is not None else "unversioned"


def _merge3_lines(
    base: list[str],
    ours: list[str],
    theirs: list[str],
    ours_rev: Rev | None,
    theirs_rev: Rev | None,
    what: str,
) -> tuple[list[str], bool, list[str]]:
    """3-way line merge. Returns (lines, clean, messages); symmetric in sides."""
    all_hunks = sorted(
        _hunks(base, ours, 0) + _hunks(base, theirs, 1), key=lambda h: (h.lo, h.hi, h.side)
    )
    out: list[str] = []
    messages: list[str] = []
    clean = True
    pos = 0
    i = 0
    while i < len(all_hunks):
        group = [all_hunks[i]]
        hi = all_hunks[i].hi
        i += 1
        # Hunks overlapping the group join it; an insertion touching the
        # boundary joins too (its anchor point is inside the disputed region).
        while i < len(all_hunks):
            nxt = all_hunks[i]
            touches_insertion = nxt.is_insertion or any(
                g.is_insertion and g.hi == hi for g in group
            )
            joins = nxt.lo < hi or (nxt.lo == hi and touches_insertion)
            if not joins:
                break
            group.append(nxt)
            hi = max(hi, nxt.hi)
            i += 1
        lo = min(h.lo for h in group)
        out.extend(base[pos:lo])
        pos = hi

        ours_part = [h for h in group if h.side == 0]
        theirs_part = [h for h in group if h.side == 1]
        if not ours_part or not theirs_part:
            out.extend(_apply(base, group, lo, hi))
            continue
        o_region = _apply(base, ours_part, lo, hi)
        t_region = _apply(base, theirs_part, lo, hi)
        if o_region == t_region:
            out.extend(o_region)
            continue
        if all(h.is_insertion and h.lo == lo for h in group):
            # Same-point additions concatenate, ordered by revision (older
            # first) — disjoint knowledge from two nodes, both kept.
            pieces = sorted(
                [(ours_rev, o_region), (theirs_rev, t_region)],
                key=lambda p: _rev_order_key(p[0], p[1]),
            )
            for _, piece in pieces:
                out.extend(piece)
            messages.append(f"{what}: concatenated concurrent additions")
            continue
        # The same region truly diverged: keep both, loudly, higher rev first.
        first, second = sorted(
            [(ours_rev, o_region), (theirs_rev, t_region)],
            key=lambda p: _rev_order_key(p[0], p[1]),
            reverse=True,
        )
        out.append(f"<<<<<<< {_label(first[0])}")
        out.extend(first[1])
        out.append("=======")
        out.extend(second[1])
        out.append(f">>>>>>> {_label(second[0])}")
        messages.append(f"{what}: conflicting edits kept under markers")
        clean = False
    out.extend(base[pos:])
    return out, clean, messages


# -- field rules ----------------------------------------------------------------


def _union3(base: list[str], ours: list[str], theirs: list[str]) -> list[str]:
    """3-way set merge: survivors keep base order; additions sorted (symmetric)."""
    survivors = [x for x in base if x in ours and x in theirs]
    additions = sorted({x for x in ours if x not in base} | {x for x in theirs if x not in base})
    return survivors + [x for x in additions if x not in survivors]


def _merge_actions(
    base: list[ActionItem], ours: list[ActionItem], theirs: list[ActionItem]
) -> list[ActionItem]:
    def by_text(items: list[ActionItem]) -> dict[str, ActionItem]:
        return {i.text.strip(): i for i in items if i.text.strip()}

    b, o, t = by_text(base), by_text(ours), by_text(theirs)
    texts = [x for x in b if x in o and x in t]
    texts += [x for x in sorted(set(o) | set(t)) if x not in b and x not in texts]
    return [
        ActionItem(
            text=text,
            done=(text in o and o[text].done) or (text in t and t[text].done),
        )
        for text in texts
    ]


def merge_fields(base: NoteFields, ours: NoteFields, theirs: NoteFields) -> MergeResult:
    """Merge the template fields of base/ours/theirs (extras handled separately)."""
    o_rev = Rev.parse(ours.rev)
    t_rev = Rev.parse(theirs.rev)
    messages: list[str] = []

    ours_wins: bool | None
    if o_rev is None and t_rev is None:
        ours_wins = None  # no causal information — fall back to symmetric max()
    elif o_rev is None or t_rev is None:
        ours_wins = t_rev is None  # a stamped edit beats an unstamped one
    else:
        ours_wins = o_rev.sort_key() > t_rev.sort_key()

    def scalar(name: str) -> str | bool:
        b, o, t = getattr(base, name), getattr(ours, name), getattr(theirs, name)
        if o == t:
            return o  # type: ignore[no-any-return]
        if o == b:
            return t  # type: ignore[no-any-return]
        if t == b:
            return o  # type: ignore[no-any-return]
        if ours_wins is None:
            winner = max(o, t)
            messages.append(f"{name}: concurrent unversioned edits; kept {winner!r}")
            return winner  # type: ignore[no-any-return]
        messages.append(f"{name}: last-writer-wins by rev")
        return o if ours_wins else t  # type: ignore[no-any-return]

    details, details_clean, detail_msgs = _merge3_lines(
        base.details.splitlines(),
        ours.details.splitlines(),
        theirs.details.splitlines(),
        o_rev,
        t_rev,
        "details",
    )
    messages.extend(detail_msgs)

    revs = [r for r in (o_rev, t_rev) if r is not None]
    merged_rev = str(max(revs, key=lambda r: r.sort_key())) if revs else ""

    merged = NoteFields(
        title=str(scalar("title")),
        summary=str(scalar("summary")),
        details="\n".join(details).strip(),
        created=str(scalar("created")),
        tags=_union3(base.tags, ours.tags, theirs.tags),
        related_to=str(scalar("related_to")),
        connections=_union3(base.connections, ours.connections, theirs.connections),
        action_items=_merge_actions(base.action_items, ours.action_items, theirs.action_items),
        references=_union3(base.references, ours.references, theirs.references),
        rev=merged_rev,
        disabled=bool(scalar("disabled")),
    )
    if not details_clean and CONFLICT_TAG not in merged.tags:
        merged.tags.append(CONFLICT_TAG)
    return MergeResult(fields=merged, extras={}, clean=details_clean, messages=messages)


# -- extra (non-template) sections ------------------------------------------------


def _extra_sections(md: str) -> dict[str, list[str]]:
    """Body lines of every ``## Heading`` the NoteFields template doesn't own.

    Uses the same splitter as ``parse_note`` — two parsers deciding what a
    section heading is would disagree eventually, and the loser's content
    would be emitted twice in every merged note.
    """
    _, sections = split_sections(md)
    return {
        h: _strip_blank(body) for h, body in sections.items() if h not in TEMPLATE_SECTIONS
    }


def _strip_blank(lines: list[str]) -> list[str]:
    start, end = 0, len(lines)
    while start < end and not lines[start].strip():
        start += 1
    while end > start and not lines[end - 1].strip():
        end -= 1
    return lines[start:end]


def _merge_extras(
    base_md: str,
    ours_md: str,
    theirs_md: str,
    o_rev: Rev | None,
    t_rev: Rev | None,
) -> tuple[dict[str, list[str]], bool, list[str]]:
    base = _extra_sections(base_md)
    ours = _extra_sections(ours_md)
    theirs = _extra_sections(theirs_md)
    merged: dict[str, list[str]] = {}
    clean = True
    messages: list[str] = []
    for heading in sorted(set(base) | set(ours) | set(theirs)):
        b = base.get(heading)
        o = ours.get(heading)
        t = theirs.get(heading)
        if o == t:
            result = o
        elif o == b:
            result = t
        elif t == b:
            result = o
        else:
            result, section_clean, msgs = _merge3_lines(
                b or [], o or [], t or [], o_rev, t_rev, f"section {heading!r}"
            )
            clean = clean and section_clean
            messages.extend(msgs)
        if result:
            merged[heading] = result
    return merged, clean, messages


# -- whole-note merge ----------------------------------------------------------------


def merge_note_texts(base_md: str, ours_md: str, theirs_md: str) -> tuple[str, bool, list[str]]:
    """Merge three versions of a note's Markdown. Returns (text, clean, messages)."""
    base = parse_note(base_md)
    ours = parse_note(ours_md)
    theirs = parse_note(theirs_md)
    result = merge_fields(base, ours, theirs)
    extras, extras_clean, extra_msgs = _merge_extras(
        base_md, ours_md, theirs_md, Rev.parse(ours.rev), Rev.parse(theirs.rev)
    )
    result.messages.extend(extra_msgs)
    clean = result.clean and extras_clean
    if not clean and CONFLICT_TAG not in result.fields.tags:
        result.fields.tags.append(CONFLICT_TAG)

    text = render_fields(result.fields)
    for heading in sorted(extras):
        text += f"\n## {heading}\n" + "\n".join(extras[heading]) + "\n"
    return text, clean, result.messages


def run_merge_driver(
    base_path: Path, ours_path: Path, theirs_path: Path, path_label: str = ""
) -> int:
    """Git merge-driver entry: merge into ``ours_path`` (git's %A convention).

    Exit 0 whenever a structured merge was produced — conflict markers
    included — so the unattended daemon keeps flowing. Exit 1 only when an
    input can't even be read/parsed, letting git fall back to its default
    conflict handling.
    """
    label = path_label or ours_path.name
    try:
        base_md = base_path.read_text(encoding="utf-8")
        ours_md = ours_path.read_text(encoding="utf-8")
        theirs_md = theirs_path.read_text(encoding="utf-8")
        merged, clean, messages = merge_note_texts(base_md, ours_md, theirs_md)
        ours_path.write_text(merged, encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        print(f"omind merge-driver: {label}: {exc}", file=sys.stderr)
        return 1
    for message in messages:
        print(f"omind merge-driver: {label}: {message}", file=sys.stderr)
    if not clean:
        print(
            f"omind merge-driver: {label}: kept both sides under conflict markers "
            f"(tagged #{CONFLICT_TAG})",
            file=sys.stderr,
        )
    return 0
