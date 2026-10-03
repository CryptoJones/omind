"""A wall-clock budget for judging one action in the guard (#460).

A hook the harness kills on its timeout returns no verdict at all, and a
missing verdict skips every gate. So judging one action runs under a deadline
well inside the hook timeout, and the guard decides what an exhausted budget
means: the hard rules fail CLOSED, the soft gates fail open.

The deadline is cooperative: the walk, the stage loops and the rule loops call
:func:`check` at their loop points, and :func:`check` raises
:class:`DeadlineExceededError` once the budget is spent. ``signal.SIGALRM``
would interrupt anything, but it is POSIX-only, fires only on the main
thread, and lands at an arbitrary bytecode; a watchdog thread cannot stop the
judging thread in Python at all. A check at each loop point works on every
platform and every thread, and always leaves the guard at a known point.

The active deadline lives in a :class:`contextvars.ContextVar`, so concurrent
judges (threads, the web app) never share one. With no active deadline,
:func:`check` does nothing, so code that runs outside a judging scope (tests,
``omind rules`` tooling) behaves exactly as before.
"""

from __future__ import annotations

import contextlib
import contextvars
import time
from collections.abc import Iterator

#: Seconds one action may spend being judged before the guard returns a
#: verdict anyway. The shortest hook timeout omind provisions for the OMI
#: guard is ``provision.OMI_GUARD_TIMEOUT`` (15 s); the budget leaves the
#: rest for the interpreter start (seconds under antivirus), the adapter's
#: rendering and the gate's retrieval suggestion.
JUDGE_BUDGET_SECONDS = 8.0

#: Seconds the hard rules may always take, however much of the budget an
#: earlier step spent (see :func:`at_least`).
HARD_RULE_FLOOR_SECONDS = 3.0


class DeadlineExceededError(Exception):
    """The active judging budget is spent (#460)."""


class Deadline:
    """An absolute :func:`time.monotonic` instant past which judging stops."""

    __slots__ = ("end",)

    def __init__(self, end: float) -> None:
        self.end = end

    def expired(self) -> bool:
        return time.monotonic() >= self.end


_ACTIVE: contextvars.ContextVar[Deadline | None] = contextvars.ContextVar(
    "omind_judge_deadline", default=None
)


def active() -> Deadline | None:
    """The deadline judging currently runs under, if any."""
    return _ACTIVE.get()


def expired() -> bool:
    """Whether a deadline is active and spent."""
    current = _ACTIVE.get()
    return current is not None and current.expired()


def check() -> None:
    """Raise :class:`DeadlineExceededError` when the active deadline is spent.
    A no-op outside a judging scope."""
    current = _ACTIVE.get()
    if current is not None and time.monotonic() >= current.end:
        raise DeadlineExceededError


@contextlib.contextmanager
def scope(seconds: float | None = None) -> Iterator[Deadline]:
    """Judge under a deadline ``seconds`` from now (default
    :data:`JUDGE_BUDGET_SECONDS`, read at call time so tests can shrink it).
    An enclosing scope wins: a nested call keeps the outer deadline, so the
    budget covers the whole action, not each step of it."""
    current = _ACTIVE.get()
    if current is not None:
        yield current
        return
    budget = JUDGE_BUDGET_SECONDS if seconds is None else seconds
    created = Deadline(time.monotonic() + budget)
    token = _ACTIVE.set(created)
    try:
        yield created
    finally:
        _ACTIVE.reset(token)


@contextlib.contextmanager
def at_least(seconds: float) -> Iterator[None]:
    """Extend the active deadline so that at least ``seconds`` remain, for
    the duration of the block. The hard rules run under this: they are
    normally judged in milliseconds, so time an earlier, slower step spent
    (a note rule's network lookup) must not turn a plain command into a
    deadline deny. Outside a judging scope it opens one of ``seconds``."""
    current = _ACTIVE.get()
    floor = time.monotonic() + seconds
    if current is not None and current.end >= floor:
        yield
        return
    token = _ACTIVE.set(Deadline(floor))
    try:
        yield
    finally:
        _ACTIVE.reset(token)
