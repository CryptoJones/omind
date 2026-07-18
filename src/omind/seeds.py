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
  Recall and persist long-term OMI memories, and obtain authoritative omind
  command syntax. Use for /omind help, CLI questions, memory recall, or when an
  insight is worth keeping across sessions.
---

# OMI memory (via omind)

Long-term memory lives in `{omi_dir}` — one Markdown note per insight,
readable by every agent on this machine. The `omi` MCP server (omind's own
node server) is already pointed at that folder.

## Help and recall

- For `/omind help` or any CLI syntax question, call the OMI MCP `help` tool.
  If MCP is unavailable, run `omind help <command path>` locally.
- Search with `search-vault`, then retrieve selected memories with
  `recall-note`. Use `read-note` only for raw Markdown/editing fields.

## Writing memory — always through omind

Never write files into the OMI folder directly: a raw write can interleave
with another agent's write and corrupt the index. Create or update notes
through the single-writer CLI (an upsert — re-running with the same title
updates the note in place):

Call the OMI MCP `help` tool with `command: "note"` before constructing the
write so the installed CLI, not this skill, supplies the current syntax.

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
  is wired up; use `search-vault` then token-bounded `recall-note` to pull
  what you already know about this machine, its hosts, and the user. The vault's
  `index.md` lists recent memories.
- **Get live syntax.** For `/omind help` or CLI questions, call OMI MCP `help`;
  do not rely on syntax embedded in this bootstrap.
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
