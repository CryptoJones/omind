# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Aaron K. Clark
"""Tests for omind.server: the `omind node` mesh-node MCP server.

In-process tests drive FastMCP's tool layer directly; one subprocess smoke
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
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError

from omind.paths import sync_signal_path
from omind.server import build_server

EXPECTED_TOOLS = {
    "read-note",
    "create-note",
    "edit-note",
    "search-vault",
    "list-notes",
    "delete-note",
    "restore-note",
    "backlinks",
    "list-tags",
    "graph-neighbors",
    "graph-path",
    "graph-orphans",
    "graph-dangling",
    "graph-stats",
}


@pytest.fixture
def omi_dir(tmp_path: Path) -> Path:
    omi = tmp_path / "OMI"
    omi.mkdir()
    return omi


@pytest.fixture
def server(omi_dir: Path) -> FastMCP:
    return build_server(omi_dir, node_id="testnode-abc123")


def call(server: FastMCP, name: str, args: dict[str, Any]) -> Any:
    """Invoke a tool in-process and return its structured result."""
    _content, structured = asyncio.run(server.call_tool(name, args))
    return structured


def test_exposes_exactly_the_designed_tools(server: FastMCP) -> None:
    tools = asyncio.run(server.list_tools())
    assert {t.name for t in tools} == EXPECTED_TOOLS
    assert all(t.description for t in tools)


def test_create_read_round_trip(server: FastMCP, omi_dir: Path) -> None:
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
    assert created == {"filename": "Server Note.md"}
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
    assert "[[Other Note]]" in got["raw"]


def test_edit_note_partial_update(server: FastMCP) -> None:
    call(server, "create-note", {"title": "Partial", "summary": "old", "tags": ["keep"]})
    edited = call(server, "edit-note", {"name": "Partial.md", "summary": "new"})
    assert edited["filename"] == "Partial.md"
    got = call(server, "read-note", {"name": "Partial.md"})
    assert got["fields"]["summary"] == "new"
    assert got["fields"]["tags"] == ["keep"]  # omitted fields untouched


def test_edit_note_version_conflict(server: FastMCP) -> None:
    call(server, "create-note", {"title": "Versioned", "summary": "v1"})
    stale = call(server, "read-note", {"name": "Versioned.md"})["version"]
    call(server, "edit-note", {"name": "Versioned.md", "summary": "v2"})
    with pytest.raises(ToolError, match="changed on disk"):
        call(
            server,
            "edit-note",
            {"name": "Versioned.md", "summary": "v3", "expected_version": stale},
        )


def test_delete_archives_and_restore(server: FastMCP, omi_dir: Path) -> None:
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


def test_search_vault(server: FastMCP) -> None:
    call(server, "create-note", {"title": "Alpha", "summary": "quantum cats", "tags": ["pets"]})
    call(server, "create-note", {"title": "Beta", "details": "classical dogs", "tags": ["pets"]})
    hits = call(server, "search-vault", {"query": "quantum"})["result"]
    assert [h["filename"] for h in hits] == ["Alpha.md"]
    by_tag = call(server, "search-vault", {"query": "", "tag": "pets"})["result"]
    assert {h["filename"] for h in by_tag} == {"Alpha.md", "Beta.md"}


def test_backlinks_and_tags(server: FastMCP) -> None:
    call(server, "create-note", {"title": "Hub", "summary": "s", "tags": ["one"]})
    call(server, "create-note", {"title": "Spoke", "summary": "see [[Hub]]", "tags": ["two"]})
    links = call(server, "backlinks", {"name": "Hub.md"})["result"]
    assert [n["filename"] for n in links] == ["Spoke.md"]
    assert call(server, "list-tags", {})["result"] == ["one", "two"]


def test_graph_tools(server: FastMCP) -> None:
    call(server, "create-note", {"title": "A", "summary": "s", "connections": ["B"]})
    call(server, "create-note", {"title": "B", "summary": "s", "connections": ["C"]})
    call(server, "create-note", {"title": "C", "summary": "s"})
    call(server, "create-note", {"title": "Lonely", "summary": "see [[Ghost]]"})

    nbrs = call(server, "graph-neighbors", {"name": "A", "depth": 2, "direction": "out"})["result"]
    assert [n["filename"] for n in nbrs] == ["B.md", "C.md"]

    assert call(server, "graph-path", {"source": "A", "target": "C"})["path"] == [
        "A.md",
        "B.md",
        "C.md",
    ]
    assert call(server, "graph-orphans", {})["result"] == ["Lonely.md"]
    dangling = call(server, "graph-dangling", {})["result"]
    assert dangling == [{"source": "Lonely.md", "target": "Ghost"}]
    assert call(server, "graph-stats", {})["notes"] == 4


def test_graph_build_is_cached_and_busted_by_a_write(omi_dir: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """The 5 graph tools reuse one cached build; a write busts the cache (#130)."""
    from omind import graph as graph_mod

    calls = {"n": 0}
    real = graph_mod.build_graph

    def counting(omi: Path) -> Any:
        calls["n"] += 1
        return real(omi)

    monkeypatch.setattr(graph_mod, "build_graph", counting)
    server = build_server(omi_dir, node_id="testnode-abc123")
    call(server, "create-note", {"title": "A", "connections": ["B"]})
    call(server, "graph-stats", {})
    call(server, "graph-orphans", {})
    call(server, "graph-dangling", {})
    assert calls["n"] == 1  # three graph queries, one build (cached)
    call(server, "create-note", {"title": "B"})  # a write changes the vault
    call(server, "graph-stats", {})
    assert calls["n"] == 2  # cache busted, rebuilt once


def test_graph_neighbors_unknown_note_is_a_tool_error(server: FastMCP) -> None:
    with pytest.raises(ToolError, match="not found"):
        call(server, "graph-neighbors", {"name": "Nope"})


def test_missing_note_is_a_tool_error(server: FastMCP) -> None:
    with pytest.raises(ToolError, match="not found"):
        call(server, "read-note", {"name": "Nope.md"})


def test_traversal_is_a_tool_error(server: FastMCP) -> None:
    with pytest.raises(ToolError, match="path separators"):
        call(server, "read-note", {"name": "../escape.md"})


def test_writes_touch_the_sync_signal(server: FastMCP, omi_dir: Path) -> None:
    signal = sync_signal_path(omi_dir)
    assert not signal.exists()
    call(server, "create-note", {"title": "Trigger", "summary": "s"})
    assert signal.exists()
    first = signal.stat().st_mtime_ns
    call(server, "edit-note", {"name": "Trigger.md", "summary": "again"})
    assert signal.stat().st_mtime_ns >= first


def test_reads_do_not_touch_the_sync_signal(server: FastMCP, omi_dir: Path) -> None:
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
