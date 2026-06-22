# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Aaron K. Clark
"""Tests for the semantic vector index (omind 3.0.0).

The index mechanics — incremental refresh, ranking, dedup, deletion reaping,
fail-open — are exercised with a deterministic keyword-vector fake encoder (no
numpy / model2vec needed). Real embedding quality is covered by test_embed.py.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from omind import embed, vectorindex

#: A tiny fixed-vocabulary "embedding": a normalised bag-of-words over these terms.
#: Enough for the index to rank a release note above a smoothie note deterministically.
_VOCAB = ["release", "push", "forge", "version", "smoothie", "banana", "auth", "token"]


def _fake_encode(texts: list[str]) -> list[list[float]]:
    rows: list[list[float]] = []
    for text in texts:
        low = text.lower()
        vec = [float(low.count(word)) for word in _VOCAB]
        norm = sum(x * x for x in vec) ** 0.5 or 1.0
        rows.append([x / norm for x in vec])
    return rows


@pytest.fixture(autouse=True)
def _fake_backend(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(embed, "available", lambda: True)
    monkeypatch.setattr(embed, "encode", _fake_encode)
    yield
    embed.reset()


def _note(omi: Path, title: str, summary: str, tags: list[str]) -> Path:
    tagline = " ".join(f"#{t}" for t in tags)
    body = (
        f"# {title}\n\n## Metadata\n- Created: 2026-06-22\n- Tags: {tagline}\n\n"
        f"## Summary\n{summary}\n\n## Details\n"
    )
    path = omi / f"{title}.md"
    path.write_text(body, encoding="utf-8")
    return path


@pytest.fixture
def omi(tmp_path: Path) -> Path:
    d = tmp_path / "OMI"
    d.mkdir()
    return d


def test_refresh_is_incremental(omi: Path) -> None:
    _note(omi, "Release Guide", "how to cut a release and push to the forge", ["release"])
    _note(omi, "Smoothie", "banana smoothie recipe", ["smoothie"])
    idx = vectorindex.VectorIndex(omi)
    assert idx.refresh() == 2  # both embedded
    assert idx.refresh() == 0  # nothing changed -> nothing re-embedded
    _note(omi, "Smoothie", "banana and mango smoothie recipe", ["smoothie"])  # edit metadata
    assert idx.refresh() == 1  # only the changed note re-embeds


def test_rank_orders_by_semantic_similarity(omi: Path) -> None:
    _note(omi, "Release Guide", "how to cut a release and push to the forge version", ["release"])
    _note(omi, "Smoothie", "banana smoothie recipe", ["smoothie"])
    idx = vectorindex.VectorIndex(omi)
    ranked = idx.rank("steps to push a new release version", limit=2)
    assert ranked is not None
    assert ranked[0][0] == "Release Guide.md"  # on-topic note ranks first


def test_deleted_note_is_reaped(omi: Path) -> None:
    _note(omi, "Release Guide", "release push forge", ["release"])
    smoothie = _note(omi, "Smoothie", "banana smoothie", ["smoothie"])
    idx = vectorindex.VectorIndex(omi)
    idx.refresh()
    smoothie.unlink()
    idx.refresh()
    ranked = idx.rank("smoothie", limit=5)
    assert ranked is not None
    assert all(fn != "Smoothie.md" for fn, _ in ranked)


def test_nearest_excludes_the_note_being_written(omi: Path) -> None:
    _note(omi, "Release Guide", "release push forge version", ["release"])
    _note(omi, "Release Notes", "release push forge version", ["release"])
    idx = vectorindex.VectorIndex(omi)
    idx.refresh()
    near = idx.nearest("release push forge version", exclude="Release Guide.md", limit=3)
    assert near is not None
    assert all(fn != "Release Guide.md" for fn, _ in near)
    assert near and near[0][0] == "Release Notes.md"  # the near-duplicate surfaces


def test_fails_open_without_a_backend(omi: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(embed, "available", lambda: False)
    idx = vectorindex.VectorIndex(omi)
    assert idx.refresh() is None
    assert idx.rank("anything") is None
    assert idx.nearest("anything") is None
