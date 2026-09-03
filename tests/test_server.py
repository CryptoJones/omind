# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Aaron K. Clark
"""Tests for omind.server: the `omind node` mesh-node MCP server.

In-process tests drive MCPServer's tool layer directly; one subprocess smoke
test does a real stdio handshake and asserts the clean-exit-on-EOF contract
(the regression test for the obsidian-mcp hang class, issue #49).
"""

from __future__ import annotations

import asyncio
import json
import queue
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

import pytest
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from omind import server as server_module
from omind.paths import sync_signal_path
from omind.server import build_server

EXPECTED_TOOLS = {
    "read-note",
    "recall-note",
    "create-note",
    "edit-note",
    "search-vault",
    "help",
    "list-notes",
    "delete-note",
    "restore-note",
    "backlinks",
    "list-tags",
    "graph-neighbors",
    "graph",
}


@pytest.fixture
def omi_dir(tmp_path: Path) -> Path:
    omi = tmp_path / "OMI"
    omi.mkdir()
    return omi


@pytest.fixture
def server(omi_dir: Path) -> MCPServer:
    return build_server(omi_dir, node_id="testnode-abc123")


def call(server: MCPServer, name: str, args: dict[str, Any]) -> Any:
    """Invoke a tool in-process and return its structured result.

    v2's ``call_tool`` returns a ``CallToolResult`` rather than v1's
    ``(content, structured)`` tuple.
    """
    return asyncio.run(server.call_tool(name, args)).structured_content


def test_exposes_exactly_the_designed_tools(server: MCPServer) -> None:
    tools = asyncio.run(server.list_tools())
    assert {t.name for t in tools} == EXPECTED_TOOLS
    assert all(t.description for t in tools)


def test_create_read_round_trip(server: MCPServer, omi_dir: Path) -> None:
    created = call(
        server,
        "create-note",
        {
            "title": "Server Note",
            "summary": "made via mcp",
            "details": "body",
            "tags": ["omi", "mesh"],
            "connections": ["Other Note"],
            "action_items": ["[x] done thing", "open thing"],
            "references": ["Source: test"],
        },
    )
    assert created == {"filename": "Server Note.md", "agent": ""}  # no identity resolved
    assert (omi_dir / "Server Note.md").is_file()

    got = call(server, "read-note", {"name": "Server Note.md"})
    assert got["fields"]["title"] == "Server Note"
    assert got["fields"]["tags"] == ["omi", "mesh"]
    assert got["fields"]["action_items"] == [
        {"text": "done thing", "done": True},
        {"text": "open thing", "done": False},
    ]
    assert got["fields"]["rev"] == "1@testnode-abc123"  # node stamps Lamport revs
    assert got["version"]
    assert "raw" not in got  # ONE representation per call, never the body twice
    raw = call(server, "read-note", {"name": "Server Note.md", "representation": "raw"})
    assert "[[Other Note]]" in raw["raw"]
    assert "fields" not in raw


def test_edit_note_partial_update(server: MCPServer) -> None:
    call(server, "create-note", {"title": "Partial", "summary": "old", "tags": ["keep"]})
    edited = call(server, "edit-note", {"name": "Partial.md", "summary": "new"})
    assert edited["filename"] == "Partial.md"
    assert edited["concurrency"] == "unverified"  # no expected_version → flagged
    got = call(server, "read-note", {"name": "Partial.md"})
    assert got["fields"]["summary"] == "new"
    assert got["fields"]["tags"] == ["keep"]  # omitted fields untouched


def test_edit_note_with_version_is_verified(server: MCPServer) -> None:
    call(server, "create-note", {"title": "Verified", "summary": "v1"})
    version = call(server, "read-note", {"name": "Verified.md"})["version"]
    edited = call(
        server, "edit-note", {"name": "Verified.md", "summary": "v2", "expected_version": version}
    )
    assert "concurrency" not in edited  # the token was honored


def test_create_note_warns_on_near_duplicate(
    server: MCPServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Roundtable-ratified: advisory near-duplicate warnings at write time —
    fail-open, never blocking, no supersedes/conflicts hint (cosine cannot
    distinguish replace from disagree), sub-threshold and superseded
    candidates filtered."""
    call(server, "create-note", {"title": "Original", "summary": "the one true fact"})
    monkeypatch.setattr(
        server_module.searchindex,
        "shared",
        lambda omi_dir: type(
            "_Ix",
            (),
            {
                "nearest": lambda self, text, exclude=None, limit=3: [
                    ("Original.md", 0.93),  # above threshold
                    ("Unrelated.md", 0.40),  # below threshold — filtered
                ]
            },
        )(),
    )
    got = call(server, "create-note", {"title": "Original Again", "summary": "the one true fact"})
    assert got["filename"] == "Original Again.md"  # the write ALWAYS succeeds
    assert [d["filename"] for d in got["near_duplicates"]] == ["Original.md"]
    assert "supersedes" in got["near_duplicates_note"]


def test_create_note_survives_a_broken_dedup_probe(
    server: MCPServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        server_module.searchindex,
        "shared",
        lambda omi_dir: (_ for _ in ()).throw(RuntimeError("index exploded")),
    )
    got = call(server, "create-note", {"title": "Doomed Probe", "summary": "s"})
    assert got["filename"] == "Doomed Probe.md"  # fail-open: no warning, write fine
    assert "near_duplicates" not in got


def test_read_note_body_is_bounded(server: MCPServer) -> None:
    """One huge note must not become one unbounded tool result — the read-note
    hole in invariant 8 (2026-08-27 review)."""
    call(server, "create-note", {"title": "Huge", "summary": "s", "details": "x" * 30000})
    got = call(server, "read-note", {"name": "Huge.md", "representation": "raw"})
    raw = str(got["raw"])
    assert len(raw) < 21000  # default cap 20000 + the marker
    assert "truncated" in raw
    bigger = call(
        server,
        "read-note",
        {"name": "Huge.md", "representation": "raw", "max_chars": 65536},
    )
    assert "truncated" not in str(bigger["raw"])  # raisable within the hard cap
    fields = call(server, "read-note", {"name": "Huge.md", "max_chars": 100})
    assert "truncated" in str(fields["fields"]["details"])


def test_edit_note_version_conflict(server: MCPServer) -> None:
    call(server, "create-note", {"title": "Versioned", "summary": "v1"})
    stale = call(server, "read-note", {"name": "Versioned.md"})["version"]
    call(server, "edit-note", {"name": "Versioned.md", "summary": "v2"})
    with pytest.raises(ToolError, match="changed on disk"):
        call(
            server,
            "edit-note",
            {"name": "Versioned.md", "summary": "v3", "expected_version": stale},
        )


def test_delete_archives_and_restore(server: MCPServer, omi_dir: Path) -> None:
    call(server, "create-note", {"title": "Archived", "summary": "s"})
    deleted = call(server, "delete-note", {"name": "Archived.md"})
    assert deleted == {"filename": "Archived.md", "status": "archived"}
    assert (omi_dir / "Archived.md").is_file()  # soft delete, file stays

    names = [n["filename"] for n in call(server, "list-notes", {})["result"]]
    assert "Archived.md" not in names
    shown = call(server, "list-notes", {"include_archived": True})["result"]
    assert any(n["filename"] == "Archived.md" and n["disabled"] for n in shown)

    restored = call(server, "restore-note", {"name": "Archived.md"})
    assert restored["status"] == "restored"
    names = [n["filename"] for n in call(server, "list-notes", {})["result"]]
    assert "Archived.md" in names


def test_search_vault(server: MCPServer) -> None:
    call(server, "create-note", {"title": "Alpha", "summary": "quantum cats", "tags": ["pets"]})
    call(server, "create-note", {"title": "Beta", "details": "classical dogs", "tags": ["pets"]})
    hits = call(server, "search-vault", {"query": "quantum"})["result"]
    assert [h["filename"] for h in hits] == ["Alpha.md"]
    by_tag = call(server, "search-vault", {"query": "", "tag": "pets"})["result"]
    assert {h["filename"] for h in by_tag} == {"Alpha.md", "Beta.md"}


def test_search_vault_is_bounded_and_pageable(server: MCPServer) -> None:
    for number in range(7):
        call(server, "create-note", {"title": f"Page {number}", "summary": "shared"})
    first = call(server, "search-vault", {"query": "shared", "limit": 2})
    second = call(
        server,
        "search-vault",
        {"query": "shared", "limit": 2, "offset": 2},
    )
    assert first["count"] == 2 and first["has_more"] is True
    assert second["count"] == 2 and second["offset"] == 2
    assert {item["filename"] for item in first["result"]}.isdisjoint(
        item["filename"] for item in second["result"]
    )


def test_every_list_tool_is_bounded(server: MCPServer) -> None:
    """No tool may return the whole vault in one result.

    `list-notes` used to: ~348 KB / 87k tokens on a 744-note vault, in a single
    tool payload. Every list-shaped tool now pages, and the caps are asserted
    here so a new one cannot quietly go unbounded again.
    """
    for number in range(40):
        call(server, "create-note", {"title": f"Note {number:02d}", "summary": "body"})
    call(server, "create-note", {"title": "Linker", "summary": "see [[Note 00]] and [[Ghost]]"})

    listed = call(server, "list-notes", {})
    assert listed["count"] == 25 and listed["has_more"] is True  # default page
    assert listed["total"] == 41
    assert call(server, "list-notes", {"limit": 5})["count"] == 5
    assert call(server, "list-notes", {"limit": 9_999})["count"] == 41  # clamped to MAX_PAGE
    second = call(server, "list-notes", {"limit": 25, "offset": 25})
    assert second["count"] == 16 and second["has_more"] is False
    assert {n["filename"] for n in listed["result"]}.isdisjoint(
        n["filename"] for n in second["result"]
    )

    for tool, args in (
        ("backlinks", {"name": "Note 00.md", "limit": 1}),
        ("list-tags", {"limit": 1}),
        ("graph-neighbors", {"name": "Linker", "limit": 1}),
        ("graph", {"op": "orphans", "limit": 1}),
        ("graph", {"op": "dangling", "limit": 1}),
        ("graph", {"op": "frontier", "limit": 1}),
    ):
        page = call(server, tool, args)
        assert set(page) >= {"result", "count", "offset", "total", "has_more"}, tool
        assert page["count"] <= 1, tool


def test_search_hits_carry_an_excerpt_of_the_matched_text(server: MCPServer) -> None:
    """The excerpt is why a search result is often enough on its own — it shows
    the matched text even when the match is in a section `summary` never shows."""
    call(
        server,
        "create-note",
        {"title": "Deploy", "summary": "unrelated one-liner", "details": "the runbook step is X"},
    )
    hit = call(server, "search-vault", {"query": "runbook"})["result"][0]
    assert hit["filename"] == "Deploy.md"
    assert "runbook" in hit["excerpt"]
    assert hit["score"] > 0


def test_recall_note_returns_one_bounded_representation(server: MCPServer) -> None:
    call(
        server,
        "create-note",
        {
            "title": "Compact",
            "summary": "short durable summary",
            "details": "D" * 2_000,
            "tags": ["memory"],
        },
    )
    recalled = call(server, "recall-note", {"name": "Compact", "max_chars": 500})
    assert set(recalled) == {
        "filename",
        "title",
        "summary",
        "content",
        "section",
        "truncated",
        "version",
    }
    assert recalled["summary"] == "short durable summary"
    assert len(recalled["content"]) <= 500
    assert recalled["truncated"] is True
    assert "raw" not in recalled and "fields" not in recalled
    # #239: the marker is actionable — it names the note and a concrete
    # follow-up call, not just "truncated".
    assert '"name": "Compact"' in recalled["content"]
    assert '"max_chars":' in recalled["content"]

    section = call(
        server,
        "recall-note",
        {"name": "Compact", "section": "Details", "max_chars": 500},
    )
    assert section["section"] == "Details"


def test_help_tool_is_generated_from_live_cli(server: MCPServer) -> None:
    result = call(server, "help", {"command": "/omind help ai usage"})
    assert result["ok"] is True
    assert result["command"] == "omind ai usage"
    assert "--since" in result["help"] and "--json" in result["help"]
    unknown = call(server, "help", {"command": "ai usgae"})
    assert unknown["ok"] is False
    assert "usage" in unknown["error"]


def test_backlinks_and_tags(server: MCPServer) -> None:
    call(server, "create-note", {"title": "Hub", "summary": "s", "tags": ["one"]})
    call(server, "create-note", {"title": "Spoke", "summary": "see [[Hub]]", "tags": ["two"]})
    links = call(server, "backlinks", {"name": "Hub.md"})["result"]
    assert [n["filename"] for n in links] == ["Spoke.md"]
    assert call(server, "list-tags", {})["result"] == ["one", "two"]


def test_graph_tools(server: MCPServer) -> None:
    call(server, "create-note", {"title": "A", "summary": "s", "connections": ["B"]})
    call(server, "create-note", {"title": "B", "summary": "s", "connections": ["C"]})
    call(server, "create-note", {"title": "C", "summary": "s"})
    call(server, "create-note", {"title": "Lonely", "summary": "see [[Ghost]]"})

    nbrs = call(server, "graph-neighbors", {"name": "A", "depth": 2, "direction": "out"})["result"]
    assert [n["filename"] for n in nbrs] == ["B.md", "C.md"]

    assert call(
        server,
        "graph",
        {"op": "path", "source": "A", "target": "C"},
    )["path"] == ["A.md", "B.md", "C.md"]
    assert call(server, "graph", {"op": "orphans"})["result"] == ["Lonely.md"]
    assert call(server, "graph", {"op": "dangling"})["result"] == [
        {"source": "Lonely.md", "target": "Ghost"}
    ]
    assert call(server, "graph", {"op": "stats"})["notes"] == 4

    ranked = call(server, "graph", {"op": "frontier"})["result"]
    # A links B, B links C, and nothing links A: A is the frontier, C the sink.
    assert ranked[0]["filename"] == "A.md"
    assert ranked[0]["out_degree"] == 1 and ranked[0]["in_degree"] == 0
    assert ranked[-1]["filename"] == "C.md"


def test_unified_graph_validates_operation_and_path_arguments(server: MCPServer) -> None:
    with pytest.raises(ToolError, match="one of"):
        call(server, "graph", {"op": "unknown"})
    with pytest.raises(ToolError, match="requires source and target"):
        call(server, "graph", {"op": "path"})


def test_graph_build_is_cached_and_busted_by_a_write(omi_dir: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """All graph tools reuse one cached build; a write busts the cache (#130)."""
    from omind import graph as graph_mod

    calls = {"n": 0}
    real = graph_mod.build_graph

    def counting(omi: Path) -> Any:
        calls["n"] += 1
        return real(omi)

    monkeypatch.setattr(graph_mod, "build_graph", counting)
    server = build_server(omi_dir, node_id="testnode-abc123")
    call(server, "create-note", {"title": "A", "connections": ["B"]})
    call(server, "graph", {"op": "stats"})
    call(server, "graph", {"op": "orphans"})
    call(server, "graph", {"op": "dangling"})
    assert calls["n"] == 1  # three graph queries, one build (cached)
    call(server, "create-note", {"title": "B"})  # a write changes the vault
    call(server, "graph", {"op": "stats"})
    assert calls["n"] == 2  # cache busted, rebuilt once


def test_graph_neighbors_unknown_note_is_a_tool_error(server: MCPServer) -> None:
    with pytest.raises(ToolError, match="not found"):
        call(server, "graph-neighbors", {"name": "Nope"})


def test_missing_note_is_a_tool_error(server: MCPServer) -> None:
    with pytest.raises(ToolError, match="not found"):
        call(server, "read-note", {"name": "Nope.md"})


def test_traversal_is_a_tool_error(server: MCPServer) -> None:
    with pytest.raises(ToolError, match="path separators"):
        call(server, "read-note", {"name": "../escape.md"})


def test_writes_touch_the_sync_signal(server: MCPServer, omi_dir: Path) -> None:
    signal = sync_signal_path(omi_dir)
    assert not signal.exists()
    call(server, "create-note", {"title": "Trigger", "summary": "s"})
    assert signal.exists()
    first = signal.stat().st_mtime_ns
    call(server, "edit-note", {"name": "Trigger.md", "summary": "again"})
    assert signal.stat().st_mtime_ns >= first


def test_reads_do_not_touch_the_sync_signal(server: MCPServer, omi_dir: Path) -> None:
    call(server, "create-note", {"title": "Quiet", "summary": "s"})
    signal = sync_signal_path(omi_dir)
    signal.unlink()
    call(server, "read-note", {"name": "Quiet.md"})
    call(server, "list-notes", {})
    assert not signal.exists()


def test_stdio_handshake_and_clean_exit_on_eof(tmp_path: Path) -> None:
    """The issue-#49 regression contract: a real `omind node` process answers
    the MCP handshake, and exits 0 the moment its client closes stdin.

    Responses are awaited *before* stdin closes — a real client holds the pipe
    open while requests are in flight; closing early legitimately lets the
    server drop in-flight work (it raced exactly that way on CI once).
    """
    vault = tmp_path / "Vault"
    (vault / "OMI").mkdir(parents=True)
    proc = subprocess.Popen(
        [sys.executable, "-m", "omind", "node", "--vault", str(vault), "--folder", "OMI"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        encoding="utf-8",
    )
    assert proc.stdin is not None and proc.stdout is not None

    lines: queue.Queue[str] = queue.Queue()

    def _pump(stdout: Any) -> None:
        for line in stdout:
            lines.put(line)

    threading.Thread(target=_pump, args=(proc.stdout,), daemon=True).start()

    def send(msg: dict[str, Any]) -> None:
        assert proc.stdin is not None
        proc.stdin.write(json.dumps(msg) + "\n")
        proc.stdin.flush()

    def recv() -> dict[str, Any]:
        try:
            while True:
                line = lines.get(timeout=60)
                if line.strip():
                    return dict(json.loads(line))
        except queue.Empty:
            proc.kill()
            pytest.fail("omind node did not answer within 60s")

    try:
        send(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "pytest", "version": "0"},
                },
            }
        )
        init = recv()
        assert init["id"] == 1
        assert init["result"]["serverInfo"]["name"] == "omi"

        send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        send({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        listed = recv()
        assert listed["id"] == 2
        assert {t["name"] for t in listed["result"]["tools"]} == EXPECTED_TOOLS

        proc.stdin.close()  # EOF — the server must exit promptly, code 0
        try:
            assert proc.wait(timeout=60) == 0
        except subprocess.TimeoutExpired:
            proc.kill()
            pytest.fail("omind node did not exit on stdin EOF (the issue-#49 hang)")
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait()
        if proc.stderr is not None:
            proc.stderr.close()


def test_recall_note_warns_about_a_conflicting_memory(server: MCPServer) -> None:
    """The agent must see the disagreement, not just one side of it (#195)."""
    call(server, "create-note", {"title": "Older", "summary": "the box has a 1060"})
    call(
        server,
        "create-note",
        {
            "title": "Newer",
            "summary": "the box has a V620",
            "confidence": "low",
            "conflicts_with": "[[Older]]",
        },
    )
    recalled = call(server, "recall-note", {"name": "Newer.md"})
    assert recalled["conflicts_with"] == "[[Older]]"
    assert recalled["confidence"] == "low"
    assert "Older" in str(recalled["warning"])

    # A note with neither field pays nothing for them.
    plain = call(server, "recall-note", {"name": "Older.md"})
    assert "conflicts_with" not in plain and "confidence" not in plain
    assert "warning" not in plain


def test_anticipated_domain_errors_surface_as_tool_errors_with_their_text() -> None:
    """#294: mcp >= 2.1 masks any non-``ToolError`` exception as a bare
    ``Error executing tool <name>``. The tool boundary re-raises the failures a
    tool anticipates as a deliberate ``ToolError`` so the text — the version
    conflict's "re-read before writing" in particular — still reaches the caller."""
    from omind.server import _anticipated
    from omind.store import NoteConflictError, NoteNotFoundError

    @_anticipated
    def conflict() -> None:
        raise NoteConflictError("note changed on disk; re-read before writing")

    @_anticipated
    def missing() -> None:
        raise NoteNotFoundError("note not found: 'x.md'")

    @_anticipated
    def crash() -> None:
        raise OSError("disk on fire")

    with pytest.raises(ToolError, match="changed on disk") as info:
        conflict()
    assert isinstance(info.value.__cause__, NoteConflictError)
    with pytest.raises(ToolError, match="not found"):
        missing()
    # A genuine crash is NOT anticipated: it stays an OSError for the SDK to
    # mask and log with its traceback, exactly as the SDK intends.
    with pytest.raises(OSError, match="disk on fire"):
        crash()
