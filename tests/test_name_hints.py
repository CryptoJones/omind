# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Aaron K. Clark
"""Tests for #388 (epic #384): a name that appears only in a tool's OUTPUT
brings its notes' titles forward, once per name per session, on the push budget."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from omind import ai_usage, audit, bench, entities, guard, hooks, namehints, retrieve, searchindex

HISTORY = "WD Blue As30p drive check 2026-09-27 — clean, the label moved off the Seagate"
SEAGATE = "Seagate As30p old spinner is NOT proven failing"
MUSIC = "DJ name and SoundCloud releases"

#: The shape of the 2026-09-30 incident: a disk listing where the volume label
#: is the only thing the vault knows about, printed twice (name + mount point).
DISKUTIL = """/dev/disk6 (external, physical):
   #:                       TYPE NAME                    SIZE       IDENTIFIER
   0:      GUID_partition_scheme                        *1.0 TB     disk6
   1:                        EFI EFI                     209.7 MB   disk6s1
   2:       Microsoft Basic Data As30p                   1000.0 GB  disk6s2
---
      Product ID: 0x1234
      Vendor ID: 0x5678
      Mount Point: /Volumes/As30p
""" + ("      Capacity: 1 TB (1,000,204,886,016 bytes)\n" * 8)


def _note(
    omi: Path, title: str, details: str, created: str = "2026-09-25", stem: str = ""
) -> None:
    (omi / f"{stem or title}.md").write_text(
        f"# {title}\n\n## Metadata\n- Created: {created}\n- Tags:\n\n"
        f"## Summary\n{title}\n\n## Details\n{details}\n",
        encoding="utf-8",
    )


@pytest.fixture(autouse=True)
def _flag_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    for env in (
        namehints.ENABLE_ENV,
        entities.ENABLE_ENV,
        entities.MAX_DF_ENV,
        retrieve.PREFLIGHT_RARE_TERMS_ENV,
        retrieve.PREFLIGHT_MIN_TERMS_ENV,
        guard.PREFLIGHT_MODE_ENV,
        ai_usage.SPLIT_BUDGET_ENV,
        searchindex.DISABLE_ENV,
    ):
        monkeypatch.delenv(env, raising=False)


@pytest.fixture
def omi(tmp_path: Path) -> Path:
    """A vault holding the As30p label history among unrelated notes, indexed."""
    d = tmp_path / "OMI"
    d.mkdir()
    _note(d, HISTORY, "As30p is now the WD Blue; it was the Seagate, then a Samsung SSD.",
          created="2026-09-27")
    _note(d, SEAGATE, "The As30p Seagate dropouts may have been the adapter.")
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


def _event(session: str, output: str, *, tool: str = "Bash", tool_input: object = None) -> dict:
    return {
        "session_id": session,
        "tool_name": tool,
        "tool_input": tool_input if tool_input is not None else {"command": "diskutil list"},
        "tool_response": {"stdout": output, "stderr": "", "interrupted": False},
    }


def _ledger(omi: Path) -> list[dict]:
    return [e for e in ai_usage.read_events(omi) if e.get("operation") == namehints.OPERATION]


# -- acceptance criteria -----------------------------------------------------


def test_a_known_name_in_tool_output_is_hinted_once(omi: Path) -> None:
    first = namehints.tool_hints(_event("s1", DISKUTIL), omi)
    assert first.count("\n") == 0, first  # exactly one hint line
    assert first.startswith("OMI: As30p → 3 notes; about it: ")
    # Newest note about the label leads, named by its stored filename (#406).
    assert f"[[{HISTORY}.md]]" in first
    assert "[[" + MUSIC not in first  # titles only, two at most
    assert "recall-note" in first
    # Same output again, and again in a later call: nothing.
    assert namehints.tool_hints(_event("s1", DISKUTIL), omi) == ""
    assert namehints.tool_hints(_event("s1", "As30p " + DISKUTIL), omi) == ""
    # A different session is a different session.
    assert "As30p" in namehints.tool_hints(_event("s2", DISKUTIL), omi)


def test_output_with_no_known_names_adds_zero_characters(omi: Path) -> None:
    for output in (
        "total 0\ndrwxr-xr-x  2 user  staff  64 Oct  1 05:00 .\n",
        "Unknown names only: ZX81Q9 and build-host-77 and 10.9.8.7 " * 40,
        "",
    ):
        assert namehints.tool_hints(_event("quiet", output), omi) == ""
    assert _ledger(omi) == []
    assert namehints.hinted("quiet") == set()


def test_hint_is_push_and_recorded_as_its_own_operation(omi: Path) -> None:
    context = namehints.tool_hints(_event("push", DISKUTIL), omi)
    rows = _ledger(omi)
    assert len(rows) == 1
    assert rows[0]["characters"] == len(context)
    assert rows[0]["session_id"] == "push"
    assert ai_usage.event_channel(rows[0]) == ai_usage.PUSH
    assert guard.session_context_chars(omi, "push") == len(context)


def test_flag_off_disables_it(omi: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(namehints.ENABLE_ENV, "0")
    assert namehints.tool_hints(_event("off", DISKUTIL), omi) == ""
    monkeypatch.delenv(namehints.ENABLE_ENV)
    monkeypatch.setenv(entities.ENABLE_ENV, "0")
    assert namehints.tool_hints(_event("off", DISKUTIL), omi) == ""


def test_audit_reports_name_hints_as_their_own_surface(omi: Path) -> None:
    for i in range(25):
        namehints.tool_hints(_event(f"aud{i}", DISKUTIL), omi)
    rows = {row.key: row for row in audit.run_audit(omi).rows}
    for key in ("namehint.p99_chars", "namehint.session_p99_chars"):
        assert rows[key].surface == "tool-output name hints"
        assert rows[key].status == audit.OK, rows[key]
        assert rows[key].samples == 25


# -- pull is never a trigger -------------------------------------------------


def test_the_agents_own_omi_reads_never_trigger_hints(omi: Path) -> None:
    assert namehints.tool_hints(_event("pull", DISKUTIL, tool="mcp__omi__search-vault"), omi) == ""
    assert namehints.tool_hints(
        _event("pull", DISKUTIL, tool_input={"command": "omind entity As30p"}), omi
    ) == ""
    note = str(omi / f"{HISTORY}.md")
    assert namehints.tool_hints(
        _event("pull", DISKUTIL, tool="Bash", tool_input={"file_path": note}), omi
    ) == ""
    assert namehints.hinted("pull") == set()
    # Working IN the omind repo is not reading memory.
    repo = {"tool_name": "Bash", "tool_input": {"command": "cd ~/src/omind && git status"}}
    assert not namehints.is_pull(repo, omi)


def test_file_tools_and_names_the_agent_typed_are_skipped(omi: Path) -> None:
    assert namehints.tool_hints(
        _event("ft", DISKUTIL, tool="Read", tool_input={"file_path": "/tmp/x"}), omi
    ) == ""
    assert namehints.tool_hints(
        _event("typed", DISKUTIL, tool_input={"command": "diskutil info As30p"}), omi
    ) == ""


# -- budgets and caps --------------------------------------------------------


def test_spent_push_budget_suppresses_hints(omi: Path) -> None:
    ai_usage.record_context(
        omi, "recall", guard.SESSION_INJECTION_BUDGET_CHARS, session_id="full"
    )
    assert namehints.tool_hints(_event("full", DISKUTIL), omi) == ""
    assert namehints.hinted("full") == set()  # not spent: it may still come later


def test_own_reads_do_not_spend_the_push_budget(omi: Path) -> None:
    ai_usage.record_context(
        omi, "mcp", guard.SESSION_INJECTION_BUDGET_CHARS, session_id="reader",
        channel=ai_usage.PULL,
    )
    assert "As30p" in namehints.tool_hints(_event("reader", DISKUTIL), omi)


def test_session_cap_on_name_hint_characters(
    omi: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(namehints, "SESSION_BUDGET_CHARS", 10)
    assert namehints.tool_hints(_event("cap", DISKUTIL), omi) == ""


def test_at_most_three_names_per_call(omi: Path) -> None:
    extra = ["Gadget KX10 manual", "Router RT55 notes", "Phone PX77 setup", "Watch WT12 band"]
    for title in extra:
        token = title.split()[1]
        _note(omi, title, f"All about the {token}.")
    assert searchindex.SearchIndex(omi).refresh(vectors=False) is not None
    output = "KX10 RT55 PX77 WT12 KX10 RT55 PX77 WT12"
    lines = namehints.tool_hints(_event("many", output), omi).splitlines()
    assert len(lines) == namehints.MAX_NAMES_PER_CALL
    rest = namehints.tool_hints(_event("many", output), omi).splitlines()
    assert len(rest) == 1  # the fourth, on the next call; the first three never repeat


def test_preflight_hint_counts_as_hinted(omi: Path) -> None:
    context = guard.preflight_turn({"session_id": "pf", "prompt": "is As30p mounted?"}, omi)
    assert "notes naming As30p" in context
    assert "as30p" in namehints.hinted("pf")
    assert namehints.tool_hints(_event("pf", DISKUTIL), omi) == ""


@pytest.mark.parametrize("tool", ["mcp__omi__create-note", "mcp__omi__edit-note"])
@pytest.mark.parametrize("shape", ["content_blocks", "json", "dict"])
def test_names_shown_at_write_time_count_as_hinted(omi: Path, tool: str, shape: str) -> None:
    """#403: a name the write response already listed under ``related_by_entity``
    or ``name_timelines`` is not hinted again by a later tool result."""
    from omind import writecontext

    fields = writecontext.response_fields(
        omi, title="New As30p mount check", details="Mounted As30p again today."
    )
    assert any(r["name"] == "As30p" for r in fields[writecontext.FIELD])  # type: ignore[union-attr]
    body = json.dumps({"filename": "New As30p mount check.md", **fields})
    response: object = {
        # Claude Code's actual MCP shape: a bare list of content blocks.
        "content_blocks": [{"type": "text", "text": body}],
        "json": body,
        "dict": {"filename": "New As30p mount check.md", **fields},
    }[shape]
    session = f"w-{tool}-{shape}"
    write = {"session_id": session, "tool_name": tool, "tool_input": {}, "tool_response": response}
    assert namehints.tool_hints(write, omi) == ""  # the write itself is pull
    assert "as30p" in namehints.hinted(session)
    assert namehints.tool_hints(_event(session, DISKUTIL), omi) == ""
    assert _ledger(omi) == []


def test_only_write_tools_mark_names_hinted(omi: Path) -> None:
    """A read whose result happens to carry the field name is still just pull."""
    body = json.dumps({"related_by_entity": [{"name": "As30p"}]})
    read = {
        "session_id": "rd",
        "tool_name": "mcp__omi__read-note",
        "tool_input": {},
        "tool_response": [{"type": "text", "text": body}],
    }
    assert namehints.tool_hints(read, omi) == ""
    assert namehints.hinted("rd") == set()
    assert "As30p" in namehints.tool_hints(_event("rd", DISKUTIL), omi)


def test_a_name_only_in_name_timelines_counts_as_hinted(omi: Path) -> None:
    """#403: ``name_timelines`` alone is enough; ``related_by_entity`` absent."""
    body = json.dumps(
        {
            "filename": "New As30p mount check.md",
            "name_timelines": [{"name": "As30p", "entries": []}],
        }
    )
    write = {
        "session_id": "tl",
        "tool_name": "mcp__omi__edit-note",
        "tool_input": {},
        "tool_response": [{"type": "text", "text": body}],
    }
    assert namehints.written_names(write) == {"As30p"}
    assert namehints.tool_hints(write, omi) == ""
    assert "as30p" in namehints.hinted("tl")
    assert namehints.tool_hints(_event("tl", DISKUTIL), omi) == ""


@pytest.mark.parametrize(
    "response",
    [
        [{"type": "text", "text": '{"related_by_entity": [{"name": "As3'}],  # truncated JSON
        None,
        [1, 2, 3],
        {
            "related_by_entity": [{"name": 5}, {"name": None}, "As30p"],
            "name_timelines": [{"name": ["As30p"]}, {"name": {"x": 1}}],
        },
    ],
    ids=["truncated-json", "none", "list-of-ints", "non-string-names"],
)
def test_an_unparseable_write_response_marks_nothing(omi: Path, response: object) -> None:
    """#403 fails open (AGENTS.md invariant 2): a malformed write response
    marks nothing and raises nothing, and a later result still hints."""
    session = f"bad-{abs(hash(repr(response)))}"
    write = {
        "session_id": session,
        "tool_name": "mcp__omi__create-note",
        "tool_input": {},
        "tool_response": response,
    }
    assert namehints.written_names(write) == set()
    assert namehints.tool_hints(write, omi) == ""
    assert namehints.hinted(session) == set()
    assert "As30p" in namehints.tool_hints(_event(session, DISKUTIL), omi)


# -- what counts as a name worth a hint --------------------------------------


def test_a_single_mention_in_a_long_output_is_not_salient(omi: Path) -> None:
    long_once = ("noise line without names\n" * 40) + "As30p\n"
    assert namehints.pick_hints(long_once, omi) == []
    assert [h.name for h in namehints.pick_hints(long_once + "As30p\n", omi)] == ["As30p"]
    assert [h.name for h in namehints.pick_hints("label: As30p", omi)] == ["As30p"]


def test_noise_tokens_are_never_hinted(omi: Path) -> None:
    _note(omi, "Build log", "commit 3f5fadf9 built with utf-8 on arm64; see video/mp4.")
    assert searchindex.SearchIndex(omi).refresh(vectors=False) is not None
    output = "3f5fadf9 3f5fadf9 utf-8 utf-8 arm64 arm64 video/mp4 video/mp4"
    assert namehints.pick_hints(output, omi) == []


def test_a_title_naming_the_thing_beats_a_newer_mention(omi: Path) -> None:
    _note(omi, "Unrelated repo move", "61 repos moved to /Volumes/As30p/source.",
          created="2026-09-29")
    _note(
        omi, "Repos moved to /Volumes/As30p/source/repos", "Moved.", created="2026-09-30",
        stem="Repos moved to Volumes As30p source repos",
    )
    assert searchindex.SearchIndex(omi).refresh(vectors=False) is not None
    (hint,) = namehints.pick_hints("As30p", omi)
    assert hint.titles[0] == HISTORY  # newest note *about* As30p, not the path mention
    assert hint.titled == 2


# -- fail open ---------------------------------------------------------------


def test_no_index_is_no_hint(tmp_path: Path) -> None:
    vault = tmp_path / "Empty"
    vault.mkdir()
    assert searchindex.entity_lookups_readonly(vault, ["As30p"]) is None
    assert namehints.tool_hints(_event("none", DISKUTIL), vault) == ""
    assert namehints.tool_hints(_event("none", DISKUTIL), None) == ""
    assert namehints.tool_hints({"tool_response": DISKUTIL}, vault) == ""  # no session


def test_readonly_lookup_agrees_with_the_index(omi: Path) -> None:
    found = searchindex.entity_lookups_readonly(omi, ["As30p", "172.16.24.46", "nope42"])
    assert found is not None
    index = searchindex.SearchIndex(omi)
    for token in ("As30p", "172.16.24.46", "nope42"):
        expected = index.entity_lookup(token)
        assert expected is not None
        got = found[entities.normalize(token)]
        assert (got.df, got.total, got.ceiling) == (expected.df, expected.total, expected.ceiling)
        assert [n.filename for n in got.notes] == [n.filename for n in expected.notes]


def test_response_text_reads_every_harness_shape() -> None:
    assert "As30p" in namehints.response_text({"stdout": "As30p", "stderr": ""})
    assert "As30p" in namehints.response_text({"file": {"content": "As30p"}})
    assert "As30p" in namehints.response_text([{"type": "text", "text": "As30p"}])
    assert namehints.response_text("x" * 50_000) == "x" * namehints.MAX_SCAN_CHARS


# -- the hook ----------------------------------------------------------------


def test_post_tool_use_hook_emits_the_hint_for_claude_only(omi: Path) -> None:
    event = {"hook_event_name": "PostToolUse", **_event("hook", DISKUTIL)}
    silent = io.StringIO()
    hooks.run_hook(
        "PostToolUse", omi, stdin=io.StringIO(json.dumps(event)), stdout=silent, harness="hermes"
    )
    assert silent.getvalue() == ""
    out = io.StringIO()
    rc = hooks.run_hook(
        "PostToolUse", omi, stdin=io.StringIO(json.dumps(event)), stdout=out, harness="claude"
    )
    assert rc == 0
    context = json.loads(out.getvalue())["hookSpecificOutput"]["additionalContext"]
    assert context.startswith("OMI: As30p → ")
    again = io.StringIO()
    hooks.run_hook(
        "PostToolUse", omi, stdin=io.StringIO(json.dumps(event)), stdout=again, harness="claude"
    )
    assert again.getvalue() == ""


# -- the 2026-09-30 replay (epic #384 definition of done) --------------------


def _incident_transcript(path: Path) -> Path:
    """The incident's shape: the prompt never names As30p, the diskutil result
    does, and many turns later the agent edits a note about that drive."""
    rows = [
        {"type": "user", "timestamp": "2026-09-30T19:16:00Z",
         "message": {"role": "user", "content": "What USB drives are plugged in?"}},
        {"type": "assistant", "timestamp": "2026-09-30T19:16:30Z",
         "message": {"role": "assistant", "content": [
             {"type": "tool_use", "id": "t1", "name": "Bash",
              "input": {"command": "diskutil list external physical"}}]}},
        {"type": "user", "timestamp": "2026-09-30T19:16:31Z",
         "message": {"role": "user", "content": [
             {"type": "tool_result", "tool_use_id": "t1", "content": DISKUTIL}]}},
        {"type": "assistant", "timestamp": "2026-09-30T19:44:00Z",
         "message": {"role": "assistant", "content": [
             {"type": "tool_use", "id": "t2", "name": "mcp__omi__edit-note",
              "input": {"name": SEAGATE, "details": "As30p is mislabeled here"}}]}},
        {"type": "user", "timestamp": "2026-09-30T19:44:01Z",
         "message": {"role": "user", "content": [
             {"type": "tool_result", "tool_use_id": "t2", "content": "As30p As30p ok"}]}},
    ]
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    return path


def test_replay_surfaces_the_label_history_before_the_incorrect_claim(
    omi: Path, tmp_path: Path
) -> None:
    transcript = _incident_transcript(tmp_path / "incident.jsonl")
    replay = namehints.replay_transcript(transcript, omi)
    assert replay.tool_results == 2
    assert replay.pull_skipped == 1  # the edit-note result is the agent's own read
    (first,) = replay.hints
    assert first.hint.name == "As30p"
    assert first.hint.titles[0] == HISTORY
    assert first.line == 3 < 4  # the diskutil result, before the edit-note on line 4
    assert first.used_later and first.consulted_later


def test_replay_does_not_hint_a_name_the_write_response_showed(
    omi: Path, tmp_path: Path
) -> None:
    """#403: the bench replay matches the live hook — a name a create-note
    response listed is not hinted by a later tool result naming it."""
    from omind import writecontext

    fields = writecontext.response_fields(
        omi, title="New As30p mount check", details="Mounted As30p again today."
    )
    body = json.dumps({"filename": "New As30p mount check.md", **fields})
    rows = [
        {"type": "assistant", "timestamp": "2026-09-30T19:10:00Z",
         "message": {"role": "assistant", "content": [
             {"type": "tool_use", "id": "w1", "name": "mcp__omi__create-note",
              "input": {"title": "New As30p mount check"}}]}},
        {"type": "user", "timestamp": "2026-09-30T19:10:01Z",
         "message": {"role": "user", "content": [
             {"type": "tool_result", "tool_use_id": "w1",
              "content": [{"type": "text", "text": body}]}]}},
        {"type": "assistant", "timestamp": "2026-09-30T19:16:30Z",
         "message": {"role": "assistant", "content": [
             {"type": "tool_use", "id": "t1", "name": "Bash",
              "input": {"command": "diskutil list external physical"}}]}},
        {"type": "user", "timestamp": "2026-09-30T19:16:31Z",
         "message": {"role": "user", "content": [
             {"type": "tool_result", "tool_use_id": "t1", "content": DISKUTIL}]}},
    ]
    transcript = tmp_path / "write-then-read.jsonl"
    transcript.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    replay = namehints.replay_transcript(transcript, omi)
    assert replay.tool_results == 2
    assert replay.pull_skipped == 1
    assert "As30p" not in [h.hint.name for h in replay.hints]


def test_replay_hides_notes_written_after_the_tool_result(omi: Path, tmp_path: Path) -> None:
    _note(omi, "Later note naming QZ81 only", "QZ81 appeared later.", created="2026-10-05")
    assert searchindex.SearchIndex(omi).refresh(vectors=False) is not None
    transcript = _incident_transcript(tmp_path / "incident.jsonl")
    text = transcript.read_text(encoding="utf-8").replace("disk6s1", "QZ81 QZ81")
    transcript.write_text(text, encoding="utf-8")
    names = [h.hint.name for h in namehints.replay_transcript(transcript, omi).hints]
    assert "QZ81" not in names


def test_bench_tool_hints_reports_on_a_directory(omi: Path, tmp_path: Path) -> None:
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    _incident_transcript(sessions / "a.jsonl")
    report = bench.run_tool_hints(omi, sessions)
    rows = {m.name: m for m in report.measurements}
    assert rows["transcripts"].value == 1
    assert rows["hint precision (named later)"].value == 100.0
    assert rows["added latency, p95"].unit == "ms"
    assert any(name.startswith("hint @ line 3") for name in rows)
    with pytest.raises(OSError):
        bench.run_tool_hints(omi, tmp_path / "missing")
