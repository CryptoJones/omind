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

import logging
import sys
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from omind.paths import sync_signal_path
from omind.store import ActionItem, NoteFields, OmiStore

SERVER_NAME = "omi"

_INSTRUCTIONS = """\
Long-term memory for this machine's OMI folder (plain Markdown notes).
Notes are linked with [[wikilinks]]. delete-note archives (soft-deletes,
restorable); nothing is removed from disk. Use the version token from
read-note as expected_version when editing to detect concurrent writers."""

logger = logging.getLogger(__name__)


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
    store = OmiStore(omi_dir, node_id=node_id)
    signal = sync_signal_path(store.omi_dir)

    def _wrote() -> None:
        # Advisory only: the daemon syncs on interval regardless.
        try:
            signal.parent.mkdir(parents=True, exist_ok=True)
            signal.touch()
        except OSError as exc:
            logger.warning("could not touch sync signal %s: %s", signal, exc)

    mcp = FastMCP(SERVER_NAME, instructions=_INSTRUCTIONS)

    @mcp.tool(
        name="read-note",
        description=(
            "Read one memory note: raw Markdown, parsed fields, and the version "
            "token to pass as expected_version when editing."
        ),
    )
    def read_note(name: str) -> dict[str, object]:
        raw = store.read_note(name)
        fields = store.read_fields(name)
        return {
            "filename": store.safe_name(name).name,
            "raw": raw,
            "fields": fields.to_dict(),
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
        _wrote()
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
        _wrote()
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
        _wrote()
        return {"filename": filename, "status": "archived"}

    @mcp.tool(name="restore-note", description="Restore an archived (soft-deleted) note.")
    def restore_note(name: str) -> dict[str, str]:
        filename = store.restore_note(name)
        _wrote()
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

    return mcp


def run_node(omi_dir: Path, node_id: str | None = None) -> int:
    """CLI entry: serve the node over stdio until the client closes stdin."""
    # stdout is the protocol channel; everything else goes to stderr.
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING)
    build_server(omi_dir, node_id=node_id).run("stdio")
    return 0
