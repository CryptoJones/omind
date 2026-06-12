# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Aaron K. Clark
"""Tests for omind.journal: stray-journal migration and weekly rollup."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from omind import hooks, journal
from omind.store import OmiStore, parse_note

_NOW = datetime(2026, 6, 9, 14, 32, 0)


def _daily_text(day: str, bullets: list[str]) -> str:
    name = f"Session Journal {day}.md"
    return hooks.journal_header(name, datetime.fromisoformat(day)) + "\n".join(bullets) + "\n"


def _write_daily(directory: Path, day: str, bullets: list[str]) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"Session Journal {day}.md"
    path.write_text(_daily_text(day, bullets), encoding="utf-8")
    return path


_BULLETS = [
    "- 09:00 [session aaaa1111] PostToolUse Bash -> `make test` (ok)",
    "- 09:01 [session aaaa1111] PostToolUse Edit -> /repo/src/x.py (ok)",
    "- 09:02 [session bbbb2222] PostToolUse Bash -> `make test` (error)",
    "- 09:03 [session bbbb2222] Stop -> turn ended",
]


# -- naming / weeks ------------------------------------------------------------


def test_journal_date_round_trip() -> None:
    assert journal.journal_date("Session Journal 2026-06-09.md") is not None
    assert journal.journal_date("Session Journal Rollup 2026-W24.md") is None
    assert journal.journal_date("Other Note.md") is None


def test_iso_week_label() -> None:
    assert journal.iso_week(_NOW.date()) == "2026-W24"
    assert journal.rollup_name("2026-W24") == "Session Journal Rollup 2026-W24.md"


# -- migration ------------------------------------------------------------------


def test_migrate_moves_root_and_legacy_logs_journals(tmp_path: Path) -> None:
    _write_daily(tmp_path, "2026-06-01", _BULLETS[:1])
    _write_daily(tmp_path / "logs", "2026-06-02", _BULLETS[:2])
    (tmp_path / "Curated Insight.md").write_text(
        "# Curated Insight\n\n## Summary\nkeep me\n", encoding="utf-8"
    )

    moved = journal.migrate_journals(tmp_path)

    assert sorted(moved) == [
        "Session Journal 2026-06-01.md",
        "Session Journal 2026-06-02.md",
    ]
    assert (tmp_path / "Journal" / "Session Journal 2026-06-01.md").is_file()
    assert (tmp_path / "Journal" / "Session Journal 2026-06-02.md").is_file()
    assert not (tmp_path / "Session Journal 2026-06-01.md").exists()
    assert not (tmp_path / "logs" / "Session Journal 2026-06-02.md").exists()
    # index regenerated: journals gone, curated note still listed
    index = (tmp_path / "index.md").read_text(encoding="utf-8")
    assert "Session Journal" not in index
    assert "[[Curated Insight]]" in index


def test_migrate_is_idempotent(tmp_path: Path) -> None:
    _write_daily(tmp_path, "2026-06-01", _BULLETS[:1])
    assert journal.migrate_journals(tmp_path) == ["Session Journal 2026-06-01.md"]
    before = (tmp_path / "Journal" / "Session Journal 2026-06-01.md").read_text(encoding="utf-8")
    assert journal.migrate_journals(tmp_path) == []  # second run: nothing to do
    after = (tmp_path / "Journal" / "Session Journal 2026-06-01.md").read_text(encoding="utf-8")
    assert before == after


def test_migrate_merges_into_existing_relocated_journal(tmp_path: Path) -> None:
    # One session wrote to the root before the layout change, another already
    # wrote to Journal/ for the same day: both trails must survive the move.
    _write_daily(tmp_path, "2026-06-09", _BULLETS[:2])
    _write_daily(tmp_path / "Journal", "2026-06-09", _BULLETS[2:])

    moved = journal.migrate_journals(tmp_path)

    assert moved == ["Session Journal 2026-06-09.md"]
    assert not (tmp_path / "Session Journal 2026-06-09.md").exists()
    text = (tmp_path / "Journal" / "Session Journal 2026-06-09.md").read_text(encoding="utf-8")
    for bullet in _BULLETS:
        assert bullet in text
    assert text.count("# Session Journal 2026-06-09") == 1  # exactly one header


def test_find_stray_journals_ignores_non_journal_notes(tmp_path: Path) -> None:
    (tmp_path / "Session Journal Notes.md").write_text("# not a daily\n", encoding="utf-8")
    _write_daily(tmp_path, "2026-06-01", _BULLETS[:1])
    strays = journal.find_stray_journals(tmp_path)
    assert [p.name for p in strays] == ["Session Journal 2026-06-01.md"]


def test_concurrent_hook_writes_after_migration_share_one_journal(tmp_path: Path) -> None:
    # A root journal exists for today; after migration, hook appends land in the
    # same relocated journal rather than recreating the root file.
    _write_daily(tmp_path, _NOW.date().isoformat(), _BULLETS[:1])
    journal.migrate_journals(tmp_path)
    hooks.append_entry(tmp_path, _BULLETS[1], _NOW)
    hooks.append_entry(tmp_path, _BULLETS[2], _NOW)
    assert not (tmp_path / hooks.journal_name(_NOW)).exists()
    text = (tmp_path / "Journal" / hooks.journal_name(_NOW)).read_text(encoding="utf-8")
    for bullet in _BULLETS[:3]:
        assert bullet in text
    assert text.count("# Session Journal") == 1


# -- rollup ---------------------------------------------------------------------


def test_rollup_week_produces_parseable_summary_and_archives(tmp_path: Path) -> None:
    jdir = tmp_path / "Journal"
    _write_daily(jdir, "2026-06-08", _BULLETS)
    _write_daily(jdir, "2026-06-09", _BULLETS[:2])

    results = journal.rollup_journals(tmp_path, week="2026-W24", now=_NOW)

    assert len(results) == 1
    result = results[0]
    assert result.week == "2026-W24"
    assert result.days == ["2026-06-08", "2026-06-09"]
    assert result.archived == [
        "Session Journal 2026-06-08.md",
        "Session Journal 2026-06-09.md",
    ]
    assert result.deleted == []

    text = (jdir / result.rollup_filename).read_text(encoding="utf-8")
    fields = parse_note(text)  # template-shaped: must parse cleanly
    assert fields.title == "Session Journal Rollup 2026-W24"
    assert set(fields.tags) >= {"session-journal", "omi", "rollup"}
    assert "5 action(s), 1 error(s), 2 session(s), 1 turn end(s)" in fields.summary
    assert "- Bash: 3" in fields.details
    assert "- Edit: 2" in fields.details
    assert "- aaaa1111" in fields.details and "- bbbb2222" in fields.details
    assert "- `make test` (3)" in fields.details  # notable target with count

    # dailies archived out of Journal/, still on disk for wikilink resolution
    assert not (jdir / "Session Journal 2026-06-08.md").exists()
    assert (jdir / "Archive" / "Session Journal 2026-06-08.md").is_file()


def test_rollup_delete_removes_dailies(tmp_path: Path) -> None:
    jdir = tmp_path / "Journal"
    _write_daily(jdir, "2026-06-08", _BULLETS)
    results = journal.rollup_journals(tmp_path, week="2026-W24", delete=True, now=_NOW)
    assert results[0].deleted == ["Session Journal 2026-06-08.md"]
    assert results[0].archived == []
    assert not (jdir / "Session Journal 2026-06-08.md").exists()
    assert not (jdir / "Archive").exists()
    assert (jdir / "Session Journal Rollup 2026-W24.md").is_file()


def test_rollup_default_respects_retention(tmp_path: Path) -> None:
    jdir = tmp_path / "Journal"
    old_day = (_NOW - timedelta(days=45)).date()
    fresh_day = (_NOW - timedelta(days=3)).date()
    _write_daily(jdir, old_day.isoformat(), _BULLETS)
    _write_daily(jdir, fresh_day.isoformat(), _BULLETS)

    results = journal.rollup_journals(tmp_path, now=_NOW)  # default 30-day retention

    assert [r.week for r in results] == [journal.iso_week(old_day)]
    assert not (jdir / f"Session Journal {old_day.isoformat()}.md").exists()
    # fresh daily kept raw, untouched
    assert (jdir / f"Session Journal {fresh_day.isoformat()}.md").is_file()


def test_rollup_nothing_to_do(tmp_path: Path) -> None:
    assert journal.rollup_journals(tmp_path, now=_NOW) == []  # no Journal/ at all
    jdir = tmp_path / "Journal"
    _write_daily(jdir, _NOW.date().isoformat(), _BULLETS)
    assert journal.rollup_journals(tmp_path, now=_NOW) == []  # all within retention


def test_rollup_is_idempotent_per_week(tmp_path: Path) -> None:
    jdir = tmp_path / "Journal"
    _write_daily(jdir, "2026-06-08", _BULLETS)
    assert len(journal.rollup_journals(tmp_path, week="2026-W24", now=_NOW)) == 1
    # dailies already archived: nothing left to roll up for that week
    assert journal.rollup_journals(tmp_path, week="2026-W24", now=_NOW) == []


def test_rollup_note_stays_out_of_listings(tmp_path: Path) -> None:
    _write_daily(tmp_path / "Journal", "2026-06-08", _BULLETS)
    journal.rollup_journals(tmp_path, week="2026-W24", now=_NOW)
    store = OmiStore(tmp_path)
    assert store.list_notes() == []  # rollup lives in Journal/, never indexed


def test_rollup_late_daily_keeps_archived_days(tmp_path: Path) -> None:
    """Re-rolling a week after its dailies were archived must not shrink the aggregate."""
    journal_dir = tmp_path / "Journal"
    _write_daily(journal_dir, "2026-06-01", _BULLETS)
    _write_daily(journal_dir, "2026-06-02", _BULLETS)
    journal.rollup_journals(tmp_path, week="2026-W23")
    # A late daily for the already-rolled-up week arrives (e.g. from a peer).
    _write_daily(journal_dir, "2026-06-03", _BULLETS)
    journal.rollup_journals(tmp_path, week="2026-W23")
    text = (journal_dir / journal.rollup_name("2026-W23")).read_text(encoding="utf-8")
    assert "2026-06-01" in text and "2026-06-02" in text and "2026-06-03" in text
