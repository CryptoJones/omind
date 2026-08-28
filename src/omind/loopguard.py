"""Autonomous-loop guard — refuse to stop while a loop is *armed*.

CryptoJones runs long autonomous ``/loop``s and requires the agent to KEEP
WORKING — never halting at a self-declared "natural stopping point", never asking
permission, never idling. This is the *enforcement* (advisory memory notes weren't
enough): while a loop is armed, the Claude Code ``Stop`` hook emits
``{"decision": "block", "reason": ...}`` so the agent cannot end its turn — it is
re-prompted to find and execute the next task instead.

Operator control (a deliberate switch, like the guard pause)::

    omind loop arm        # start refusing stops
    omind loop disarm     # allow stops again — the ONLY clean way out
    omind loop status

Safety (a runaway no-stop hook is dangerous, so it is bounded three ways):

* **Work resets the spin counter.** Any ``PostToolUse`` (real work) resets the
  consecutive-block counter, so relentless work never trips the backstop; only a
  *pure* stop-spin with zero work between attempts climbs it.
* **A counter cap auto-disarms.** Exceeding ``max_blocks`` consecutive blocks
  auto-disarms and ALLOWS the stop (the agent is genuinely wedged) — logged.
* **An expiry self-clears.** Arming carries an expiry so a forgotten flag can't
  trap a future, unrelated session.
* **The loop is owned by one session.** The armed state records an *owner*
  session (from ``omind loop arm --session`` / ``$CLAUDE_SESSION_ID``, or claimed
  by the first session to hit a Stop). A DIFFERENT concurrent session is never
  refused and its ``PostToolUse`` never resets the owner's counter — arming one
  ``/loop`` can no longer trap every other session on the machine.

Like the rest of :mod:`omind.hooks`, every entry point swallows errors and fails
**open** — to *allowing* the stop. A broken guard must never trap the agent.
"""

from __future__ import annotations

import contextlib
import json
import os
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from omind import filelock, paths

DEFAULT_MAX_BLOCKS = 25
DEFAULT_HOURS = 24.0

#: The instruction fed back to the agent when a stop is refused.
DIRECTIVE = (
    "AUTONOMOUS LOOP ARMED (omind loop guard) — DO NOT STOP. "
    "You are NOT out of work. Re-read the active plan / backlog / roadmap and execute the next "
    "concrete task NOW. If the headline next step is blocked on a resource only CryptoJones can "
    "provide, do the most valuable adjacent work instead (the next milestone's prep, hardening, "
    "tests, a de-risk, docs-coherence) — never idle, never ask permission, never declare a "
    "'natural stopping point'. Answer any pending question in one line, then keep working. "
    "The ONLY way to stop is for CryptoJones to run `omind loop disarm`."
)


def _path() -> Path:
    return paths.state_dir() / "loop_guard.json"


def _now(now: datetime | None) -> datetime:
    return now if now is not None else datetime.now(timezone.utc)


def _clean_session(session: object) -> str | None:
    cleaned = "".join(c for c in str(session or "") if c.isalnum() or c in "._-")
    return cleaned or None


def _load() -> dict[str, Any]:
    try:
        return json.loads(_path().read_text(encoding="utf-8"))  # type: ignore[no-any-return]
    except (OSError, ValueError):
        return {}


def _save(state: dict[str, Any]) -> None:
    try:
        p = _path()
        p.parent.mkdir(parents=True, exist_ok=True)
        # Unique tmp name: a fixed ".tmp" is shared by concurrent hook processes
        # and two writers would corrupt each other's payload (2026-08-27 review).
        tmp = p.with_suffix(f".json.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
        os.replace(tmp, p)
    except OSError:
        pass


def _mutate(mutate: Callable[[dict[str, Any]], dict[str, Any] | None]) -> None:
    """Locked read-modify-write of the loop-guard state file.

    Stop-hook and PostToolUse hook processes from concurrent sessions fire in
    parallel; an unlocked load→save pair loses increments (delays the backstop)
    and reset signals (premature auto-disarm) (2026-08-27 review). The flock
    lives on a sibling ``.lock`` file — the state file itself is replaced
    atomically, so a lock on the data inode would protect nothing."""
    p = _path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        with filelock.exclusive(p.with_suffix(".json.lock")):
            result = mutate(_load())
            if result is not None:
                _save(result)
    except OSError:
        pass


def arm(
    *,
    reason: str | None = None,
    max_blocks: int = DEFAULT_MAX_BLOCKS,
    hours: float = DEFAULT_HOURS,
    session: object | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Arm the guard: refuse stops until disarmed (or the expiry/backstop fires).

    ``session`` is the loop's owner — only that session is refused. When unknown
    (a plain operator ``omind loop arm``) it is claimed by the first session to
    hit a Stop, so a global arm still can't trap every concurrent session.
    """
    t = _now(now)
    expires = t + timedelta(hours=hours) if hours and hours > 0 else None
    state: dict[str, Any] = {
        "armed": True,
        "reason": reason,
        "armed_at": t.isoformat(),
        "expires_at": expires.isoformat() if expires else None,
        "blocks": 0,
        "max_blocks": max_blocks,
        "owner": _clean_session(session),
    }
    _save(state)
    return state


def disarm() -> None:
    """Allow stops again. Idempotent; never raises."""
    with contextlib.suppress(OSError):
        _path().unlink(missing_ok=True)


def _expired(state: dict[str, Any], now: datetime | None) -> bool:
    raw = state.get("expires_at")
    if not raw:
        return False
    try:
        return _now(now) >= datetime.fromisoformat(str(raw))
    except ValueError:
        return False


def is_armed(now: datetime | None = None) -> bool:
    """True when a loop is armed and not expired (the expiry self-clears)."""
    state = _load()
    if not state.get("armed"):
        return False
    if _expired(state, now):
        disarm()
        return False
    return True


def reset(session: object | None = None) -> None:
    """Reset the consecutive-block counter — called when real work happens
    (``PostToolUse``), so relentless work never trips the no-work backstop. Only
    the OWNER session's work resets the counter; a concurrent session's work must
    not, or it would keep the backstop from ever firing for a wedged owner."""

    def _apply(state: dict[str, Any]) -> dict[str, Any] | None:
        if not state.get("armed") or not state.get("blocks"):
            return None
        owner = state.get("owner")
        sess = _clean_session(session)
        if owner is not None and sess is not None and sess != owner:
            return None
        state["blocks"] = 0
        return state

    _mutate(_apply)


def register_block(session: object | None = None, now: datetime | None = None) -> tuple[bool, str]:
    """Account for a stop attempt while armed.

    Returns ``(True, directive)`` to REFUSE the stop, or ``(False, note)`` to
    ALLOW it (not armed, expired, a DIFFERENT session than the loop's owner, or
    the no-work backstop tripped → auto-disarm).
    """
    if not is_armed(now):
        return (False, "")
    verdict: list[tuple[bool, str]] = []

    def _apply(state: dict[str, Any]) -> dict[str, Any] | None:
        sess = _clean_session(session)
        owner = state.get("owner")
        if sess is not None:
            if owner is None:
                # First session to hit a Stop under a global arm claims the loop.
                state["owner"] = sess
            elif sess != owner:
                # A different, concurrent session — never trap it.
                verdict.append((False, ""))
                return None
        blocks = int(state.get("blocks", 0)) + 1
        max_blocks = int(state.get("max_blocks", DEFAULT_MAX_BLOCKS))
        if blocks > max_blocks:
            disarm()
            verdict.append(
                (
                    False,
                    f"loop guard: {blocks - 1} consecutive stops with no work — "
                    "auto-disarmed (backstop).",
                )
            )
            return None
        state["blocks"] = blocks
        _save(state)
        extra = f" (reason: {state['reason']})" if state.get("reason") else ""
        verdict.append((True, DIRECTIVE + extra))
        return state

    _mutate(_apply)
    return verdict[0] if verdict else (False, "")


def status(now: datetime | None = None) -> dict[str, Any]:
    """A human-readable snapshot for ``omind loop status``."""
    state = _load()
    return {
        "armed": is_armed(now),
        "reason": state.get("reason"),
        "armed_at": state.get("armed_at"),
        "expires_at": state.get("expires_at"),
        "blocks": state.get("blocks", 0),
        "max_blocks": state.get("max_blocks", DEFAULT_MAX_BLOCKS),
        "owner": state.get("owner"),
    }
