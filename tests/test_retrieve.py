# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Aaron K. Clark
"""Tests for just-in-time relevance retrieval (Phase 3.2)."""

from __future__ import annotations

from pathlib import Path

from omind import guard, retrieve
from omind.store import NoteFields, OmiStore


def _vault(tmp_path: Path) -> Path:
    omi = tmp_path / "OMI"
    store = OmiStore(omi)
    store.create_note(
        NoteFields(
            title="Codeberg release workflow",
            summary="push releases to codeberg first then mirror to github",
            tags=["codeberg", "release"],
        )
    )
    store.create_note(
        NoteFields(
            title="Banana smoothie recipe",
            summary="mango and banana blended with ice",
            tags=["food"],
        )
    )
    store.create_note(
        NoteFields(
            title="Credential locations",
            summary="where the codeberg release api token and passwords live",
            tags=["secret", "credentials"],
        )
    )
    return omi


def test_overlap_score_basics() -> None:
    assert retrieve.overlap_score("", "anything") == 0.0  # no task -> can't judge
    assert retrieve.overlap_score("codeberg release push", "codeberg release push now") == 1.0
    assert 0.0 < retrieve.overlap_score("codeberg release push", "codeberg only") < 1.0
    assert retrieve.overlap_score("codeberg release", "banana mango") == 0.0


def test_relevant_titles_ranks_on_topic_note_first(tmp_path: Path) -> None:
    omi = _vault(tmp_path)
    titles = retrieve.relevant_titles("how do I do a codeberg release push", omi)
    assert titles[0] == "Codeberg release workflow"
    assert "Banana smoothie recipe" not in titles  # zero overlap -> not suggested


def test_credential_note_is_deprioritized_for_a_non_credential_task(tmp_path: Path) -> None:
    omi = _vault(tmp_path)
    # "codeberg release" matches both the workflow note and the credential note,
    # but the credential note must rank below it for a non-credential task.
    titles = retrieve.relevant_titles("codeberg release", omi, limit=3)
    assert titles[0] == "Codeberg release workflow"
    if "Credential locations" in titles:
        assert titles.index("Codeberg release workflow") < titles.index("Credential locations")


def test_credential_note_surfaces_when_task_is_about_credentials(tmp_path: Path) -> None:
    omi = _vault(tmp_path)
    titles = retrieve.relevant_titles("where is the api token and credentials", omi)
    assert "Credential locations" in titles


def test_suggest_message_names_notes_or_falls_back(tmp_path: Path) -> None:
    omi = _vault(tmp_path)
    msg = retrieve.suggest_message("codeberg release push", omi)
    assert "[[Codeberg release workflow]]" in msg
    assert "credential" in msg.lower()  # keeps the do-not-open-secrets caveat
    # No task -> generic gate message (never invents a note).
    assert retrieve.suggest_message("", omi) == guard.GATE_MESSAGE
