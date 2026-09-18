# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Aaron K. Clark
"""Tests for omind.filelock: the portable lock shim.

Serialization under contention is covered end-to-end by the store and hooks
concurrency tests; this exercises the shim's own contract on the host
platform (including the windows-latest CI legs).
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import pytest

from omind import filelock

# On Windows os.open defaults to the CRT's text mode; O_BINARY keeps the
# written bytes literal, matching how the journal hot path opens files.
_O_BINARY = getattr(os, "O_BINARY", 0)


def test_lock_unlock_roundtrip(tmp_path: Path) -> None:
    fd = os.open(tmp_path / "lockfile", os.O_WRONLY | os.O_CREAT | _O_BINARY, 0o644)
    try:
        filelock.lock_fd(fd)
        filelock.unlock_fd(fd)
        filelock.lock_fd(fd)  # re-lockable after release
        filelock.unlock_fd(fd)
    finally:
        os.close(fd)


def test_lock_works_on_empty_and_append_fds(tmp_path: Path) -> None:
    """The journal locks an O_APPEND fd on a possibly empty file."""
    path = tmp_path / "journal.md"
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | _O_BINARY, 0o644)
    try:
        filelock.lock_fd(fd)
        os.write(fd, b"- entry\n")
        filelock.unlock_fd(fd)
    finally:
        os.close(fd)
    assert path.read_bytes() == b"- entry\n"


def test_append_locked_creates_appends_and_closes(tmp_path: Path) -> None:
    path = tmp_path / "log.jsonl"
    with filelock.append_locked(path) as fd:
        os.write(fd, b"one\n")
    with filelock.append_locked(path) as fd:
        os.write(fd, b"two\n")
        assert os.fstat(fd).st_size == 8  # the fd sees what is already there
    assert path.read_bytes() == b"one\ntwo\n"
    with pytest.raises(OSError):
        os.fstat(fd)  # the context manager closed it


def test_append_locked_creates_the_file_unreadable_by_others(tmp_path: Path) -> None:
    path = tmp_path / "log.jsonl"
    with filelock.append_locked(path) as fd:
        os.write(fd, b"x\n")
    if os.name != "nt":  # POSIX permission bits only
        assert path.stat().st_mode & 0o077 == 0


@pytest.mark.skipif(
    not hasattr(os, "O_NOFOLLOW"), reason="O_NOFOLLOW is POSIX-only (Windows CI legs)"
)
def test_append_locked_refuses_to_follow_a_symlink(tmp_path: Path) -> None:
    """A symlink swapped in at the path must not redirect the append (#187)."""
    victim = tmp_path / "elsewhere.txt"
    victim.write_bytes(b"untouched\n")
    path = tmp_path / "log.jsonl"
    path.symlink_to(victim)

    with pytest.raises(OSError), filelock.append_locked(path) as fd:
        os.write(fd, b"redirected\n")

    assert victim.read_bytes() == b"untouched\n"


def test_lock_fd_serializes_threads_in_same_process(tmp_path: Path) -> None:
    """``lock_fd`` must BLOCK (not raise EDEADLK) under same-process contention (#319).

    On Windows ``msvcrt.locking(LK_LOCK)`` returns EDEADLK (errno 36) when a
    second thread in the same process tries to lock a byte range its own
    process already holds — even with separate fds.  The fix uses ``LK_NBLCK``
    in a retry loop so contention surfaces as ``EACCES`` (errno 13), which we
    catch and wait on.  This test verifies that 40 threads, each holding the
    lock long enough to prove serialization, all complete without losing a
    single acquisition.
    """
    path = tmp_path / "mutex"
    fds = [
        os.open(path, os.O_RDWR | os.O_CREAT | _O_BINARY, 0o600) for _ in range(40)
    ]
    acquired = [False] * 40
    held_count = 0
    max_held = 0
    guard = threading.Lock()
    barrier = threading.Barrier(40)

    def worker(i: int) -> None:
        barrier.wait()  # maximise contention: all threads fire at once
        filelock.lock_fd(fds[i])
        try:
            with guard:
                nonlocal held_count, max_held
                held_count += 1
                max_held = max(max_held, held_count)
            # hold the lock briefly so other threads block on it
            time.sleep(0.01)
            acquired[i] = True
        finally:
            with guard:
                held_count -= 1
            filelock.unlock_fd(fds[i])

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(40)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    for fd in fds:
        os.close(fd)

    assert all(acquired), "some threads failed to acquire the lock"
    assert max_held == 1, "lock was not serialized (observed concurrent holders)"
