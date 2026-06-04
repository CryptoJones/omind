# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Aaron K. Clark
"""Seed content written into a fresh OMI folder.

These constants capture the exact files an OMI memory folder needs so that
`obsidian-mcp` accepts it as a vault and Claude Code (or the omind web UI) has
a template and index to work from. Nothing here is clobbered if it already
exists on disk -- see :mod:`omind.provision`.
"""

from __future__ import annotations

# obsidian-mcp validates a vault by reading <vault>/.obsidian/app.json at
# startup; without it the server refuses to load. The other two files make the
# folder a well-formed standalone Obsidian vault.
APP_JSON = """\
{
  "livePreview": true,
  "newFileLocation": "root",
  "attachmentFolderPath": "./"
}
"""

CORE_PLUGINS_JSON = """\
{
  "file-explorer": true,
  "global-search": true,
  "switcher": true,
  "graph": true,
  "backlink": true,
  "tag-pane": true,
  "page-preview": true,
  "templates": true,
  "note-composer": true,
  "command-palette": true
}
"""

APPEARANCE_JSON = "{}\n"

OBSIDIAN_CONFIG_FILES = {
    "app.json": APP_JSON,
    "core-plugins.json": CORE_PLUGINS_JSON,
    "appearance.json": APPEARANCE_JSON,
}

# Node preload that guarantees obsidian-mcp exits when its stdin (the MCP client
# pipe) closes. The server's chokidar file watcher otherwise keeps the Node
# event loop alive, so the process orphans when Claude Code exits. Registering
# the server as a direct `node --require <this file> ...` command (instead of
# `npx -y obsidian-mcp`) also lets Claude Code's terminating signal reach Node
# directly rather than being swallowed by the npx/npm wrapper chain.
EOF_GUARD_FILENAME = "obsidian-exit-on-eof.js"
EOF_GUARD_JS = """\
// Managed by omind. Exit obsidian-mcp when its stdin (the MCP client pipe)
// closes; the chokidar file watcher otherwise keeps the Node event loop alive
// and the process orphans when Claude Code exits.
const die = () => process.exit(0);
process.stdin.on("end", die);
process.stdin.on("close", die);
"""

# The structured note template. The `## ` headings here are the contract
# omind.store.parse_note reads against — keep them in sync with the section
# names it looks up.
MEMORY_TEMPLATE = """\
# OMI Memory Template

## Metadata
- Created: {{date}}
- Tags: #omi #memory
- Related to:

## Summary
{{summary}}

## Details
{{details}}

## Connections
[[Related Concept 1]]
[[Related Concept 2]]

## Action Items
- [ ]

## References
- Source:
"""

MEMORY_TEMPLATE_FILENAME = "Memory Template.md"
INDEX_FILENAME = "index.md"

# Heading that begins the auto-maintained wikilink list in index.md. Everything
# before it is preserved verbatim on update; everything after is regenerated.
INDEX_RECENT_HEADING = "## Recent Memories"
INDEX_RECENT_COMMENT = "<!-- Maintained by omind; entries below are regenerated. -->"

INDEX_INTRO = """\
# OMI (Open Mind Interface) Memory System

This vault contains memories and knowledge for the OMI system.

## Structure
- `./Memory Template.md` - Template for new memories
- `./` - Directory for individual memory notes

## Usage
Use the template to create new memory notes. Link memories using Obsidian's [[wikilink]] syntax.
"""

# Files that are scaffolding, not memories -- excluded from listings.
RESERVED_FILENAMES = frozenset({MEMORY_TEMPLATE_FILENAME, INDEX_FILENAME})
