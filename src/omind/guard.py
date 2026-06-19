# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Aaron K. Clark
"""Harness-agnostic OMI-compliance enforcement decision engine.

`omind guard` is the single place every agent harness asks "may I run this
action?". Thin per-harness adapters (Claude Code's ``omi-guard.sh``, Hermes'
``pre_llm_call`` adapter, ...) normalize their event into the action schema
below and pipe it to ``omind guard check``. The policy and the per-turn gate
live HERE, so a rule — or a later learned lesson — enforces identically across
every agent.

Action schema (JSON on stdin to ``check``)::

    {
      "tool": "Bash",          # the tool / operation name
      "command": "...",        # shell command, for Bash-like tools (optional)
      "session": "abc123",     # session id, for the per-turn gate (optional)
      "is_omi_consult": false  # adapter sets true when this action reads OMI
    }

Decision order:
  1. An OMI consult sets the per-turn sentinel and is always allowed (so the
     gate can never deadlock — the clear-path is always available).
  2. HARD BLOCKS — the destructive/forge deny set, deterministic, no bypass.
  3. THE GATE — block until OMI was consulted this turn; ``omind guard reset``
     (the harness's turn-start hook) clears the sentinel.

The adapter fails open on its own parse errors, but the hard blocks are matched
here on the raw command so they cannot be skipped by a broken adapter.
"""

from __future__ import annotations

import contextlib
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

from omind import paths

#: The destructive / forge deny set. Phase 2 promotes this to a data table the
#: learning loop appends to; for now it is the seed policy, in code.
HARD_RULES: tuple[tuple[str, str], ...] = (
    (
        r"\bgh\s+auth\s+setup-git\b",
        "never 'gh auth setup-git'. GitHub auth = SSH, else the gh-YOLO PAT "
        "from pass via a one-shot credential helper. Read OMI: github-auth-ssh.",
    ),
    (
        r"(push|remote\s+(set-url|add))[^|;&]*https://[^\s]*github\.com",
        "no HTTPS-GitHub push/remote-set. Use SSH, or the gh-YOLO pass "
        "credential helper. Read OMI: github-auth-ssh.",
    ),
    (
        r"\bgh\s+pr\s+(create|merge)\b",
        "GitHub never gets a PR. PR + merge happen on Codeberg; GitHub mirrors "
        "Codeberg's exact commit. Read OMI: codeberg-authoritative.",
    ),
    (
        r"\bgit\s+push\b[^|;&]*github",
        "no discretionary GitHub push. Codeberg is the source of truth (push "
        "it first, over SSH). Read OMI: codeberg-authoritative.",
    ),
    (
        r"\bgh\s+repo\s+delete\b",
        "never delete a repo from a hook-reachable command. Typed-name "
        "confirmation only. Read OMI: Operational Rules - Git Repos and Secrets.",
    ),
    (
        r"gh\s+api[^|;&]*(-X\s*|--method\s*)DELETE[^|;&]*repos/",
        "never DELETE a repo via the API. Typed-name confirmation only. "
        "Read OMI: Operational Rules - Git Repos and Secrets.",
    ),
)

_HARD_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(rx), msg) for rx, msg in HARD_RULES
)

GATE_MESSAGE = (
    "consult OMI before acting this turn — read a note relevant to your task "
    "(an OMI search or read), then retry. One consult clears the rest of the "
    "turn. This is NOT a prompt to open the credential/auth notes."
)


@dataclass(frozen=True)
class Verdict:
    """A guard decision: allow (exit 0) or deny (exit 2 + ``reason``)."""

    allow: bool
    reason: str = ""

    @property
    def exit_code(self) -> int:
        return 0 if self.allow else 2


def _sentinel_path(session: str) -> Path:
    # Lives in omind's state dir (not /tmp) so the bash adapter and this Python
    # core agree on the path cross-platform — macOS's tempdir is not /tmp.
    safe = re.sub(r"[^A-Za-z0-9._-]", "", session) or "nosid"
    return paths.state_dir() / f"gate-{safe}"


def mark_consulted(session: str) -> None:
    """Record that OMI was consulted this turn (sets the per-turn sentinel)."""
    path = _sentinel_path(session)
    with contextlib.suppress(OSError):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()


def consulted_this_turn(session: str) -> bool:
    return _sentinel_path(session).exists()


def clear_gate(session: str) -> None:
    """Clear the per-turn consult sentinel (the harness's turn-start reset)."""
    with contextlib.suppress(OSError):
        _sentinel_path(session).unlink()


def decide(action: dict[str, Any]) -> Verdict:
    """The harness-agnostic policy. See the module docstring for the schema."""
    session = str(action.get("session") or "")
    command = str(action.get("command") or "")

    # 1) Consulting OMI sets the per-turn sentinel and is always allowed.
    if action.get("is_omi_consult"):
        mark_consulted(session)
        return Verdict(allow=True)

    # 2) Hard blocks — deterministic, no bypass.
    for pattern, message in _HARD_PATTERNS:
        if pattern.search(command):
            return Verdict(allow=False, reason=f"omi-guard (hard): {message}")

    # 3) The gate — block until OMI was consulted this turn.
    if consulted_this_turn(session):
        return Verdict(allow=True)
    return Verdict(allow=False, reason=f"omi-gate: {GATE_MESSAGE}")


def _load(stream: TextIO) -> dict[str, Any]:
    try:
        data = json.loads(stream.read() or "{}")
    except (ValueError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def run_guard(action_name: str, stream: TextIO | None = None) -> int:
    """CLI entry for ``omind guard <action>``. Returns the process exit code.

    ``check`` reads an action descriptor on stdin and prints the deny reason to
    stderr when blocking. ``reset`` clears the session's per-turn sentinel.
    Unknown actions are a no-op (exit 0) — a guard must never wedge the agent.
    """
    src = stream if stream is not None else sys.stdin
    if action_name == "reset":
        clear_gate(str(_load(src).get("session") or ""))
        return 0
    if action_name == "check":
        verdict = decide(_load(src))
        if not verdict.allow:
            sys.stderr.write(f"BLOCKED by {verdict.reason}\n")
        return verdict.exit_code
    return 0
