# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Aaron K. Clark
"""``omind node`` — the local mesh-node MCP server (docs/mesh.md).

A stdio MCP server exposing :class:`omind.store.OmiStore` as tools, replacing
the provisioned ``obsidian-mcp``. Claude clients talk only to this local node;
reads and writes never cross the network. After every successful write the
server touches the sync-signal file, which the mesh replication daemon watches
to debounce a commit+sync — until the daemon exists the signal is inert.

The server exits cleanly when its client closes stdin, which retires the
entire eof-guard/hang class of the old obsidian-mcp (issue #49).
"""

from __future__ import annotations

import contextlib
import logging
import os
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import anyio
import mcp.types as mcp_types
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream
from mcp.server.fastmcp import FastMCP
from mcp.shared.message import SessionMessage

from omind import graph
from omind.store import ActionItem, NoteFields, OmiStore, parse_note

SERVER_NAME = "omi"

_INSTRUCTIONS = """\
Long-term memory for this machine's OMI folder (plain Markdown notes).
Notes are linked with [[wikilinks]]. delete-note archives (soft-deletes,
restorable); nothing is removed from disk. Use the version token from
read-note as expected_version when editing to detect concurrent writers."""

logger = logging.getLogger(__name__)


@asynccontextmanager
async def _fd_stdio_server() -> AsyncIterator[
    tuple[
        MemoryObjectReceiveStream[SessionMessage | Exception],
        MemoryObjectSendStream[SessionMessage],
    ]
]:
    """MCP stdio transport using fd readiness instead of AnyIO file wrappers."""
    stdin_fd = sys.stdin.buffer.fileno()
    stdout_fd = sys.stdout.buffer.fileno()

    read_stream_writer, read_stream = anyio.create_memory_object_stream[
        SessionMessage | Exception
    ](0)
    write_stream, write_stream_reader = anyio.create_memory_object_stream[SessionMessage](0)

    async def send_line(line: str) -> None:
        try:
            message = mcp_types.JSONRPCMessage.model_validate_json(line)
        except Exception as exc:
            await read_stream_writer.send(exc)
            return
        await read_stream_writer.send(SessionMessage(message))

    async def stdin_reader() -> None:
        buffer = b""
        try:
            async with read_stream_writer:
                while True:
                    await anyio.wait_readable(stdin_fd)
                    try:
                        chunk = os.read(stdin_fd, 65536)
                    except BlockingIOError:
                        continue
                    if not chunk:
                        break
                    buffer += chunk
                    while b"\n" in buffer:
                        raw, buffer = buffer.split(b"\n", 1)
                        await send_line(raw.decode("utf-8", errors="replace"))
                if buffer:
                    await send_line(buffer.decode("utf-8", errors="replace"))
        except anyio.ClosedResourceError:  # pragma: no cover
            await anyio.lowlevel.checkpoint()

    async def write_all(data: bytes) -> None:
        offset = 0
        while offset < len(data):
            try:
                written = os.write(stdout_fd, data[offset:])
            except BlockingIOError:
                await anyio.wait_writable(stdout_fd)
                continue
            if written == 0:
                await anyio.wait_writable(stdout_fd)
                continue
            offset += written

    async def stdout_writer() -> None:
        try:
            async with write_stream_reader:
                async for session_message in write_stream_reader:
                    payload = session_message.message.model_dump_json(
                        by_alias=True, exclude_none=True
                    )
                    await write_all((payload + "\n").encode("utf-8"))
        except (anyio.ClosedResourceError, BrokenPipeError):  # pragma: no cover
            await anyio.lowlevel.checkpoint()

    async with anyio.create_task_group() as tg:
        tg.start_soon(stdin_reader)
        tg.start_soon(stdout_writer)
        yield read_stream, write_stream


def _parse_action_items(items: list[str]) -> list[ActionItem]:
    """``"[x] text"`` marks a completed item; anything else is open."""
    parsed: list[ActionItem] = []
    for raw in items:
        text = raw.strip()
        done = text.lower().startswith("[x]")
        if done or text.startswith("[ ]"):
            text = text[3:].strip()
        if text:
            parsed.append(ActionItem(text=text, done=done))
    return parsed


def build_server(omi_dir: Path | str, node_id: str | None = None) -> FastMCP:
    """Build the node MCP server over one OMI folder.

    ``node_id`` (from the mesh config, when initialized) turns on Lamport
    stamping in the store; without it the store still soft-deletes whenever
    the folder is a git working tree.
    """
    # Write-signal touching lives in OmiStore now: every write surface nudges
    # the mesh daemon, not just this server's tools.
    store = OmiStore(omi_dir, node_id=node_id)

    mcp = FastMCP(SERVER_NAME, instructions=_INSTRUCTIONS)

    # The five graph tools each rebuilt the whole [[wikilink]] graph from disk
    # (a full-vault read+parse) on every call. Cache it, invalidated by a cheap
    # signature over the note files' (count, total size, newest mtime) — so a
    # burst of graph queries costs one parse, and any write busts the cache.
    graph_cache: dict[str, object] = {}

    def _vault_signature() -> tuple[int, int, int]:
        count = size = mtime = 0
        with contextlib.suppress(OSError):
            for p in store.omi_dir.glob("*.md"):
                try:
                    st = p.stat()
                except OSError:
                    continue
                count += 1
                size += st.st_size
                mtime = max(mtime, st.st_mtime_ns)
        return (count, size, mtime)

    def graph_for() -> graph.Graph:
        sig = _vault_signature()
        if graph_cache.get("sig") != sig:
            graph_cache["sig"] = sig
            graph_cache["graph"] = graph.build_graph(store.omi_dir)
        return graph_cache["graph"]  # type: ignore[return-value]

    @mcp.tool(
        name="read-note",
        description=(
            "Read one memory note: raw Markdown, parsed fields, and the version "
            "token to pass as expected_version when editing."
        ),
    )
    def read_note(name: str) -> dict[str, object]:
        raw = store.read_note(name)
        # One read + one parse: read_fields would re-read the file just read.
        return {
            "filename": store.safe_name(name).name,
            "raw": raw,
            "fields": parse_note(raw).to_dict(),
            "version": store.note_version(name),
        }

    @mcp.tool(
        name="create-note",
        description=(
            "Create a memory note. Lists: tags (no leading #), connections "
            "([[wikilink]] targets), references, action_items ('[x] text' = done)."
        ),
    )
    def create_note(
        title: str,
        summary: str = "",
        details: str = "",
        tags: list[str] | None = None,
        related_to: str = "",
        connections: list[str] | None = None,
        action_items: list[str] | None = None,
        references: list[str] | None = None,
    ) -> dict[str, str]:
        fields = NoteFields(
            title=title,
            summary=summary,
            details=details,
            tags=tags or [],
            related_to=related_to,
            connections=connections or [],
            action_items=_parse_action_items(action_items or []),
            references=references or [],
        )
        filename = store.create_note(fields)
        return {"filename": filename}

    @mcp.tool(
        name="edit-note",
        description=(
            "Update fields of an existing note; omitted fields keep their current "
            "value. Pass expected_version from read-note to fail loudly (instead "
            "of overwriting) when another writer changed the note in between."
        ),
    )
    def edit_note(
        name: str,
        title: str | None = None,
        summary: str | None = None,
        details: str | None = None,
        tags: list[str] | None = None,
        related_to: str | None = None,
        connections: list[str] | None = None,
        action_items: list[str] | None = None,
        references: list[str] | None = None,
        expected_version: str | None = None,
    ) -> dict[str, str]:
        fields = store.read_fields(name)
        if title is not None:
            fields.title = title
        if summary is not None:
            fields.summary = summary
        if details is not None:
            fields.details = details
        if tags is not None:
            fields.tags = tags
        if related_to is not None:
            fields.related_to = related_to
        if connections is not None:
            fields.connections = connections
        if action_items is not None:
            fields.action_items = _parse_action_items(action_items)
        if references is not None:
            fields.references = references
        filename = store.update_note(name, fields, expected_version=expected_version)
        return {"filename": filename, "version": store.note_version(name)}

    @mcp.tool(
        name="search-vault",
        description=(
            "Case-insensitive substring search over note titles, summaries, "
            "details, and tags; optionally filter to one tag."
        ),
    )
    def search_vault(
        query: str, tag: str | None = None, include_archived: bool = False
    ) -> list[dict[str, object]]:
        results = store.search(query, tag=tag, include_disabled=include_archived)
        return [s.__dict__ for s in results]

    @mcp.tool(
        name="list-notes",
        description="List all memory notes (newest first). Archived notes are hidden by default.",
    )
    def list_notes(include_archived: bool = False) -> list[dict[str, object]]:
        return [s.__dict__ for s in store.list_notes(include_disabled=include_archived)]

    @mcp.tool(
        name="delete-note",
        description=(
            "Archive (soft-delete) a note: it disappears from listings and search "
            "but stays on disk and can be restored with restore-note."
        ),
    )
    def delete_note(name: str) -> dict[str, str]:
        filename = store.disable_note(name)
        return {"filename": filename, "status": "archived"}

    @mcp.tool(name="restore-note", description="Restore an archived (soft-deleted) note.")
    def restore_note(name: str) -> dict[str, str]:
        filename = store.restore_note(name)
        return {"filename": filename, "status": "restored"}

    @mcp.tool(
        name="backlinks",
        description="List the notes whose [[wikilinks]] point at the given note.",
    )
    def backlinks(name: str) -> list[dict[str, object]]:
        return [s.__dict__ for s in store.backlinks(name)]

    @mcp.tool(name="list-tags", description="List every tag in use across the notes.")
    def list_tags() -> list[str]:
        return store.all_tags()

    @mcp.tool(
        name="graph-neighbors",
        description=(
            "Notes within `depth` hops of a note in the [[wikilink]] graph. "
            "direction: out (links it makes), in (links to it), or both (default)."
        ),
    )
    def graph_neighbors(
        name: str, depth: int = 1, direction: str = "both"
    ) -> list[dict[str, object]]:
        g = graph_for()
        return [
            {"filename": filename, "distance": distance}
            for filename, distance in graph.neighbors(
                g, name, depth=depth, direction=direction
            )
        ]

    @mcp.tool(
        name="graph-path",
        description=(
            "Shortest [[wikilink]] path between two notes, as a list of filenames; "
            "`path` is null when no path connects them."
        ),
    )
    def graph_path(source: str, target: str) -> dict[str, object]:
        g = graph_for()
        return {"path": graph.shortest_path(g, source, target)}

    @mcp.tool(
        name="graph-orphans",
        description=(
            "Notes with no inbound or outbound [[wikilinks]] (disconnected from the graph)."
        ),
    )
    def graph_orphans() -> list[str]:
        return graph.orphans(graph_for())

    @mcp.tool(
        name="graph-dangling",
        description=(
            "[[wikilinks]] that resolve to no existing note (broken links), with their source."
        ),
    )
    def graph_dangling() -> list[dict[str, str]]:
        g = graph_for()
        return [{"source": src, "target": target} for src, target in graph.dangling_links(g)]

    @mcp.tool(
        name="graph-stats",
        description="Whole-graph counts: notes, links, orphans, and dangling links.",
    )
    def graph_stats() -> dict[str, int]:
        return graph.stats(graph_for())

    return mcp


def run_node(omi_dir: Path, node_id: str | None = None) -> int:
    """CLI entry: serve the node over stdio until the client closes stdin."""
    # stdout is the protocol channel; everything else goes to stderr.
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING)

    async def run_stdio() -> None:
        mcp = build_server(omi_dir, node_id=node_id)
        async with _fd_stdio_server() as (read_stream, write_stream):
            await mcp._mcp_server.run(  # noqa: SLF001 - FastMCP exposes no public lower-level runner.
                read_stream,
                write_stream,
                mcp._mcp_server.create_initialization_options(),  # noqa: SLF001
            )

    anyio.run(run_stdio)
    return 0
