# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Aaron K. Clark
"""Canonical filenames inside an OMI folder and omind's managed installs.

Single source of truth for the names half the codebase needs (store, transfer,
backup, provision, agents): renaming one of these is a one-line change here.
The seed *content* written into those files lives in :mod:`omind.seeds`.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import tempfile
from pathlib import Path

MEMORY_TEMPLATE_FILENAME = "Memory Template.md"
INDEX_FILENAME = "index.md"

#: Files that are scaffolding, not memories — excluded from listings.
RESERVED_FILENAMES = frozenset({MEMORY_TEMPLATE_FILENAME, INDEX_FILENAME})

#: Basenames that live in (or alongside) the OMI folder but are the vault's
#: table-of-contents / scaffolding, not a real memory: the derived ``index.md``
#: ("Recent Memories" TOC), the legacy ``MEMORY.md`` recent-memories index, and
#: the note template. Reading one is "relevant to everything", which is exactly
#: why it was the consult-gate DODGE — an agent could clear the per-turn gate
#: every turn by re-reading the index without ever consulting a task-relevant
#: note. So a Read of one of these does NOT count as a gate-clearing OMI consult
#: (the bash guard adapters and :func:`omind.verify.consult_target` both honor
#: this). Superset of :data:`RESERVED_FILENAMES`. Keep the bash adapters'
#: hard-coded basename list in sync with this set.
NON_CONSULT_FILENAMES = RESERVED_FILENAMES | {"MEMORY.md"}

#: Skill manifest name both Hermes and OpenClaw discover in a skill folder.
AGENT_SKILL_FILENAME = "SKILL.md"

#: The session-journal filename convention. Single source of truth: hooks
#: (the writer), journal (rollup/migration globs), and store (index
#: exclusion) all derive their names, globs, and regexes from this prefix.
JOURNAL_PREFIX = "Session Journal"
JOURNAL_GLOB = f"{JOURNAL_PREFIX} *.md"


def fsync_dir(directory: Path) -> None:
    """Best-effort fsync of a directory so a rename into it is durable.
    Silently skipped where the platform can't open a directory fd (Windows)."""
    with contextlib.suppress(OSError, AttributeError):
        dir_fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)


def atomic_write_bytes(path: Path, data: bytes, *, mode: int | None = None) -> None:
    """Write ``data`` to ``path`` atomically: same-dir temp file + ``os.replace``.

    The one writer for notes, managed configs/hooks, and state files. A plain
    ``path.write_text`` truncates in place, so a crash / OOM / ENOSPC mid-write
    leaves a torn file — for a note, lost memory; for a harness config, a
    bricked agent; for ``omi-guard.sh``, every tool call denied. The temp file +
    rename makes a concurrent reader see either the old file or the new one in
    full, and the directory fsync makes the rename itself durable across a
    power loss. ``mode`` is applied to the temp file before the rename.
    """
    directory = path.parent
    directory.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".tmp-", suffix=path.suffix or ".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        if mode is not None:
            with contextlib.suppress(OSError):
                os.chmod(tmp, mode)
        os.replace(tmp, path)
        fsync_dir(directory)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def atomic_write_text(path: Path, text: str, *, mode: int | None = None) -> None:
    """:func:`atomic_write_bytes` for UTF-8 text, never translating ``\n``.

    Bytes are identical on every OS. Text mode's default newline rewrote every
    ``\n`` to ``os.linesep``, so notes were CRLF on Windows and nothing could
    see it because reads translate back — four Windows-only bugs (compliance
    log rotation, both halves of the transaction journal) — and a shell hook
    whose shebang line ends ``\r\n`` is not a shell script.
    """
    atomic_write_bytes(path, text.encode("utf-8"), mode=mode)


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


def access_state_path(omi_dir: Path) -> Path:
    """Machine-local access-frequency state for dynamic core-memory selection."""
    return state_dir() / f"access-{_omi_dir_digest(omi_dir)}.sqlite3"


def consolidation_dir(omi_dir: Path) -> Path:
    """Machine-local proposal/draft storage for one vault's reviewed merges."""
    return state_dir() / f"consolidate-{_omi_dir_digest(omi_dir)}"


def transaction_dir(omi_dir: Path) -> Path:
    """Journal + pre-image storage for one vault's multi-note transactions.

    Machine-local and outside the vault: it records what the *filesystem* was
    doing, is meaningless on another peer, and must never be mesh-synced.
    """
    return state_dir() / f"txn-{_omi_dir_digest(omi_dir)}"


def maintain_lock_path(omi_dir: Path) -> Path:
    """Single-instance mutex for ``omind maintain`` (one janitor per vault)."""
    return state_dir() / f"maintain-{_omi_dir_digest(omi_dir)}.lock"


def maintain_state_path(omi_dir: Path) -> Path:
    """Last ``omind maintain`` run report — state-dir, never a vault note."""
    return state_dir() / f"maintain-{_omi_dir_digest(omi_dir)}.json"
