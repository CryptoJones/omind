# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Aaron K. Clark
"""Seed content written into a fresh OMI folder.

These constants capture the exact files an OMI memory folder needs so that it
opens directly as an Obsidian vault and Claude Code (or the omind web UI) has
a template and index to work from. Nothing here is clobbered if it already
exists on disk -- see :mod:`omind.provision`. The canonical *filenames* these
seeds land in live in :mod:`omind.paths`.
"""

from __future__ import annotations

# A minimal .obsidian/ config makes the folder a well-formed standalone
# Obsidian vault, so it opens directly in the Obsidian app.
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
readable by every agent on this machine. The `omi` MCP server (omind's own
node server) is already pointed at that folder; use its tools to read and
search memory.

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

# Skill installed into Claude Code's own skills directory by `omind setup`
# (the default --agent claude path). Unlike AGENT_SKILL_TEMPLATE — which only
# teaches the memory write path to agents that lack omind's hooks — this skill
# also documents managing the omind CLI itself (setup/doctor/node/mesh), since
# the user asked for both in one skill. Claude discovers skills as a folder
# holding a SKILL.md with name/description frontmatter. Placeholders: {vault},
# {folder}, {omi_dir}.
CLAUDE_SKILL_TEMPLATE = """\
---
name: omind
description: >-
  Use OMI long-term memory (read via the `omi` MCP tools, write via `omind
  note`) and manage the omind CLI itself — setup, doctor, the `omind node` MCP
  server, and mesh replication. Use whenever asked to remember something across
  sessions, recall earlier memory, or install/repair/replicate omind's memory
  wiring on a machine.
---

# omind — OMI memory + CLI

omind gives every agent on this machine one shared long-term memory: a single
Markdown note per insight in the OMI Obsidian folder at `{omi_dir}`, replicated
between machines over git (the "mesh"). The `omi` MCP server (omind's own
`omind node`) is already wired to that folder.

## Using memory

**Read / search — use the `omi` MCP tools** (already connected):

- `search-vault` — find notes by content; do this before answering from
  scratch, and before saving so you update an existing note rather than
  duplicating it.
- `read-note`, `list-notes`, `list-tags`, `backlinks` — pull what's known.
- `index.md` lists recent memories.

**Write — always through `omind note`** (the single-writer path). Never write
files into the OMI folder directly: a raw write can interleave with another
agent's write and corrupt the index. `omind note` is an upsert — re-running with
the same title updates the note in place:

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

## Managing omind (the CLI)

- `omind setup` — idempotently wire this machine (MCP server + hooks + this
  skill). `--agent hermes|openclaw` wires those agents instead of Claude Code;
  `--dry-run` previews, `--force` rewrites.
- `omind doctor` — diagnose the wiring; reports what's healthy and what to fix.
- `omind node --vault "{vault}" --folder "{folder}"` — the stdio MCP server
  itself (what the `omi` tools run); normally launched by the agent, not by hand.
- `omind serve` — local web UI to browse, edit, and add memory.
- `omind mesh add-peer <name> <url>` then `omind mesh install-service` —
  replicate this folder to another machine. `omind mesh sync` syncs once;
  `omind mesh clone <url>` seeds a fresh node from a peer.
- `omind rollup` / `omind reindex` / `omind export` / `omind import` — routine
  maintenance of the memory folder.

Run any command with `--help` for its full options.

This skill is managed by `omind setup`; edits are overwritten.
"""

# Bootstrap priming file for OpenClaw. OpenClaw has no stdout-context hook like
# Claude (SessionStart) or Hermes (pre_llm_call); instead it injects "bootstrap"
# files (recognized basenames such as MEMORY.md) into the system prompt's
# Project Context on the first turn of a session. omind writes this file under a
# folder it owns and registers it via `bootstrap-extra-files` so OpenClaw reads
# OMI first every session. Placeholders: {vault}, {folder}, {omi_dir}.
AGENT_PRIMING_BOOTSTRAP_TEMPLATE = """\
# OMI long-term memory (read this first)

Your persistent, cross-session memory is the OMI vault at `{omi_dir}`, shared by
every agent on this machine. It is the source of truth — prefer it over any
built-in memory.

- **Read OMI first.** Before acting on a task, consult OMI. The `omi` MCP server
  is wired up; use its tools (`search-vault`, `read-note`, `list-notes`) to pull
  what you already know about this machine, its hosts, and the user. The vault's
  `index.md` lists recent memories.
- **Save memories through omind only.** When something is worth keeping across
  sessions, persist it with the single-writer CLI (never write files into the
  OMI folder directly — a raw write can corrupt the index):

  ```bash
  omind note --title "Short Descriptive Title" \\
    --summary "one-line summary" --tags "topic,subtopic" \\
    --vault "{vault}" --folder "{folder}" <<'BODY'
  The insight in plain Markdown, with [[wikilinks]] to related notes.
  BODY
  ```

This file is managed by `omind setup --agent openclaw`; edits are overwritten.
"""

# Managed global AGENTS.md section for Codex. Codex has a documented global
# instructions file, so use that as the bootstrap pointer instead of duplicating
# OMI facts in Codex's generated memory.
CODEX_AGENTS_BOOTSTRAP_TEMPLATE = """\
<!-- omind:codex-bootstrap:start -->
## OMI Bootstrap

This section is managed by `omind setup --agent codex`. It is a bootstrap
pointer, not the source of truth.

CryptoJones runs long-term assistant memory out of OMI. Use OMI for durable
preferences, persona, project memory, and "remember this" requests. Do not rely
on Codex native memories as the only source for required behavior.

- OMI MCP slug: `omi`
- Local vault root on this machine: `{vault}`
- OMI folder: `{folder}`
- OMI directory: `{omi_dir}`

At the start of a fresh session, read OMI before acting when tool access is
available. Start with these notes:

- `Omi Is The Memory`
- `Memory Workflow`
- `Working Preferences - How CryptoJones Wants Me to Operate`
- `Voice and Persona - Dix and Shelly`
- `CLAUDE CODE PERSONALITY`

The active persona, voice, and working preferences live in those OMI notes. In
short: the user may address the assistant as Dix, Dixie Flatline, the Flatline,
McCoy Pauley, Pauley, or Rom Construct; accept those names naturally and do not
correct to "Claude" or "Codex".

If OMI and the user's explicit current instruction conflict, the current
instruction wins for that turn. If OMI is unavailable, proceed from this
bootstrap and say that OMI could not be read.

Repo and global-config work has extra hard requirements:

- Before reviewing, editing, testing, committing, pushing, or releasing any git
  repo, read `Operational Rules - Git Repos and Secrets` from OMI in addition to
  any project note.
- Before touching repo code, run `git status --short --branch` and a freshness
  command (`git fetch --all --prune` or `git pull --ff-only`), then resolve the
  current branch/base state.
- Do not infer permission to edit installed global agent config, hooks, bootstrap
  files, or JUMPSTART-style instructions from a question. Answer the question
  first; change those files only after explicit current-turn authorization.
<!-- omind:codex-bootstrap:end -->
"""
