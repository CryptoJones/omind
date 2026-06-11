# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Aaron K. Clark
"""Seed content written into a fresh OMI folder.

These constants capture the exact files an OMI memory folder needs so that
`obsidian-mcp` accepts it as a vault and Claude Code (or the omind web UI) has
a template and index to work from. Nothing here is clobbered if it already
exists on disk -- see :mod:`omind.provision`. The canonical *filenames* these
seeds land in live in :mod:`omind.paths`.
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

# Node preload that guarantees obsidian-mcp dies when it can no longer serve.
# The server's chokidar file watcher keeps the Node event loop alive through
# every failure mode, so the process must be told to exit. Two are handled:
# stdin EOF (the agent exited; otherwise the server orphans), and the MCP SDK
# transport detaching from stdin without an EOF (the server then reads and
# silently discards every request, and clients hang forever — see issue #49).
# Registering the server as a direct `node --require <this file> ...` command
# (instead of `npx -y obsidian-mcp`) also lets Claude Code's terminating signal
# reach Node directly rather than being swallowed by the npx/npm wrapper chain.
# (Installed as omind.paths.EOF_GUARD_FILENAME.)
EOF_GUARD_JS = """\
// Managed by omind. Exit obsidian-mcp when it can no longer serve; its
// chokidar file watcher otherwise keeps the Node event loop alive forever.
//
// 1. Orphaning: stdin (the MCP client pipe) closes when the agent exits, but
//    the process lingers. Exit cleanly on stdin end/close.
// 2. Silent deafness (issue #49): the MCP SDK's stdio transport can close
//    WITHOUT an EOF, removing its stdin "data" listener while the process
//    lives on. stdin keeps flowing with no consumer, so every request is read
//    and discarded and clients hang with no error. Once a transport has
//    attached, its disappearance is fatal: exit non-zero so the client sees a
//    dead server immediately instead of an unbounded hang.
const die = (code) => process.exit(code);
process.stdin.on("end", () => die(0));
process.stdin.on("close", () => die(0));

const intervalMs = Number(process.env.OMIND_EOF_GUARD_INTERVAL_MS) || 5000;
let sawTransport = false;
setInterval(() => {
  if (process.stdin.listenerCount("data") > 0) {
    sawTransport = true;
  } else if (sawTransport) {
    console.error("omind eof-guard: MCP transport detached from stdin; exiting.");
    die(1);
  }
}, intervalMs).unref();
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
