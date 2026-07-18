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
    assert '`recall-note` with `{"name":"Codeberg release workflow"}`' in msg
    assert "credential" in msg.lower()  # keeps the do-not-open-secrets caveat
    # No task -> generic gate message (never invents a note).
    assert retrieve.suggest_message("", omi) == guard.GATE_MESSAGE


# -- 2.43.2: stemming + instruction-filler stopwords so a real consult isn't
#    scored off-topic purely from word-form mismatch (the verifier-wedge fix) --


def test_stem_folds_morphological_variants() -> None:
    # The families that bit us: a note saying "relevant"/"scored"/"consulted"
    # must match a task saying "relevance"/"scoring"/"consult".
    assert retrieve._stem("scoring") == retrieve._stem("scored") == retrieve._stem("score")
    assert retrieve._stem("consult") == retrieve._stem("consults") == retrieve._stem("consulted")
    assert retrieve._stem("relevance") == retrieve._stem("relevant")
    assert retrieve._stem("gate") == retrieve._stem("gating") == retrieve._stem("gated")
    # Conservative guards: short words and double-s endings are left whole.
    assert retrieve._stem("fix") == "fix"
    assert retrieve._stem("pass") == "pass"
    assert retrieve._stem("address") == "address"


def test_overlap_score_matches_across_word_forms() -> None:
    # Before the fix this scored ~0 (no exact token shared) and the verifier
    # re-closed the gate; stemming makes the shared subject visible.
    score = retrieve.overlap_score(
        "verifier relevance scoring",
        "the verifier scored each consult for relevant material",
    )
    assert score == 1.0  # all three task stems are covered
    # A genuinely-unrelated consult still scores zero (the gate still bites).
    assert retrieve.overlap_score("verifier relevance scoring", "banana mango smoothie") == 0.0


def test_instruction_filler_does_not_dilute_the_task() -> None:
    # The chatty wrapper ("please … before we move any further") must not inflate
    # the task's term count and drag a relevant consult below the relevant band.
    bare = retrieve.overlap_score("fix the verifier scoring", "the verifier scoring logic")
    chatty = retrieve.overlap_score(
        "please fix the verifier scoring before we get any further",
        "the verifier scoring logic",
    )
    assert chatty == bare  # filler stripped -> identical, undiluted score
    assert chatty >= 0.5


def test_credential_detection_survives_stemming(tmp_path: Path) -> None:
    # Stemming must not break the de-prioritization (else the gate could steer
    # toward the secrets notes). Plural/variant credential words still register.
    omi = _vault(tmp_path)
    titles = retrieve.relevant_titles("rotating the api tokens and secrets", omi)
    assert "Credential locations" in titles  # task_is_cred true despite stemming
    assert retrieve._looks_credential("the API tokens and passwords")


def test_normalize_intent_strips_cd_and_paths() -> None:
    assert retrieve.normalize_intent("cd /srv/www && deploy.sh now") == "deploy.sh now"
    assert retrieve.normalize_intent("cd repo; ./run.sh") == "run.sh"
    assert retrieve.normalize_intent("a/b/c.py x/y.txt") == "c.py y.txt"
    assert retrieve.normalize_intent("plain command here") == "plain command here"
    assert retrieve.normalize_intent("") == ""


def test_normalize_intent_lifts_path_heavy_pending_score() -> None:
    # #97: a path-heavy blocked command's directory tokens diluted the overlap
    # denominator into the model-tiebreaker band. Normalizing to basenames drops
    # the dir noise, so a consult about the real subject scores higher — and the
    # path-dir tokens no longer appear in the scored text at all.
    pending = (
        "./spike/bsim/run-spike.sh prototype/corpus/bin/mathlib.x86-64.O0.elf "
        "prototype/corpus/bin/mathlib.i386.O0.elf | grep '[bsim]'"
    )
    consult = "bsim spike harness for mathlib decompilation matching"
    norm = retrieve.normalize_intent(pending)
    assert "prototype" not in norm and "corpus" not in norm  # dir scaffolding gone
    assert retrieve.overlap_score(norm, consult) > retrieve.overlap_score(pending, consult)
