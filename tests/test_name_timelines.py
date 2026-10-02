# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Aaron K. Clark
"""Tests for #390 (epic #384): a name whose facts changed shows its dated
history, superseded and corrected notes marked instead of suppressed."""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from omind import (
    ai_usage,
    compliance,
    entities,
    guard,
    namehints,
    retrieve,
    searchindex,
    timeline,
    writecontext,
)

SEAGATE = "Seagate As30p old spinner is NOT proven failing"
SAMSUNG = "As30p moved to the Samsung 870 EVO SSD"
WD_BLUE = "WD Blue As30p drive check 2026-09-27 — clean"
MUSIC = "DJ name and SoundCloud releases"

DISKUTIL = "   2:       Microsoft Basic Data As30p    1000.0 GB  disk6s2\n"


def _note(
    omi: Path,
    title: str,
    details: str,
    *,
    created: str = "2026-09-25",
    meta: str = "",
    mtime: float | None = None,
) -> Path:
    path = omi / f"{title}.md"
    path.write_text(
        f"# {title}\n\n## Metadata\n- Created: {created}\n- Tags:\n{meta}\n"
        f"## Summary\n{title}\n\n## Details\n{details}\n",
        encoding="utf-8",
    )
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


@pytest.fixture(autouse=True)
def _flag_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    for env in (
        timeline.ENABLE_ENV,
        namehints.ENABLE_ENV,
        writecontext.ENABLE_ENV,
        entities.ENABLE_ENV,
        entities.MAX_DF_ENV,
        retrieve.PREFLIGHT_RARE_TERMS_ENV,
        retrieve.PREFLIGHT_MIN_TERMS_ENV,
        guard.PREFLIGHT_MODE_ENV,
        guard.MISS_STRICT_ENV,
        ai_usage.SPLIT_BUDGET_ENV,
        searchindex.DISABLE_ENV,
    ):
        monkeypatch.delenv(env, raising=False)


def _fillers(omi: Path) -> None:
    for i, topic in enumerate(
        ("Kitchen recipes", "Garden watering", "Bicycle tyres", "Tax paperwork",
         "Piano practice", "Bird feeder", "Holiday packing", "Library books",
         "Coffee grinder", "Window cleaning", "Running shoes")
    ):
        _note(omi, topic, f"Plain notes about {topic.lower()}, item {i}.")


def _refresh(omi: Path) -> None:
    assert searchindex.SearchIndex(omi).refresh(vectors=False) is not None


@pytest.fixture
def omi(tmp_path: Path) -> Path:
    """The As30p label history: Seagate, then Samsung, then WD Blue."""
    d = tmp_path / "OMI"
    d.mkdir()
    _note(
        d,
        SEAGATE,
        "The As30p Seagate dropouts were blamed on the drive.\n\n"
        "**CORRECTION (2026-09-26):** the dropouts were the adapter, not the drive.",
        mtime=1_758_800_000,
    )
    _note(
        d,
        SAMSUNG,
        "The As30p label now sits on the Samsung SSD.",
        meta=f"- Superseded by: {WD_BLUE}.md\n",
        mtime=1_758_810_000,
    )
    _note(
        d,
        WD_BLUE,
        "As30p is now the WD Blue; it was the Seagate, then a Samsung SSD.",
        created="2026-09-27",
        meta=f"- Supersedes: [[{SAMSUNG}]]\n",
    )
    _note(d, MUSIC, "Releases as As30p on SoundCloud.", created="2026-07-05")
    _note(d, "Pluto GPUs", "pluto has a V100 and a P100 at 172.16.24.46.")
    _fillers(d)
    _refresh(d)
    return d


def _event(session: str, output: str) -> dict:
    return {
        "session_id": session,
        "tool_name": "Bash",
        "tool_input": {"command": "diskutil list"},
        "tool_response": {"stdout": output, "stderr": "", "interrupted": False},
    }


# -- acceptance criteria -----------------------------------------------------


def test_as30p_timeline_lists_the_three_drives_in_order_with_superseded_marked(
    omi: Path,
) -> None:
    (found,) = timeline.for_names(omi, ["As30p"])
    assert [(e.title, e.date, e.mark) for e in found.entries] == [
        (SEAGATE, "2026-09-25", timeline.CORRECTION),
        (SAMSUNG, "2026-09-25", timeline.SUPERSEDED),
        (WD_BLUE, "2026-09-27", timeline.SUPERSEDES),
    ]
    line = found.line()
    assert line.index(SEAGATE) < line.index(SAMSUNG) < line.index(WD_BLUE)
    # Each entry names the stored filename, which recall-note opens as-is (#406).
    assert f"2026-09-25 [[{SAMSUNG}.md]] (superseded)" in line
    assert f"2026-09-25 [[{SEAGATE}.md]] (has a correction)" in line
    assert f"2026-09-27 [[{WD_BLUE}.md]] (replaces an older note, newest)" in line
    assert MUSIC not in line  # notes ABOUT the name only; the DJ note merely mentions it


def test_tool_output_hint_shows_the_timeline_and_counts_as_push(omi: Path) -> None:
    context = namehints.tool_hints(_event("t1", DISKUTIL), omi)
    assert context.startswith("OMI: As30p has a dated history (4 notes; oldest first): ")
    assert context.index(SEAGATE) < context.index(SAMSUNG) < context.index(WD_BLUE)
    assert "(superseded)" in context
    rows = [e for e in ai_usage.read_events(omi) if e.get("operation") == namehints.OPERATION]
    assert rows and rows[-1]["characters"] == len(context)
    assert ai_usage.event_channel(rows[-1]) == ai_usage.PUSH
    assert namehints.tool_hints(_event("t1", DISKUTIL), omi) == ""  # still once per session


def test_preflight_rare_name_shows_the_timeline_even_for_a_stale_best_match(
    omi: Path,
) -> None:
    context = guard.preflight_turn({"session_id": "pf1", "prompt": "is As30p mounted?"}, omi)
    assert "carries a supersession" not in context
    assert "notes naming As30p" in context
    assert "As30p has a dated history" in context
    assert context.index(SEAGATE, context.index("dated history")) < context.index(
        WD_BLUE, context.index("dated history")
    )
    assert guard.consulted_this_turn("pf1")
    assert "timeline=['As30p']" in compliance.read_events()[-1]["detail"]
    recorded = [e for e in ai_usage.read_events(omi) if e.get("operation") == "recall"]
    assert recorded and recorded[-1]["characters"] == len(context)
    # The name was hinted for the prompt, so tool output does not repeat it.
    assert namehints.tool_hints(_event("pf1", DISKUTIL), omi) == ""


def test_topic_match_preflight_still_suppresses_a_stale_note(tmp_path: Path) -> None:
    """Regression: a plain topic match with a CORRECTION line is never named."""
    d = tmp_path / "OMI"
    d.mkdir()
    _note(
        d,
        "Kitchen knife sharpening schedule",
        "Sharpen the kitchen knife every month on the whetstone schedule.\n\n"
        "**CORRECTION:** every two months, the whetstone schedule was wrong.",
    )
    _fillers(d)
    _refresh(d)
    prompt = "what is the kitchen knife sharpening schedule on the whetstone?"
    context = guard.preflight_turn({"session_id": "topic", "prompt": prompt}, d)
    assert "carries a supersession or correction marker" in context
    assert "dated history" not in context


def test_write_context_lists_the_timeline(omi: Path) -> None:
    fields = writecontext.response_fields(
        omi,
        title="As30p is mislabeled",
        summary="The As30p label is on the wrong drive.",
        exclude=["As30p is mislabeled.md"],
    )
    assert writecontext.FIELD in fields
    timelines = fields[writecontext.TIMELINE_FIELD]
    assert isinstance(timelines, list)
    (as30p,) = timelines
    assert as30p["name"] == "As30p"
    assert [n["title"] for n in as30p["notes"]] == [SEAGATE, SAMSUNG, WD_BLUE]
    assert [n["mark"] for n in as30p["notes"]] == [
        timeline.CORRECTION, timeline.SUPERSEDED, timeline.SUPERSEDES,
    ]


# -- only names with history; bounded; flag ---------------------------------


def test_a_name_without_history_keeps_the_plain_hint(omi: Path) -> None:
    assert timeline.for_names(omi, ["172.16.24.46"]) == []
    (hint,) = namehints.pick_hints("pluto 172.16.24.46", omi)
    assert hint.timeline is None
    assert "about it" in hint.line() or "newest" in hint.line()


def test_flag_off_restores_the_plain_paths(omi: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(timeline.ENABLE_ENV, "0")
    assert timeline.for_names(omi, ["As30p"]) == []
    context = namehints.tool_hints(_event("off", DISKUTIL), omi)
    assert context.startswith("OMI: As30p → 4 notes; about it: ")
    pre = guard.preflight_turn({"session_id": "off2", "prompt": "is As30p mounted?"}, omi)
    assert "dated history" not in pre
    fields = writecontext.response_fields(omi, title="As30p check", exclude=["x.md"])
    assert writecontext.TIMELINE_FIELD not in fields


def test_timeline_is_bounded(tmp_path: Path) -> None:
    d = tmp_path / "OMI"
    d.mkdir()
    for day in range(1, 10):  # 9 notes: under the 10-note rarity floor
        _note(
            d,
            f"Zq77x host rebuild number {day}" + " with a long descriptive title" * 4,
            "Zq77x was rebuilt.\n\nSUPERSEDED: see the next rebuild." if day < 9 else "Now.",
            created=f"2026-09-{day:02d}",
        )
    _fillers(d)
    _refresh(d)
    (found,) = timeline.for_names(d, ["Zq77x"])
    assert len(found.entries) == timeline.MAX_ENTRIES
    assert [e.date for e in found.entries] == [f"2026-09-{day:02d}" for day in range(4, 10)]
    line = found.line()
    assert len(line) <= timeline.MAX_CHARS
    assert "2026-09-09" in line  # the newest always survives the cut
    # An entry carries the stored filename uncut (#406); the store bounds it at
    # 200 bytes, so at most 200 characters. MAX_CHARS counts characters too.
    assert all(len(e.render()) < 200 + 60 for e in found.entries)


def test_as_of_hides_later_notes(omi: Path) -> None:
    (found,) = timeline.for_names(omi, ["As30p"], as_of="2026-09-25")
    assert [e.title for e in found.entries] == [SEAGATE, SAMSUNG]


def test_no_index_is_no_timeline(tmp_path: Path) -> None:
    d = tmp_path / "OMI"
    d.mkdir()
    _note(d, SEAGATE, "As30p\n\nCORRECTION: adapter.")
    assert timeline.for_names(d, ["As30p"]) == []


def test_line_drops_oldest_entries_to_fit(monkeypatch: pytest.MonkeyPatch) -> None:
    # Six mid-length entries fit the shipped cap, so tighten it.
    monkeypatch.setattr(timeline, "MAX_CHARS", 500)
    entries = [
        timeline.Entry(
            filename=f"{i} {'t' * 80}.md", title="t" * 200, date=f"2026-09-0{i}", mark=""
        )
        for i in range(1, 7)
    ]
    line = timeline.Timeline(name="Zq77x", key="zq77x", df=6, entries=entries).line()
    assert len(line) <= timeline.MAX_CHARS
    assert "2026-09-06" in line and "2026-09-01" not in line
    assert "older)" in line


def test_bench_preflight_replica_applies_the_same_exemption(
    omi: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omind import bench

    pick = bench.preflight_pick(omi, "is As30p mounted?")
    assert pick is not None and pick.rare == ("As30p",)
    assert "As30p has a dated history" in pick.timelines
    monkeypatch.setenv(timeline.ENABLE_ENV, "0")
    off = bench.preflight_pick(omi, "is As30p mounted?")
    assert off is None or not off.timelines


def test_a_newer_notes_supersedes_marks_the_older_one(tmp_path: Path) -> None:
    """``Supersedes:`` on the newer note marks the older one even when the older
    note was never stamped ``Superseded by:``."""
    d = tmp_path / "OMI"
    d.mkdir()
    _note(d, "Kq55z listens on port 8080", "Kq55z serves on 8080.", created="2026-09-01")
    _note(
        d,
        "Kq55z moved to port 9090",
        "Kq55z serves on 9090 now.",
        created="2026-09-20",
        meta="- Supersedes: [[Kq55z listens on port 8080]]\n",
    )
    _fillers(d)
    _refresh(d)
    (found,) = timeline.for_names(d, ["Kq55z"])
    assert [(e.date, e.mark) for e in found.entries] == [
        ("2026-09-01", timeline.SUPERSEDED),
        ("2026-09-20", timeline.SUPERSEDES),
    ]


def test_write_context_exclusion_survives_a_generator(omi: Path) -> None:
    fields = writecontext.response_fields(
        omi, title="As30p check", exclude=(name for name in [f"{WD_BLUE}.md"])
    )
    (as30p,) = fields[writecontext.TIMELINE_FIELD]  # type: ignore[misc]
    assert WD_BLUE not in [n["title"] for n in as30p["notes"]]
    assert [r["title"] for r in fields[writecontext.FIELD]].count(WD_BLUE) == 0  # type: ignore[union-attr]


# -- #406: every emitted name resolves through recall-note -------------------

_LINK = re.compile(r"\[\[(.+?)\]\]")


def _raw(omi: Path, filename: str, title: str, details: str, *, created: str) -> str:
    (omi / filename).write_text(
        f"# {title}\n\n## Metadata\n- Created: {created}\n- Tags:\n\n"
        f"## Summary\n{title}\n\n## Details\n{details}\n",
        encoding="utf-8",
    )
    return filename


@pytest.fixture
def awkward(tmp_path: Path) -> tuple[Path, set[str]]:
    """Notes whose titles do not resolve: one longer than any cut, one
    retitled after it was written, one whose title ends in ``.md``, one whose
    title holds a colon."""
    d = tmp_path / "OMI"
    d.mkdir()
    long_title = "Vx91k and Wq42m array rebuild runbook " + "with every step spelled out " * 8
    files = {
        _raw(
            d,
            "Vx91k and Wq42m array rebuild runbook with every step spelled out.md",
            long_title.strip(),
            "Vx91k Wq42m rebuild.\n\n**CORRECTION:** the spare was the wrong size.",
            created="2026-09-01",
        ),
        _raw(
            d,
            "Vx91k first draft.md",
            "Vx91k and Wq42m moved to the NAS shelf",
            "Vx91k Wq42m live on the NAS now.",
            created="2026-09-10",
        ),
        _raw(
            d,
            "Vx91k and Wq42m notes about README.md.md",
            "Vx91k and Wq42m notes about README.md",
            "Vx91k Wq42m README.\n\nSupersedes: [[Vx91k first draft]]",
            created="2026-09-20",
        ),
    }
    # #416: a colon title, stored without its colon.
    files.add(
        _raw(
            d,
            "Vx91k Wq42m spare log.md",
            "Vx91k: Wq42m spare log",
            "Vx91k Wq42m spare disks on the shelf.",
            created="2026-09-15",
        )
    )
    # The stem of the ``.md``-titled note names this unrelated one instead.
    _raw(d, "Vx91k and Wq42m notes about README.md", "Unrelated readme", "x", created="2026-01-01")
    _fillers(d)
    _refresh(d)
    return d, files


def _resolves(omi: Path, names: list[str], files: set[str]) -> None:
    from omind import recall

    assert names
    for name in names:
        got = recall.compact_recall(omi, name, organic=False)
        assert got["filename"] == name and name in files, name


def test_every_emitted_hint_name_resolves_through_recall(
    awkward: tuple[Path, set[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    omi, files = awkward
    (found,) = timeline.for_names(omi, ["Vx91k"])
    linked = _LINK.findall(found.line())
    assert len(linked) == 4
    _resolves(omi, linked, files)
    notes = found.to_dict()["notes"]
    assert isinstance(notes, list)
    _resolves(omi, [n["note"] for n in notes], files)

    hints = namehints.pick_hints("Vx91k Wq42m", omi)
    assert hints and hints[0].timeline is not None
    _resolves(omi, [m for h in hints for m in _LINK.findall(h.line())], files)

    # The plain titles-only hint (no timeline) names notes the same way.
    monkeypatch.setenv(timeline.ENABLE_ENV, "0")
    plain = namehints.pick_hints("Vx91k Wq42m", omi)
    assert plain and all(h.timeline is None for h in plain)
    names = [m for h in plain for m in _LINK.findall(h.line())]
    assert len(names) >= 2
    _resolves(omi, names, files)


def test_write_context_timeline_carries_the_stored_filename(
    awkward: tuple[Path, set[str]],
) -> None:
    omi, files = awkward
    fields = writecontext.response_fields(omi, title="Vx91k check", exclude=["x.md"])
    entries = fields[writecontext.TIMELINE_FIELD]
    assert isinstance(entries, list) and entries
    _resolves(omi, [n["note"] for e in entries for n in e["notes"]], files)


def test_write_context_related_entries_carry_the_stored_filename(
    awkward: tuple[Path, set[str]],
) -> None:
    # #416: related_by_entity named notes by a cut title only.
    omi, files = awkward
    fields = writecontext.response_fields(omi, title="Vx91k Wq42m check", exclude=["x.md"])
    related = fields[writecontext.FIELD]
    assert isinstance(related, list) and related
    _resolves(omi, [r["note"] for r in related], files)


def _emitted_names(context: str) -> list[str]:
    """The [[…]] names preflight itself wrote: its first line, plus an inject
    runner-up line. An injected excerpt's own wikilinks are the note's
    content, not names omind emitted."""
    lines = context.split("\n")
    own = [lines[0]] + [ln for ln in lines if ln.startswith("Also possibly relevant: ")]
    return [m for ln in own for m in _LINK.findall(ln)]


@pytest.mark.parametrize("mode", ["hint", "inject"])
@pytest.mark.parametrize(
    ("prompt", "expected"),
    [
        ("Vx91k Wq42m moved to the NAS shelf", "Vx91k first draft.md"),
        ("Vx91k Wq42m spare log", "Vx91k Wq42m spare log.md"),
        ("Vx91k Wq42m notes about README", "Vx91k and Wq42m notes about README.md.md"),
        # A body with a correction: the stale-note message names it instead.
        (
            "Vx91k Wq42m array rebuild runbook every step spelled out",
            "Vx91k and Wq42m array rebuild runbook with every step spelled out.md",
        ),
    ],
)
def test_preflight_names_resolve_through_recall(
    awkward: tuple[Path, set[str]],
    monkeypatch: pytest.MonkeyPatch,
    prompt: str,
    expected: str,
    mode: str,
) -> None:
    # #416: the preflight topic hint named its candidates by title; under
    # OMIND_PREFLIGHT=inject the recalled line and its runner-up did too.
    from omind import recall

    omi, _files = awkward
    monkeypatch.setenv(retrieve.PREFLIGHT_MIN_TERMS_ENV, "0")
    monkeypatch.setenv(timeline.ENABLE_ENV, "0")
    monkeypatch.setenv(guard.PREFLIGHT_MODE_ENV, mode)
    context = guard.preflight_turn(
        {"session_id": f"pf-416-{mode}-{expected[:12]}", "prompt": prompt}, omi
    )
    names = _emitted_names(context)
    assert names and names[0] == expected, context
    for name in names:
        assert recall.compact_recall(omi, name, organic=False)["filename"] == name


@pytest.mark.parametrize("mode", ["hint", "inject"])
@pytest.mark.parametrize(
    ("notes", "prompt", "expected"),
    [
        # A ``.md`` title whose stem names a NEWER, unrelated note.
        (
            [
                (
                    "Qz77 notes about README.md.md",
                    "Qz77 notes about README.md",
                    "Qz77 setup readme guidance zebra.",
                    "2026-09-01",
                ),
                ("Qz77 notes about README.md", "Unrelated", "x", "2026-09-30"),
            ],
            "Qz77 notes about README zebra",
            "Qz77 notes about README.md.md",
        ),
        # Two notes share a title; the newer one is the off-topic one.
        (
            [
                (
                    "Kp12 deploy A.md",
                    "Kp12 deploy steps",
                    "Kp12 deploy steps via ansible playbook frobnicate.",
                    "2026-09-01",
                ),
                (
                    "Kp12 deploy B.md",
                    "Kp12 deploy steps",
                    "unrelated garden tomatoes.",
                    "2026-09-30",
                ),
            ],
            "Kp12 deploy steps ansible playbook frobnicate",
            "Kp12 deploy A.md",
        ),
        # A retitled note whose title is a NEWER note's stem.
        (
            [
                (
                    "Old name.md",
                    "Mx55 backup policy",
                    "Mx55 backup policy restic nightly glacier.",
                    "2026-09-01",
                ),
                ("Mx55 backup policy.md", "Something else", "garden tomatoes.", "2026-09-30"),
            ],
            "Mx55 backup policy restic nightly glacier",
            "Old name.md",
        ),
    ],
)
def test_preflight_names_the_ranked_note_not_a_newer_decoy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    notes: list[tuple[str, str, str, str]],
    prompt: str,
    expected: str,
    mode: str,
) -> None:
    # #416 round 2: re-resolving the top title picked the NEWEST note whose
    # title, filename or stem matched — a decoy, not the note retrieval ranked.
    from omind import recall

    omi = tmp_path / "OMI"
    omi.mkdir()
    for filename, title, details, created in notes:
        _raw(omi, filename, title, details, created=created)
    _fillers(omi)
    _refresh(omi)
    monkeypatch.setenv(retrieve.PREFLIGHT_MIN_TERMS_ENV, "0")
    monkeypatch.setenv(timeline.ENABLE_ENV, "0")
    monkeypatch.setenv(guard.PREFLIGHT_MODE_ENV, mode)
    assert retrieve.relevant_notes(prompt, omi, limit=1)[0][1] == expected
    context = guard.preflight_turn(
        {"session_id": f"pf-decoy-{mode}-{expected[:12]}", "prompt": prompt}, omi
    )
    names = _emitted_names(context)
    assert names and names[0] == expected, context
    for name in names:
        assert recall.compact_recall(omi, name, organic=False)["filename"] == name


def test_inject_mode_names_both_notes_by_stored_filename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # #416 round 2: inject mode named the recalled note and the colon-titled
    # runner-up by title, and neither opened through recall-note.
    from omind import recall

    omi = tmp_path / "OMI"
    omi.mkdir()
    _raw(
        omi,
        "Rq88 first.md",
        "Rq88 restic backup nightly glacier",
        "Rq88 restic backup nightly glacier.",
        created="2026-09-01",
    )
    _raw(
        omi,
        "Rq88 colon.md",
        "Rq88: restic backup glacier",
        "Rq88 restic backup glacier tiers.",
        created="2026-09-02",
    )
    _fillers(omi)
    _refresh(omi)
    monkeypatch.setenv(retrieve.PREFLIGHT_MIN_TERMS_ENV, "0")
    monkeypatch.setenv(timeline.ENABLE_ENV, "0")
    monkeypatch.setenv(guard.PREFLIGHT_MODE_ENV, "inject")
    prompt = "Rq88 restic backup nightly glacier"
    context = guard.preflight_turn({"session_id": "pf-inject-416", "prompt": prompt}, omi)
    assert "Also possibly relevant" in context, context
    assert _emitted_names(context) == ["Rq88 first.md", "Rq88 colon.md"], context
    for name in ("Rq88 first.md", "Rq88 colon.md"):
        assert recall.compact_recall(omi, name, organic=False)["filename"] == name


def test_preflight_drops_the_runner_up_rather_than_cut_a_link(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omind import recall
    from omind.store import NoteFields, OmiStore

    monkeypatch.setenv(retrieve.PREFLIGHT_MIN_TERMS_ENV, "0")
    omi = tmp_path / "OMI"
    omi.mkdir()
    store = OmiStore(omi)
    for word in ("Alpha", "Beta"):
        store.create_note(
            NoteFields(
                title=f"Token Budget {word} " + "with a very long descriptive tail " * 6,
                summary="Keep OMI token usage bounded.",
            )
        )
    context = guard.preflight_turn({"session_id": "pf-416-long", "prompt": "token budget"}, omi)
    assert len(context) <= guard.PREFLIGHT_HINT_CHARS
    assert context.count("[[") == context.count("]]") == 1
    (name,) = _LINK.findall(context)
    assert recall.compact_recall(omi, name, organic=False)["filename"] == name


def test_a_line_is_never_cut_inside_a_link(monkeypatch: pytest.MonkeyPatch) -> None:
    # One entry left and still over the cap: no half-name, no line.
    monkeypatch.setattr(timeline, "MAX_CHARS", 250)
    entries = [timeline.Entry(filename=f"{'n' * 190}.md", title="t", date="2026-09-01", mark="")]
    found = timeline.Timeline(name="Zq77x", key="zq77x", df=1, entries=entries)
    assert not found.fits()
    assert found.line() == ""
    assert "[[" not in timeline.lines([found])


def test_build_drops_a_timeline_whose_newest_entry_cannot_fit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    monkeypatch.setattr(timeline, "MAX_CHARS", 250)
    note = SimpleNamespace(
        filename=f"Zq77x {'n' * 190}.md",
        title="Zq77x drive",
        last_seen="2026-09-01",
        superseded_by="[[Zq77x newer]]",
        supersedes="",
    )
    assert timeline.build("/nonexistent", "Zq77x", [note], read=lambda _f: "") is None
    monkeypatch.setattr(timeline, "MAX_CHARS", 900)
    assert timeline.build("/nonexistent", "Zq77x", [note], read=lambda _f: "") is not None


def test_long_filenames_keep_the_newest_three_of_six() -> None:
    # Uncut filenames (#406) cost depth: six ~190-char filenames fit three
    # entries under the shipped 900-char cap; the oldest (superseded) go first.
    entries = [
        timeline.Entry(
            filename=f"{i} {'f' * 184}.md",
            title=f"t{i}",
            date=f"2026-09-0{i}",
            mark=timeline.SUPERSEDED if i < 6 else "",
        )
        for i in range(1, 7)
    ]
    line = timeline.Timeline(name="Zq77x", key="zq77x", df=6, entries=entries).line()
    assert len(line) <= timeline.MAX_CHARS
    assert [m.split(" ", 1)[0] for m in _LINK.findall(line)] == ["4", "5", "6"]
    assert "(+3 older)" in line


def test_bench_counts_a_recall_by_stored_filename_as_a_consult(tmp_path: Path) -> None:
    # Hints name notes by filename (#406), so the bench must match a recall of it.
    import json

    d = tmp_path / "OMI"
    d.mkdir()
    _raw(
        d,
        "drive-notes-2026.md",
        "Qp55z label history across three drives " + "x" * 40,
        "Qp55z moved twice.",
        created="2026-09-20",
    )
    _fillers(d)
    _refresh(d)
    call = {"name": "drive-notes-2026.md"}
    rows = [
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {}}],
            },
        },
        {
            "type": "user",
            "timestamp": "2026-09-30T19:16:31Z",
            "message": {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "sdb1 Qp55z"}],
            },
        },
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": "t2", "name": "mcp__omi__recall-note", "input": call}
                ],
            },
        },
    ]
    path = tmp_path / "t.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    replay = namehints.replay_transcript(path, d)
    (hint,) = replay.hints
    assert hint.hint.filenames == ["drive-notes-2026.md"]
    assert hint.consulted_later
