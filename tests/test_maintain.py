# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Aaron K. Clark
"""Tests for the sleep-time janitor (omind maintain), item #3."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from omind import consolidate, filelock, maintain, paths
from omind.store import NoteFields, OmiStore


@pytest.fixture
def omi(tmp_path: Path) -> Path:
    d = tmp_path / "OMI"
    d.mkdir()
    store = OmiStore(d)
    store.create_note(NoteFields(title="One", summary="a note about widgets", tags=["x"]))
    store.create_note(NoteFields(title="Two", summary="another note on gadgets", tags=["x"]))
    return d


def test_default_run_is_a_dry_run_that_changes_nothing(omi: Path) -> None:
    before = {p.name for p in omi.glob("*.md")}
    report = maintain.run(omi, log=lambda _msg: None)
    assert report.ran and not report.refused and not report.aborted
    # Consolidation is proposed, never applied; scratch is only *reported* on a
    # dry run; no reindex/rollup/sync step.
    assert [s.name for s in report.steps] == ["propose-consolidations", "expire-scratch"]
    assert {p.name for p in omi.glob("*.md")} == before  # vault untouched
    assert not (omi / "Maintenance Report.md").exists()  # no vault report note


def test_apply_refreshes_the_index(omi: Path) -> None:
    report = maintain.run(omi, apply=True, log=lambda _msg: None)
    names = [s.name for s in report.steps]
    assert "reindex" in names
    assert all(s.ok for s in report.steps)


def test_a_second_janitor_is_refused(omi: Path) -> None:
    # Hold the single-instance mutex, then a run must decline rather than double.
    with filelock.try_exclusive(paths.maintain_lock_path(omi)) as got:
        assert got
        report = maintain.run(omi, log=lambda _msg: None)
    assert not report.ran
    assert "already running" in report.refused


def test_refuses_while_a_vault_write_or_sync_is_in_flight(omi: Path) -> None:
    # Holding the vault write-lock stands in for an in-flight mesh sync.
    with filelock.exclusive(omi / ".omi.lock"):
        report = maintain.run(omi, log=lambda _msg: None)
    assert not report.ran
    assert "in flight" in report.refused


def test_pipeline_is_fail_closed_sync_never_runs_after_a_failure(
    omi: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_args: object, **_kwargs: object) -> list[object]:
        raise RuntimeError("propose exploded")

    monkeypatch.setattr(consolidate, "propose", boom)
    report = maintain.run(omi, sync=True, node_id="node-x", log=lambda _msg: None)
    assert report.aborted
    step_names = [s.name for s in report.steps]
    assert "propose-consolidations" in step_names
    # The fleet-propagating, irreversible sync must not run after the failure.
    assert "mesh-sync" not in step_names


def test_apply_expires_stale_scratch_notes_by_archiving_them(omi: Path) -> None:
    import os

    store = OmiStore(omi)
    stale = store.create_note(NoteFields(title="Old Scratch", summary="temp"), scratch=True)
    fresh = store.create_note(NoteFields(title="New Scratch", summary="temp"), scratch=True)
    assert stale.endswith(".scratch.md") and fresh.endswith(".scratch.md")
    old = time.time() - 8 * 86_400  # past the 7-day TTL, by last-modified time
    os.utime(omi / stale, (old, old))

    report = maintain.run(omi, apply=True, log=lambda _msg: None)
    detail = next(s.detail for s in report.steps if s.name == "expire-scratch")
    assert "1 scratch note(s) archived" in detail
    # Expiry ARCHIVES (soft-delete), never deletes — both files still on disk.
    assert (omi / stale).is_file() and (omi / fresh).is_file()
    assert store.read_fields(stale).disabled  # the stale one is archived
    assert not store.read_fields(fresh).disabled  # the fresh one is untouched


def test_dry_run_only_reports_scratch_expiry_never_archives(omi: Path) -> None:
    import os

    store = OmiStore(omi)
    stale = store.create_note(NoteFields(title="Old Scratch", summary="temp"), scratch=True)
    old = time.time() - 8 * 86_400
    os.utime(omi / stale, (old, old))

    report = maintain.run(omi, log=lambda _msg: None)  # no --apply
    detail = next(s.detail for s in report.steps if s.name == "expire-scratch")
    assert "1 scratch note(s) would expire" in detail
    assert not store.read_fields(stale).disabled  # dry run changed nothing


def test_report_note_is_opt_in(omi: Path) -> None:
    maintain.run(omi, report_note=True, log=lambda _msg: None)
    assert (omi / "Maintenance Report.md").is_file()


def test_run_persists_a_state_file_outside_the_vault(omi: Path) -> None:
    maintain.run(omi, log=lambda _msg: None)
    assert paths.maintain_state_path(omi).is_file()
    assert not (omi / "maintain.json").exists()  # never inside the vault
