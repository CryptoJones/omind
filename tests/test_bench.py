# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Aaron K. Clark
"""Tests for `omind bench` — the retrieval performance report.

The point of these is that the report *runs and measures the right things* on
any vault; the absolute numbers are hardware-dependent, so nothing here asserts
a latency. The one behavioural claim worth pinning is that the index answers a
query the pre-index substring scan could not.
"""

from __future__ import annotations

from pathlib import Path

from omind import bench
from omind.store import NoteFields, OmiStore


def _vault(tmp_path: Path) -> Path:
    omi = tmp_path / "OMI"
    store = OmiStore(omi)
    store.create_note(
        NoteFields(
            title="Signing Runbook",
            summary="what to do when a release fails to sign",
            details="Re-run the notarization step with a fresh Developer ID cert.",
            tags=["release"],
        )
    )
    store.create_note(NoteFields(title="Nebraska", summary="Go Big Red", tags=["fun"]))
    return omi


def test_report_covers_latency_and_tokens(tmp_path: Path) -> None:
    report = bench.run(_vault(tmp_path), queries=("nebraska",))
    names = [m.name for m in report.measurements]
    assert "notes in vault" in names
    assert "index build (from scratch)" in names
    assert "index refresh (no changes)" in names
    assert any(name.startswith("search ") for name in names)
    assert "SessionStart capsule" in names
    assert "list-notes, one page" in names
    units = {m.unit for m in report.measurements}
    assert {"ms", "tokens", "count"} <= units


def test_paging_is_reported_as_a_token_saving(tmp_path: Path) -> None:
    report = bench.run(_vault(tmp_path), queries=("nebraska",))
    by_name = {m.name: m.value for m in report.measurements}
    assert by_name["list-notes, one page"] <= by_name["list-notes, unpaged (was)"]


def test_the_index_answers_what_the_scan_could_not(tmp_path: Path) -> None:
    """A query whose words are spread across a note's sections: the substring
    scan needs the literal phrase and finds nothing; the index ranks it first."""
    omi = _vault(tmp_path)
    store = OmiStore(omi)
    query = "release signing failure"
    assert store._scan_search(query) == []
    assert [s.filename for s in store.search(query)][:1] == ["Signing Runbook.md"]


def test_report_serialises_to_json_and_text(tmp_path: Path) -> None:
    report = bench.run(_vault(tmp_path), queries=("nebraska",))
    assert report.to_dict()["vault"].endswith("OMI")
    assert "omind bench" in report.format()


def test_quality_report_calculates_recall_and_mrr(tmp_path: Path) -> None:
    omi = _vault(tmp_path)
    report = bench.run_quality(
        omi,
        cases=(
            ("release signing failure", "Signing Runbook.md"),
            ("nebraska", "Nebraska.md"),
            ("missing target is skipped", "Absent.md"),
        ),
    )
    by_name = {measurement.name: measurement for measurement in report.measurements}
    assert by_name["quality cases"].value == 2
    assert by_name["quality cases"].detail == "1 skipped"
    assert by_name["recall@1"].value == 100.0
    assert by_name["recall@5"].value == 100.0
    assert by_name["MRR"].value == 1.0


def test_quality_report_handles_no_matching_labelled_notes(tmp_path: Path) -> None:
    report = bench.run_quality(
        _vault(tmp_path),
        cases=(("unknown", "Absent.md"),),
    )
    by_name = {measurement.name: measurement for measurement in report.measurements}
    assert by_name["quality cases"].value == 0
    assert by_name["MRR"].value == 0
