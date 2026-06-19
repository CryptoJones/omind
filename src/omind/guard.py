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
  2b. GITHUB PUSH — blocked unless the command opts in with ``OMI_PUSH_GITHUB=1``
     (a deliberate mirror of Codeberg's exact commit); otherwise denied.
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
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

from omind import paths

#: The destructive / forge deny set — absolute, no bypass. Phase 2 promotes this
#: to a data table the learning loop appends to; for now it is the seed policy.
HARD_RULES: tuple[tuple[str, str], ...] = (
    (
        r"\bgh\s+auth\s+setup-git\b",
        "never 'gh auth setup-git'. GitHub auth = the gh-YOLO PAT from pass via "
        "a one-shot (per-command) credential helper. Read OMI: github-auth-ssh.",
    ),
    (
        r"\bgh\s+pr\s+(create|merge)\b",
        "GitHub never gets a PR. PR + merge happen on Codeberg; GitHub mirrors "
        "Codeberg's exact commit. Read OMI: codeberg-authoritative.",
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

#: GitHub PUSH — relaxed from the hard set to a DELIBERATE opt-in (2026-06-19).
#: A github push is the mirror of Codeberg's exact commit; it is blocked unless
#: the command explicitly opts in with ``OMI_PUSH_GITHUB=1``, so an impulsive
#: github-first push is still caught while a deliberate mirror sync goes through.
#: Codeberg stays the source of truth — push it first.
GITHUB_PUSH_RULES: tuple[tuple[str, str], ...] = (
    (
        r"(push|remote\s+(set-url|add))[^|;&]*https://[^\s]*github\.com",
        "no HTTPS-GitHub push/remote-set. For a deliberate mirror of Codeberg's "
        "exact commit, prefix OMI_PUSH_GITHUB=1 and use the gh-YOLO pass "
        "credential helper. Read OMI: github-auth-ssh, codeberg-authoritative.",
    ),
    (
        r"\bgit\s+push\b[^|;&]*github",
        "no discretionary GitHub push. Codeberg is the source of truth (push it "
        "first). A deliberate mirror push opts in with OMI_PUSH_GITHUB=1. "
        "Read OMI: codeberg-authoritative.",
    ),
)

#: Set in a command to deliberately allow a single GitHub mirror push.
GITHUB_PUSH_OPT_IN = re.compile(r"OMI_PUSH_GITHUB=1")

_HARD_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(rx), msg) for rx, msg in HARD_RULES
)
_GITHUB_PUSH_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(rx), msg) for rx, msg in GITHUB_PUSH_RULES
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


#: Pre-state-dir prototype guards wrote the per-turn sentinel to ``/tmp`` rather
#: than the state dir. The canonical guard never writes there, so any such file
#: is legacy litter the turn-start reset reaps — otherwise a machine upgrading
#: from the buggy version leaves stale ``/tmp/omi-gate-*`` files behind. A tuple
#: (not a hardcoded path) so tests can point it at a temp dir.
_LEGACY_SENTINEL_DIRS: tuple[Path, ...] = (Path("/tmp"), Path(tempfile.gettempdir()))
_LEGACY_SENTINEL_GLOB = "omi-gate-*"


def _reap_legacy_sentinels() -> None:
    """Delete leftover ``/tmp/omi-gate-*`` sentinels from the prototype guard."""
    seen: set[Path] = set()
    for directory in _LEGACY_SENTINEL_DIRS:
        if directory in seen:
            continue
        seen.add(directory)
        try:
            stale = list(directory.glob(_LEGACY_SENTINEL_GLOB))
        except OSError:
            continue
        for path in stale:
            with contextlib.suppress(OSError):
                path.unlink()


def clear_gate(session: str) -> None:
    """Clear the per-turn consult sentinel (the harness's turn-start reset).

    Also reaps legacy ``/tmp/omi-gate-*`` sentinels left by the pre-state-dir
    prototype guard, so a machine upgrading from that version does not keep stale
    sentinels around (the canonical guard never writes ``/tmp``)."""
    with contextlib.suppress(OSError):
        _sentinel_path(session).unlink()
    _reap_legacy_sentinels()


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

    # 2b) GitHub push — blocked unless the command deliberately opts in.
    if not GITHUB_PUSH_OPT_IN.search(command):
        for pattern, message in _GITHUB_PUSH_PATTERNS:
            if pattern.search(command):
                return Verdict(allow=False, reason=f"omi-guard (github-push): {message}")

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
