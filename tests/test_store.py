# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Aaron K. Clark
"""Tests for omind.store: parse/render round-trips, CRUD, index, traversal."""

from __future__ import annotations

from pathlib import Path

import pytest

from omind import seeds
from omind.store import (
    ActionItem,
    NoteConflictError,
    NoteError,
    NoteFields,
    NoteNotFoundError,
    OmiStore,
    parse_note,
    render_fields,
)


@pytest.fixture
def store(tmp_path: Path) -> OmiStore:
    omi = tmp_path / "OMI"
    omi.mkdir()
    return OmiStore(omi)


def test_render_parse_round_trip() -> None:
    fields = NoteFields(
        title="A Test Memory",
        summary="A one line summary.",
        details="Line one.\n\nLine two with more detail.",
        created="2026-06-03",
        tags=["omi", "memory", "thesis"],
        related_to="Some project",
        connections=["Concept One", "Concept Two"],
        action_items=[
            ActionItem("do the thing", done=False),
            ActionItem("already done", done=True),
        ],
        references=["Source: somewhere", "https://example.com"],
    )
    parsed = parse_note(render_fields(fields))
    assert parsed == fields


@pytest.mark.parametrize("tag", ["память", "记忆", "ذاكرة", "café", "tag_1/sub"])
def test_non_latin_tags_round_trip(tag: str) -> None:
    parsed = parse_note(render_fields(NoteFields(title="X", tags=[tag])))
    assert parsed.tags == [tag]


def test_render_fills_created_when_blank() -> None:
    md = render_fields(NoteFields(title="X"))
    assert "- Created:" in md
    assert parse_note(md).created  # today() filled in


def test_create_note_writes_file_and_index(store: OmiStore) -> None:
    name = store.create_note(NoteFields(title="My First Card", summary="hi", tags=["omi"]))
    assert name == "My First Card.md"
    assert (store.omi_dir / name).is_file()
    index = (store.omi_dir / seeds.INDEX_FILENAME).read_text()
    assert "[[My First Card]]" in index
    assert seeds.INDEX_RECENT_HEADING in index


def test_list_excludes_reserved_files(store: OmiStore) -> None:
    (store.omi_dir / seeds.MEMORY_TEMPLATE_FILENAME).write_text(seeds.MEMORY_TEMPLATE)
    (store.omi_dir / seeds.INDEX_FILENAME).write_text("# index")
    store.create_note(NoteFields(title="Real Note", summary="s"))
    names = [n.filename for n in store.list_notes()]
    assert names == ["Real Note.md"]


def test_list_summary_and_tags(store: OmiStore) -> None:
    store.create_note(NoteFields(title="Tagged", summary="snippet here", tags=["a", "b"]))
    [note] = store.list_notes()
    assert note.title == "Tagged"
    assert note.summary == "snippet here"
    assert note.tags == ["a", "b"]
    assert store.all_tags() == ["a", "b"]


def test_create_duplicate_rejected(store: OmiStore) -> None:
    store.create_note(NoteFields(title="Dup"))
    with pytest.raises(NoteError):
        store.create_note(NoteFields(title="Dup"))


def test_create_requires_title(store: OmiStore) -> None:
    with pytest.raises(NoteError):
        store.create_note(NoteFields(title="   "))


def test_update_note_overwrites(store: OmiStore) -> None:
    name = store.create_note(NoteFields(title="Edit Me", summary="before"))
    store.update_note(name, NoteFields(title="Edit Me", summary="after"))
    assert "after" in store.read_note(name)
    assert "before" not in store.read_note(name)


def test_note_version_changes_on_write(store: OmiStore) -> None:
    name = store.create_note(NoteFields(title="Versioned"))
    v1 = store.note_version(name)
    assert v1
    assert store.note_version("missing.md") == ""
    store.write_note(name, store.read_note(name) + "\nextra\n")
    assert store.note_version(name) != v1


def test_write_with_stale_version_raises_conflict(store: OmiStore) -> None:
    name = store.create_note(NoteFields(title="Contested", summary="orig"))
    stale = store.note_version(name)
    # Someone else writes the file (Claude Code MCP, Hermes cron, another tab).
    store.write_note(name, store.read_note(name) + "\nexternal edit\n")
    mine = NoteFields(title="Contested", summary="mine")
    with pytest.raises(NoteConflictError):
        store.update_note(name, mine, expected_version=stale)
    # A matching version (or no version) writes cleanly.
    store.update_note(name, mine, expected_version=store.note_version(name))
    assert "mine" in store.read_note(name)


def test_write_without_expected_version_skips_check(store: OmiStore) -> None:
    name = store.create_note(NoteFields(title="Forced"))
    store.write_note(name, store.read_note(name) + "\nchanged\n")
    # No expected_version → last-write-wins, no conflict raised.
    store.write_note(name, "# Forced\noverwritten\n")
    assert "overwritten" in store.read_note(name)


def test_backlinks_finds_referrers(store: OmiStore) -> None:
    store.create_note(NoteFields(title="Target", summary="t"))
    store.create_note(NoteFields(title="Referrer", summary="r", connections=["Target"]))
    store.create_note(NoteFields(title="Unrelated", summary="u"))
    assert [link.filename for link in store.backlinks("Target.md")] == ["Referrer.md"]


def test_backlinks_match_link_anywhere_in_body(store: OmiStore) -> None:
    store.create_note(NoteFields(title="Alpha", summary="a"))
    store.write_note("Beta.md", "# Beta\n\n## Details\nSee [[Alpha]] for more.\n")
    assert [link.filename for link in store.backlinks("Alpha.md")] == ["Beta.md"]


def test_backlinks_missing_raises(store: OmiStore) -> None:
    with pytest.raises(NoteNotFoundError):
        store.backlinks("ghost.md")


def test_delete_removes_file_and_index_entry(store: OmiStore) -> None:
    name = store.create_note(NoteFields(title="Doomed"))
    store.delete_note(name)
    assert not (store.omi_dir / name).exists()
    assert "[[Doomed]]" not in (store.omi_dir / seeds.INDEX_FILENAME).read_text()


def test_delete_missing_raises(store: OmiStore) -> None:
    with pytest.raises(NoteNotFoundError):
        store.delete_note("nope.md")


def test_delete_reserved_rejected(store: OmiStore) -> None:
    (store.omi_dir / seeds.INDEX_FILENAME).write_text("# index")
    with pytest.raises(NoteError):
        store.delete_note(seeds.INDEX_FILENAME)


def test_read_missing_raises(store: OmiStore) -> None:
    with pytest.raises(NoteNotFoundError):
        store.read_note("ghost.md")


def test_filename_for_title_sanitizes(store: OmiStore) -> None:
    assert store.filename_for_title("a/b:c*d") == "a b c d.md"
    assert store.filename_for_title("  spaced   out  ") == "spaced out.md"


def test_filename_for_title_empty_rejected(store: OmiStore) -> None:
    with pytest.raises(NoteError):
        store.filename_for_title("///")


@pytest.mark.parametrize(
    "bad",
    ["../escape.md", "../../etc/passwd", "sub/dir.md", "..", ".", "", "a\\b.md"],
)
def test_safe_name_rejects_traversal(store: OmiStore, bad: str) -> None:
    with pytest.raises(NoteError):
        store.safe_name(bad)


def test_safe_name_appends_md(store: OmiStore) -> None:
    path = store.safe_name("plain")
    assert path.name == "plain.md"
    assert path.parent == store.omi_dir.resolve()


def test_update_index_preserves_intro(store: OmiStore) -> None:
    intro = "# Custom Intro\n\nKeep me.\n\n" + seeds.INDEX_RECENT_HEADING + "\n- [[old]]\n"
    (store.omi_dir / seeds.INDEX_FILENAME).write_text(intro)
    store.create_note(NoteFields(title="New One"))
    text = (store.omi_dir / seeds.INDEX_FILENAME).read_text()
    assert "# Custom Intro" in text
    assert "Keep me." in text
    assert "[[New One]]" in text
    assert "[[old]]" not in text  # recent list regenerated
