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
from collections.abc import Callable
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


# -- a real lock failure is not contention -------------------------------------


def _fail_lock_primitive(monkeypatch: pytest.MonkeyPatch, fail: Callable[[], None]) -> None:
    """Route the platform's non-blocking lock call through ``fail``."""

    def fake(*_args: object) -> None:
        fail()

    if sys.platform == "win32":
        monkeypatch.setattr(filelock.msvcrt, "locking", fake)
    else:
        monkeypatch.setattr(filelock.fcntl, "flock", fake)


@pytest.mark.parametrize("code", sorted(filelock._CONTENTION_ERRNOS))
def test_try_lock_fd_reports_contention_as_false(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: int
) -> None:
    def fail() -> None:
        raise OSError(code, os.strerror(code))

    _fail_lock_primitive(monkeypatch, fail)
    fd = os.open(tmp_path / "mutex", os.O_RDWR | os.O_CREAT | _O_BINARY, 0o600)
    try:
        assert filelock.try_lock_fd(fd) is False
    finally:
        os.close(fd)


def test_try_lock_fd_propagates_a_non_contention_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``ENOLCK`` is a broken lock, not a held one — it must not read as ``False``."""

    def fail() -> None:
        raise OSError(errno.ENOLCK, os.strerror(errno.ENOLCK))

    _fail_lock_primitive(monkeypatch, fail)
    fd = os.open(tmp_path / "mutex", os.O_RDWR | os.O_CREAT | _O_BINARY, 0o600)
    try:
        with pytest.raises(OSError) as raised:
            filelock.try_lock_fd(fd)
    finally:
        os.close(fd)
    assert raised.value.errno == errno.ENOLCK


def test_poll_lock_stops_on_the_first_non_contention_error() -> None:
    """The poll loop must not burn its ten-second ceiling on a real failure and
    then relabel it as contention (the Windows ``lock_fd`` path)."""
    calls = 0
    sleeps: list[float] = []

    def try_once() -> bool:
        nonlocal calls
        calls += 1
        raise OSError(errno.EBADF, os.strerror(errno.EBADF))

    with pytest.raises(OSError) as raised:
        filelock._poll_lock(try_once, sleep=sleeps.append)
    assert raised.value.errno == errno.EBADF
    assert calls == 1
    assert sleeps == []


def test_try_exclusive_closes_the_fd_when_the_lock_attempt_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    opened: list[int] = []
    real_open = os.open

    def tracking_open(*args: object, **kwargs: object) -> int:
        fd: int = real_open(*args, **kwargs)  # type: ignore[arg-type]
        opened.append(fd)
        return fd

    def fail() -> None:
        raise OSError(errno.ENOLCK, os.strerror(errno.ENOLCK))

    monkeypatch.setattr(filelock.os, "open", tracking_open)
    _fail_lock_primitive(monkeypatch, fail)
    with pytest.raises(OSError), filelock.try_exclusive(tmp_path / "mutex.lock"):
        pytest.fail("the block must not run when the lock attempt errors")
    assert len(opened) == 1
    with pytest.raises(OSError):
        os.fstat(opened[0])  # closed, not leaked
