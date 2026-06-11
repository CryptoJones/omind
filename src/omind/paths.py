# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Aaron K. Clark
"""Canonical filenames inside an OMI folder and omind's managed installs.

Single source of truth for the names half the codebase needs (store, transfer,
backup, provision, agents): renaming one of these is a one-line change here.
The seed *content* written into those files lives in :mod:`omind.seeds`.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

MEMORY_TEMPLATE_FILENAME = "Memory Template.md"
INDEX_FILENAME = "index.md"

#: Files that are scaffolding, not memories — excluded from listings.
RESERVED_FILENAMES = frozenset({MEMORY_TEMPLATE_FILENAME, INDEX_FILENAME})

#: Skill manifest name both Hermes and OpenClaw discover in a skill folder.
AGENT_SKILL_FILENAME = "SKILL.md"


def state_dir() -> Path:
    """omind's state directory: ``$XDG_STATE_HOME/omind`` or ``~/.local/state/omind``."""
    env = os.environ.get("XDG_STATE_HOME")
    base = Path(env).expanduser() if env else Path.home() / ".local" / "state"
    return base / "omind"


def _omi_dir_digest(omi_dir: Path) -> str:
    return hashlib.sha256(str(Path(omi_dir).expanduser().resolve()).encode()).hexdigest()[:12]


def sync_signal_path(omi_dir: Path) -> Path:
    """The write-signal file the node server touches and the mesh daemon watches.

    Keyed by the resolved OMI folder (not the node-id) so the server and the
    daemon agree on the path without sharing any configuration.
    """
    return state_dir() / f"sync-request-{_omi_dir_digest(omi_dir)}"


def sync_state_path(omi_dir: Path) -> Path:
    """Where `omind mesh sync` records its last outcome (read by doctor)."""
    return state_dir() / f"mesh-sync-{_omi_dir_digest(omi_dir)}.json"
