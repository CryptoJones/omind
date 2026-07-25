---
name: omind
description: Use OMI durable memory and obtain authoritative omind command syntax. Trigger for `/omind help`, omind CLI usage or troubleshooting, requests to remember or recall information across sessions, OMI note maintenance, MCP memory operations, setup/doctor/mesh work, and AI token-expense questions.
---

# omind

Use the local `omi` MCP server as the source of truth. The configured memory
folder is `__OMI_DIR__`.

## Help and command syntax

For `/omind help` or any syntax question, call the OMI MCP `help` tool first.
Pass the requested command path, such as `ai usage`, `mesh sync`, or `guard
pause`. Present its returned syntax without inventing flags. If MCP is
unavailable, run `omind help <command path>` locally.

## Recall memory

1. Search with OMI `search-vault`; keep the default bounded result page unless
   more results are necessary.
2. Read selected memories with OMI `recall-note`. Use `read-note` only when raw
   Markdown and parsed editing fields are actually required.
3. Prefer current explicit user instructions when they conflict with memory.

## Save memory

Search before saving to avoid duplicates. Write through `omind note`, never a
raw file edit inside the OMI folder. Get live syntax from OMI `help` with
`command: "note"` before constructing the command. Use one descriptive note per
insight and connect related notes with `[[wikilinks]]`.

## Operate omind

Query OMI `help` before running setup, doctor, mesh, guard, backup, AI profile,
or release commands. Preserve repository and secret-handling rules recalled
from OMI. This skill is managed by `omind setup`; installed edits are replaced.
