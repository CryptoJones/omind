# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Aaron K. Clark
"""Agent-identity attribution — 2026-08-27 roundtable consensus.

The panel (11/12 lanes) ratified: `Agent:` is a metadata line, NEVER part of
the Lamport Rev; optional/best-effort (fail-open over mandatory identity);
ADVISORY ONLY — observed, never consumed for ranking, access, or trust; slug
capped at 64 chars; merges follow the body's Rev winner but BLANK the field on
the equal-rev content tiebreak rather than letting max() fabricate authorship.
"""

from pathlib import Path

from omind.merge import merge_note_texts
from omind.store import NoteFields, OmiStore, parse_note, render_fields


def test_agent_round_trips_through_markdown() -> None:
    fields = NoteFields(title="X", summary="s", agent="hermes@makemake")
    parsed = parse_note(render_fields(fields))
    assert parsed.agent == "hermes@makemake"
    assert "- Agent: hermes@makemake" in render_fields(parsed)


def test_agent_absent_by_default_and_never_rendered() -> None:
    rendered = render_fields(NoteFields(title="X"))
    assert "Agent:" not in rendered
    assert parse_note(rendered).agent == ""


def test_agent_is_sanitized_and_capped() -> None:
    raw = "We!ird  Agent–Name " + "x" * 200
    parsed = parse_note(render_fields(NoteFields(title="X", agent=raw)))
    assert parsed.agent.startswith("We-ird-Agent-Name-")
    assert len(parsed.agent) == 64
    fields = NoteFields(title="X", agent="bad name!spaces")
    assert parse_note(render_fields(fields)).agent == "bad-name-spaces"


def test_merge_agent_follows_the_rev_winner() -> None:
    base = parse_note(render_fields(NoteFields(title="X", summary="b", agent="")))
    ours = parse_note(render_fields(NoteFields(title="X", summary="o", agent="hermes")))
    ours.rev = "5@node-a"
    theirs = parse_note(render_fields(NoteFields(title="X", summary="t", agent="")))
    theirs.rev = "3@node-b"
    merged, clean, _messages = merge_note_texts(
        render_fields(base), render_fields(ours), render_fields(theirs)
    )
    assert parse_note(merged).agent == "hermes"  # newer rev wins, normally
    assert clean


def test_merge_blanks_agent_on_the_equal_rev_tiebreak() -> None:
    """max('hermes', 'pluto') would pick 'pluto' and FABRICATE authorship — the
    roundtable's sharpest amendment. Both claims are dropped and named."""
    base = parse_note(render_fields(NoteFields(title="X", summary="b", agent="")))
    ours = parse_note(render_fields(NoteFields(title="X", summary="o", agent="hermes")))
    ours.rev = "5@node-a"
    theirs = parse_note(render_fields(NoteFields(title="X", summary="t", agent="pluto")))
    # Same Rev IDENTITY, different content — the equal-rev content tiebreak
    # (an unstamped mutation reusing a rev), NOT a normal LWW between nodes.
    theirs.rev = "5@node-a"
    merged, clean, messages = merge_note_texts(
        render_fields(base), render_fields(ours), render_fields(theirs)
    )
    assert parse_note(merged).agent == ""
    assert any("agent" in m and "hermes" in m and "pluto" in m for m in messages)
    assert clean  # a blanked scalar is not a conflict-marker merge


def test_store_stamps_no_identity_without_a_caller(tmp_path: Path) -> None:
    omi = tmp_path / "OMI"
    store = OmiStore(omi)
    filename = store.create_note(NoteFields(title="No Id", summary="s"))
    assert OmiStore(omi).read_fields(filename).agent == ""
