# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Aaron K. Clark
"""Tests for #389 (epic #384): create-note / edit-note answer with what other
notes already say about the names in the write (``related_by_entity``)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from mcp.server.mcpserver import MCPServer

from omind import ai_usage, bench, entities, guard, namehints, searchindex, writecontext
from omind.server import build_server

HISTORY = "WD Blue As30p drive check 2026-09-27 — clean, the label moved off the Seagate"
SEAGATE = "Seagate As30p old spinner is NOT proven failing"
COPY = "As30p copy to telesto running 2026-09-26"
MUSIC = "DJ name and SoundCloud releases"
HISTORY_SUMMARY = (
    "The As30p volume label now belongs to the WD Blue; it was the Seagate, then a "
    "Samsung SSD. A disk called As30p is not proof of which drive it is."
)


def _note(
    omi: Path, title: str, details: str, *, created: str = "2026-09-25", summary: str = ""
) -> None:
    (omi / f"{title}.md").write_text(
        f"# {title}\n\n## Metadata\n- Created: {created}\n- Tags:\n\n"
        f"## Summary\n{summary or title}\n\n## Details\n{details}\n",
        encoding="utf-8",
    )


@pytest.fixture(autouse=True)
def _flag_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    for env in (
        writecontext.ENABLE_ENV,
        entities.ENABLE_ENV,
        entities.MAX_DF_ENV,
        ai_usage.SPLIT_BUDGET_ENV,
        searchindex.DISABLE_ENV,
    ):
        monkeypatch.delenv(env, raising=False)


@pytest.fixture
def omi(tmp_path: Path) -> Path:
    """A vault holding the As30p label history among unrelated notes, indexed."""
    d = tmp_path / "OMI"
    d.mkdir()
    _note(d, HISTORY, "As30p is now the WD Blue.", created="2026-09-27",
          summary=HISTORY_SUMMARY)
    _note(d, SEAGATE, "The As30p Seagate dropouts may have been the adapter.")
    _note(d, COPY, "Copying As30p to telesto.", created="2026-09-26")
    _note(d, MUSIC, "Releases as As30p on SoundCloud.", created="2026-07-05")
    _note(d, "Pluto GPUs", "pluto has a V100 and a P100 at 172.16.24.46.")
    for i, topic in enumerate(
        ("Kitchen recipes", "Garden watering", "Bicycle tyres", "Tax paperwork",
         "Piano practice", "Bird feeder", "Holiday packing", "Library books",
         "Coffee grinder", "Window cleaning", "Running shoes")
    ):
        _note(d, topic, f"Plain notes about {topic.lower()}, item {i}.")
    assert searchindex.SearchIndex(d).refresh(vectors=False) is not None
    return d


@pytest.fixture
def server(omi: Path) -> MCPServer:
    return build_server(omi, node_id="testnode-abc123")


def call(server: MCPServer, name: str, args: dict[str, Any]) -> Any:
    return asyncio.run(server.call_tool(name, args)).structured_content


def _titles(result: dict[str, Any]) -> list[str]:
    return [entry["title"] for entry in result.get(writecontext.FIELD, [])]


# -- acceptance criteria -----------------------------------------------------


def test_create_note_naming_as30p_returns_the_as30p_notes(server: MCPServer) -> None:
    got = call(
        server,
        "create-note",
        {"title": "makemake drive adapters", "summary": "The As30p disk is mislabeled."},
    )
    assert got["filename"] == "makemake drive adapters.md"  # the write succeeded
    related = got[writecontext.FIELD]
    # Notes about the label (it is in their title), newest first, at most three.
    assert _titles(got) == [HISTORY, COPY, SEAGATE]
    assert all(entry["name"] == "As30p" for entry in related)
    assert related[0]["updated"] == "2026-09-27"
    assert set(related[0]) == {"name", "title", "summary", "updated"}
    assert "Advisory only" in got[f"{writecontext.FIELD}_note"]


def test_edit_note_returns_the_same_field(server: MCPServer) -> None:
    call(server, "create-note", {"title": "Drive notes", "summary": "nothing yet"})
    got = call(
        server,
        "edit-note",
        {"name": "Drive notes.md", "summary": "As30p is the Seagate, the note is wrong."},
    )
    assert got["filename"] == "Drive notes.md"
    assert got["concurrency"] == "unverified"
    assert _titles(got) == [HISTORY, COPY, SEAGATE]


def test_a_contradicting_write_surfaces_the_note_it_contradicts(server: MCPServer) -> None:
    """The 2026-09-30 shape: the agent claims As30p is mislabeled; the response
    shows the note whose summary says the label moved between drives."""
    got = call(
        server,
        "create-note",
        {
            "title": "As30p label is wrong",
            "summary": "As30p is the Seagate; the WD Blue note is mislabeled.",
        },
    )
    by_title = {entry["title"]: entry for entry in got[writecontext.FIELD]}
    assert by_title[HISTORY]["summary"] == HISTORY_SUMMARY


def test_the_note_being_written_is_never_listed(server: MCPServer, omi: Path) -> None:
    got = call(server, "edit-note", {"name": f"{HISTORY}.md", "details": "As30p again."})
    assert HISTORY not in _titles(got)
    assert _titles(got) == [COPY, SEAGATE]


def test_response_size_is_capped(omi: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for i in range(12):
        _note(omi, f"ZQ81X drive note {i:02d}", "ZQ81X details.",
              created=f"2026-09-{i + 1:02d}", summary="s" * 2000)
        _note(omi, f"KP42Y host note {i:02d}", "KP42Y details.",
              created=f"2026-09-{i + 1:02d}", summary="k" * 2000)
    _note(omi, "NV77T pump note", "NV77T details.")
    _note(omi, "RB19W relay note", "RB19W details.")
    assert searchindex.SearchIndex(omi).refresh(vectors=False) is not None
    text = "ZQ81X KP42Y NV77T RB19W As30p"
    related = writecontext.pick(omi, title="new", summary=text)
    assert len({r.name for r in related}) <= writecontext.MAX_NAMES
    for name in {r.name for r in related}:
        assert sum(r.name == name for r in related) <= writecontext.PER_NAME
    assert all(len(r.summary) <= writecontext.SUMMARY_CHARS for r in related)
    fields = writecontext.response_fields(omi, title="new", summary=text)
    size = len(json.dumps(fields[writecontext.FIELD], ensure_ascii=False))
    assert size <= writecontext.MAX_CHARS
    # A tighter cap drops entries rather than overflowing.
    monkeypatch.setattr(writecontext, "MAX_CHARS", 400)
    small = writecontext.response_fields(omi, title="new", summary=text)
    assert len(json.dumps(small[writecontext.FIELD], ensure_ascii=False)) <= 400


def test_newest_first_within_a_name(omi: Path) -> None:
    related = writecontext.pick(omi, title="As30p check")
    assert [r.updated for r in related] == sorted((r.updated for r in related), reverse=True)


# -- what never counts -------------------------------------------------------


def test_no_names_means_no_field(server: MCPServer) -> None:
    got = call(server, "create-note", {"title": "Groceries", "summary": "milk and eggs"})
    assert writecontext.FIELD not in got


def test_flag_off_and_entity_index_off(
    server: MCPServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(writecontext.ENABLE_ENV, "0")
    got = call(server, "create-note", {"title": "Off one", "summary": "As30p"})
    assert writecontext.FIELD not in got
    monkeypatch.delenv(writecontext.ENABLE_ENV)
    monkeypatch.setenv(entities.ENABLE_ENV, "0")
    got = call(server, "create-note", {"title": "Off two", "summary": "As30p"})
    assert writecontext.FIELD not in got


def test_common_and_untitled_frequent_names_are_skipped(omi: Path) -> None:
    # In more than UNTITLED_MAX_DF notes and no note is titled after it.
    for i in range(namehints.UNTITLED_MAX_DF + 1):
        _note(omi, f"Filler {i:02d}", "built with gcc12x tooling")
    assert searchindex.SearchIndex(omi).refresh(vectors=False) is not None
    assert writecontext.pick(omi, title="Build", summary="gcc12x again") == []
    # Over the vault's rarity ceiling: never an entity.
    lookup = searchindex.entity_lookups_readonly(omi, ["gcc12x"])
    assert lookup is not None and lookup["gcc12x"].df > namehints.UNTITLED_MAX_DF


def test_sequence_tokens_and_path_only_mentions_are_skipped(omi: Path) -> None:
    _note(omi, "run4 of the voice LoRA", "run4 finished.")
    assert searchindex.SearchIndex(omi).refresh(vectors=False) is not None
    assert writecontext.pick(omi, title="Training", summary="run4 is done") == []
    # A course stored under /Volumes/As30p/ says nothing about the drive.
    assert writecontext.pick(
        omi, title="Course videos", details="Rendered to /Volumes/As30p/courses/aws."
    ) == []


def test_a_name_only_in_a_long_body_must_recur(omi: Path) -> None:
    filler = "plain words about nothing in particular. " * 20
    assert writecontext.pick(omi, title="Log", details=filler + " As30p " + filler) == []
    assert writecontext.pick(omi, title="Log", details="As30p " + filler + " As30p")


def test_archived_notes_are_not_listed(server: MCPServer, omi: Path) -> None:
    call(server, "delete-note", {"name": f"{HISTORY}.md"})
    assert searchindex.SearchIndex(omi).refresh(vectors=False) is not None
    got = call(server, "create-note", {"title": "After archive", "summary": "As30p"})
    assert HISTORY not in _titles(got)
    assert _titles(got)  # the live notes still are


def test_a_broken_lookup_never_breaks_the_write(
    server: MCPServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_: object, **__: object) -> None:
        raise RuntimeError("index exploded")

    monkeypatch.setattr(searchindex, "entity_lookups_readonly", boom)
    got = call(server, "create-note", {"title": "Still written", "summary": "As30p"})
    assert got["filename"] == "Still written.md"
    assert writecontext.FIELD not in got


def test_missing_index_fails_open(tmp_path: Path) -> None:
    d = tmp_path / "Bare"
    d.mkdir()
    assert writecontext.pick(d, title="As30p") == []


# -- accounting: pull, never push --------------------------------------------


def test_the_response_is_charged_to_pull_not_push(server: MCPServer, omi: Path) -> None:
    got = call(server, "create-note", {"title": "Pull check", "summary": "As30p"})
    assert got[writecontext.FIELD]
    event = {
        "session_id": "writer",
        "tool_name": "mcp__omi__create-note",
        "tool_response": got,
    }
    ai_usage.record_mcp_response(omi, event)
    rows = [e for e in ai_usage.read_events(omi) if e.get("session_id") == "writer"]
    assert len(rows) == 1
    assert ai_usage.event_channel(rows[0]) == ai_usage.PULL
    assert rows[0]["characters"] >= len(json.dumps(got[writecontext.FIELD]))
    assert guard.session_context_chars(omi, "writer") == 0  # the push budget is untouched
    # And a write's response never triggers tool-output name hints (#388).
    assert namehints.tool_hints({**event, "tool_input": {}}, omi) == ""


# -- replay -------------------------------------------------------------------


def _transcript(path: Path, calls: list[tuple[str, dict[str, Any]]]) -> Path:
    lines = []
    for i, (tool, tool_input) in enumerate(calls):
        lines.append(json.dumps({
            "type": "assistant",
            "timestamp": "2026-09-30T05:00:00Z",
            "message": {"content": [
                {"type": "tool_use", "id": f"t{i}", "name": tool, "input": tool_input}
            ]},
        }))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_replay_and_bench(omi: Path, tmp_path: Path) -> None:
    transcript = _transcript(tmp_path / "s.jsonl", [
        ("mcp__omi__recall-note", {"name": SEAGATE}),
        ("mcp__omi__edit-note", {"name": f"{SEAGATE}.md", "summary": "As30p is mislabeled"}),
        ("mcp__omi__create-note", {"title": "Groceries", "summary": "milk"}),
        ("Bash", {"command": "diskutil list"}),
    ])
    writes = writecontext.replay_transcript(transcript, omi)
    assert [w.tool for w in writes] == ["edit", "create"]
    edit = writes[0]
    assert [r.title for r in edit.related] == [HISTORY, COPY]  # never itself
    assert writes[1].related == []
    report = bench.run_write_context(omi, transcript)
    values = {m.name: m.value for m in report.measurements}
    assert values["writes replayed"] == 2
    assert values["writes with related_by_entity"] == 50.0


def test_replay_hides_notes_written_later(omi: Path, tmp_path: Path) -> None:
    _note(omi, "As30p future note", "As30p later.", created="2026-12-01")
    assert searchindex.SearchIndex(omi).refresh(vectors=False) is not None
    transcript = _transcript(tmp_path / "s.jsonl", [
        ("mcp__omi__create-note", {"title": "As30p now", "summary": "As30p"}),
    ])
    (write,) = writecontext.replay_transcript(transcript, omi)
    assert "As30p future note" not in [r.title for r in write.related]
