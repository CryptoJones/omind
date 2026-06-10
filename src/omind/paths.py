# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Aaron K. Clark
"""Canonical filenames inside an OMI folder and omind's managed installs.

Single source of truth for the names half the codebase needs (store, transfer,
backup, provision, agents): renaming one of these is a one-line change here.
The seed *content* written into those files lives in :mod:`omind.seeds`.
"""

from __future__ import annotations

MEMORY_TEMPLATE_FILENAME = "Memory Template.md"
INDEX_FILENAME = "index.md"

#: Files that are scaffolding, not memories — excluded from listings.
RESERVED_FILENAMES = frozenset({MEMORY_TEMPLATE_FILENAME, INDEX_FILENAME})

#: The stdin-EOF preload installed next to obsidian-mcp (see omind.seeds).
EOF_GUARD_FILENAME = "obsidian-exit-on-eof.js"

#: Skill manifest name both Hermes and OpenClaw discover in a skill folder.
AGENT_SKILL_FILENAME = "SKILL.md"
