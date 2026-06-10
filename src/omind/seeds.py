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

AGENT_SKILL_FILENAME = "SKILL.md"

# Memory skill installed into an agent's skills directory by `omind setup
# --agent hermes|openclaw`. Both agents discover skills as a folder holding a
# SKILL.md with name/description frontmatter. Placeholders: {vault}, {folder},
# {omi_dir}.
AGENT_SKILL_TEMPLATE = """\
---
name: omind-omi-memory
description: >-
  Persist long-term memories as clean, single-insight Markdown notes in the
  OMI Obsidian folder through omind's safe write path. Use when asked to
  remember something, or when an insight is worth keeping across sessions.
---

# OMI memory (via omind)

Long-term memory lives in `{omi_dir}` — one Markdown note per insight,
readable by every agent on this machine. The `obsidian` MCP server is already
pointed at that folder; use its tools to read and search memory.

## Writing memory — always through omind

Never write files into the OMI folder directly: a raw write can interleave
with another agent's write and corrupt the index. Create or update notes
through the single-writer CLI (an upsert — re-running with the same title
updates the note in place):

```bash
omind note --title "Short Descriptive Title" \\
  --summary "one-line summary of the insight" \\
  --tags "topic,subtopic" \\
  --vault "{vault}" --folder "{folder}" <<'BODY'
The full insight, in plain Markdown. Link related notes with real
[[wikilinks]] so the memory graph stays connected.
BODY
```

Rules:

- One note per insight, with a descriptive title — never a combined dump.
- Real `[[wikilinks]]` to related notes; tags are plain comma-separated words.
- `index.md` is maintained by omind — never edit it by hand.
"""
