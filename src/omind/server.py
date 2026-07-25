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
import sys
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from omind import graph
from omind.help_system import render_help
from omind.recall import DEFAULT_RECALL_CHARS, compact_recall
from omind.store import ActionItem, NoteFields, OmiStore, parse_note

SERVER_NAME = "omi"

_INSTRUCTIONS = """\
Long-term memory for this machine's OMI folder (plain Markdown notes).
Notes are linked with [[wikilinks]]. delete-note archives (soft-deletes,
restorable); nothing is removed from disk. Use the version token from
read-note as expected_version when editing to detect concurrent writers."""

logger = logging.getLogger(__name__)


#: Default and hard-cap page sizes for every list-shaped tool. Unbounded list
#: tools were the single largest token leak in the memory layer: `list-notes` on
#: a 744-note vault returned ~348 KB (~87k tokens) in ONE tool result, and
#: graph-orphans/graph-dangling returned hundreds of rows nobody paged through.
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
            "Read one note for EDITING, with a version token. representation: "
            "fields (default, parsed) or raw (Markdown). Prefer recall-note to "
            "simply remember something."
        ),
    )
    def read_note(name: str, representation: str = "fields") -> dict[str, object]:
        raw = store.read_note(name)
        # One read + one parse: read_fields would re-read the file just read.
        # ONE representation, never both: returning `raw` and `fields` together
        # sent every note body through the context twice, and the editing caller
        # only ever uses one of them.
        payload: dict[str, object] = {
            "filename": store.safe_name(name).name,
            "version": store.note_version(name),
        }
        if representation == "raw":
            payload["raw"] = raw
        else:
            payload["fields"] = parse_note(raw).to_dict()
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
        return compact_recall(store.omi_dir, name, max_chars=max_chars, section=section)

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
            "value. Pass expected_version from recall-note/read-note to fail loudly (instead "
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
            "Relevance-ranked memory search (keyword + semantic + recency) over "
            "titles, summaries, details, and tags; each hit carries a matched "
            "excerpt. Empty query + tag lists that tag. Optional limit/offset."
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
            "One page of notes with no inbound or outbound [[wikilinks]]. "
            "graph-stats gives the count without the list."
        ),
    )
    def graph_orphans(limit: int = DEFAULT_PAGE, offset: int = 0) -> dict[str, object]:
        return _page(graph.orphans(graph_for()), limit, offset)

    @mcp.tool(
        name="graph-dangling",
        description=(
            "One page of [[wikilinks]] resolving to no existing note, with their "
            "source. graph-stats gives the count without the list."
        ),
    )
    def graph_dangling(limit: int = DEFAULT_PAGE, offset: int = 0) -> dict[str, object]:
        rows = [
            {"source": src, "target": target} for src, target in graph.dangling_links(graph_for())
        ]
        return _page(rows, limit, offset)

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
    build_server(omi_dir, node_id=node_id).run("stdio")
    return 0
