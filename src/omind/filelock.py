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

import os
import sys

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
