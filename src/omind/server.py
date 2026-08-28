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
from typing import Any

import anyio
import mcp.types as mcp_types
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream
from mcp.server.mcpserver import MCPServer
from mcp.shared.message import SessionMessage

from omind import graph, searchindex
from omind.help_system import render_help
from omind.recall import DEFAULT_RECALL_CHARS, compact_recall
from omind.store import ActionItem, NoteFields, OmiStore, _clean_agent, parse_note

SERVER_NAME = "omi"

_INSTRUCTIONS = """\
Long-term memory for this machine's OMI folder (plain Markdown notes).
Notes are linked with [[wikilinks]]. delete-note archives (soft-deletes,
restorable); nothing is removed from disk. Use the version token from
read-note as expected_version when editing to detect concurrent writers."""

logger = logging.getLogger(__name__)


def _session_id() -> str:
    """Best-effort session key for per-session usefulness dedupe (item #2).

    ``$CLAUDE_SESSION_ID`` is the same handle the loop guard and journal already
    key on; an empty value simply means reads cannot be deduped this run.
    """
    return os.environ.get("CLAUDE_SESSION_ID", "")


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
            # mcp 2.x: JSONRPCMessage is a plain union alias, not a RootModel —
            # it has no .model_validate_json. Parse through the SDK's TypeAdapter,
            # with the same flags the SDK's own stdio transport uses.
            message = mcp_types.jsonrpc_message_adapter.validate_json(line, by_name=False)
        except Exception as exc:
            await read_stream_writer.send(exc)
            return
        await read_stream_writer.send(SessionMessage(message))

    async def stdin_reader() -> None:
        buffer = b""
        try:
            async with read_stream_writer:
                while True:
                    if sys.platform == "win32":
                        # Windows pipe handles are not sockets and cannot be
                        # registered with AnyIO's readiness backend. Keep the
                        # event loop free while one worker blocks on a line.
                        chunk = await anyio.to_thread.run_sync(sys.stdin.buffer.readline)
                    else:
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
        if sys.platform == "win32":
            def write_stdout() -> None:
                sys.stdout.buffer.write(data)
                sys.stdout.buffer.flush()

            await anyio.to_thread.run_sync(write_stdout)
            return

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
                    # exclude_unset (not exclude_none) matches the SDK's own
                    # stdio transport. It is what keeps the 2026-07-28 envelope
                    # fields the server explicitly set — resultType, ttlMs,
                    # cacheScope — on the wire.
                    payload = session_message.message.model_dump_json(
                        by_alias=True, exclude_unset=True
                    )
                    await write_all((payload + "\n").encode("utf-8"))
        except (anyio.ClosedResourceError, BrokenPipeError):  # pragma: no cover
            await anyio.lowlevel.checkpoint()

    async with anyio.create_task_group() as tg:
        tg.start_soon(stdin_reader)
        tg.start_soon(stdout_writer)
        yield read_stream, write_stream


#: Default and hard-cap page sizes for every list-shaped tool. Unbounded list
#: tools were the single largest token leak in the memory layer: `list-notes` on
#: a 744-note vault returned ~348 KB (~87k tokens) in ONE tool result, and
#: graph(op=orphans|dangling) returned hundreds of rows nobody paged through.
#: An agent that needs more asks for the next page; an agent that needed one
#: note no longer pays for the whole vault.
DEFAULT_PAGE = 25
MAX_PAGE = 100


def _page(rows: list[Any], limit: int, offset: int) -> dict[str, object]:
    """One bounded page of ``rows`` in the shape every list tool returns."""
    start = max(0, int(offset))
    size = min(MAX_PAGE, max(1, int(limit)))
    window = rows[start : start + size]
    return {
        "result": window,
        "count": len(window),
        "offset": start,
        "total": len(rows),
        "has_more": start + size < len(rows),
    }


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


def build_server(omi_dir: Path | str, node_id: str | None = None) -> MCPServer:
    """Build the node MCP server over one OMI folder.

    ``node_id`` (from the mesh config, when initialized) turns on Lamport
    stamping in the store; without it the store still soft-deletes whenever
    the folder is a git working tree.
    """
    # Write-signal touching lives in OmiStore now: every write surface nudges
    # the mesh daemon, not just this server's tools.
    store = OmiStore(omi_dir, node_id=node_id)

    mcp = MCPServer(SERVER_NAME, instructions=_INSTRUCTIONS)

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

    read_note_default_chars = 20000
    read_note_hard_cap = 65536

    def _clamp_body(text: str, max_chars: int) -> str:
        """Bound a note body so one huge note cannot become one unbounded
        tool result (the read-note hole in invariant 8; 2026-08-27 review)."""
        if len(text) <= max_chars:
            return text
        return text[:max_chars] + (
            f"\n\n[truncated: showing {max_chars} of {len(text)} chars — "
            f"raise max_chars (hard cap {read_note_hard_cap}) or use "
            "recall-note with a section]"
        )

    @mcp.tool(
        name="read-note",
        description=(
            "Read one note for EDITING, with a version token. representation: "
            "fields (default, parsed) or raw (Markdown). Prefer recall-note to "
            "simply remember something. Bodies are capped (max_chars, default "
            "20000) with an explicit truncation marker."
        ),
    )
    def read_note(
        name: str, representation: str = "fields", max_chars: int = read_note_default_chars
    ) -> dict[str, object]:
        raw = store.read_note(name)
        filename = store.safe_name(name).name
        from omind import access

        access.record(store.omi_dir, filename, session=_session_id())
        # One read + one parse: read_fields would re-read the file just read.
        # ONE representation, never both: returning `raw` and `fields` together
        # sent every note body through the context twice, and the editing caller
        # only ever uses one of them.
        payload: dict[str, object] = {
            "filename": filename,
            "version": store.note_version(name),
        }
        cap = max(1, min(int(max_chars), read_note_hard_cap))
        if representation == "raw":
            payload["raw"] = _clamp_body(raw, cap)
        else:
            fields = parse_note(raw).to_dict()
            if isinstance(fields.get("details"), str):
                fields["details"] = _clamp_body(fields["details"], cap)
            payload["fields"] = fields
        return payload

    @mcp.tool(
        name="recall-note",
        description=(
            "Token-efficient memory recall. Returns title, summary, one bounded "
            "content representation, and version. Use this instead of read-note "
            "unless raw Markdown/parsed edit fields are required."
        ),
    )
    def recall_note(
        name: str,
        max_chars: int = DEFAULT_RECALL_CHARS,
        section: str = "",
    ) -> dict[str, object]:
        return compact_recall(
            store.omi_dir, name, max_chars=max_chars, section=section, session=_session_id()
        )

    def _resolve_agent(explicit: str | None) -> str:
        """Self-declared caller identity: explicit arg > OMIND_AGENT env > "".

        2026-08-27 roundtable consensus: ADVISORY ONLY — stored on the note and
        echoed back, but never consumed for ranking, access, or trust (a spoofed
        identity is one env var away in a single-operator fleet)."""
        return _clean_agent(explicit or "") or os.environ.get("OMIND_AGENT", "")

    @mcp.tool(
        name="create-note",
        description=(
            "Create a memory note. Lists: tags (no leading #), connections "
            "([[wikilink]] targets), references, action_items ('[x] text' = done). "
            "confidence: high|medium|low, omit if unknown. conflicts_with: a "
            "[[wikilink]] to a memory this one DISAGREES with (use supersedes "
            "instead when this cleanly replaces the older fact). agent: your "
            "self-declared identity (advisory attribution only)."
        ),
    )
    def create_note(
        title: str,
        summary: str = "",
        details: str = "",
        tags: list[str] | None = None,
        related_to: str = "",
        supersedes: str = "",
        superseded_by: str = "",
        confidence: str = "",
        conflicts_with: str = "",
        connections: list[str] | None = None,
        action_items: list[str] | None = None,
        references: list[str] | None = None,
        agent: str = "",
    ) -> dict[str, object]:
        fields = NoteFields(
            title=title,
            summary=summary,
            details=details,
            tags=tags or [],
            related_to=related_to,
            supersedes=supersedes,
            superseded_by=superseded_by,
            confidence=confidence,
            conflicts_with=conflicts_with,
            agent=_resolve_agent(agent),
            connections=connections or [],
            action_items=_parse_action_items(action_items or []),
            references=references or [],
        )
        filename = store.create_note(fields)
        result: dict[str, object] = {"filename": filename, "agent": fields.agent}
        # Write-time near-duplicate warning (2026-08-27 roundtable: ADOPT —
        # advisory, fail-open, never blocks the write; hint field DROPPED for
        # v1 because cosine cannot distinguish "replaces" from "disagrees";
        # archived and already-superseded notes excluded). Threshold 0.88 is a
        # placeholder pending `lint --calibrate-dup`.
        near: list[dict[str, object]] = []
        with contextlib.suppress(Exception):
            ix = searchindex.shared(store.omi_dir)
            if ix is not None:
                probe = "\n".join(part for part in (title, summary) if part)
                for name, similarity in (
                    ix.nearest(probe, exclude=filename, limit=3) or []
                ):
                    if float(similarity) < 0.88:
                        continue
                    fields_of = store.read_fields(name)
                    if fields_of.disabled or fields_of.superseded_by:
                        continue
                    near.append(
                        {
                            "filename": name,
                            "similarity": round(float(similarity), 4),
                            "title": fields_of.title,
                        }
                    )
        if near:
            result["near_duplicates"] = near
            result["near_duplicates_note"] = (
                "Advisory only — the write succeeded. If this note cleanly "
                "replaces an existing one, set supersedes; if they disagree, "
                "set conflicts_with; if it is redundant, archive it."
            )
        return result

    @mcp.tool(
        name="edit-note",
        description=(
            "Update fields of an existing note; omitted fields keep their current "
            "value. Pass expected_version from recall-note/read-note to fail loudly (instead "
            "of overwriting) when another writer changed the note in between — without "
            "it the response is flagged concurrency=unverified. When a note makes a "
            "memory obsolete, set supersedes (or the target's superseded_by) rather "
            "than silently rewriting history."
        ),
    )
    def edit_note(
        name: str,
        title: str | None = None,
        summary: str | None = None,
        details: str | None = None,
        tags: list[str] | None = None,
        related_to: str | None = None,
        supersedes: str | None = None,
        superseded_by: str | None = None,
        confidence: str | None = None,
        conflicts_with: str | None = None,
        connections: list[str] | None = None,
        action_items: list[str] | None = None,
        references: list[str] | None = None,
        agent: str | None = None,
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
        if supersedes is not None:
            fields.supersedes = supersedes
        if superseded_by is not None:
            fields.superseded_by = superseded_by
        if confidence is not None:
            fields.confidence = confidence
        if conflicts_with is not None:
            fields.conflicts_with = conflicts_with
        if connections is not None:
            fields.connections = connections
        if action_items is not None:
            fields.action_items = _parse_action_items(action_items)
        if references is not None:
            fields.references = references
        if agent is not None:
            # Omitted keeps the current writer; an explicit value re-attributes
            # (e.g. a takeover). Resolution: arg > OMIND_AGENT env.
            fields.agent = _resolve_agent(agent)
        filename = store.update_note(name, fields, expected_version=expected_version)
        result: dict[str, str] = {"filename": filename, "version": store.note_version(name)}
        if expected_version is None:
            # Make the silent-last-write-wins case visible to the caller
            # (2026-08-27 review): without the token the write was not checked
            # against concurrent writers.
            result["concurrency"] = "unverified"
        return result

    @mcp.tool(
        name="search-vault",
        description=(
            "Relevance-ranked memory search (keyword + semantic + recency) over "
            "titles, summaries, details, and tags; each hit carries a matched "
            "excerpt. Empty query + tag lists that tag. include_archived adds "
            "soft-deleted (archived) notes — without it a second agent's delete "
            "hides its target from your results. Optional limit/offset."
        ),
    )
    def search_vault(
        query: str,
        tag: str | None = None,
        include_archived: bool = False,
        limit: int = 5,
        offset: int = 0,
    ) -> dict[str, object]:
        results = store.search(query, tag=tag, include_disabled=include_archived)
        return _page([s.__dict__ for s in results], limit, offset)

    @mcp.tool(
        name="help",
        description=(
            "Authoritative omind command syntax generated from the installed CLI. "
            "Use for `/omind help` and command-specific help such as `ai usage` "
            "or `mesh sync`; never rely on stale skill-embedded syntax."
        ),
    )
    def help_tool(command: str = "") -> dict[str, object]:
        return render_help(command)

    @mcp.tool(
        name="list-notes",
        description=(
            "One page of memory notes, newest first (limit default 25, max 100). "
            "include_archived adds soft-deleted (archived) notes. "
            "To FIND a note use search-vault; listing is for browsing."
        ),
    )
    def list_notes(
        include_archived: bool = False, limit: int = DEFAULT_PAGE, offset: int = 0
    ) -> dict[str, object]:
        rows = [s.__dict__ for s in store.list_notes(include_disabled=include_archived)]
        return _page(rows, limit, offset)

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
        description="One page of the notes whose [[wikilinks]] point at the given note.",
    )
    def backlinks(
        name: str, limit: int = DEFAULT_PAGE, offset: int = 0
    ) -> dict[str, object]:
        return _page([s.__dict__ for s in store.backlinks(name)], limit, offset)

    @mcp.tool(name="list-tags", description="Every tag in use across the notes (paged).")
    def list_tags(limit: int = MAX_PAGE, offset: int = 0) -> dict[str, object]:
        return _page(store.all_tags(), limit, offset)

    @mcp.tool(
        name="graph-neighbors",
        description=(
            "Notes within `depth` hops of a note in the [[wikilink]] graph. "
            "direction: out (links it makes), in (links to it), or both (default)."
        ),
    )
    def graph_neighbors(
        name: str,
        depth: int = 1,
        direction: str = "both",
        limit: int = DEFAULT_PAGE,
        offset: int = 0,
    ) -> dict[str, object]:
        g = graph_for()
        rows = [
            {"filename": filename, "distance": distance}
            for filename, distance in graph.neighbors(
                g, name, depth=depth, direction=direction
            )
        ]
        return _page(rows, limit, offset)

    def _graph_query(
        op: str,
        source: str = "",
        target: str = "",
        limit: int = DEFAULT_PAGE,
        offset: int = 0,
    ) -> dict[str, object]:
        operation = op.strip().lower()
        if operation == "path":
            if not source.strip() or not target.strip():
                raise ValueError("graph op=path requires source and target")
            g = graph_for()
            return {"path": graph.shortest_path(g, source, target)}
        if operation == "orphans":
            return _page(graph.orphans(graph_for()), limit, offset)
        if operation == "dangling":
            rows = [
                {"source": src, "target": raw_target}
                for src, raw_target in graph.dangling_links(graph_for())
            ]
            return _page(rows, limit, offset)
        if operation == "stats":
            return dict(graph.stats(graph_for()))
        if operation == "frontier":
            # Paged like every other list-shaped result (invariant 8): the caller
            # asks for a page, the ranking is computed over the whole graph.
            ranked = [
                {
                    "filename": entry.filename,
                    "title": entry.title,
                    "score": round(entry.score, 4),
                    "out_degree": entry.out_degree,
                    "in_degree": entry.in_degree,
                    "days_since_updated": round(entry.days_since_updated, 1),
                }
                for entry in graph.frontier(graph_for(), limit=0)
            ]
            return _page(ranked, limit, offset)
        raise ValueError(
            "graph op must be one of: path, orphans, dangling, stats, frontier"
        )

    @mcp.tool(
        name="graph",
        description=(
            "Graph audit/query selected by op: path (requires source + target), "
            "orphans, dangling, stats, or frontier (notes that reach out further "
            "than anything reaches back — what to consolidate next). List-shaped "
            "results are paged."
        ),
    )
    def graph_tool(
        op: str,
        source: str = "",
        target: str = "",
        limit: int = DEFAULT_PAGE,
        offset: int = 0,
    ) -> dict[str, object]:
        return _graph_query(op, source, target, limit, offset)

    return mcp


def run_node(omi_dir: Path, node_id: str | None = None) -> int:
    """CLI entry: serve the node over stdio until the client closes stdin."""
    # stdout is the protocol channel; everything else goes to stderr.
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING)

    async def run_stdio() -> None:
        mcp = build_server(omi_dir, node_id=node_id)
        async with _fd_stdio_server() as (read_stream, write_stream):
            await mcp._lowlevel_server.run(  # noqa: SLF001 - MCPServer exposes no public lower-level runner.
                read_stream,
                write_stream,
                mcp._lowlevel_server.create_initialization_options(),  # noqa: SLF001
            )

    anyio.run(run_stdio)
    return 0
