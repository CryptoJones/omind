# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Aaron K. Clark
"""Tests for omind.filelock: the portable lock shim.

Serialization under contention is covered end-to-end by the store and hooks
concurrency tests; this exercises the shim's own contract on the host
platform (including the windows-latest CI legs).
"""

from __future__ import annotations

import errno
import os
import sys
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
    """``lock_fd`` must not lose acquisitions under same-process contention (#319).

    On Windows ``msvcrt.locking(LK_LOCK)`` is a 10-attempt poll at 1-second
    spacing: ten contenders at 1 Hz drain through in a second, but the 11th
    onwards exhaust their attempts and raise ``EDEADLK`` — which
    ``append_locked`` / ``exclusive`` swallow as ``except OSError``, silently
    dropping the write.  The fix retries ``LK_NBLCK`` (which returns ``EACCES``,
    errno 13, on every real contention) at 1 ms, draining 40 contenders in
    ~1 s.  This test fires 40 threads through a barrier on 40 separate fds to
    the same file and asserts that (a) every thread eventually acquires the
    lock and (b) at most one holds it at any instant.
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


@pytest.mark.skipif(sys.platform != "win32", reason="msvcrt-only: Windows")
def test_lock_fd_raises_after_timeout_on_persistent_contention(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``lock_fd`` must raise ``OSError`` after the deadline, not retry forever."""
    path = tmp_path / "mutex"
    fd = os.open(path, os.O_RDWR | os.O_CREAT | _O_BINARY, 0o600)
    try:
        # Hold the lock so every LK_NBLCK attempt fails with EACCES.
        filelock.lock_fd(fd)

        holder = os.open(path, os.O_RDWR | _O_BINARY, 0o600)
        monkeypatch.setattr(filelock, "_LOCK_TIMEOUT", 0.05)  # 50 ms deadline
        start = time.monotonic()
        with pytest.raises(OSError):
            filelock.lock_fd(holder)  # retries for ~50 ms, then gives up
        elapsed = time.monotonic() - start
        assert 0.05 <= elapsed < 2.0  # bounded by the shortened timeout
        os.close(holder)
    finally:
        filelock.unlock_fd(fd)
        os.close(fd)


@pytest.mark.skipif(sys.platform != "win32", reason="msvcrt-only: Windows")
def test_lock_fd_propagates_non_contention_oserror_immediately(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An ``OSError`` that is not ``EACCES``/``EDEADLK`` must propagate without retry."""
    path = tmp_path / "mutex"
    fd = os.open(path, os.O_RDWR | os.O_CREAT | _O_BINARY, 0o600)
    calls = [0]
    bogus = errno.ENOENT  # "No such file or directory" — not a lock error

    def fake_locking(_fd: int, _mode: int, _nbytes: int) -> int:
        calls[0] += 1
        raise OSError(bogus, os.strerror(bogus))

    import msvcrt as _msvcrt  # noqa: E402 — Windows-only
    monkeypatch.setattr(_msvcrt, "locking", fake_locking)
    monkeypatch.setattr(filelock, "_LOCK_TIMEOUT", 5.0)  # would loop 5 s if not short-circuited

    try:
        with pytest.raises(OSError) as exc_info:
            filelock.lock_fd(fd)
        assert exc_info.value.errno == bogus
        assert calls[0] == 1  # exactly one attempt, no retry
    finally:
        os.close(fd)
