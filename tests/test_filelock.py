# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Aaron K. Clark
"""Tests for omind.filelock: the portable lock shim.

Serialization under contention is covered end-to-end by the store and hooks
concurrency tests; this exercises the shim's own contract on the host
platform (including the windows-latest CI legs).
"""

from __future__ import annotations

import os
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


# -- #319: the contended-lock wait must not starve a waiter --------------------


def test_poll_lock_serves_every_waiter_under_a_thundering_herd() -> None:
    """40 waiters on one non-blocking lock all get it — the property
    ``msvcrt.locking(LK_LOCK)``'s ten lockstep retries could not provide."""
    import threading

    mutex = threading.Lock()
    served: list[int] = []

    def worker(i: int) -> None:
        filelock._poll_lock(lambda: mutex.acquire(blocking=False))
        try:
            served.append(i)
        finally:
            mutex.release()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(40)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(served) == list(range(40))


def test_poll_lock_raises_oserror_at_the_deadline_with_jittered_backoff() -> None:
    now = [0.0]
    sleeps: list[float] = []

    def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        now[0] += seconds

    with pytest.raises(OSError, match="still contended"):
        filelock._poll_lock(lambda: False, timeout=1.0, sleep=fake_sleep, clock=lambda: now[0])
    assert len(sleeps) > 10  # many attempts, not LK_LOCK's fixed ten
    assert max(sleeps) <= filelock._POLL_MAX_S * 1.5
    assert len(set(sleeps)) > len(sleeps) // 2  # jittered, not lockstep
