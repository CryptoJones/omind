# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Aaron K. Clark
"""Tests for #390 (epic #384): a name whose facts changed shows its dated
history, superseded and corrected notes marked instead of suppressed."""

from __future__ import annotations

import os
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
    assert f"2026-09-25 [[{SAMSUNG}]] (superseded)" in line
    assert f"2026-09-25 [[{SEAGATE}]] (has a correction)" in line
    assert f"2026-09-27 [[{WD_BLUE}]] (replaces an older note, newest)" in line
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
    assert all(len(e.render()) < timeline.TITLE_CHARS + 60 for e in found.entries)


def test_as_of_hides_later_notes(omi: Path) -> None:
    (found,) = timeline.for_names(omi, ["As30p"], as_of="2026-09-25")
    assert [e.title for e in found.entries] == [SEAGATE, SAMSUNG]


def test_no_index_is_no_timeline(tmp_path: Path) -> None:
    d = tmp_path / "OMI"
    d.mkdir()
    _note(d, SEAGATE, "As30p\n\nCORRECTION: adapter.")
    assert timeline.for_names(d, ["As30p"]) == []


def test_line_drops_oldest_entries_to_fit(monkeypatch: pytest.MonkeyPatch) -> None:
    # Six full-length entries fit the shipped cap (~830 chars), so tighten it.
    monkeypatch.setattr(timeline, "MAX_CHARS", 500)
    entries = [
        timeline.Entry(filename=f"{i}.md", title="t" * 200, date=f"2026-09-0{i}", mark="")
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
