# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Aaron K. Clark
"""Portable advisory file locking for the single-writer guarantees.

POSIX gets ``fcntl.flock``; Windows gets ``msvcrt.locking`` over a one-byte
region at offset 0. Both serialize every omind writer that locks the same
file, which is all the store's ``.omi.lock`` and the journal append path
need — no byte of locked region ever overlaps actual data.

Windows has no blocking lock worth using. ``msvcrt.locking(LK_LOCK)`` is ten
attempts exactly one second apart, so every waiter that lost round one sleeps
the same second and wakes on the same timer tick: the herd re-collides each
round, and with enough writers one of them loses all ten and gets ``OSError``.
Every append caller here is best-effort, so that surfaced as a silently dropped
journal / compliance / AI-usage line (#319 — 39 of 40 concurrent appends).
:func:`_poll_lock` polls the non-blocking lock with jittered backoff instead:
hundreds of de-synchronised attempts inside the same ceiling. omind holds these
locks for milliseconds, so a ten-second stall still means something is
genuinely wedged, and surfacing the error still beats queueing forever.
"""

from __future__ import annotations

import contextlib
import errno
import os
import random
import sys
import time
from collections.abc import Callable, Iterator
from pathlib import Path

#: Ceiling on waiting for a contended lock, and the backoff bounds inside it.
LOCK_TIMEOUT_S = 10.0
_POLL_MIN_S = 0.001
_POLL_MAX_S = 0.05


def _poll_lock(
    try_once: Callable[[], bool],
    *,
    timeout: float = LOCK_TIMEOUT_S,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    """Call ``try_once`` until it takes the lock; ``OSError`` after ``timeout``.

    The jitter is the point, not a nicety: waiters that sleep identical
    intervals stay in lockstep and keep colliding (see the module docstring).
    """
    deadline = clock() + timeout
    delay = _POLL_MIN_S
    while not try_once():
        if clock() >= deadline:
            raise OSError(errno.EDEADLK, f"file lock still contended after {timeout:g}s")
        sleep(delay * (0.5 + random.random()))
        delay = min(delay * 2, _POLL_MAX_S)


if sys.platform == "win32":
    import msvcrt

    _REGION_BYTES = 1

    def lock_fd(fd: int) -> None:
        """Block until this process holds the exclusive lock on ``fd``."""
        _poll_lock(lambda: try_lock_fd(fd))

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

#: Public alias: callers that open their own fd to hold a lock need the same
#: no-text-mode flag this module uses, and shouldn't re-derive it.
BINARY = _BINARY


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
