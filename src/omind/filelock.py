# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Aaron K. Clark
"""Portable advisory file locking for the single-writer guarantees.

POSIX gets ``fcntl.flock``; Windows gets ``msvcrt.locking`` over a one-byte
region at offset 0. Both serialize every omind writer that locks the same
file, which is all the store's ``.omi.lock`` and the journal append path
need — no byte of locked region ever overlaps actual data.

``msvcrt.locking(LK_LOCK)`` retries once a second for ten seconds before
raising ``OSError``; omind holds these locks for milliseconds, so a ten-second
stall means something is genuinely wedged and surfacing the error beats
queueing forever.
"""

from __future__ import annotations

import contextlib
import os
import sys
from collections.abc import Iterator
from pathlib import Path

if sys.platform == "win32":
    import msvcrt

    _REGION_BYTES = 1

    def lock_fd(fd: int) -> None:
        """Block until this process holds the exclusive lock on ``fd``."""
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_LOCK, _REGION_BYTES)

    def try_lock_fd(fd: int) -> bool:
        """Take the exclusive lock without blocking; ``False`` if held elsewhere."""
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, _REGION_BYTES)
        except OSError:
            return False
        return True

    def unlock_fd(fd: int) -> None:
        """Release the lock taken by :func:`lock_fd`."""
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, _REGION_BYTES)

else:
    import fcntl

    def lock_fd(fd: int) -> None:
        """Block until this process holds the exclusive lock on ``fd``."""
        fcntl.flock(fd, fcntl.LOCK_EX)

    def try_lock_fd(fd: int) -> bool:
        """Take the exclusive lock without blocking; ``False`` if held elsewhere."""
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False
        return True

    def unlock_fd(fd: int) -> None:
        """Release the lock taken by :func:`lock_fd`."""
        fcntl.flock(fd, fcntl.LOCK_UN)


#: ``O_BINARY``: on Windows ``os.open`` defaults to the CRT's text mode, which
#: rewrites the ``\n`` in our bytes to ``\r\n`` mid-write.
#: ``O_NOFOLLOW``: refuse to open the path if its final component is a symlink.
#: Both are absent on the platforms that don't need them, hence ``getattr``.
_BINARY = getattr(os, "O_BINARY", 0)
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


@contextlib.contextmanager
def append_locked(path: Path, *, mode: int = 0o600) -> Iterator[int]:
    """Open ``path`` for appending, hold the exclusive lock, yield the fd.

    The single discipline for omind's append-only hot-path writers — the
    journal, the compliance log, the AI-usage log. Each of them ran its own
    copy of open → lock → write → unlock → close, and each resolved the path
    *before* taking the lock: a symlink swapped in at that path between the two
    would redirect the append somewhere of an attacker's choosing. ``O_NOFOLLOW``
    closes that window, giving these writers the same property the store already
    gets from its lockfile discipline.

    Not exploitable on a single-user box — these paths live under the user's own
    state dir and vault. This is defense-in-depth parity, and one place to fix
    rather than three (see issue #187).

    Raises ``OSError`` (``ELOOP`` when the final component is a symlink); every
    caller here is best-effort and already catches it.
    """
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | _BINARY | _NOFOLLOW, mode)
    try:
        lock_fd(fd)
        yield fd
    finally:
        with contextlib.suppress(OSError):
            unlock_fd(fd)
        os.close(fd)


@contextlib.contextmanager
def try_exclusive(path: Path, *, mode: int = 0o600) -> Iterator[bool]:
    """Try to take the exclusive lock on ``path`` WITHOUT blocking.

    Yields ``True`` when this process took the lock (held for the block) and
    ``False`` when another process already holds it — the primitive behind the
    janitor's single-instance mutex and its "is a sync in flight?" probe, where
    waiting is exactly the wrong behaviour. Same sibling-``.lock`` discipline as
    :func:`exclusive`.
    """
    fd = os.open(path, os.O_RDWR | os.O_CREAT | _BINARY | _NOFOLLOW, mode)
    acquired = try_lock_fd(fd)
    try:
        yield acquired
    finally:
        if acquired:
            with contextlib.suppress(OSError):
                unlock_fd(fd)
        os.close(fd)


@contextlib.contextmanager
def exclusive(path: Path, *, mode: int = 0o600) -> Iterator[int]:
    """Open (creating) ``path`` read-write and hold the exclusive lock on it.

    For serializing read-modify-write of small state files — gate sentinels,
    re-close/off-topic counters, loop-guard counters, the mesh node config.
    Hook processes from parallel tool calls and multiple agents fire
    concurrently; without this, interleaved load→mutate→save pairs lose
    increments and consult records (2026-08-27 review). Callers must hold the
    lock on a SIBLING path (``<name>.lock``), never on the data file itself:
    the write side replaces the data file atomically, and a flock on a
    replaced inode protects nothing.
    """
    fd = os.open(path, os.O_RDWR | os.O_CREAT | _BINARY | _NOFOLLOW, mode)
    try:
        lock_fd(fd)
        yield fd
    finally:
        with contextlib.suppress(OSError):
            unlock_fd(fd)
        os.close(fd)
