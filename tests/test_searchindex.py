# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Aaron K. Clark
"""Tests for the derived hybrid search index (omind 5.0.0).

Index mechanics — chunking, incremental refresh, BM25 relevance, RRF fusion with
the vector leg, dedup, deletion reaping, fail-open — are exercised with a
deterministic fixed-vocabulary fake encoder, so no model2vec download is needed.
Real embedding quality is covered by test_embed.py.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from omind import embed, searchindex

#: A tiny fixed-vocabulary "embedding": a normalised bag-of-words over these terms.
_VOCAB = ["release", "push", "forge", "version", "smoothie", "banana", "auth", "token"]


def _fake_encode(texts: list[str]) -> object:
    import numpy as np

    rows: list[list[float]] = []
    for text in texts:
        low = text.lower()
        vec = [float(low.count(word)) for word in _VOCAB]
        norm = sum(x * x for x in vec) ** 0.5 or 1.0
        rows.append([x / norm for x in vec])
    return np.asarray(rows, dtype="float32")


@pytest.fixture
def semantic(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Turn the vector leg on with the deterministic fake encoder.

    Skipped without numpy: the vector leg is part of the optional ``[embed]``
    extra, and the keyword index must (and does) work without it.
    """
    pytest.importorskip("numpy")
    monkeypatch.setattr(embed, "available", lambda: True)
    monkeypatch.setattr(embed, "encode", _fake_encode)
    yield None
    embed.reset()


def _note(
    omi: Path,
    title: str,
    summary: str,
    tags: list[str],
    *,
    created: str = "2026-06-22",
    details: str = "",
) -> Path:
    tagline = " ".join(f"#{t}" for t in tags)
    body = (
        f"# {title}\n\n## Metadata\n- Created: {created}\n- Tags: {tagline}\n\n"
        f"## Summary\n{summary}\n\n## Details\n{details}\n"
    )
    path = omi / f"{title}.md"
    path.write_text(body, encoding="utf-8")
    return path


@pytest.fixture
def omi(tmp_path: Path) -> Path:
    d = tmp_path / "OMI"
    d.mkdir()
    return d


# -- ingest ----------------------------------------------------------------


def test_refresh_is_incremental(omi: Path) -> None:
    _note(omi, "Release Guide", "how to cut a release", ["release"])
    _note(omi, "Smoothie", "banana smoothie recipe", ["smoothie"])
    idx = searchindex.SearchIndex(omi)
    first = idx.refresh()
    assert first is not None and first.reindexed == 2
    again = idx.refresh()
    assert again is not None and again.reindexed == 0  # nothing changed
    _note(omi, "Smoothie", "banana and mango smoothie", ["smoothie"])
    third = idx.refresh()
    assert third is not None and third.reindexed == 1  # only the edited note


def test_query_burst_throttles_refresh_but_signal_invalidates(
    omi: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _note(omi, "Release Guide", "how to cut a release", ["release"])
    idx = searchindex.SearchIndex(omi)
    original = idx.refresh
    calls = 0

    def counted_refresh(*, vectors: bool = True) -> searchindex.Refresh | None:
        nonlocal calls
        calls += 1
        return original(vectors=vectors)

    monkeypatch.setattr(idx, "refresh", counted_refresh)
    assert idx.search("release")
    assert idx.search("release")
    assert calls == 1

    signal = searchindex.paths.sync_signal_path(omi)
    signal.parent.mkdir(parents=True, exist_ok=True)
    signal.touch()
    assert idx.search("release")
    assert calls == 2


def test_refresh_throttle_expires_for_external_edits(
    omi: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    note = _note(omi, "Release Guide", "how to cut a release", ["release"])
    idx = searchindex.SearchIndex(omi)
    now = [100.0]
    monkeypatch.setattr(searchindex.time, "monotonic", lambda: now[0])

    assert idx.search("release")
    note.write_text(note.read_text(encoding="utf-8") + "\nexternal edit", encoding="utf-8")
    assert not idx.search("external")  # still inside the bounded burst window
    now[0] += searchindex._REFRESH_THROTTLE_SECONDS
    assert idx.search("external")


def test_touch_without_a_content_change_does_not_reindex(omi: Path) -> None:
    """An mtime bump with identical bytes (a mesh sync, a `touch`) is not a change."""
    path = _note(omi, "Stable", "unchanged text", ["x"])
    idx = searchindex.SearchIndex(omi)
    assert idx.refresh() is not None
    text = path.read_text(encoding="utf-8")
    path.write_text(text + " ", encoding="utf-8")  # different bytes -> reindex
    changed = idx.refresh()
    assert changed is not None and changed.reindexed == 1
    path.write_text(text + " ", encoding="utf-8")  # same bytes, new mtime
    same = idx.refresh()
    assert same is not None and same.reindexed == 0


def test_deleted_note_is_reaped(omi: Path) -> None:
    _note(omi, "Release Guide", "release notes", ["release"])
    smoothie = _note(omi, "Smoothie", "banana smoothie", ["smoothie"])
    idx = searchindex.SearchIndex(omi)
    idx.refresh()
    smoothie.unlink()
    reaped = idx.refresh()
    assert reaped is not None and reaped.removed == 1
    assert [h.filename for h in idx.search("smoothie") or []] == []


def test_chunking_splits_at_headings_and_caps_length(omi: Path) -> None:
    long_body = "\n\n".join(["paragraph " + "x" * 300] * 8)
    md = f"# Big\n\n## Summary\nshort\n\n## Details\n{long_body}\n"
    chunks = searchindex.chunk_note(md, "Big")
    assert chunks[0].heading == ""  # identity chunk is always first
    assert {c.heading for c in chunks} == {"", "Summary", "Details"}
    assert all(len(c.text) <= searchindex._MAX_CHUNK_CHARS for c in chunks)
    assert sum(1 for c in chunks if c.heading == "Details") > 1  # split, not truncated


def test_details_only_facts_are_findable(omi: Path) -> None:
    """The old metadata-only vector index could never retrieve these."""
    _note(omi, "Prefs", "accessibility", ["a11y"], details="Aaron is red-green colorblind.")
    idx = searchindex.SearchIndex(omi)
    hits = idx.search("colorblind") or []
    assert [h.filename for h in hits] == ["Prefs.md"]
    assert "colorblind" in hits[0].excerpt.lower()


def test_wikilinks_are_indexed_for_backlinks(omi: Path) -> None:
    _note(omi, "Hub", "the hub", ["x"])
    _note(omi, "Spoke", "points at [[Hub]]", ["x"])
    idx = searchindex.SearchIndex(omi)
    assert idx.backlinks("Hub.md", title="Hub") == ["Spoke.md"]
    assert idx.backlinks("Spoke.md", title="Spoke") == []


# -- ranking ---------------------------------------------------------------


def test_relevance_beats_recency(omi: Path) -> None:
    """The defect this index exists to fix: hits used to come back newest-first."""
    _note(omi, "On Topic", "kubernetes ingress troubleshooting", ["ops"], created="2020-01-01")
    _note(omi, "Off Topic", "sourdough starter notes", ["food"], created="2026-07-01")
    idx = searchindex.SearchIndex(omi)
    hits = idx.search("kubernetes ingress") or []
    assert [h.filename for h in hits] == ["On Topic.md"]


def test_recency_only_reorders_matches_it_never_adds_notes(omi: Path) -> None:
    for number in range(5):
        _note(omi, f"Filler {number}", "unrelated filler", ["f"], created=f"2026-07-0{number + 1}")
    _note(omi, "Target", "the specific thing", ["t"], created="2019-01-01")
    idx = searchindex.SearchIndex(omi)
    assert [h.filename for h in idx.search("specific") or []] == ["Target.md"]


def test_stemmed_query_matches_inflected_text(omi: Path) -> None:
    _note(omi, "Scoring", "how the gate scores a note", ["gate"])
    idx = searchindex.SearchIndex(omi)
    assert [h.filename for h in idx.search("scored") or []] == ["Scoring.md"]


def test_empty_query_with_a_tag_lists_that_tag_newest_first(omi: Path) -> None:
    _note(omi, "Older", "a", ["pets"], created="2026-01-01")
    _note(omi, "Newer", "b", ["pets"], created="2026-06-01")
    _note(omi, "Other", "c", ["work"])
    idx = searchindex.SearchIndex(omi)
    assert [h.filename for h in idx.search("", tag="pets") or []] == ["Newer.md", "Older.md"]


def test_archived_notes_are_hidden_unless_asked_for(omi: Path) -> None:
    path = _note(omi, "Gone", "archived content", ["x"])
    path.write_text(
        path.read_text(encoding="utf-8").replace("- Tags:", "- Disabled: true\n- Tags:"),
        encoding="utf-8",
    )
    idx = searchindex.SearchIndex(omi)
    assert idx.search("archived") == []
    assert [h.filename for h in idx.search("archived", include_disabled=True) or []] == ["Gone.md"]


def test_credential_notes_are_deprioritised_for_unrelated_tasks(omi: Path) -> None:
    _note(omi, "Forge Token", "the api token password for the forge", ["auth"])
    _note(omi, "Forge Guide", "how to push to the forge", ["ops"])
    idx = searchindex.SearchIndex(omi)
    hits = idx.search("push to the forge") or []
    assert hits[0].filename == "Forge Guide.md"


def test_generated_worklogs_rank_below_equivalent_curated_notes(omi: Path) -> None:
    detail = "zebracorn deployment rollback procedure"
    _note(omi, "Handbook", "curated operations", ["ops"], details=detail)
    _note(omi, "Worklog 2026-07-27", "automatic activity", ["worklog"], details=detail)
    hits = searchindex.SearchIndex(omi).search("zebracorn rollback") or []
    by_name = {hit.filename: hit for hit in hits}
    assert set(by_name) == {"Handbook.md", "Worklog 2026-07-27.md"}
    assert by_name["Handbook.md"].score > by_name["Worklog 2026-07-27.md"].score
    assert hits[0].filename == "Handbook.md"


def test_superseded_notes_remain_searchable_but_rank_lower(omi: Path) -> None:
    detail = "zebracorn release status"
    _note(omi, "Release v1", "old release", ["release"], details=detail)
    current = _note(omi, "Release v2", "current release", ["release"], details=detail)
    current.write_text(
        current.read_text(encoding="utf-8").replace(
            "- Tags: #release",
            "- Tags: #release\n- Supersedes: [[Release v1]]",
        ),
        encoding="utf-8",
    )
    hits = searchindex.SearchIndex(omi).search("zebracorn status") or []
    by_name = {hit.filename: hit for hit in hits}
    assert set(by_name) == {"Release v1.md", "Release v2.md"}
    assert by_name["Release v2.md"].score > by_name["Release v1.md"].score
    assert hits[0].filename == "Release v2.md"


def test_query_punctuation_cannot_break_the_match_expression(omi: Path) -> None:
    """FTS5 operators in user text are data, not syntax (every term is quoted)."""
    _note(omi, "Quoted", "handling NEAR and OR in queries", ["x"])
    idx = searchindex.SearchIndex(omi)
    for query in ['NEAR(a b)', 'OR AND NOT', '"unterminated', "co-lon: x*", "()"]:
        assert idx.search(query) is not None  # no OperationalError, no None


def test_excerpt_is_bounded(omi: Path) -> None:
    _note(omi, "Wordy", "x", ["x"], details="haystack " * 500 + " needle")
    idx = searchindex.SearchIndex(omi)
    hits = idx.search("needle") or []
    assert hits and len(hits[0].excerpt) <= 120


def test_query_complexity_adapts_result_and_excerpt_budgets(omi: Path) -> None:
    detail = "needle alpha beta gamma relationship " + "context " * 100
    for number in range(12):
        _note(omi, f"Item {number}", "matching record", ["x"], details=detail)
    idx = searchindex.SearchIndex(omi)
    simple = idx.search("needle", limit=50) or []
    complex_hits = idx.search(
        "why needle alpha beta gamma relationship matters",
        limit=50,
    ) or []
    assert len(simple) == 5
    assert len(complex_hits) == 12
    assert all(len(hit.excerpt) <= 120 for hit in simple)
    assert any(len(hit.excerpt) > 180 for hit in complex_hits)
    assert all(len(hit.excerpt) <= 240 for hit in complex_hits)


def test_identity_hit_shows_the_summary_not_the_title_echo(omi: Path) -> None:
    _note(omi, "Nebraska", "Go Big Red and the corn", ["fun"])
    hits = searchindex.SearchIndex(omi).search("Nebraska") or []
    assert hits[0].excerpt == "Go Big Red and the corn"


# -- vector leg ------------------------------------------------------------


def test_vector_leg_finds_a_paraphrase_with_no_shared_term(
    omi: Path, semantic: None
) -> None:
    _note(omi, "Release Guide", "release push forge version", ["release"])
    _note(omi, "Smoothie", "banana smoothie", ["smoothie"])
    idx = searchindex.SearchIndex(omi)
    built = idx.refresh()
    assert built is not None and built.embedded > 0
    hits = idx.search("push release version forge") or []
    assert hits[0].filename == "Release Guide.md"
    assert hits[0].vector_rank > 0  # the semantic leg actually contributed


def test_vectors_are_quantized_to_int8_with_scale_and_residual(
    omi: Path, semantic: None
) -> None:
    _note(omi, "Release Guide", "release push forge version", ["release"])
    idx = searchindex.SearchIndex(omi)
    assert idx.refresh() is not None
    db = idx._connect()
    assert db is not None
    row = db.execute("SELECT vec, scale, residual FROM vectors LIMIT 1").fetchone()
    assert row is not None
    assert len(row["vec"]) == len(_VOCAB)
    assert float(row["scale"]) > 0
    assert float(row["residual"]) >= 0


def test_duplicate_pairs_use_mean_chunk_vector_similarity(
    omi: Path, semantic: None
) -> None:
    _note(omi, "First", "release push forge version", ["release"])
    _note(omi, "Second", "release push forge version", ["release"])
    _note(omi, "Different", "banana smoothie", ["food"])
    pairs = searchindex.SearchIndex(omi).duplicate_pairs(threshold=0.99)
    assert pairs is not None
    assert [(left, right) for left, right, _score in pairs] == [
        ("First.md", "Second.md")
    ]


def test_reranker_scores_the_whole_chunk_body(omi: Path, semantic: None) -> None:
    _note(omi, "Strong", "release push forge version", ["x"])
    _note(omi, "Weak", "release", ["x"])
    hits = searchindex.SearchIndex(omi).search("release push") or []
    assert [hit.filename for hit in hits[:2]] == ["Strong.md", "Weak.md"]
    assert hits[0].score > hits[1].score


def test_nearest_excludes_the_note_being_written(omi: Path, semantic: None) -> None:
    _note(omi, "Release Guide", "release push forge version", ["release"])
    _note(omi, "Release Notes", "release push forge version", ["release"])
    idx = searchindex.SearchIndex(omi)
    idx.refresh()
    near = idx.nearest("release push forge version", exclude="Release Guide.md", limit=3)
    assert near is not None
    assert all(fn != "Release Guide.md" for fn, _ in near)
    assert near[0][0] == "Release Notes.md"  # the near-duplicate surfaces


def test_nearest_is_none_without_a_backend(omi: Path) -> None:
    _note(omi, "Solo", "content", ["x"])
    assert searchindex.SearchIndex(omi).nearest("content") is None


def test_a_changed_model_invalidates_every_vector(omi: Path, semantic: None) -> None:
    _note(omi, "Release Guide", "release push forge", ["release"])
    searchindex.SearchIndex(omi).refresh()
    other = searchindex.SearchIndex(omi, model="some/other-model")
    rebuilt = other.refresh()
    assert rebuilt is not None and rebuilt.reindexed == 1  # wiped and rebuilt


# -- fail-open -------------------------------------------------------------


def test_disable_env_turns_the_index_off(omi: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(searchindex.DISABLE_ENV, "1")
    idx = searchindex.SearchIndex(omi)
    assert searchindex.available() is False
    assert idx.refresh() is None
    assert idx.search("anything") is None
    assert idx.backlinks("x.md") is None
    assert searchindex.shared(omi) is None


def test_a_corrupt_index_file_does_not_raise(omi: Path) -> None:
    _note(omi, "Fine", "content here", ["x"])
    idx = searchindex.SearchIndex(omi)
    idx.refresh()
    idx.close()
    path = searchindex.index_path(omi)
    path.write_bytes(b"this is not a database")
    broken = searchindex.SearchIndex(omi)
    assert broken.search("content") is None  # fails open; caller scans instead


def test_health_reports_size_age_counts_and_staleness(omi: Path) -> None:
    note = _note(omi, "Fine", "content here", ["x"])
    idx = searchindex.SearchIndex(omi)
    assert idx.refresh() is not None
    idx.close()

    healthy = searchindex.health(omi)
    assert healthy.fts5 is True
    assert healthy.exists is True
    assert healthy.size_bytes > 0
    assert healthy.age_seconds is not None
    assert healthy.notes == 1
    assert healthy.stale == 0
    assert healthy.corrupt == ""

    note.write_text(note.read_text(encoding="utf-8") + "\nchanged\n", encoding="utf-8")
    assert searchindex.health(omi).stale == 1


def test_health_explains_a_corrupt_index(omi: Path) -> None:
    path = searchindex.index_path(omi)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"this is not a database")

    result = searchindex.health(omi)
    assert result.exists is True
    assert result.corrupt


def test_store_search_falls_back_when_the_index_is_off(
    omi: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omind.store import OmiStore

    _note(omi, "Fallback", "substring findable", ["x"])
    monkeypatch.setenv(searchindex.DISABLE_ENV, "1")
    store = OmiStore(omi)
    assert store.index() is None
    assert [s.filename for s in store.search("substring")] == ["Fallback.md"]


def test_shared_returns_one_instance_per_vault(omi: Path) -> None:
    assert searchindex.shared(omi) is searchindex.shared(omi)


def test_stats_reports_what_is_indexed(omi: Path) -> None:
    _note(omi, "One", "first", ["x"])
    idx = searchindex.SearchIndex(omi)
    idx.refresh()
    stats = idx.stats()
    assert stats is not None
    assert stats["notes"] == 1 and int(str(stats["chunks"])) >= 1
