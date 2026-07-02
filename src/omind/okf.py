# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Aaron K. Clark
"""OKF (Open Knowledge Format v0.1) conformance + conversion for an OMI vault.

OKF (Google Cloud, ``GoogleCloudPlatform/knowledge-catalog`` → ``okf/SPEC.md``)
represents a body of knowledge as a directory of markdown files, each carrying a
leading YAML frontmatter block whose one required field is ``type``. A bundle is
conformant (SPEC §9) iff:

1. every non-reserved ``.md`` file has a parseable YAML frontmatter block,
2. every such block has a non-empty ``type`` field, and
3. the reserved ``index.md`` / ``log.md`` follow their structure when present.

omind's OMI vault already IS such a bundle — :func:`omind.store.render_fields`
emits the frontmatter for every note it writes. This module (a) validates a
folder against the conformance rules and (b) migrates a *legacy* vault (whose
notes carried metadata only in a ``## Metadata`` section) into the frontmatter
form, in place and idempotently.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from omind.paths import NON_CONSULT_FILENAMES
from omind.store import OmiStore, parse_frontmatter, parse_note, render_fields

#: Files that are NOT OKF concept documents, so the ``type``-required rule does
#: not apply: OKF reserves ``index.md`` / ``log.md`` (directory listing / change
#: log), and omind additionally treats its own scaffolding (``MEMORY.md``
#: recent-index, ``Memory Template.md``) as non-concept. Superset of the vault's
#: reserved names.
RESERVED_OKF_FILES = frozenset({"index.md", "log.md"}) | NON_CONSULT_FILENAMES


@dataclass
class OkfProblem:
    """A single conformance failure: which file, and what's wrong with it."""

    filename: str
    problem: str


@dataclass
class OkfReport:
    """Result of an OKF conformance scan over a vault folder."""

    concepts: int = 0
    conformant: int = 0
    problems: list[OkfProblem] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


def _leading_frontmatter_block(text: str) -> str:
    """Return the verbatim leading ``---`` … ``---`` block, or ``""`` if absent.

    A block that opens with ``---`` but is never closed is not a valid
    frontmatter block, so it returns ``""`` (reported as non-conformant) rather
    than swallowing the whole document.
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return ""
    out = [lines[0]]
    for line in lines[1:]:
        out.append(line)
        if line.strip() == "---":
            return "\n".join(out)
    return ""


def check_conformance(omi_dir: Path | str) -> OkfReport:
    """Validate a vault folder against OKF v0.1 conformance rules 1 & 2.

    Rule 3 (reserved-file structure) holds by construction — omind generates
    ``index.md`` with no frontmatter and ``#`` section headings — so this checks
    the per-concept rules: every non-reserved note has a parseable frontmatter
    block carrying a non-empty ``type``.
    """
    folder = Path(omi_dir).expanduser()
    report = OkfReport()
    for path in sorted(folder.glob("*.md")):
        if path.name in RESERVED_OKF_FILES:
            continue
        report.concepts += 1
        text = path.read_text(encoding="utf-8-sig", errors="replace")
        block = _leading_frontmatter_block(text)
        if not block:
            report.problems.append(OkfProblem(path.name, "no parseable YAML frontmatter block"))
            continue
        fm = parse_frontmatter(block)
        if not fm:
            report.problems.append(
                OkfProblem(path.name, "frontmatter is not a parseable YAML mapping")
            )
            continue
        if not str(fm.get("type", "") or "").strip():
            report.problems.append(OkfProblem(path.name, "frontmatter has no non-empty 'type'"))
            continue
        report.conformant += 1
    return report


@dataclass
class ConvertResult:
    """Outcome of :func:`convert_vault`: how many notes changed, plus the scan."""

    converted: int = 0
    unchanged: int = 0
    report: OkfReport = field(default_factory=OkfReport)


def convert_vault(omi_dir: Path | str, *, dry_run: bool = False) -> ConvertResult:
    """Rewrite every note in ``omi_dir`` into OKF form (frontmatter + ``type``).

    Idempotent: a note already in OKF form renders byte-identical and is skipped
    (no write, no mesh revision bump), so re-running is a no-op. The conformance
    scan runs afterwards and rides back on the result.

    Run this on ONE node and let the mesh replicate the reformatted notes: each
    rewrite bumps the note's Lamport revision, so converting the same vault
    concurrently on two peers would create needless merge work.
    """
    store = OmiStore(omi_dir)
    result = ConvertResult()
    for summary in store.list_notes(include_disabled=True):
        name = summary.filename
        raw = store.read_note(name)
        rendered = render_fields(parse_note(raw))
        if rendered.strip() == raw.strip():
            result.unchanged += 1
            continue
        if not dry_run:
            store.write_note(name, rendered)
        result.converted += 1
    result.report = check_conformance(store.omi_dir)
    return result
