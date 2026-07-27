# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Aaron K. Clark
"""Reviewed near-duplicate consolidation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from omind import consolidate
from omind.store import NoteFields, OmiStore, parse_note, render_fields


@pytest.fixture
def vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    omi = tmp_path / "vault" / "OMI"
    omi.mkdir(parents=True)
    store = OmiStore(omi)
    store.create_note(
        NoteFields(
            title="Alpha Memory",
            summary="same durable fact",
            details="alpha-only detail",
            tags=["one"],
            connections=["Alpha Link"],
        )
    )
    store.create_note(
        NoteFields(
            title="Alpha Memory Copy",
            summary="same durable fact restated",
            details="beta-only detail",
            tags=["two"],
            connections=["Beta Link"],
        )
    )
    monkeypatch.setattr(
        consolidate,
        "_candidate_pairs",
        lambda _omi: [("Alpha Memory.md", "Alpha Memory Copy.md", 0.97)],
    )
    return omi


def test_proposal_is_outside_vault_and_leaves_notes_byte_identical(vault: Path) -> None:
    before = {path.name: path.read_bytes() for path in vault.glob("*.md")}

    proposals = consolidate.propose(vault, limit=1)

    assert len(proposals) == 1
    proposal = proposals[0]
    assert vault not in proposal.plan_path.parents
    assert vault not in proposal.draft_path.parents
    assert proposal.plan_path.is_file()
    draft = parse_note(proposal.draft_path.read_text(encoding="utf-8"))
    assert "alpha-only detail" in draft.details
    assert "beta-only detail" in draft.details
    assert {"one", "two", "consolidated"} <= set(draft.tags)
    assert before == {path.name: path.read_bytes() for path in vault.glob("*.md")}


def test_apply_edited_draft_creates_merge_and_archives_sources(vault: Path) -> None:
    proposal = consolidate.propose(vault, limit=1)[0]
    draft = parse_note(proposal.draft_path.read_text(encoding="utf-8"))
    draft.title = "Reviewed Alpha Memory"
    draft.summary = "human-approved summary"
    proposal.draft_path.write_text(render_fields(draft), encoding="utf-8")

    result = consolidate.apply(vault, proposal.plan_id)

    assert result.filename == "Reviewed Alpha Memory.md"
    store = OmiStore(vault)
    merged = store.read_fields(result.filename)
    assert merged.summary == "human-approved summary"
    assert "alpha-only detail" in merged.details
    assert "beta-only detail" in merged.details
    assert store.read_fields("Alpha Memory.md").disabled is True
    assert store.read_fields("Alpha Memory Copy.md").disabled is True


def test_apply_rejects_source_changed_after_review(vault: Path) -> None:
    proposal = consolidate.propose(vault, limit=1)[0]
    store = OmiStore(vault)
    changed = store.read_fields("Alpha Memory.md")
    changed.summary = "changed after the proposal"
    store.update_note("Alpha Memory.md", changed)

    with pytest.raises(consolidate.ConsolidationError, match="source changed"):
        consolidate.apply(vault, proposal.plan_id)

    expected = vault / "Consolidated — Alpha Memory + Alpha Memory Copy.md"
    assert not expected.exists()
    assert store.read_fields("Alpha Memory.md").disabled is False
    assert store.read_fields("Alpha Memory Copy.md").disabled is False


@pytest.mark.parametrize("plan_id", ("../bad", "ABCDEF0123456789", "abcd"))
def test_apply_rejects_unsafe_plan_ids(vault: Path, plan_id: str) -> None:
    with pytest.raises(consolidate.ConsolidationError, match="plan id"):
        consolidate.apply(vault, plan_id)


def test_apply_rejects_tampered_source_path(vault: Path) -> None:
    proposal = consolidate.propose(vault, limit=1)[0]
    payload = json.loads(proposal.plan_path.read_text(encoding="utf-8"))
    payload["sources"][0]["filename"] = "../outside.md"
    proposal.plan_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(consolidate.ConsolidationError, match="source is invalid"):
        consolidate.apply(vault, proposal.plan_id)
