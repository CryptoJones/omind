# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Aaron K. Clark
"""Tests for #386 (epic #384): a rare identifier shared by the prompt and the
candidate note clears the preflight's 3-shared-terms threshold on its own."""

from __future__ import annotations

from pathlib import Path

import pytest

from omind import bench, compliance, entities, guard, retrieve, searchindex

AS30P_TITLE = "As30p label history"


def _note(omi: Path, title: str, details: str) -> None:
    (omi / f"{title}.md").write_text(
        f"# {title}\n\n## Metadata\n- Created: 2026-09-25\n- Tags:\n\n"
        f"## Summary\n{title}\n\n## Details\n{details}\n",
        encoding="utf-8",
    )


@pytest.fixture(autouse=True)
def _flag_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    for env in (
        retrieve.PREFLIGHT_RARE_TERMS_ENV,
        retrieve.PREFLIGHT_MIN_TERMS_ENV,
        entities.ENABLE_ENV,
        entities.MAX_DF_ENV,
        guard.PREFLIGHT_MODE_ENV,
        guard.MISS_STRICT_ENV,
    ):
        monkeypatch.delenv(env, raising=False)


@pytest.fixture
def omi(tmp_path: Path) -> Path:
    """A vault where ``As30p`` is one note among a dozen unrelated ones."""
    d = tmp_path / "OMI"
    d.mkdir()
    _note(
        d,
        AS30P_TITLE,
        "The label As30p moved from the Seagate to the Samsung SSD, then to the WD Blue.",
    )
    for i, topic in enumerate(
        (
            "Kitchen recipes",
            "Garden watering",
            "Bicycle tyres",
            "Tax paperwork",
            "Piano practice",
            "Bird feeder",
            "Holiday packing",
            "Library books",
            "Coffee grinder",
            "Window cleaning",
            "Running shoes",
        )
    ):
        _note(d, topic, f"Plain notes about {topic.lower()}, item {i}.")
    return d


def test_rare_identifier_alone_names_the_note(omi: Path) -> None:
    context = guard.preflight_turn({"session_id": "rare-1", "prompt": "is As30p mounted?"}, omi)
    assert "weak memory match" not in context
    assert f"[[{AS30P_TITLE}]]" in context
    assert "notes naming As30p" in context
    assert guard.consulted_this_turn("rare-1")
    events = compliance.read_events()
    assert "rare=['As30p']" in events[-1]["detail"]


def test_flag_off_restores_the_plain_threshold(omi: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(retrieve.PREFLIGHT_RARE_TERMS_ENV, "0")
    context = guard.preflight_turn({"session_id": "rare-off", "prompt": "is As30p mounted?"}, omi)
    assert "weak memory match" in context
    assert "[[" not in context


def test_name_index_off_restores_the_plain_threshold(
    omi: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(entities.ENABLE_ENV, "0")
    context = guard.preflight_turn({"session_id": "rare-noidx", "prompt": "is As30p mounted?"}, omi)
    assert "weak memory match" in context


def test_common_identifier_does_not_clear(tmp_path: Path) -> None:
    # A token in more notes than the rarity ceiling (floor 10) is not evidence.
    d = tmp_path / "OMI"
    d.mkdir()
    for i in range(12):
        _note(d, f"Shelf {i}", f"The rack dl380 holds item {i}.")
    context = guard.preflight_turn({"session_id": "rare-common", "prompt": "is dl380 up?"}, d)
    assert "weak memory match" in context


def test_plain_single_word_overlap_is_still_weak(omi: Path) -> None:
    context = guard.preflight_turn({"session_id": "rare-plain", "prompt": "label?"}, omi)
    assert "weak memory match" in context


def test_rare_clear_is_a_hint_even_in_inject_mode(
    omi: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(guard.PREFLIGHT_MODE_ENV, "inject")
    context = guard.preflight_turn({"session_id": "rare-inj", "prompt": "is As30p mounted?"}, omi)
    assert f"[[{AS30P_TITLE}]]" in context
    assert "Seagate" not in context  # titles only: the body is not pushed


def test_rare_hits_require_the_candidate_to_mention_the_name(omi: Path) -> None:
    assert retrieve.rare_identifier_hits("is As30p mounted?", omi, f"{AS30P_TITLE}.md") == [
        "As30p"
    ]
    assert retrieve.rare_identifier_hits("is As30p mounted?", omi, "Kitchen recipes.md") == []
    assert retrieve.rare_identifier_hits("no names here", omi, f"{AS30P_TITLE}.md") == []


def test_rare_hits_fail_open(omi: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(_omi: object) -> None:
        raise RuntimeError("index exploded")

    monkeypatch.setattr(searchindex, "shared", boom)
    assert retrieve.rare_identifier_hits("is As30p mounted?", omi, f"{AS30P_TITLE}.md") == []


def test_bench_pick_mirrors_the_live_path(omi: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pick = bench.preflight_pick(omi, "is As30p mounted?")
    assert pick is not None
    assert pick.filename == f"{AS30P_TITLE}.md"
    assert pick.rare == ("As30p",)
    monkeypatch.setenv(retrieve.PREFLIGHT_RARE_TERMS_ENV, "off")
    assert bench.preflight_pick(omi, "is As30p mounted?") is None
