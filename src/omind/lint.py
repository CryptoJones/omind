# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Aaron K. Clark
"""``omind lint`` — a health check over the OMI vault.

The store enforces structure on the *write* path, but notes also arrive by hand
(Obsidian, an editor, a botched ``--connections`` split) and drift: a wikilink
points at a note that was renamed, a note ends up disconnected from the graph, a
note loses its ``# Title``, two notes say nearly the same thing. None of those
break a single read, so nothing surfaces them — they just quietly rot the vault.

:func:`lint_vault` walks the notes once and reports four classes of problem:

* **broken-link** — a ``[[wikilink]]`` whose target resolves to no note (by stem
  or title). The comma-split bug that motivated the 2.41.0 ``--connections`` fix
  produced exactly these.
* **missing-title** — a note with no ``# Title`` heading (parses to an empty
  title; the store would have rejected it on write).
* **isolated** — a note with neither inbound nor outbound links: orphaned from
  the graph entirely (a leaf with *some* link is fine; this flags the truly
  disconnected).
* **near-duplicate** — two notes whose titles overlap heavily (likely the same
  memory saved twice).

It is read-only: it never edits a note. Severity is advisory — broken links are
``error``, the rest ``warn``/``info`` — so a caller can gate on real breakage.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from omind.paths import RESERVED_FILENAMES
from omind.store import _WIKILINK_RE, NoteFields, parse_note

#: Titles overlapping at/above this Jaccard score are flagged as near-duplicates.
_NEAR_DUP = 0.6
_TOKEN_RE = re.compile(r"[a-z0-9]+")
#: Title tokens too generic to anchor a duplicate match on.
_STOP = frozenset(
    {"the", "and", "for", "with", "note", "omi", "memory", "cj", "cryptojones"}
)


@dataclass(frozen=True)
class LintIssue:
    """One problem found in the vault."""

    kind: str  # broken-link | missing-title | isolated | near-duplicate
    severity: str  # error | warn | info
    note: str  # the offending note's filename (or "A | B" for a pair)
    detail: str

    def format(self) -> str:
        return f"[{self.severity}] {self.kind}: {self.note} — {self.detail}"


def _link_target(raw: str) -> str:
    """The note a ``[[wikilink]]`` body names — the part before ``|`` (alias) or
    ``#`` (heading), trimmed."""
    return raw.split("|", 1)[0].split("#", 1)[0].strip()


def _outbound(text: str) -> set[str]:
    """Link targets named by the note body, original-case (deduped, blanks
    dropped). Resolution against :data:`known` is case-insensitive; the original
    case is kept so a broken-link report shows the link as the author wrote it."""
    return {t for t in (_link_target(m) for m in _WIKILINK_RE.findall(text)) if t}


def _title_tokens(title: str) -> frozenset[str]:
    return frozenset(t for t in _TOKEN_RE.findall(title.lower()) if len(t) > 2 and t not in _STOP)


def _jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


@dataclass
class _Note:
    path: Path
    fields: NoteFields
    outbound: set[str]
    ids: frozenset[str]  # stem + title, lowercased — how others link to this note


def _load(omi_dir: Path | str) -> list[_Note]:
    """Parse every live (non-disabled, non-reserved) note once."""
    omi = Path(omi_dir)
    notes: list[_Note] = []
    if not omi.is_dir():
        return notes
    for path in sorted(omi.glob("*.md")):
        if path.name in RESERVED_FILENAMES or path.name.startswith("."):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        fields = parse_note(text)
        if fields.disabled:
            continue
        ids = {path.stem.strip().lower()}
        if fields.title.strip():
            ids.add(fields.title.strip().lower())
        notes.append(_Note(path, fields, _outbound(text), frozenset(ids)))
    return notes


def lint_vault(omi_dir: Path | str) -> list[LintIssue]:
    """Every problem found in the vault, ordered error → warn → info then by note."""
    notes = _load(omi_dir)
    # Every identifier any note can be linked by (+ reserved stems, which are
    # legitimate link targets even though they're skipped as notes).
    known = {stem for path in RESERVED_FILENAMES for stem in (Path(path).stem.lower(),)}
    for n in notes:
        known |= n.ids
    linked: set[str] = set()  # ids that at least one OTHER note links to
    for n in notes:
        linked |= {t.lower() for t in n.outbound if t.lower() not in n.ids}

    issues: list[LintIssue] = []
    for n in notes:
        for target in sorted(n.outbound):
            if target.lower() not in known:
                issues.append(
                    LintIssue(
                        "broken-link", "error", n.path.name, f"[[{target}]] resolves to no note"
                    )
                )
        if not n.fields.title.strip():
            issues.append(
                LintIssue("missing-title", "warn", n.path.name, "no `# Title` heading")
            )
        if not n.outbound and n.ids.isdisjoint(linked):
            issues.append(
                LintIssue("isolated", "info", n.path.name, "no inbound or outbound links")
            )

    # Near-duplicate titles — each unordered pair reported once.
    toks = [(_title_tokens(n.fields.title or n.path.stem), n) for n in notes]
    for i in range(len(toks)):
        for j in range(i + 1, len(toks)):
            score = _jaccard(toks[i][0], toks[j][0])
            if score >= _NEAR_DUP:
                a, b = sorted((toks[i][1].path.name, toks[j][1].path.name))
                issues.append(
                    LintIssue("near-duplicate", "info", f"{a} | {b}", f"titles {score:.0%} similar")
                )

    rank = {"error": 0, "warn": 1, "info": 2}
    issues.sort(key=lambda x: (rank.get(x.severity, 9), x.kind, x.note))
    return issues


def format_report(issues: list[LintIssue], *, omi_dir: Path | str) -> str:
    """A human-readable report; the all-clear line when the vault is clean."""
    if not issues:
        return f"omind lint: {omi_dir} — no issues found."
    by_sev: dict[str, int] = {}
    for it in issues:
        by_sev[it.severity] = by_sev.get(it.severity, 0) + 1
    summary = ", ".join(f"{by_sev[s]} {s}" for s in ("error", "warn", "info") if s in by_sev)
    lines = [f"omind lint: {omi_dir} — {len(issues)} issue(s) ({summary})", ""]
    lines.extend(it.format() for it in issues)
    return "\n".join(lines)
