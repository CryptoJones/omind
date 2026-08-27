# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Aaron K. Clark
"""Reviewed consolidation of near-duplicate OMI notes.

Proposals and editable drafts are machine-local derived state.  The Markdown
vault changes only when an operator explicitly applies a proposal.
"""

from __future__ import annotations

import json
import re
import secrets
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from omind.lint import lint_vault
from omind.paths import atomic_write_text, consolidation_dir
from omind.store import (
    ActionItem,
    NoteConflictError,
    NoteError,
    NoteFields,
    OmiStore,
    parse_note,
    render_fields,
)

_PLAN_ID_RE = re.compile(r"^[0-9a-f]{16}$")
_PERCENT_RE = re.compile(r"(\d+)%")


class ConsolidationError(Exception):
    """A proposal cannot be safely created or applied."""


@dataclass(frozen=True)
class Source:
    filename: str
    version: str


@dataclass(frozen=True)
class Proposal:
    plan_id: str
    sources: tuple[Source, Source]
    similarity: float
    plan_path: Path
    draft_path: Path


@dataclass(frozen=True)
class ApplyResult:
    filename: str
    archived: tuple[str, str]


def _candidate_pairs(omi_dir: Path) -> list[tuple[str, str, float]]:
    """Use lint's semantic detector and its title/periodic-series safeguards."""
    pairs: list[tuple[str, str, float]] = []
    for issue in lint_vault(omi_dir):
        if issue.kind != "near-duplicate":
            continue
        names = issue.note.split(" | ", 1)
        if len(names) != 2:
            continue
        match = _PERCENT_RE.search(issue.detail)
        score = int(match.group(1)) / 100 if match else 0.0
        pairs.append((names[0], names[1], score))
    return sorted(pairs, key=lambda row: (-row[2], row[0].lower(), row[1].lower()))


def _unique(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        clean = value.strip()
        key = clean.casefold()
        if clean and key not in seen:
            seen.add(key)
            result.append(clean)
    return result


def _draft_fields(
    left_name: str,
    left: NoteFields,
    right_name: str,
    right: NoteFields,
) -> NoteFields:
    """Build a lossless-by-default editable draft from two parsed notes."""
    left_title = left.title.strip() or Path(left_name).stem
    right_title = right.title.strip() or Path(right_name).stem
    summaries = _unique([left.summary, right.summary])

    def source_body(fields: NoteFields) -> str:
        parts = [fields.lead.strip(), fields.details.strip()]
        if fields.supersedes:
            parts.append(f"Supersedes: {fields.supersedes}")
        if fields.superseded_by:
            parts.append(f"Superseded by: {fields.superseded_by}")
        return "\n\n".join(part for part in parts if part) or "_No detail body._"

    details = "\n\n".join(
        (
            f"### From [[{Path(left_name).stem}]]\n\n" + source_body(left),
            f"### From [[{Path(right_name).stem}]]\n\n" + source_body(right),
        )
    )
    extras: dict[str, list[str]] = {}
    for filename, fields in ((left_name, left), (right_name, right)):
        label = Path(filename).stem
        for heading, lines in fields.extras.items():
            extras[f"{heading} — from {label}"] = list(lines)
        if fields.frontmatter.strip():
            extras[f"Source metadata — from {label}"] = [
                "```yaml",
                *fields.frontmatter.strip().splitlines(),
                "```",
            ]
    actions = [
        ActionItem(text=item.text, done=item.done)
        for item in [*left.action_items, *right.action_items]
    ]
    return NoteFields(
        title=f"Consolidated — {left_title} + {right_title}",
        summary=" ".join(summaries),
        details=details,
        tags=_unique([*left.tags, *right.tags, "consolidated"]),
        related_to="; ".join(_unique([left.related_to, right.related_to])),
        connections=_unique(
            [
                *left.connections,
                *right.connections,
                Path(left_name).stem,
                Path(right_name).stem,
            ]
        ),
        action_items=actions,
        references=_unique([*left.references, *right.references]),
        # The #169 chain: the merged note supersedes both sources, so retrieval
        # de-ranks the archived originals instead of serving them as current
        # (2026-08-27 review — consolidation was bypassing temporal validity).
        supersedes=", ".join([Path(left_name).stem, Path(right_name).stem]),
        extras=extras,
        okf_type=left.okf_type or right.okf_type,
    )


def _proposal_paths(omi_dir: Path, plan_id: str) -> tuple[Path, Path]:
    root = consolidation_dir(omi_dir)
    return root / f"{plan_id}.json", root / f"{plan_id}.md"


def propose(omi_dir: Path | str, *, limit: int = 5) -> list[Proposal]:
    """Write at most ``limit`` non-overlapping review plans; never touch notes."""
    if limit < 1:
        raise ConsolidationError("limit must be at least 1")
    omi = Path(omi_dir).expanduser().resolve()
    store = OmiStore(omi)
    selected: list[tuple[str, str, float]] = []
    used: set[str] = set()
    for left, right, score in _candidate_pairs(omi):
        # Route every candidate through safe_name and skip stale/archived rows.
        left_path = store.safe_name(left)
        right_path = store.safe_name(right)
        if left in used or right in used or not left_path.is_file() or not right_path.is_file():
            continue
        left_fields = store.read_fields(left)
        right_fields = store.read_fields(right)
        if left_fields.disabled or right_fields.disabled:
            continue
        selected.append((left, right, score))
        used.update((left, right))
        if len(selected) >= limit:
            break

    proposals: list[Proposal] = []
    for left, right, score in selected:
        plan_id = secrets.token_hex(8)
        plan_path, draft_path = _proposal_paths(omi, plan_id)
        # Capture versions BEFORE reading fields: the draft must describe the
        # content its version token vouches for, or apply's revalidation passes
        # against content the draft never saw (2026-08-27 review).
        left_version = store.note_version(left)
        right_version = store.note_version(right)
        left_fields = store.read_fields(left)
        right_fields = store.read_fields(right)
        sources = (
            Source(left, left_version),
            Source(right, right_version),
        )
        draft = _draft_fields(left, left_fields, right, right_fields)
        atomic_write_text(draft_path, render_fields(draft))
        payload: dict[str, Any] = {
            "version": 1,
            "plan_id": plan_id,
            "vault": str(omi),
            "similarity": score,
            "draft": draft_path.name,
            "sources": [asdict(source) for source in sources],
        }
        atomic_write_text(plan_path, json.dumps(payload, indent=2, sort_keys=True) + "\n")
        proposals.append(Proposal(plan_id, sources, score, plan_path, draft_path))
    return proposals


def _load_plan(omi: Path, plan_id: str) -> tuple[dict[str, Any], Path]:
    if not _PLAN_ID_RE.fullmatch(plan_id):
        raise ConsolidationError("plan id must be exactly 16 lowercase hexadecimal characters")
    plan_path, expected_draft = _proposal_paths(omi, plan_id)
    try:
        payload = json.loads(plan_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConsolidationError(f"proposal not found: {plan_id}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise ConsolidationError(f"cannot read proposal {plan_id}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ConsolidationError("proposal metadata is invalid")
    if payload.get("version") != 1 or payload.get("plan_id") != plan_id:
        raise ConsolidationError("proposal metadata is invalid")
    if payload.get("vault") != str(omi):
        raise ConsolidationError("proposal belongs to a different vault")
    if payload.get("draft") != expected_draft.name:
        raise ConsolidationError("proposal draft path is invalid")
    sources = payload.get("sources")
    if not isinstance(sources, list) or len(sources) != 2:
        raise ConsolidationError("proposal must name exactly two sources")
    return payload, expected_draft


def apply(omi_dir: Path | str, plan_id: str) -> ApplyResult:
    """Create the reviewed draft, then archive both unchanged source notes."""
    omi = Path(omi_dir).expanduser().resolve()
    payload, draft_path = _load_plan(omi, plan_id)
    store = OmiStore(omi)
    sources: list[Source] = []
    for raw in payload["sources"]:
        if not isinstance(raw, dict):
            raise ConsolidationError("proposal source metadata is invalid")
        source = Source(str(raw.get("filename", "")), str(raw.get("version", "")))
        try:
            store.safe_name(source.filename)
        except NoteError as exc:
            raise ConsolidationError(f"proposal source is invalid: {exc}") from exc
        if not source.version or store.note_version(source.filename) != source.version:
            raise ConsolidationError(
                f"source changed since review: {source.filename}; generate a new proposal"
            )
        sources.append(source)
    if sources[0].filename.casefold() == sources[1].filename.casefold():
        raise ConsolidationError("proposal sources must be two different notes")
    try:
        draft = parse_note(draft_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConsolidationError(f"proposal draft is missing: {draft_path}") from exc
    except OSError as exc:
        raise ConsolidationError(f"cannot read proposal draft: {exc}") from exc
    if not draft.title.strip():
        raise ConsolidationError("reviewed draft requires a title")
    draft.rev = ""
    draft.disabled = False

    try:
        filename = store.create_and_disable_sources(
            draft,
            [(source.filename, source.version) for source in sources],
            superseded_by=store.filename_for_title(draft.title),
        )
    except NoteConflictError as exc:
        raise ConsolidationError(
            f"{exc}; generate a new proposal before applying"
        ) from exc
    except NoteError as exc:
        raise ConsolidationError(str(exc)) from exc
    return ApplyResult(filename, (sources[0].filename, sources[1].filename))
