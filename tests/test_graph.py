# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Aaron K. Clark
"""Tests for omind.graph: wikilink resolution, traversal, orphans, export."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from omind import graph
from omind.store import NoteFields, OmiStore


@pytest.fixture
def store(tmp_path: Path) -> OmiStore:
    omi = tmp_path / "OMI"
    omi.mkdir()
    return OmiStore(omi)


def _chain(store: OmiStore) -> None:
    """A -> B -> C, plus D linking A, plus an Island with no links and a note
    with a broken link to a note that does not exist."""
    store.create_note(NoteFields(title="A", summary="start", connections=["B"]))
    store.create_note(NoteFields(title="B", summary="mid", connections=["C"]))
    store.create_note(NoteFields(title="C", summary="end"))
    store.create_note(NoteFields(title="D", summary="see [[A]] and [[Ghost]]"))
    store.create_note(NoteFields(title="Island", summary="alone"))


def test_build_graph_resolves_edges_both_ways(store: OmiStore) -> None:
    _chain(store)
    g = graph.build_graph(store.omi_dir)
    assert set(g.nodes) == {"A.md", "B.md", "C.md", "D.md", "Island.md"}
    assert g.nodes["A.md"].out == {"B.md"}
    assert g.nodes["B.md"].inn == {"A.md"}
    assert g.nodes["A.md"].inn == {"D.md"}  # D -> A


def test_dangling_links_are_collected(store: OmiStore) -> None:
    _chain(store)
    g = graph.build_graph(store.omi_dir)
    assert graph.dangling_links(g) == [("D.md", "Ghost")]


def test_orphans_are_fully_disconnected_only(store: OmiStore) -> None:
    _chain(store)
    g = graph.build_graph(store.omi_dir)
    # C is a leaf but linked-to, so not an orphan; Island has no links at all.
    assert graph.orphans(g) == ["Island.md"]


def test_neighbors_respects_depth_and_direction(store: OmiStore) -> None:
    _chain(store)
    g = graph.build_graph(store.omi_dir)
    assert graph.neighbors(g, "A", depth=1, direction="out") == [("B.md", 1)]
    assert graph.neighbors(g, "A", depth=2, direction="out") == [("B.md", 1), ("C.md", 2)]
    # Undirected: D links A, so it is one hop away in "both".
    assert ("D.md", 1) in graph.neighbors(g, "A", depth=1, direction="both")
    assert graph.neighbors(g, "A", depth=1, direction="in") == [("D.md", 1)]


def test_neighbors_accepts_filename_and_title(store: OmiStore) -> None:
    _chain(store)
    g = graph.build_graph(store.omi_dir)
    assert graph.neighbors(g, "A.md", direction="out") == graph.neighbors(g, "A", direction="out")


def test_shortest_path_follows_the_chain(store: OmiStore) -> None:
    _chain(store)
    g = graph.build_graph(store.omi_dir)
    assert graph.shortest_path(g, "A", "C") == ["A.md", "B.md", "C.md"]
    assert graph.shortest_path(g, "A", "A") == ["A.md"]


def test_shortest_path_none_when_unreachable(store: OmiStore) -> None:
    _chain(store)
    g = graph.build_graph(store.omi_dir)
    assert graph.shortest_path(g, "A", "Island") is None
    # Directed: C cannot reach A by following outbound edges.
    assert graph.shortest_path(g, "C", "A", direction="out") is None


def test_resolve_unknown_note_raises(store: OmiStore) -> None:
    _chain(store)
    g = graph.build_graph(store.omi_dir)
    with pytest.raises(ValueError, match="not found"):
        graph.neighbors(g, "Nonexistent")


def test_self_link_is_not_an_edge(store: OmiStore) -> None:
    store.create_note(NoteFields(title="Selfie", summary="I am [[Selfie]]"))
    g = graph.build_graph(store.omi_dir)
    assert g.nodes["Selfie.md"].out == set()
    assert graph.orphans(g) == ["Selfie.md"]


def test_stats_counts(store: OmiStore) -> None:
    _chain(store)
    g = graph.build_graph(store.omi_dir)
    assert graph.stats(g) == {"notes": 5, "links": 3, "orphans": 1, "dangling": 1}


def test_to_json_shape(store: OmiStore) -> None:
    _chain(store)
    g = graph.build_graph(store.omi_dir)
    data = graph.to_json(g)
    ids = [n["id"] for n in data["nodes"]]  # type: ignore[index]
    assert ids == sorted(ids)
    assert ["A.md", "B.md"] in data["edges"]  # type: ignore[operator]
    assert ["D.md", "Ghost"] in data["dangling"]  # type: ignore[operator]
    # Round-trips through json without error.
    json.dumps(data)


def test_to_dot_is_renderable_text(store: OmiStore) -> None:
    _chain(store)
    g = graph.build_graph(store.omi_dir)
    dot = graph.to_dot(g)
    assert dot.startswith("digraph omi {")
    assert dot.rstrip().endswith("}")
    assert '"A.md" -> "B.md";' in dot


def test_empty_vault_is_an_empty_graph(tmp_path: Path) -> None:
    g = graph.build_graph(tmp_path / "OMI")  # directory does not exist
    assert g.nodes == {}
    assert graph.stats(g) == {"notes": 0, "links": 0, "orphans": 0, "dangling": 0}
