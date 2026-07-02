# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Aaron K. Clark
"""Tests for omind.merge: the field-level 3-way note merge driver.

The merge driver is where the bugs would eat memories, so this is the
heaviest suite in the repo: every field rule, both LWW directions, the
tie-break, disable-vs-edit in both orders, disjoint vs truly-conflicting
details, determinism, symmetry (sides swapped -> byte-identical output),
and losslessness (every changed line survives somewhere).
"""

from __future__ import annotations

from pathlib import Path

from omind.merge import (
    CONFLICT_TAG,
    merge_fields,
    merge_note_texts,
    run_merge_driver,
)
from omind.store import ActionItem, NoteFields, parse_note, render_fields


def note(
    rev: str = "",
    *,
    title: str = "Note",
    summary: str = "base summary",
    details: str = "line one\nline two\nline three",
    tags: list[str] | None = None,
    connections: list[str] | None = None,
    action_items: list[ActionItem] | None = None,
    references: list[str] | None = None,
    related_to: str = "",
    created: str = "2026-06-01",
    disabled: bool = False,
) -> NoteFields:
    return NoteFields(
        title=title,
        summary=summary,
        details=details,
        created=created,
        tags=list(tags or ["base"]),
        related_to=related_to,
        connections=list(connections or []),
        action_items=list(action_items or []),
        references=list(references or []),
        rev=rev,
        disabled=disabled,
    )


# -- scalar LWW ------------------------------------------------------------------


def test_summary_lww_both_directions() -> None:
    base = note("1@a")
    newer = note("3@b", summary="newer wins")
    older = note("2@a", summary="older loses")
    assert merge_fields(base, newer, older).fields.summary == "newer wins"
    assert merge_fields(base, older, newer).fields.summary == "newer wins"


def test_lww_tie_breaks_by_node_id() -> None:
    base = note("1@a")
    from_b = note("2@b", summary="from b")
    from_z = note("2@z", summary="from z")
    assert merge_fields(base, from_b, from_z).fields.summary == "from z"
    assert merge_fields(base, from_z, from_b).fields.summary == "from z"


def test_stamped_edit_beats_unstamped() -> None:
    base = note()
    legacy = note("", summary="legacy edit")
    stamped = note("1@a", summary="stamped edit")
    assert merge_fields(base, legacy, stamped).fields.summary == "stamped edit"
    assert merge_fields(base, stamped, legacy).fields.summary == "stamped edit"


def test_unchanged_side_never_wins_by_rev() -> None:
    # theirs has the higher rev (it edited something else) but did NOT touch
    # the summary — the 3-way rule keeps ours' actual edit.
    base = note("1@a")
    ours = note("2@a", summary="real change")
    theirs = note("3@b")  # summary untouched
    assert merge_fields(base, ours, theirs).fields.summary == "real change"


def test_both_unversioned_is_deterministic_and_symmetric() -> None:
    base = note()
    one = note("", summary="apple")
    two = note("", summary="zebra")
    a = merge_fields(base, one, two)
    b = merge_fields(base, two, one)
    assert a.fields.summary == b.fields.summary == "zebra"  # max() — side-free
    assert a.messages  # and loud about it


def test_result_rev_is_max_of_sides() -> None:
    base = note("1@a")
    assert merge_fields(base, note("4@a"), note("2@b")).fields.rev == "4@a"
    assert merge_fields(base, note("2@b"), note("4@a")).fields.rev == "4@a"


def test_equal_rev_different_content_is_symmetric() -> None:
    """Equal rev identity with differing content must converge (not resolve by side)."""
    base = note("1@a", summary="base")
    one = note("5@a", summary="apple")
    two = note("5@a", summary="zebra")  # same rev identity, different content
    a = merge_fields(base, one, two)
    b = merge_fields(base, two, one)
    assert a.fields.summary == b.fields.summary  # converges regardless of side
    assert a.fields.summary == "zebra"  # symmetric max() tiebreak


def test_merge_preserves_frontmatter_and_lead() -> None:
    """A YAML frontmatter block and lead prose must survive a merge, not vanish."""
    base = "---\ntags: [x]\n---\n# N\n\nlead text.\n\n## Summary\ns\n\n## Details\nd\n"
    ours = "---\ntags: [x]\n---\n# N\n\nlead text.\n\n## Summary\ns2\n\n## Details\nd\n"
    theirs = base
    merged, _clean, _msgs = merge_note_texts(base, ours, theirs)
    assert "#x" in merged  # frontmatter tag survives the merge (block-style YAML + ## Metadata)
    assert "lead text." in merged


# -- list union ------------------------------------------------------------------


def test_tag_additions_union_and_removals_stick() -> None:
    base = note(tags=["keep", "drop"])
    ours = note("2@a", tags=["keep", "drop", "ours-new"])
    theirs = note("2@b", tags=["keep", "theirs-new"])  # dropped "drop"
    merged = merge_fields(base, ours, theirs).fields
    assert merged.tags == ["keep", "ours-new", "theirs-new"]


def test_connections_and_references_union() -> None:
    base = note(connections=["A"], references=["r1"])
    ours = note("2@a", connections=["A", "B"], references=["r1", "r2"])
    theirs = note("2@b", connections=["A", "C"], references=[])  # removed r1
    merged = merge_fields(base, ours, theirs).fields
    assert merged.connections == ["A", "B", "C"]
    assert merged.references == ["r2"]


def test_action_items_union_by_text_done_is_or() -> None:
    base = note(action_items=[ActionItem("shared"), ActionItem("done by them")])
    ours = note(
        "2@a",
        action_items=[ActionItem("shared"), ActionItem("done by them"), ActionItem("mine")],
    )
    theirs = note(
        "2@b",
        action_items=[ActionItem("shared"), ActionItem("done by them", done=True)],
    )
    merged = merge_fields(base, ours, theirs).fields
    assert merged.action_items == [
        ActionItem("shared", done=False),
        ActionItem("done by them", done=True),
        ActionItem("mine", done=False),
    ]


# -- disable / restore -----------------------------------------------------------


def test_disable_vs_edit_both_orders() -> None:
    base = note("1@a")
    editor = note("2@a", summary="edited")
    disabler = note("3@b", disabled=True)
    for ours, theirs in ((editor, disabler), (disabler, editor)):
        merged = merge_fields(base, ours, theirs).fields
        assert merged.disabled is True  # disable is newer -> wins
        assert merged.summary == "edited"  # the edit is NOT lost


def test_edit_after_disable_restores_nothing_but_newer_edit_wins_flag() -> None:
    base = note("2@b", disabled=True)
    restorer = note("3@a", disabled=False)  # explicit restore
    bystander = note("2@b", disabled=True)
    merged = merge_fields(base, restorer, bystander).fields
    assert merged.disabled is False


# -- details ----------------------------------------------------------------------


def test_details_disjoint_edits_both_apply() -> None:
    base = note()
    ours = note("2@a", details="EDITED one\nline two\nline three")
    theirs = note("2@b", details="line one\nline two\nEDITED three")
    merged = merge_fields(base, ours, theirs)
    assert merged.clean is True
    assert merged.fields.details == "EDITED one\nline two\nEDITED three"


def test_details_same_point_additions_concatenate() -> None:
    base = note(details="line one")
    ours = note("2@a", details="line one\nours added")
    theirs = note("3@b", details="line one\ntheirs added")
    merged = merge_fields(base, ours, theirs)
    assert merged.clean is True
    # Ordered by rev (older first), not by side:
    assert merged.fields.details == "line one\nours added\ntheirs added"
    swapped = merge_fields(base, theirs, ours)
    assert swapped.fields.details == merged.fields.details


def test_details_true_conflict_keeps_both_under_markers() -> None:
    base = note()
    ours = note("2@a", details="OURS one\nline two\nline three")
    theirs = note("3@b", details="THEIRS one\nline two\nline three")
    merged = merge_fields(base, ours, theirs)
    assert merged.clean is False
    text = merged.fields.details
    assert "<<<<<<< 3@b" in text  # higher rev first, labeled by rev
    assert "THEIRS one" in text
    assert "=======" in text
    assert "OURS one" in text
    assert ">>>>>>> 2@a" in text
    assert CONFLICT_TAG in merged.fields.tags


def test_details_conflict_is_byte_symmetric() -> None:
    base = note()
    ours = note("2@a", details="OURS one\nline two")
    theirs = note("3@b", details="THEIRS one\nline two")
    a = merge_fields(base, ours, theirs).fields
    b = merge_fields(base, theirs, ours).fields
    assert render_fields(a) == render_fields(b)


def test_details_lossless_every_changed_line_survives() -> None:
    base = note(details="alpha\nbeta\ngamma")
    ours = note("2@a", details="alpha CHANGED\nbeta\ngamma\nours tail")
    theirs = note("3@b", details="alpha DIFFERENT\nbeta\ngamma\ntheirs tail")
    merged = merge_fields(base, ours, theirs).fields.details
    for line in ("alpha CHANGED", "alpha DIFFERENT", "ours tail", "theirs tail", "beta", "gamma"):
        assert line in merged


# -- whole-note text merge ---------------------------------------------------------


def base_ours_theirs() -> tuple[str, str, str]:
    base = render_fields(note("1@a"))
    ours = render_fields(note("2@a", summary="ours summary", tags=["base", "ours"]))
    theirs = render_fields(note("3@b", details="line one\nline two\nline three\nappended"))
    return base, ours, theirs


def test_merge_note_texts_round_trips_and_converges() -> None:
    base, ours, theirs = base_ours_theirs()
    merged_ab, clean_ab, _ = merge_note_texts(base, ours, theirs)
    merged_ba, clean_ba, _ = merge_note_texts(base, theirs, ours)
    assert clean_ab and clean_ba
    assert merged_ab == merged_ba  # convergence: both nodes get identical bytes
    fields = parse_note(merged_ab)
    assert fields.summary == "ours summary"
    assert "appended" in fields.details
    assert fields.tags == ["base", "ours"]
    assert fields.rev == "3@b"


def test_merge_is_deterministic_across_runs() -> None:
    base, ours, theirs = base_ours_theirs()
    assert merge_note_texts(base, ours, theirs) == merge_note_texts(base, ours, theirs)


def test_extra_sections_survive_the_merge() -> None:
    base, ours, theirs = base_ours_theirs()
    ours += "\n## Hand Curated\nprecious knowledge\n"
    merged, clean, _ = merge_note_texts(base, ours, theirs)
    assert clean
    assert "## Hand Curated\nprecious knowledge" in merged
    # And symmetric:
    assert merged == merge_note_texts(base, theirs, ours)[0]


def test_extra_section_edited_both_sides_merges_linewise() -> None:
    section = "\n## Log\nentry one\nentry two\n"
    base = render_fields(note("1@a")) + section
    ours = render_fields(note("2@a")) + "\n## Log\nentry one EDITED\nentry two\n"
    theirs = render_fields(note("3@b")) + "\n## Log\nentry one\nentry two\nentry three\n"
    merged, clean, _ = merge_note_texts(base, ours, theirs)
    assert clean
    assert "entry one EDITED" in merged
    assert "entry three" in merged


def test_identical_sides_merge_to_themselves() -> None:
    base, ours, _ = base_ours_theirs()
    merged, clean, messages = merge_note_texts(base, ours, ours)
    assert clean
    assert messages == []
    assert parse_note(merged) == parse_note(ours)


def test_legacy_notes_without_revs_still_merge() -> None:
    base = render_fields(note())
    ours = render_fields(note(summary="legacy a", tags=["base", "x"]))
    theirs = render_fields(note(tags=["base", "y"]))
    merged, clean, _ = merge_note_texts(base, ours, theirs)
    assert clean
    fields = parse_note(merged)
    assert fields.summary == "legacy a"
    assert fields.tags == ["base", "x", "y"]
    assert fields.rev == ""


# -- the git driver entry ---------------------------------------------------------


def write_three(tmp_path: Path) -> tuple[Path, Path, Path]:
    base, ours, theirs = base_ours_theirs()
    b, o, t = tmp_path / "base.md", tmp_path / "ours.md", tmp_path / "theirs.md"
    b.write_text(base, encoding="utf-8")
    o.write_text(ours, encoding="utf-8")
    t.write_text(theirs, encoding="utf-8")
    return b, o, t


def test_run_merge_driver_merges_into_ours(tmp_path: Path) -> None:
    b, o, t = write_three(tmp_path)
    assert run_merge_driver(b, o, t, "Note.md") == 0
    fields = parse_note(o.read_text(encoding="utf-8"))
    assert fields.summary == "ours summary"
    assert "appended" in fields.details


def test_run_merge_driver_exits_zero_on_conflict(tmp_path: Path) -> None:
    b = tmp_path / "base.md"
    o = tmp_path / "ours.md"
    t = tmp_path / "theirs.md"
    b.write_text(render_fields(note()), encoding="utf-8")
    o.write_text(render_fields(note("2@a", details="OURS")), encoding="utf-8")
    t.write_text(render_fields(note("3@b", details="THEIRS")), encoding="utf-8")
    assert run_merge_driver(b, o, t) == 0  # the daemon must keep flowing
    merged = o.read_text(encoding="utf-8")
    assert "<<<<<<<" in merged
    assert CONFLICT_TAG in parse_note(merged).tags


def test_run_merge_driver_exits_one_on_unreadable_input(tmp_path: Path) -> None:
    b, o, t = write_three(tmp_path)
    assert run_merge_driver(tmp_path / "missing.md", o, t) == 1
    bad = tmp_path / "bad.md"
    bad.write_bytes(b"\xff\xfe invalid utf-8 \xff")
    assert run_merge_driver(b, bad, t) == 1
