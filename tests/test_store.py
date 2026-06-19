# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Aaron K. Clark
"""Tests for omind.store: parse/render round-trips, CRUD, index, traversal."""

from __future__ import annotations

import multiprocessing
from pathlib import Path

import pytest

from omind import paths, seeds
from omind.store import (
    LOCK_FILENAME,
    RECENT_LIMIT,
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


def test_extras_round_trip() -> None:
    fields = NoteFields(
        title="X",
        details="Intro.",
        extras={"Custom Section": ["body line one", "body line two"]},
    )
    parsed = parse_note(render_fields(fields))
    assert parsed.extras == {"Custom Section": ["body line one", "body line two"]}
    assert parsed.details == "Intro."


def test_h2_heading_inside_details_captured_as_extra() -> None:
    # The footgun: an author puts a ``## H2`` inside the details body. The H2
    # opens a new section, so it is captured as an extra instead of silently
    # dropped on the next render (the 2026-06-14 data-loss bug).
    md = (
        "# X\n\n## Summary\ns\n\n## Details\nintro line\n\n"
        "## A Subheading\nunder the subheading\n"
    )
    fields = parse_note(md)
    assert fields.details == "intro line"
    assert fields.extras == {"A Subheading": ["under the subheading"]}


def test_update_note_preserves_extras_on_partial_edit(store: OmiStore) -> None:
    # Regression for the 2026-06-14 bug: a partial edit whose fields carry no
    # extras must not drop the note's existing non-template sections.
    name = store.create_note(
        NoteFields(
            title="Has Extras",
            summary="before",
            details="intro",
            extras={"Extra": ["keep me"]},
        )
    )
    store.update_note(name, NoteFields(title="Has Extras", summary="after"))
    reread = store.read_fields(name)
    assert reread.summary == "after"
    assert reread.extras == {"Extra": ["keep me"]}


def test_h2_in_details_survives_repeated_edits_without_duplicating(store: OmiStore) -> None:
    # The corruption that bit the omind roadmap note: the MCP/CLI API can only
    # express a multi-section body through `details`, but a `## H2` inside details
    # reads back as an extra — so without normalization, every re-edit rendered the
    # body's H2s AND the inherited extras, doubling each section on every save.
    body = "intro\n\n## Origin\nA\n\n## Phase\nB"
    name = store.create_note(NoteFields(title="Roadmap", summary="s", details=body))
    for _ in range(3):
        store.update_note(name, NoteFields(title="Roadmap", summary="s", details=body))
    text = store.read_note(name)
    assert text.count("## Origin") == 1
    assert text.count("## Phase") == 1
    reread = store.read_fields(name)
    assert reread.details == "intro"
    assert reread.extras == {"Origin": ["A"], "Phase": ["B"]}


def test_h2_edit_replaces_section_and_keeps_other_extras(store: OmiStore) -> None:
    # A re-supplied body updates its own section in place; a genuine extra the
    # body never mentions is preserved (not dropped, not duplicated).
    name = store.create_note(
        NoteFields(title="Doc", summary="s", details="## Section\nv1", extras={"Aside": ["keep"]})
    )
    store.update_note(name, NoteFields(title="Doc", summary="s", details="## Section\nv2"))
    reread = store.read_fields(name)
    assert reread.extras == {"Aside": ["keep"], "Section": ["v2"]}
    assert store.read_note(name).count("## Section") == 1


def test_from_dict_preserves_extras() -> None:
    restored = NoteFields.from_dict({"title": "X", "extras": {"H": ["a", "b"]}})
    assert restored.extras == {"H": ["a", "b"]}


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
    index = (store.omi_dir / paths.INDEX_FILENAME).read_text()
    assert "[[My First Card]]" in index
    assert seeds.INDEX_RECENT_HEADING in index


def test_list_excludes_reserved_files(store: OmiStore) -> None:
    (store.omi_dir / paths.MEMORY_TEMPLATE_FILENAME).write_text(seeds.MEMORY_TEMPLATE)
    (store.omi_dir / paths.INDEX_FILENAME).write_text("# index")
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
    assert "[[Doomed]]" not in (store.omi_dir / paths.INDEX_FILENAME).read_text()


def test_delete_missing_raises(store: OmiStore) -> None:
    with pytest.raises(NoteNotFoundError):
        store.delete_note("nope.md")


def test_delete_reserved_rejected(store: OmiStore) -> None:
    (store.omi_dir / paths.INDEX_FILENAME).write_text("# index")
    with pytest.raises(NoteError):
        store.delete_note(paths.INDEX_FILENAME)


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
    (store.omi_dir / paths.INDEX_FILENAME).write_text(intro)
    store.create_note(NoteFields(title="New One"))
    text = (store.omi_dir / paths.INDEX_FILENAME).read_text()
    assert "# Custom Intro" in text
    assert "Keep me." in text
    assert "[[New One]]" in text
    assert "[[old]]" not in text  # recent list regenerated


# -- index descriptions / cap / migration -------------------------------------


def _index_text(store: OmiStore) -> str:
    return (store.omi_dir / paths.INDEX_FILENAME).read_text(encoding="utf-8")


def _recent_links(store: OmiStore) -> list[str]:
    return [ln for ln in _index_text(store).splitlines() if ln.startswith("- [[")]


def test_index_renders_summary_as_description(store: OmiStore) -> None:
    store.create_note(NoteFields(title="Described", summary="A crisp one-liner."))
    assert "- [[Described]] — A crisp one-liner." in _recent_links(store)


def test_index_note_without_summary_renders_bare_link(store: OmiStore) -> None:
    store.write_note("Bare.md", "# Bare\n\n## Metadata\n- Created: 2026-06-01\n\n## Summary\n\n")
    assert _recent_links(store) == ["- [[Bare]]"]


def test_index_description_collapsed_to_one_line_and_100_chars(store: OmiStore) -> None:
    long_summary = "word " * 60  # multi-word, ~300 chars
    store.create_note(NoteFields(title="Wordy", summary="first\nsecond   line\n" + long_summary))
    [line] = _recent_links(store)
    prefix = "- [[Wordy]] — "
    description = line[len(prefix) :]
    assert "\n" not in description
    assert "first second line word" in description  # whitespace collapsed
    assert len(description) <= 100
    assert description.endswith("...")


def test_index_regeneration_is_idempotent(store: OmiStore) -> None:
    store.create_note(NoteFields(title="Stable", summary="keep this description"))
    store.update_index()
    first = _index_text(store)
    store.update_index()
    assert _index_text(store) == first
    assert "- [[Stable]] — keep this description" in first  # survives regen


def test_index_caps_at_recent_limit_with_total_line(store: OmiStore) -> None:
    n = RECENT_LIMIT + 5
    for i in range(n):
        store.create_note(
            NoteFields(title=f"Note {i:02d}", summary=f"s{i}", created=f"2026-01-{i + 1:02d}")
        )
    links = _recent_links(store)
    assert len(links) == RECENT_LIMIT
    # Newest-first: the most recent note leads, the oldest five fall off.
    assert links[0].startswith(f"- [[Note {n - 1:02d}]]")
    assert links[-1].startswith("- [[Note 05]]")
    assert _index_text(store).rstrip().endswith(f"*({n} notes total)*")


def test_index_under_limit_has_no_total_line(store: OmiStore) -> None:
    store.create_note(NoteFields(title="Lonely", summary="s"))
    assert "notes total" not in _index_text(store)


def test_index_excludes_journal_notes(store: OmiStore) -> None:
    (store.omi_dir / "Session Journal 2026-06-10.md").write_text(
        "# Session Journal 2026-06-10\n\n## Summary\nAuto-recorded journal.\n\n## Actions\n- x\n",
        encoding="utf-8",
    )
    store.create_note(NoteFields(title="Curated", summary="s"))
    index = _index_text(store)
    assert "[[Curated]]" in index
    assert "Session Journal" not in index


def test_migration_copies_hand_description_into_empty_summary(store: OmiStore) -> None:
    (store.omi_dir / "Old.md").write_text(
        "# Old\n\n## Metadata\n- Created: 2026-05-01\n\n## Summary\n\n## Details\nbody\n",
        encoding="utf-8",
    )
    old_index = (
        f"{seeds.INDEX_INTRO}\n{seeds.INDEX_RECENT_HEADING}\n- [[Old]] — hand-written gloss\n"
    )
    (store.omi_dir / paths.INDEX_FILENAME).write_text(old_index, encoding="utf-8")
    store.update_index()
    assert store.read_fields("Old.md").summary == "hand-written gloss"
    assert "- [[Old]] — hand-written gloss" in _recent_links(store)
    assert "## Details\nbody" in store.read_note("Old.md")  # surgical edit, body intact
    # Idempotent: a second regeneration changes neither note nor index.
    note, index = store.read_note("Old.md"), _index_text(store)
    store.update_index()
    assert store.read_note("Old.md") == note
    assert _index_text(store) == index


def test_migration_adds_summary_section_when_missing(store: OmiStore) -> None:
    (store.omi_dir / "Loose.md").write_text("# Loose\n\nFree-form note.\n", encoding="utf-8")
    (store.omi_dir / paths.INDEX_FILENAME).write_text(
        seeds.INDEX_RECENT_HEADING + "\n- [[Loose]] — kept gloss\n", encoding="utf-8"
    )
    store.update_index()
    assert store.read_fields("Loose.md").summary == "kept gloss"
    assert "Free-form note." in store.read_note("Loose.md")
    assert "- [[Loose]] — kept gloss" in _recent_links(store)


def test_migration_leaves_existing_summary_alone(store: OmiStore) -> None:
    store.create_note(NoteFields(title="Authored", summary="real summary"))
    (store.omi_dir / paths.INDEX_FILENAME).write_text(
        seeds.INDEX_RECENT_HEADING + "\n- [[Authored]] — stale hand gloss\n", encoding="utf-8"
    )
    store.update_index()
    assert store.read_fields("Authored.md").summary == "real summary"
    assert "- [[Authored]] — real summary" in _recent_links(store)
    assert "stale hand gloss" not in _index_text(store)


def test_migration_skipped_for_generated_index(store: OmiStore) -> None:
    """A generated description must not be migrated back into a blanked note."""
    name = store.create_note(NoteFields(title="Rewritten", summary="old generated desc"))
    # User deliberately rewrites the note raw, with no Summary section at all.
    store.write_note(name, "# Rewritten\n\nraw body\n")
    assert store.read_note(name) == "# Rewritten\n\nraw body\n"
    assert _recent_links(store) == ["- [[Rewritten]]"]


def test_migration_ignores_dangling_and_bare_entries(store: OmiStore) -> None:
    (store.omi_dir / paths.INDEX_FILENAME).write_text(
        seeds.INDEX_RECENT_HEADING + "\n- [[Ghost]] — desc for missing note\n- [[Also Ghost]]\n",
        encoding="utf-8",
    )
    store.update_index()  # must not raise or resurrect entries
    assert _recent_links(store) == []


# -- concurrency -------------------------------------------------------------


def test_lock_and_temp_files_excluded_from_listing(store: OmiStore) -> None:
    store.create_note(NoteFields(title="Visible"))
    assert (store.omi_dir / LOCK_FILENAME).is_file()  # lock created on first write
    names = {s.filename for s in store.list_notes()}
    assert names == {"Visible.md"}  # lock + any temp files never listed
    assert LOCK_FILENAME not in names


def test_atomic_write_leaves_no_temp_files(store: OmiStore) -> None:
    store.create_note(NoteFields(title="Clean"))
    leftovers = [p.name for p in store.omi_dir.glob(".tmp-*")]
    assert leftovers == []


def _worker_create(args: tuple[str, int]) -> None:
    omi_dir, i = args
    OmiStore(omi_dir).create_note(NoteFields(title=f"Note {i:03d}", summary=f"n{i}"))


def test_concurrent_processes_keep_index_consistent(tmp_path: Path) -> None:
    """N separate processes each create a note; index.md must list all N intact."""
    omi = tmp_path / "OMI"
    omi.mkdir()
    n = 24
    # fork is unavailable on Windows (and 3.14 makes spawn the default
    # everywhere); the worker is a module-level function, so spawn works too.
    method = "fork" if "fork" in multiprocessing.get_all_start_methods() else "spawn"
    ctx = multiprocessing.get_context(method)
    with ctx.Pool(processes=8) as pool:
        pool.map(_worker_create, [(str(omi), i) for i in range(n)])

    store = OmiStore(omi)
    # Every note file landed, none torn (each parses to its own title).
    titles = {s.title for s in store.list_notes()}
    assert titles == {f"Note {i:03d}" for i in range(n)}

    # index.md is well-formed: the Recent list has exactly N wikilinks, no dupes,
    # no torn lines — proof the read-modify-write serialized under the lock.
    index = (omi / paths.INDEX_FILENAME).read_text(encoding="utf-8")
    assert seeds.INDEX_RECENT_HEADING in index
    links = [ln for ln in index.splitlines() if ln.startswith("- [[")]
    assert len(links) == n
    assert len(set(links)) == n
    for i in range(n):
        assert f"- [[Note {i:03d}]] — n{i}" in links


# -- mesh: Lamport revs + soft delete (docs/mesh.md) -------------------------


@pytest.fixture
def mesh_store(tmp_path: Path) -> OmiStore:
    omi = tmp_path / "OMI"
    omi.mkdir()
    return OmiStore(omi, node_id="testnode-abc123")


def test_rev_and_disabled_round_trip() -> None:
    fields = NoteFields(title="Meshy", summary="s", rev="12@laptop-3f9a2c", disabled=True)
    md = render_fields(fields)
    assert "- Rev: 12@laptop-3f9a2c" in md
    assert "- Disabled: true" in md
    parsed = parse_note(md)
    assert parsed.rev == "12@laptop-3f9a2c"
    assert parsed.disabled is True


def test_legacy_note_round_trips_without_mesh_lines() -> None:
    md = render_fields(NoteFields(title="Legacy", summary="s", created="2026-06-01"))
    assert "- Rev:" not in md
    assert "- Disabled:" not in md
    parsed = parse_note(md)
    assert parsed.rev == ""
    assert parsed.disabled is False
    # Byte-identical round-trip: render(parse(md)) == md.
    assert render_fields(parsed) == md


def test_mesh_store_stamps_and_ticks_monotonically(mesh_store: OmiStore) -> None:
    name = mesh_store.create_note(NoteFields(title="Stamped", summary="v1"))
    first = mesh_store.read_fields(name)
    assert first.rev == "1@testnode-abc123"
    mesh_store.update_note(name, first)
    assert mesh_store.read_fields(name).rev == "2@testnode-abc123"


def test_mesh_store_ticks_past_incoming_rev(mesh_store: OmiStore) -> None:
    """Lamport receive rule: a write carrying a higher foreign rev bumps past it."""
    name = mesh_store.create_note(NoteFields(title="Foreign", summary="s"))
    fields = mesh_store.read_fields(name)
    fields.rev = "7@othernode-9"
    mesh_store.update_note(name, fields)
    assert mesh_store.read_fields(name).rev == "8@testnode-abc123"


def test_plain_store_never_stamps(store: OmiStore) -> None:
    name = store.create_note(NoteFields(title="Plain", summary="s"))
    assert store.read_fields(name).rev == ""
    assert "- Rev:" not in store.read_note(name)


def test_disable_hides_and_restore_unhides(mesh_store: OmiStore) -> None:
    name = mesh_store.create_note(NoteFields(title="Gone", summary="s", tags=["secret"]))
    mesh_store.create_note(NoteFields(title="Keeper", summary="links [[Gone]]"))
    mesh_store.disable_note(name)

    assert name not in [s.filename for s in mesh_store.list_notes()]
    listed = [s.filename for s in mesh_store.list_notes(include_disabled=True)]
    assert name in listed
    assert "secret" not in mesh_store.all_tags()
    assert (mesh_store.omi_dir / name).is_file()  # never unlinked
    index = (mesh_store.omi_dir / paths.INDEX_FILENAME).read_text(encoding="utf-8")
    entries = [ln for ln in index.splitlines() if ln.startswith("- [[")]
    assert not any(ln.startswith("- [[Gone]]") for ln in entries)

    mesh_store.restore_note(name)
    restored = mesh_store.read_fields(name)
    assert restored.disabled is False
    assert name in [s.filename for s in mesh_store.list_notes()]
    assert "secret" in mesh_store.all_tags()


def test_disabled_note_excluded_from_backlinks(mesh_store: OmiStore) -> None:
    target = mesh_store.create_note(NoteFields(title="Hub", summary="s"))
    linker = mesh_store.create_note(NoteFields(title="Spoke", summary="see [[Hub]]"))
    assert [s.filename for s in mesh_store.backlinks(target)] == [linker]
    mesh_store.disable_note(linker)
    assert mesh_store.backlinks(target) == []


def test_delete_note_disables_in_mesh_mode(mesh_store: OmiStore) -> None:
    name = mesh_store.create_note(NoteFields(title="Soft", summary="s"))
    mesh_store.delete_note(name)
    assert (mesh_store.omi_dir / name).is_file()
    assert mesh_store.read_fields(name).disabled is True


def test_delete_note_unlinks_without_mesh(store: OmiStore) -> None:
    name = store.create_note(NoteFields(title="Hard", summary="s"))
    store.delete_note(name)
    assert not (store.omi_dir / name).exists()


def test_git_dir_implies_mesh_mode(store: OmiStore) -> None:
    (store.omi_dir / ".git").mkdir()
    name = store.create_note(NoteFields(title="Replicated", summary="s"))
    store.delete_note(name)
    assert (store.omi_dir / name).is_file()
    assert parse_note((store.omi_dir / name).read_text(encoding="utf-8")).disabled is True


def test_purge_note_always_unlinks(mesh_store: OmiStore) -> None:
    name = mesh_store.create_note(NoteFields(title="Purged", summary="s"))
    mesh_store.purge_note(name)
    assert not (mesh_store.omi_dir / name).exists()
    with pytest.raises(NoteNotFoundError):
        mesh_store.purge_note(name)


def test_disable_refuses_reserved_files(mesh_store: OmiStore) -> None:
    (mesh_store.omi_dir / paths.INDEX_FILENAME).write_text("# index")
    with pytest.raises(NoteError):
        mesh_store.disable_note(paths.INDEX_FILENAME)


def test_disable_preserves_unknown_sections(mesh_store: OmiStore) -> None:
    """Soft delete is a surgical edit — hand-curated sections survive."""
    md = (
        "# Custom\n\n## Metadata\n- Created: 2026-06-01\n- Tags: #x\n"
        "- Related to:\n\n## Summary\ns\n\n## Hand Curated\nprecious\n"
    )
    mesh_store.write_note("Custom.md", md)
    mesh_store.disable_note("Custom.md")
    text = mesh_store.read_note("Custom.md")
    assert "## Hand Curated\nprecious" in text
    assert parse_note(text).disabled is True
    mesh_store.restore_note("Custom.md")
    text = mesh_store.read_note("Custom.md")
    assert "- Disabled:" not in text
    assert "## Hand Curated\nprecious" in text


def test_search_matches_fields_and_filters_disabled(mesh_store: OmiStore) -> None:
    a = mesh_store.create_note(NoteFields(title="Alpha", summary="quantum cats", tags=["pets"]))
    mesh_store.create_note(NoteFields(title="Beta", details="quantum dogs", tags=["pets"]))
    mesh_store.create_note(NoteFields(title="Gamma", summary="classical fish"))
    hits = {s.filename for s in mesh_store.search("quantum")}
    assert hits == {"Alpha.md", "Beta.md"}
    assert {s.filename for s in mesh_store.search("", tag="pets")} == {"Alpha.md", "Beta.md"}
    assert {s.filename for s in mesh_store.search("QUANTUM CATS")} == {"Alpha.md"}
    mesh_store.disable_note(a)
    assert {s.filename for s in mesh_store.search("quantum")} == {"Beta.md"}
    assert {s.filename for s in mesh_store.search("quantum", include_disabled=True)} == {
        "Alpha.md",
        "Beta.md",
    }


def test_rev_naive_update_preserves_mesh_metadata(mesh_store: OmiStore) -> None:
    """A legacy writer (fresh NoteFields, no rev) must not strip Rev/Disabled."""
    name = mesh_store.create_note(NoteFields(title="Sticky", summary="v1"))
    mesh_store.disable_note(name)  # now at rev 2, disabled
    mesh_store.update_note(name, NoteFields(title="Sticky", summary="v2"))
    after = mesh_store.read_fields(name)
    assert after.summary == "v2"
    assert after.disabled is True  # naive update does not resurrect
    assert after.rev == "3@testnode-abc123"  # ticked past, not reset


def test_mesh_aware_update_controls_disabled(mesh_store: OmiStore) -> None:
    """A caller passing its own rev is mesh-aware: its disabled value is trusted."""
    name = mesh_store.create_note(NoteFields(title="Aware", summary="s"))
    mesh_store.disable_note(name)
    fields = mesh_store.read_fields(name)
    fields.disabled = False
    mesh_store.update_note(name, fields)
    assert mesh_store.read_fields(name).disabled is False


def test_update_note_preserves_created_date(store: OmiStore) -> None:
    """A fresh-NoteFields update must not reset Created: to today."""
    name = store.create_note(NoteFields(title="Old Note", created="2026-01-10"))
    store.update_note(name, NoteFields(title="Old Note", summary="edited"))
    assert store.read_fields(name).created == "2026-01-10"


def test_upsert_keeps_fields_the_caller_left_unset(store: OmiStore) -> None:
    """`omind note` can't express Action Items etc.; empty means keep, not clear."""
    from omind.notes import upsert_note

    store.create_note(
        NoteFields(
            title="Sticky Upsert",
            summary="original summary",
            created="2026-01-10",
            tags=["keep"],
            action_items=[ActionItem("still todo")],
            references=["Source: somewhere"],
        )
    )
    action, filename = upsert_note(store.omi_dir, NoteFields(title="Sticky Upsert", details="new"))
    assert action == "updated"
    after = store.read_fields(filename)
    assert after.details == "new"
    assert after.summary == "original summary"
    assert after.created == "2026-01-10"
    assert after.tags == ["keep"]
    assert after.action_items == [ActionItem("still todo")]
    assert after.references == ["Source: somewhere"]


@pytest.mark.parametrize("title", ["index", "Memory Template"])
def test_writes_refuse_reserved_filenames(store: OmiStore, title: str) -> None:
    """A note titled 'index' must not clobber the generated index.md."""
    with pytest.raises(NoteError):
        store.create_note(NoteFields(title=title))
    with pytest.raises(NoteError):
        store.write_note(f"{title}.md", "# pwned\n")
    with pytest.raises(NoteError):
        store.update_note(f"{title}.md", NoteFields(title=title))


def test_stale_write_after_purge_conflicts(store: OmiStore) -> None:
    """A version token taken before a purge must not resurrect the note."""
    name = store.create_note(NoteFields(title="Doomed", summary="s"))
    token = store.note_version(name)
    store.purge_note(name)
    with pytest.raises(NoteConflictError):
        store.write_note(name, "# Doomed\n", expected_version=token)
    assert not (store.omi_dir / name).exists()


def test_backlinks_match_aliased_and_heading_links(store: OmiStore) -> None:
    """Obsidian counts [[Note|alias]] and [[Note#heading]] as backlinks; so do we."""
    store.create_note(NoteFields(title="Note A", summary="target"))
    store.create_note(NoteFields(title="Aliased", details="See [[Note A|the project]]."))
    store.create_note(NoteFields(title="Headed", details="See [[Note A#Details]]."))
    store.create_note(NoteFields(title="Unrelated", details="See [[Note B]]."))
    names = {s.filename for s in store.backlinks("Note A")}
    assert names == {"Aliased.md", "Headed.md"}


def test_listing_cache_tracks_writes_and_deletes(store: OmiStore) -> None:
    """The stat-keyed summary cache must never serve a deleted or stale note."""
    a = store.create_note(NoteFields(title="Cached A", summary="v1"))
    store.create_note(NoteFields(title="Cached B", summary="b"))
    assert {s.title for s in store.list_notes()} == {"Cached A", "Cached B"}
    store.update_note(a, NoteFields(title="Cached A", summary="v2 longer text"))
    assert [s.summary for s in store.list_notes() if s.title == "Cached A"] == [
        "v2 longer text"
    ]
    store.purge_note(a)
    assert {s.title for s in store.list_notes()} == {"Cached B"}
