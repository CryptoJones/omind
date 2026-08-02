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

    def unlock_fd(fd: int) -> None:
        """Release the lock taken by :func:`lock_fd`."""
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, _REGION_BYTES)

else:
    import fcntl

    def lock_fd(fd: int) -> None:
        """Block until this process holds the exclusive lock on ``fd``."""
        fcntl.flock(fd, fcntl.LOCK_EX)

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
