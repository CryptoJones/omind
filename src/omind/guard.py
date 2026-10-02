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
  2. HARD BLOCKS — every ``hard`` rule in the data-driven policy
     (:mod:`omind.policy`): the destructive/forge seed set plus any learned rule
     the recidivism loop escalated. The ``github_push`` tier denies unless the
     command opts in with ``OMI_PUSH_GITHUB=1`` (a deliberate Codeberg mirror).
     ``soft`` rules never block here — the detector (Layer E) records them.
  3. THE GATE — block until OMI was consulted this turn; ``omind guard reset``
     (the harness's turn-start hook) clears the sentinel. Provably-inert
     inspection commands (a bare ``pwd``/``whoami``/...) skip the gate without
     satisfying it (#147).

The policy lives in data, but the seed rules live in code, so the hard blocks
are always enforceable here on the raw command even on a blank machine — they
cannot be skipped by a broken adapter or a missing policy file.
"""

from __future__ import annotations

import bisect
import contextlib
import functools
import json
import os
import re
import shlex
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, TextIO

from omind import compliance, filelock, paths, policy

if TYPE_CHECKING:
    from omind import rules as rules_mod

GATE_MESSAGE = (
    "ACTION BLOCKED. Next call OMI MCP `search-vault` with a focused task query, "
    "then call `recall-note` on one result and retry the blocked action. Do not "
    "open credential/auth notes unless the task is explicitly about credentials."
)
#: Opt back into the old strict behavior: a preflight MISS (the vault was
#: searched and nothing scored as relevant to the turn's task) forces a manual
#: search-vault/recall-note round trip instead of auto-clearing the gate. Default
#: off, mirroring OMI_VERIFY_REQUIRE's "cheap default, opt-in strictness" shape —
#: forcing a consult of a note that is, by construction, not relevant to the task
#: is the exact "read any note to dodge the gate" failure retrieval was built to
#: prevent; it just reappears one layer up when the vault genuinely has nothing
#: on-topic. The auto-clear is always logged to the compliance log (never silent)
#: so a session that's mostly missing is visible in `omind guard log`.
MISS_STRICT_ENV = "OMI_GATE_MISS_STRICT"
#: Synthetic rule id for a preflight miss that auto-cleared the gate.
GATE_NO_MATCH_RULE = "omi-gate-no-match"
GATE_WEAK_MATCH_RULE = "omi-gate-weak-match"
#: #321: a stale/superseded note was the best match and was NOT injected.
GATE_STALE_NOTE_RULE = "omi-gate-stale-note"
#: #321: this session already spent its whole preflight budget.
GATE_BUDGET_RULE = "omi-gate-budget-spent"

#: How the per-turn preflight delivers memory (#321).
#:
#: ``hint`` (default) names the candidate notes in one line and stops there —
#: the agent PULLS the body with ``recall-note`` if the turn actually needs it.
#: ``inject`` is the pre-9.2 push behavior, kept as an escape hatch. ``off``
#: clears the gate and says nothing.
#:
#: Why the default flipped: measured on this operator's own ledger, push
#: injection had shipped ~3.4M tokens of unrequested recall across 5,816 turns
#: at roughly 25% precision, framed as binding instruction and never removed.
#: The cost never surfaced as an omind error — it surfaced as "the model has
#: gotten worse in long sessions." Retrieval that costs tokens only when it is
#: useful beats injection that costs them always.
PREFLIGHT_MODE_ENV = "OMIND_PREFLIGHT"
PREFLIGHT_MODES = ("hint", "inject", "off")
DEFAULT_PREFLIGHT_MODE = "hint"
#: Hard ceiling on an automatic (non-hard-rule) preflight payload.
PREFLIGHT_HINT_CHARS = 500
#: Cumulative per-session preflight budget. Past this, automatic injection
#: tapers to nothing — a session already carrying this much omind context does
#: not need more of it (#321, fix 6). Hard rules are exempt; they are the
#: enforcement surface, not recall.
SESSION_INJECTION_BUDGET_CHARS = 60_000
#: Notes whose body advertises that part of it is wrong. Injecting one of these
#: under "the memory governs" is a confabulation generator (#321, fix 3); it is
#: still reachable by an explicit ``recall-note``, which shows the whole note
#: including the correction.
_STALE_MARKER_RE = re.compile(
    r"(?:^|\n)[ \t>*#-]*(?:\*\*|__)?\s*"
    r"(?:SUPERSEDED|CORRECTION|PATH UPDATE|OUT OF DATE|NO LONGER TRUE|WAS WRONG)",
    re.IGNORECASE,
)
#: Unchecked checkboxes in an injected excerpt read as a task list assigned to
#: the current turn. They are somebody else's TODO from another day (#321, fix 4).
_ACTION_ITEM_RE = re.compile(r"^[ \t]*[-*][ \t]*\[ \][ \t].*$", re.MULTILINE)
#: Consult continuity across a long session (#296). The per-turn gate keyed off
#: the user prompt alone, and in a long session most prompts are continuations
#: ("retry", "go ahead", a task notification) that carry no signal — so the
#: preflight auto-cleared and the turn ran dozens of actions with no memory
#: contact. Two controls close that: a continuation prompt is retrieved against
#: the PRIOR task plus the agent's recent activity, and every allowed action
#: counts against a per-turn budget after which the core re-checks whether an
#: unseen relevant memory exists for the work in progress.
ACTION_BUDGET_ENV = "OMIND_GATE_ACTION_BUDGET"
_DEFAULT_ACTION_BUDGET = 25
#: Per-turn ceiling on mid-turn re-arms/injections — an anti-wedge cap, like the
#: verifier's re-close cap: a turn can never be re-gated indefinitely.
MAX_REARM_ENV = "OMIND_GATE_MAX_REARM"
_DEFAULT_MAX_REARM = 4
#: Synthetic rule ids for the continuity decisions (all soft; all ceremonies).
GATE_REARM_RULE = "omi-gate-rearm"
GATE_REARM_NO_MATCH_RULE = "omi-gate-rearm-no-match"
GATE_CARRY_RULE = "omi-gate-carry"
GATE_PREFLIGHT_RULE = "omi-gate-preflight"
#: A prompt with fewer meaningful terms than this is a continuation of the
#: prior turn's work, not a new task ("retry", "Yes please", "Delete it").
CONTINUATION_MAX_TERMS = 3
#: An identical prompt re-sent within this window is the harness's own API
#: auto-retry (or a human re-poking), not a new turn: the gate state carries.
RETRY_WINDOW_SECS = 120.0
#: How many recent action texts the sentinel keeps as the turn's activity trail
#: (the harness-agnostic activity signal — every adapter reaches the core).
_TRAIL_LEN = 8
_TRAIL_ITEM_CAP = 160
GIT_RULES_NOTE = "Operational Rules - Git Repos and Secrets"
#: The demand names ``read-note`` (raw, at its hard cap) because recall-note
#: stops at ``recall.MAX_RECALL_CHARS`` however it is asked: demanding recall at
#: 8000 for a longer note was unsatisfiable, and the verifier then credited the
#: truncated read anyway (#392). ``verify._update_demanded_completeness`` checks
#: exactly what this says. Literal (not built from ``recall``) to keep the hook
#: path's imports light; a test pins it to ``recall.full_read_args``.
GIT_RULES_MESSAGE = (
    "ACTION BLOCKED. Next call OMI MCP `read-note` with "
    '`{"name":"Operational Rules - Git Repos and Secrets","representation":"raw",'
    '"max_chars":65536}`, then retry. Repo work requires that specific memory this '
    "turn — read it in full: a truncated or section-only read does not clear this "
    "gate (recall-note stops at 8000 chars, so it cannot return a longer note whole)."
)
#: Value of the per-turn demanded-note marker once the git-rules note is known
#: to be absent (#358). Never a substring of a real consult target, so the
#: verifier's ``_guard_demanded`` cannot mistake it for an obeyed demand.
_GIT_RULES_MISSING_MARK = f"(missing) {GIT_RULES_NOTE}"
GIT_RULES_MISSING_MESSAGE = (
    f"omi-guard: repo work normally requires reading the OMI note '{GIT_RULES_NOTE}', "
    "but this vault has no such note, so the git-rules gate is NOT enforcing "
    "anything. Run `omind setup` to seed a starter copy (it never overwrites an "
    "existing note), then edit it to hold this operator's real rules."
)
GIT_FRESHNESS_MESSAGE = (
    "a git commit requires a same-turn freshness check — refresh the local base "
    "before recording work onto it. (Only the commit is gated; edits, tests, reads, "
    "and pushes are not.) If the repo has no remote there is nothing to be stale "
    "against and no fetch is required. Otherwise, run a LITERAL-path fetch as ITS OWN "
    "command FIRST, then commit as a SEPARATE command — two calls, not one:\n"
    '  git -C "/abs/path/to/repo" fetch --all --prune\n'
    '  git -C "/abs/path/to/repo" commit -am "..."\n'
    "Do NOT chain the commit onto the fetch. A command that also contains the commit — "
    "or ANY non-git-read step, even a harmless `&& echo ok` — is not recognised as a "
    "freshness command, records nothing, and the commit stays blocked. The fetch "
    "command may only be combined with other git READS, e.g. "
    '`git -C "/repo" fetch --all --prune && git -C "/repo" status`. Gotchas that make '
    "it silently fail: (1) the path must be a LITERAL absolute path — a $VAR is not "
    "resolved by the static parser; (2) no pipe, redirect, or command-substitution "
    "anywhere in the fetch command; (3) the fetch must succeed (exit 0) — if `--all` "
    "hits an unreachable mirror (e.g. a Codeberg remote with no key loaded), use "
    "`fetch origin --prune`; (4) freshness resets every turn, so re-run the standalone "
    "fetch once per turn before your next commit."
)
GLOBAL_MUTATION_MESSAGE = (
    "global config/hook/bootstrap mutation requires explicit user authorization in the "
    "current turn; answer questions first instead of inferring permission. A message "
    "the user sends mid-turn counts once it says to proceed."
)
CAPABILITY_SIDE_EFFECT_MESSAGE = (
    "side-effect actions require explicit imperative authorization; answer capability "
    "questions like `can you ...?` without acting until the user says to proceed "
    "(a mid-turn message saying so counts)."
)


@dataclass(frozen=True)
class Verdict:
    """A guard decision: allow (exit 0) or deny (exit 2 + ``reason``).

    ``rule_id`` names the policy rule (or ``omi-gate``) responsible for a deny,
    so the compliance log and the recidivism loop can attribute it.
    """

    allow: bool
    reason: str = ""
    rule_id: str = ""

    @property
    def exit_code(self) -> int:
        return 0 if self.allow else 2


def _safe_sid(session: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "", session) or "nosid"


def _sentinel_path(session: str) -> Path:
    # Lives in omind's state dir (not /tmp) so the bash adapter and this Python
    # core agree on the path cross-platform — macOS's tempdir is not /tmp.
    return paths.state_dir() / f"gate-{_safe_sid(session)}"


def _turn_path(session: str) -> Path:
    """The turn's captured task (the user prompt), stamped by the turn-start
    reset so the verifier (Layer C) and retrieval know what the agent is working
    on. A sibling of the gate sentinel, so both turn-start paths agree."""
    return paths.state_dir() / f"turn-{_safe_sid(session)}.txt"


def _injected_path(session: str) -> Path:
    """Per-session note versions already injected by proactive turn preflight."""
    return paths.state_dir() / f"injected-{_safe_sid(session)}.json"


def _injected_versions(session: str) -> dict[str, str]:
    try:
        value = json.loads(_injected_path(session).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {str(k): str(v) for k, v in value.items()} if isinstance(value, dict) else {}


def _record_injected(session: str, filename: str, version: str) -> None:
    if not session or not filename:
        return
    with contextlib.suppress(OSError):
        path = _injected_path(session)
        path.parent.mkdir(parents=True, exist_ok=True)
        values = _injected_versions(session)
        values[filename] = version
        paths.atomic_write_text(path, json.dumps(values), mode=0o600)


def begin_turn(session: str, task: str) -> None:
    """Record this turn's task (best-effort, never raises). Written by
    ``omind guard reset``; the Claude adapter writes the same file in pure bash.

    Also resets the per-turn re-close counter and the pending-intent (#96), so the
    verifier's anti-wedge cap and the transition signal are both measured per turn
    (the bash turn-start hook clears the same counter file)."""
    _clear_reclose(session)
    _clear_rearm(session)
    _clear_pending(session)
    _clear_git_freshness(session)
    _clear_demanded(session)
    clear_incomplete_consult(session)
    with contextlib.suppress(OSError):
        path = _turn_path(session)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(task, encoding="utf-8")


def turn_task(session: str) -> str:
    """This turn's captured task, or ``""`` if none was stamped. Never raises."""
    try:
        return _turn_path(session).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _pending_path(session: str) -> Path:
    """The text of the most recent action the consult-gate BLOCKED this turn — the
    agent's freshest statement of intent. The verifier scores a consult against it
    (#96) so the FIRST consult after a work-transition, where the captured task and
    recent activity are both still cold (the previous thread), clears instead of
    burning re-closes. A sibling of the turn-task path; reset at turn start."""
    return paths.state_dir() / f"pending-{_safe_sid(session)}.txt"


def _git_fresh_path(session: str) -> Path:
    return paths.state_dir() / f"git-fresh-{_safe_sid(session)}.json"


def _demanded_path(session: str) -> Path:
    """The note a guard block message DEMANDED this turn (e.g. the git-rules
    note). The verifier treats a consult of it as obedience, not gaming — the
    deny log showed the verifier re-closing the gate over reads of the very
    note the guard itself required (#148). Reset at turn start."""
    return paths.state_dir() / f"demanded-{_safe_sid(session)}.txt"


def record_demanded_note(session: str, note: str) -> None:
    """Record the note a guard block just demanded (best-effort, never raises)."""
    if not session or not note:
        return
    with contextlib.suppress(OSError):
        path = _demanded_path(session)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(note, encoding="utf-8")


def demanded_note(session: str) -> str:
    """The note a guard block demanded this turn, or ``""``. Never raises."""
    try:
        return _demanded_path(session).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _incomplete_path(session: str) -> Path:
    """Notes whose *demanded* read came back truncated this turn (#239)."""
    return paths.state_dir() / f"incomplete-{_safe_sid(session)}.txt"


def _read_incomplete_record(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _write_incomplete_record(session: str, value: str) -> None:
    """Replace the per-turn read-completeness record under its sibling lock.
    Best-effort, never raises."""
    with contextlib.suppress(OSError):
        path = _incomplete_path(session)
        path.parent.mkdir(parents=True, exist_ok=True)
        with filelock.exclusive(_sibling_lock(path)):
            paths.atomic_write_text(path, value, mode=0o600)


def record_incomplete_consult(session: str, note: str) -> None:
    """Mark a demanded note as read-but-truncated; the gate stays armed until a
    full read (or a best-possible one) lands. Best-effort, never raises."""
    _write_incomplete_record(session, note.strip().lower())


def clear_incomplete_consult(session: str) -> None:
    with contextlib.suppress(OSError):
        path = _incomplete_path(session)
        with filelock.exclusive(_sibling_lock(path)):
            path.unlink()


#: Prefix in the same per-turn file once a FULL read of the demanded note has
#: landed (#392), so a later section drill-down cannot re-arm the gate.
_FULL_MARK = "full:"


def record_full_consult(session: str, note: str) -> None:
    """Mark the demanded note as read in full this turn. Best-effort."""
    _write_incomplete_record(session, _FULL_MARK + note.strip().lower())


def record_partial_consult(session: str, note: str) -> bool:
    """Record a partial read of ``note`` unless a full read already landed this
    turn (#392). The check and the write share one sibling-lock critical
    section, so a concurrent full read cannot be overwritten by a stale
    "incomplete" (AGENTS.md locking invariant). Returns ``True`` when the read
    was recorded as incomplete. Fails open: on any I/O error nothing is
    recorded and ``False`` is returned."""
    key = note.strip().lower()
    try:
        path = _incomplete_path(session)
        path.parent.mkdir(parents=True, exist_ok=True)
        with filelock.exclusive(_sibling_lock(path)):
            if _read_incomplete_record(path) == _FULL_MARK + key:
                return False  # already read in full this turn; a drill-down adds to it
            paths.atomic_write_text(path, key, mode=0o600)
            return True
    except OSError:
        return False


def has_full_consult(session: str, note: str) -> bool:
    return _read_incomplete_record(_incomplete_path(session)) == _FULL_MARK + note.strip().lower()


def incomplete_consult(session: str) -> str:
    """The demanded note whose only read this turn was truncated, or ``""``."""
    text = _read_incomplete_record(_incomplete_path(session))
    return "" if text.startswith(_FULL_MARK) else text


#: Tool-name prefixes of the OMI MCP server across harness spellings (kept in
#: sync with ``adapters._OMI_CONSULT_PREFIXES``; not imported, adapters imports
#: this module).
_OMI_TOOL_PREFIXES = ("mcp__omi__", "mcp__omi_", "mcp_omi_", "omi__", "omi_")
_NOTE_READ_TOOLS = ("read-note", "recall-note")


def is_note_read(tool: str, target: str) -> bool:
    """True when a consult by ``tool`` of ``target`` actually READ the note's
    text (#392): OMI ``read-note`` / ``recall-note``, or a native file read of
    the note (any non-OMI tool whose consult target is a vault path — the
    adapters only report those as consults). A search, ``backlinks`` or
    ``graph-neighbors`` naming the note returns other text and reads none of
    its rules."""
    low = tool.strip().lower().replace("_", "-")
    if low.endswith(_NOTE_READ_TOOLS):
        return True
    if tool.strip().lower().startswith(_OMI_TOOL_PREFIXES):
        return False
    return "/" in target or "\\" in target or target.lower().endswith(".md")


def _clear_demanded(session: str) -> None:
    with contextlib.suppress(OSError):
        _demanded_path(session).unlink()


def record_pending(session: str, text: str) -> None:
    """Stash the gate-blocked action's text as this turn's pending intent
    (best-effort, never raises). Empty/blank text is a no-op."""
    text = (text or "").strip()
    if not text:
        return
    with contextlib.suppress(OSError):
        path = _pending_path(session)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def pending_intent(session: str) -> str:
    """This turn's most-recent gate-blocked action text, or ``""``. Never raises."""
    try:
        return _pending_path(session).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _clear_pending(session: str) -> None:
    with contextlib.suppress(OSError):
        _pending_path(session).unlink()


def _fresh_repos(session: str) -> dict[str, int]:
    """The repos freshened this turn (``{repo: ts}``). Reads both the current
    set-shaped payload and the pre-3.8.3 single-slot ``{"repo": ...}`` shape (a
    mid-upgrade session may still carry one). Never raises."""
    try:
        data = json.loads(_git_fresh_path(session).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    repos = data.get("repos")
    if isinstance(repos, dict):
        return {str(k): int(v) for k, v in repos.items() if isinstance(v, (int, float))}
    legacy = data.get("repo")
    if isinstance(legacy, str) and legacy:
        ts = data.get("ts")
        return {legacy: int(ts) if isinstance(ts, (int, float)) else 0}
    return {}


def _record_git_freshness(session: str, repo: Path, command: str) -> None:
    # A SET of repos, not a single slot (#147): a cross-repo turn fetches A and
    # B, and the second fetch must not evict the first — otherwise the turn
    # ping-pongs between re-fetches. Cleared on turn reset like before.
    if not session:
        return
    with contextlib.suppress(OSError, ValueError):
        path = _git_fresh_path(session)
        path.parent.mkdir(parents=True, exist_ok=True)
        with filelock.exclusive(_sibling_lock(path)):
            repos = _fresh_repos(session)
            repos[str(repo)] = int(time.time())
            payload = {"repos": repos, "command": command}
            paths.atomic_write_text(path, json.dumps(payload), mode=0o600)


def _git_fresh_for_repo(session: str, repo: Path) -> bool:
    return str(repo) in _fresh_repos(session)


def _retract_git_freshness(session: str, repo: Path) -> None:
    """Remove one repo from this turn's fresh set (a fetch that failed)."""
    if not session:
        return
    with contextlib.suppress(OSError, ValueError):
        path = _git_fresh_path(session)
        with filelock.exclusive(_sibling_lock(path)):
            repos = _fresh_repos(session)
            if str(repo) not in repos:
                return
            del repos[str(repo)]
            if repos:
                paths.atomic_write_text(path, json.dumps({"repos": repos}), mode=0o600)
            else:
                path.unlink()


def _tool_outcome_failed(tool_response: object) -> bool:
    """True on an EXPLICIT failure signal only — a harness that reports no
    outcome at all is trusted (mirrors hooks._extract_outcome's discipline)."""
    if not isinstance(tool_response, dict):
        return False
    if (
        tool_response.get("is_error")
        or tool_response.get("success") is False
        or tool_response.get("error")
    ):
        return True
    for key in ("exit_code", "returncode"):
        value = tool_response.get(key)
        if isinstance(value, int) and value != 0:
            return True
    return False


def record_freshness_outcome(event: dict[str, Any]) -> None:
    """PostToolUse retraction for the git-freshness grant.

    PreToolUse records freshness BEFORE the fetch runs, so a fetch that exits 1
    (unreachable mirror) used to still satisfy the commit-time freshness gate —
    the block message promises "the fetch must succeed (exit 0)" but nothing
    enforced it (2026-08-27 review). This retracts the grant when the outcome
    says the command failed. Best-effort; never raises."""
    try:
        if str(event.get("tool_name") or "") != "Bash":
            return
        tool_input = event.get("tool_input")
        command = str(tool_input.get("command") or "") if isinstance(tool_input, dict) else ""
        if not command or not _is_freshness_command(command):
            return
        if not _tool_outcome_failed(event.get("tool_response")):
            return
        session = str(event.get("session_id") or "")
        repo = _repo_root_for_action({"command": command, **event})
        if session and repo is not None:
            _retract_git_freshness(session, repo)
    except Exception:
        return


def _clear_git_freshness(session: str) -> None:
    with contextlib.suppress(OSError):
        _git_fresh_path(session).unlink()


def _read_sentinel(session: str) -> dict[str, Any]:
    """The gate sentinel's JSON body ({} when empty/absent/garbage). The bash
    adapter creates the file empty (``touch``); Python enriches it with the
    turn's consult records."""
    try:
        raw = _sentinel_path(session).read_text(encoding="utf-8")
    except OSError:
        return {}
    try:
        data = json.loads(raw or "{}")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _write_sentinel(session: str, data: dict[str, Any]) -> None:
    with contextlib.suppress(OSError):
        path = _sentinel_path(session)
        path.parent.mkdir(parents=True, exist_ok=True)
        paths.atomic_write_text(path, json.dumps(data), mode=0o600)


def _mutate_sentinel(session: str, mutate: Callable[[dict[str, Any]], dict[str, Any]]) -> None:
    """Locked read-modify-write of the turn sentinel.

    Hook processes fire concurrently (parallel tool calls, several agents on
    one box); an unlocked read→write pair loses the other side's consult
    record, which spuriously re-arms the gate (2026-08-27 review). The flock
    lives on a sibling ``.lock`` file — the sentinel itself is replaced
    atomically, so a lock on the data inode would protect nothing."""
    path = _sentinel_path(session)
    with contextlib.suppress(OSError):
        path.parent.mkdir(parents=True, exist_ok=True)
        with filelock.exclusive(_sibling_lock(path)):
            _write_sentinel(session, mutate(_read_sentinel(session)))


def _sibling_lock(path: Path) -> Path:
    """The lock-file path guarding ``path``'s read-modify-write cycle."""
    return path.with_name(path.name + ".lock")


def mark_consulted(session: str) -> None:
    """Mark OMI consulted this turn — the sentinel's *existence* is the gate.
    Preserves any consult records already captured this turn."""

    def _mark(data: dict[str, Any]) -> dict[str, Any]:
        data.setdefault("consults", [])
        data["actions"] = 0
        return data

    _mutate_sentinel(session, _mark)


def record_consult(
    session: str, *, kind: str, target: str, relevant: bool | None = None, tool: str = ""
) -> None:
    """Append one OMI consult (note read / search) to the turn's sentinel with
    its relevance verdict (``None`` = not yet judged), and mark the gate
    consulted. ``tool`` is the consulting tool's name, when known, so the
    git-rules gate can tell a read of the note from a search naming it (#392).
    Never raises."""

    def _record(data: dict[str, Any]) -> dict[str, Any]:
        existing = data.get("consults")
        consult_list = existing if isinstance(existing, list) else []
        record: dict[str, Any] = {"kind": kind, "target": target, "relevant": relevant}
        if tool:
            record["tool"] = tool
        consult_list.append(record)
        data["consults"] = consult_list
        data["actions"] = 0
        return data

    _mutate_sentinel(session, _record)


def consults(session: str) -> list[dict[str, Any]]:
    """The consults recorded this turn (each ``{kind, target, relevant}``)."""
    raw = _read_sentinel(session).get("consults")
    return [c for c in raw if isinstance(c, dict)] if isinstance(raw, list) else []


def retract_consult(session: str, target: str) -> None:
    """Retract the consult PreToolUse credited for a read that then FAILED (#358).

    PreToolUse records a consult BEFORE the read runs, so a ``recall-note`` that
    came back ``note not found`` used to clear the git-rules gate having read
    nothing. PostToolUse calls this when the outcome says the read failed — the
    same record-then-retract shape as :func:`record_freshness_outcome`.

    Only the ATTEMPT is retracted: the latest still-unjudged record for
    ``target`` (``relevant is None`` is what PreToolUse writes; a read that
    succeeded has since gained a judged record, which is left alone). The
    record is flagged rather than dropped so the turn history shows the attempt.

    The sentinel's *existence* is the ordinary consult gate, so flagging alone
    would leave that gate open: ``recall-note`` on any made-up name would clear
    it, the same dodge as re-reading ``index.md``. When no un-failed consult is
    left the sentinel is removed and the gate re-arms; any successful consult
    (or a preflight that spoke) keeps it open. Never raises."""
    needle = target.strip().lower()
    if not needle:
        return
    path = _sentinel_path(session)
    with contextlib.suppress(OSError), filelock.exclusive(_sibling_lock(path)):
        data = _read_sentinel(session)
        existing = data.get("consults")
        records = [c for c in existing if isinstance(c, dict)] if isinstance(existing, list) else []
        for consult in reversed(records):
            if (
                str(consult.get("target") or "").lower() == needle
                and consult.get("relevant") is None
                and not consult.get("failed")
            ):
                consult["failed"] = True
                consult["relevant"] = False
                break
        else:
            return  # nothing was credited for this read; nothing to retract
        if any(not c.get("failed") for c in records):
            _write_sentinel(session, data)
        else:
            path.unlink()


def demanded_note_missing(omi_dir: Path | str, note: str = GIT_RULES_NOTE) -> bool:
    """True only when ``note`` is POSITIVELY absent from the vault (#358).

    A gate that demands a read which is guaranteed to fail teaches the agent to
    perform a ceremony, not to read rules. Conservative like
    :func:`_repo_has_remote`: any doubt (unreadable vault, resolution error)
    answers ``False`` so the demand stands. Never raises."""
    try:
        from omind.store import NoteError, OmiStore

        root = Path(omi_dir)
        if not root.is_dir():
            return False
        try:
            return not OmiStore(root).safe_name(note).is_file()
        except NoteError:
            return True
    except Exception:
        return False


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


# --------------------------------------------------------------------------
# Consult continuity (#296): action budget, activity trail, continuation turns
# --------------------------------------------------------------------------


def _env_int(env: str, default: int) -> int:
    raw = os.environ.get(env, "").strip()
    if raw:
        try:
            return max(0, int(raw))
        except ValueError:
            pass
    return default


def action_budget() -> int:
    """Allowed non-consult actions per turn before the core re-checks memory
    (``0`` disables the budget)."""
    return _env_int(ACTION_BUDGET_ENV, _DEFAULT_ACTION_BUDGET)


def _max_rearm() -> int:
    return _env_int(MAX_REARM_ENV, _DEFAULT_MAX_REARM)


def _rearm_path(session: str) -> Path:
    return paths.state_dir() / f"rearm-{_safe_sid(session)}"


def rearm_count(session: str) -> int:
    """Mid-turn re-arms + injections so far this turn (reset at turn start)."""
    try:
        return int(_rearm_path(session).read_text(encoding="utf-8").strip() or "0")
    except (OSError, ValueError):
        return 0


def bump_rearm(session: str) -> int:
    path = _rearm_path(session)
    with contextlib.suppress(OSError, ValueError):
        path.parent.mkdir(parents=True, exist_ok=True)
        with filelock.exclusive(_sibling_lock(path)):
            nxt = rearm_count(session) + 1
            path.write_text(str(nxt), encoding="utf-8")
            return nxt
    return rearm_count(session)


def _clear_rearm(session: str) -> None:
    with contextlib.suppress(OSError):
        _rearm_path(session).unlink()


def _last_turn_path(session: str) -> Path:
    """The previous turn's prompt + substantive task + timestamp, so a
    continuation prompt can be resolved against what the agent was doing."""
    return paths.state_dir() / f"lastturn-{_safe_sid(session)}.json"


def _read_last_turn(session: str) -> dict[str, Any]:
    try:
        data = json.loads(_last_turn_path(session).read_text(encoding="utf-8") or "{}")
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_last_turn(session: str, *, prompt: str, task: str, ts: float) -> None:
    with contextlib.suppress(OSError, ValueError):
        path = _last_turn_path(session)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"prompt": prompt[:2_000], "task": task[:4_000], "ts": ts}),
            encoding="utf-8",
        )


def is_continuation_prompt(prompt: str) -> bool:
    """True when ``prompt`` continues the prior turn rather than starting a task:
    a harness-injected wrapper (``<task-notification>``, ``<system-reminder>``)
    or fewer than :data:`CONTINUATION_MAX_TERMS` meaningful terms. Empty is not
    a continuation — an empty task keeps the gate strict elsewhere."""
    text = prompt.strip()
    if not text:
        return False
    if text.startswith("<"):
        return True
    from omind import retrieve

    return retrieve.term_count(text) < CONTINUATION_MAX_TERMS


def actions_since_consult(session: str) -> int:
    """Allowed non-consult actions since this turn's last OMI consult."""
    try:
        return int(_read_sentinel(session).get("actions") or 0)
    except (TypeError, ValueError):
        return 0


def action_trail(session: str) -> list[str]:
    """The most recent allowed action texts this turn (newest last)."""
    raw = _read_sentinel(session).get("trail")
    return [str(t) for t in raw if isinstance(t, str)] if isinstance(raw, list) else []


def count_action(session: str, text: str) -> None:
    """Record one allowed non-consult action against the turn's budget and
    append it to the activity trail. Never raises."""

    def _count(data: dict[str, Any]) -> dict[str, Any]:
        try:
            data["actions"] = int(data.get("actions") or 0) + 1
        except (TypeError, ValueError):
            data["actions"] = 1
        raw = data.get("trail")
        trail = [str(t) for t in raw if isinstance(t, str)] if isinstance(raw, list) else []
        item = " ".join(text.split())[:_TRAIL_ITEM_CAP]
        if item:
            trail.append(item)
        data["trail"] = trail[-_TRAIL_LEN:]
        return data

    _mutate_sentinel(session, _count)


def reset_action_count(session: str) -> None:
    def _reset(data: dict[str, Any]) -> dict[str, Any]:
        data["actions"] = 0
        return data

    _mutate_sentinel(session, _reset)


#: Path components that name a machine's layout, not the work (a trail item
#: ``/home/x/Source/repos/telesto/deploy.sh`` should contribute "telesto" and
#: "deploy", not "home"/"source"/"repos").
_TRAIL_NOISE_PARTS = frozenset(
    {"home", "users", "source", "repos", "src", "tmp", "srv", "var", "opt", "usr", "bin", "lib"}
)
_TRAIL_SPLIT_RE = re.compile(r"[\\/]+")


def _trail_words(item: str) -> str:
    """A trail item as retrieval words: path separators become spaces and the
    layout-only components drop out, so a file's project and name both count
    (the verifier's ``normalize_intent`` keeps only basenames, which throws
    away the project — the strongest signal for *which memory* applies)."""
    words = []
    for token in item.split():
        for part in _TRAIL_SPLIT_RE.split(token):
            if part and part.casefold() not in _TRAIL_NOISE_PARTS:
                words.append(part)
    return " ".join(words)


def _activity_text(session: str, omi_dir: Path | str | None) -> str:
    """What the agent has been doing: the sentinel's action trail (reaches the
    core under every harness) plus the journal's recent activity where a
    harness journals. Never raises."""
    parts = [_trail_words(item) for item in action_trail(session)]
    if omi_dir is not None:
        try:
            from omind import verify

            parts.append(verify.recent_activity(session, omi_dir))
        except Exception:
            pass
    return " ".join(part for part in parts if part)


def _seen_note_stems(session: str) -> set[str]:
    """Notes this session has already been shown or has consulted this turn —
    a mid-turn re-arm must surface something NEW, never re-demand these."""
    stems = {Path(name).stem.casefold() for name in _injected_versions(session)}
    for consult in consults(session):
        # A consult recorded ``relevant=False`` is an auto-clear REJECTION
        # (weak/no/stale/budget match) where the note was named but never shown —
        # so it is not "seen", and the work drifting onto it should still re-arm.
        if consult.get("relevant") is False:
            continue
        target = str(consult.get("target") or "")
        if target:
            stems.add(Path(target).stem.casefold())
    demanded = demanded_note(session)
    if demanded:
        stems.add(Path(demanded).stem.casefold())
    return stems


def midturn_candidate(
    session: str, omi_dir: Path | str, *, query: str = ""
) -> tuple[str, str] | None:
    """``(filename, title)`` of the best note relevant to the work in progress
    that this session has not seen yet, or ``None``. ``query`` defaults to the
    turn's task plus the activity signal. Deterministic; no model call."""
    from omind import recall, retrieve

    if not query:
        query = " ".join(p for p in (turn_task(session), _activity_text(session, omi_dir)) if p)
    if not retrieve.term_count(query):
        return None
    seen = _seen_note_stems(session)
    min_terms = retrieve.preflight_min_terms()
    for title in retrieve.relevant_titles(query, omi_dir, limit=5):
        filename = recall.filename_for_title(omi_dir, title)
        if filename is None or Path(filename).stem.casefold() in seen:
            continue
        if min_terms:
            memory = recall.compact_recall(omi_dir, filename, max_chars=recall.MIN_RECALL_CHARS)
            haystack = " ".join(
                str(memory.get(key) or "") for key in ("title", "summary", "content")
            )
            if retrieve.matched_terms(query, haystack) < min_terms:
                continue
        return filename, title
    return None


def _log_continuity(
    session: str, *, tool: str, command: str, rule_id: str, outcome: str, detail: str
) -> None:
    compliance.log_event(
        compliance.KIND_DECISION,
        session=session,
        tool=tool,
        command=command,
        rule_id=rule_id,
        severity="soft",
        outcome=outcome,
        detail=detail,
    )


def budget_verdict(action: dict[str, Any], omi_dir: Path | str | None) -> Verdict | None:
    """The mid-turn continuity check for an action the gate already ALLOWED.

    Counts the action against the turn's budget; at the budget, looks for a
    relevant memory this session has not seen. Found → the gate re-arms and
    demands that note (one ``recall-note`` clears it — the verifier treats a
    demanded read as obedience). Nothing new → the budget resets and the
    auto-clear is logged. Returns a deny :class:`Verdict` or ``None`` (allow).
    Runs in the harness-agnostic core, so every adapter inherits it; a harness
    that can inject context after a tool call gets the same memory as a nudge
    first (:func:`midturn_context`) and never reaches the deny.
    """
    session = str(action.get("session") or "")
    if not session or omi_dir is None or action.get("is_omi_consult") or gate_paused():
        return None
    command = str(action.get("command") or "")
    if command and _is_inert_command(command):
        return None
    if not consulted_this_turn(session):
        return None
    tool = str(action.get("tool") or "")
    text = command or _action_path(action) or tool
    actions = actions_since_consult(session)
    budget = action_budget()
    if budget and actions >= budget and rearm_count(session) < _max_rearm():
        found = midturn_candidate(session, omi_dir)
        if found is not None:
            from omind import recall

            filename, title = found
            clear_gate(session)
            record_demanded_note(session, filename)
            record_pending(session, text)
            bump_rearm(session)
            _log_continuity(
                session,
                tool=tool,
                command=command,
                rule_id=GATE_REARM_RULE,
                outcome="deny",
                detail=f"actions={actions} note={filename!r}",
            )
            call = json.dumps(
                {"name": filename, "max_chars": recall.MAX_RECALL_CHARS},
                ensure_ascii=False,
                separators=(",", ":"),
            )
            reason = (
                f"omi-gate (re-arm): {actions} actions since this turn's last memory "
                "consult and the work has moved on. Relevant memory not yet consulted "
                f"this session: [[{title}]]. Next call OMI MCP `recall-note` with "
                f"`{call}`, then retry this action. (Budget: {budget} actions between "
                f"consults; {ACTION_BUDGET_ENV} tunes it.)"
            )
            excerpt = _governing_excerpt(omi_dir, filename)
            if excerpt:
                reason += f"\n\n--- Governing memory (excerpt) ---\n{excerpt}"
            return Verdict(allow=False, reason=reason, rule_id=GATE_REARM_RULE)
        reset_action_count(session)
        _log_continuity(
            session,
            tool=tool,
            command=command,
            rule_id=GATE_REARM_NO_MATCH_RULE,
            outcome="auto-clear",
            detail=f"actions={actions}",
        )
    count_action(session, text)
    return None


def midturn_context(event: dict[str, Any], omi_dir: Path | str | None) -> str:
    """Proactive mid-turn recall for a harness whose post-tool hook can inject
    context (Claude Code ``PostToolUse`` ``additionalContext``). At the action
    budget, the same candidate :func:`budget_verdict` would demand is injected
    as a nudge instead, and the budget resets — so the deny never fires there.
    Returns ``""`` when there is nothing to inject. Never raises."""
    try:
        session = str(event.get("session_id") or event.get("session") or "")
        if not session or omi_dir is None or gate_paused():
            return ""
        if not consulted_this_turn(session):
            return ""
        budget = action_budget()
        actions = actions_since_consult(session)
        if not budget or actions < budget or rearm_count(session) >= _max_rearm():
            return ""
        found = midturn_candidate(session, omi_dir)
        if found is None:
            reset_action_count(session)
            _log_continuity(
                session,
                tool="PostToolUse",
                command="",
                rule_id=GATE_REARM_NO_MATCH_RULE,
                outcome="auto-clear",
                detail=f"actions={actions}",
            )
            return ""
        from omind import ai_usage, recall

        filename, title = found
        memory = recall.compact_recall(
            omi_dir, filename, max_chars=ai_usage.policy(omi_dir).preflight_chars
        )
        summary = str(memory.get("summary") or "").strip()
        excerpt = str(memory.get("content") or "").strip()
        content = "\n\n".join(part for part in (summary, excerpt) if part and part != summary)
        if not content:
            content = str(memory.get("title") or filename)
        record_consult(session, kind="midturn", target=filename, relevant=True)
        reset_offtopic(session)
        _record_injected(session, filename, str(memory.get("version") or ""))
        bump_rearm(session)
        _log_continuity(
            session,
            tool="PostToolUse",
            command="",
            rule_id=GATE_REARM_RULE,
            outcome="inject",
            detail=f"actions={actions} note={filename!r}",
        )
        context = (
            f"OMI mid-turn recall: {actions} actions since this turn's last memory "
            f"consult and the work has moved on. [[{memory.get('title') or title}]] is a "
            "standing operator instruction/memory relevant to the work in progress — "
            "apply it unless the user's current message explicitly overrides it. "
            "Silence is not an override.\n\n" + content
        )
        ai_usage.record_context(omi_dir, "recall", len(context), session_id=session)
        return context
    except Exception:
        return ""


def clear_gate(session: str) -> None:
    """Clear the per-turn consult sentinel (the harness's turn-start reset).

    Also reaps legacy ``/tmp/omi-gate-*`` sentinels left by the pre-state-dir
    prototype guard, so a machine upgrading from that version does not keep stale
    sentinels around (the canonical guard never writes ``/tmp``).

    Does NOT touch the re-close counter — the verifier re-closes the gate by
    calling this, and the counter must survive across re-closes within a turn (it
    is reset only at turn start, by :func:`begin_turn`)."""
    with contextlib.suppress(OSError):
        _sentinel_path(session).unlink()
    _reap_legacy_sentinels()


def _reclose_path(session: str) -> Path:
    """Per-turn count of how many times REQUIRE-mode re-closed the gate. A sibling
    of the sentinel that SURVIVES :func:`clear_gate` (which the re-close calls), so
    the verifier can cap re-closes and never deadlock the agent. Reset at turn
    start, alongside the sentinel."""
    return paths.state_dir() / f"reclose-{_safe_sid(session)}"


def reclose_count(session: str) -> int:
    """How many times the gate was re-closed this turn (0 when none/absent)."""
    try:
        return int(_reclose_path(session).read_text(encoding="utf-8").strip() or "0")
    except (OSError, ValueError):
        return 0


def bump_reclose(session: str) -> int:
    """Increment and return this turn's re-close count. Never raises.

    Locked: concurrent PostToolUse hook processes otherwise interleave the
    read→write pair and lose increments, tripping the anti-wedge cap later
    than designed (2026-08-27 review)."""
    path = _reclose_path(session)
    nxt = reclose_count(session) + 1
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with filelock.exclusive(_sibling_lock(path)):
            nxt = reclose_count(session) + 1
            paths.atomic_write_text(path, str(nxt), mode=0o600)
    except OSError:
        pass
    return nxt


def _clear_reclose(session: str) -> None:
    with contextlib.suppress(OSError):
        _reclose_path(session).unlink()


def _offtopic_path(session: str) -> Path:
    """Running count of CONSECUTIVE off-topic consults this SESSION — a relevant consult
    resets it (see :func:`reset_offtopic`). Unlike the per-turn re-close counter this
    SURVIVES turn boundaries (it is NOT cleared by :func:`begin_turn`): it measures a
    sustained off-topic STREAK, the signal that separates an agent gaming the gate (only
    ever reads arbitrary notes) from one doing honest work (lands relevant consults,
    which reset the streak). The graduated gate (#98) escalates REQUIRE-mode enforcement
    only once the streak crosses a threshold; a new session is a new id, so it starts at 0."""
    return paths.state_dir() / f"offtopic-{_safe_sid(session)}"


def offtopic_count(session: str) -> int:
    """The current consecutive off-topic-consult streak this session (0 if none)."""
    try:
        return int(_offtopic_path(session).read_text(encoding="utf-8").strip() or "0")
    except (OSError, ValueError):
        return 0


def bump_offtopic(session: str) -> int:
    """Increment and return the consecutive off-topic streak. Never raises.
    Locked like :func:`bump_reclose` (2026-08-27 review)."""
    path = _offtopic_path(session)
    nxt = offtopic_count(session) + 1
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with filelock.exclusive(_sibling_lock(path)):
            nxt = offtopic_count(session) + 1
            paths.atomic_write_text(path, str(nxt), mode=0o600)
    except OSError:
        pass
    return nxt


def reset_offtopic(session: str) -> None:
    """Reset the off-topic streak — called on a RELEVANT consult, so honest work breaks
    the streak and sporadic off-topic flags never accumulate to enforcement (#98)."""
    with contextlib.suppress(OSError):
        _offtopic_path(session).unlink()


#: Default pause window if ``omind guard pause`` is run without ``--for`` — long
#: enough for a burst of mission-critical work, short enough that a forgotten pause
#: self-heals within the hour.
_DEFAULT_PAUSE_SECONDS = 1800

#: Hard ceiling on a single `omind guard pause --for`. A pause is meant to be a
#: work-burst window; past a few hours it is indistinguishable from disabling the
#: gate, and it silently masks doctor's enforcement check for the duration. One
#: box was found paused for 185h, which is how that failure mode was discovered.
#: Re-pausing is always allowed — the cap forces the operator to mean it.
_MAX_PAUSE_SECONDS = 4 * 3600


def _pause_path() -> Path:
    """The OPERATOR pause sentinel. While it exists and is unexpired, the consult
    gate + the PostToolUse verifier are skipped for a time-boxed fast window
    (``omind guard pause``) — for mission-critical speed / token savings. The HARD
    destructive blocks are NOT affected (they run earlier in :func:`decide`). It is
    deliberately NOT named ``gate-*`` so :func:`clear_all_gates` (the by-hand
    un-wedge) leaves an intentional pause intact, and it has no session id — a
    by-hand ``omind guard pause`` cannot know the live session, so the pause is
    machine-global for its window. Stores the expiry epoch so it auto-resumes."""
    return paths.state_dir() / "paused"


def pause_gate(seconds: int, *, now: float | None = None) -> float:
    """Engage the operator pause for ``seconds`` and return the expiry epoch.
    Persisting the expiry (not just a flag) makes the gate auto-resume, so a fast
    window can never silently become the permanent state. Never raises."""
    when = (now if now is not None else time.time()) + max(0, seconds)
    with contextlib.suppress(OSError):
        path = _pause_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(int(when)), encoding="utf-8")
    return when


def resume_gate() -> None:
    """Clear the operator pause (re-arm the gate immediately). Never raises."""
    with contextlib.suppress(OSError):
        _pause_path().unlink()


def pause_remaining(now: float | None = None) -> int:
    """Seconds left on the operator pause (0 if not paused / expired / malformed).
    An expired sentinel is reaped, so a stale file can never read as paused forever
    — the gate fails *safe* (re-armed) when the window lapses."""
    try:
        expiry = int(_pause_path().read_text(encoding="utf-8").strip() or "0")
    except (OSError, ValueError):
        return 0
    left = expiry - int(now if now is not None else time.time())
    if left <= 0:
        with contextlib.suppress(OSError):
            _pause_path().unlink()
        return 0
    return left


def gate_paused(now: float | None = None) -> bool:
    """True while the operator pause is engaged and unexpired (gate/verifier off)."""
    return pause_remaining(now) > 0


def clear_all_gates() -> None:
    """Clear EVERY per-turn sentinel + re-close counter — the recovery path for a
    by-hand ``omind guard reset`` with no session id (a human un-wedging the gate
    cannot know the live session id, so a single-session clear would miss it).
    Also reaps the legacy ``/tmp`` sentinels. Never raises."""
    state = paths.state_dir()
    # ``turn-*`` holds the captured raw prompt; it was never reaped, so those
    # files accumulated unboundedly (and leaked prompt text) across sessions.
    for pattern in (
        "gate-*",
        "reclose-*",
        "pending-*",
        "offtopic-*",
        "git-fresh-*",
        "turn-*",
        "injected-*",
        "namehints-*",
        "session-primed/*",
    ):
        try:
            stale = list(state.glob(pattern))
        except OSError:
            continue
        for path in stale:
            with contextlib.suppress(OSError):
                path.unlink()
    _reap_legacy_sentinels()


#: Tools that load OTHER tools' schemas (so a deferred OMI MCP tool can become
#: callable) must never be gated. Gating them deadlocks the turn: the only way
#: to clear the gate is to consult OMI, but where the OMI tools are deferred the
#: consult needs the very schema this tool loads.
#:
#: Poolside's ``exit`` and ``todo_action`` are control flow, not actions on the
#: world (#313): ``pool exec`` ends a run through the ``exit`` tool, so gating
#: it is gating "stop" — Claude Code's Stop hook is never gated either — and a
#: denied ``exit`` aborts the run with ``exit_tool_called: unexpected error``.
#: Hard rules still apply to everything here; they match on command text, which
#: none of these tools carry.
_GATE_EXEMPT_TOOLS = frozenset({"ToolSearch", "exit", "todo_action"})
_WRITE_TOOLS = frozenset(
    {
        "Edit",
        "MultiEdit",
        "Write",
        "NotebookEdit",
        "apply_patch",
        "functions.apply_patch",
    }
)
_READ_REVIEW_TOOLS = frozenset({"Read", "Grep", "Glob", "LS", "find", "rg"})
# A python interpreter is not listed: `_python_runs_tests` judges every
# spelling (`python`, `python3`, `python3.N`) by one rule (#434 review).
_REPO_TEST_RE = re.compile(
    r"(?:^|[;&|\n(]\s*)(?P<prog>uv|pytest|tox|nox|hatch|npm|pnpm|yarn|cargo|go|make)\b"
)
# Optional leading git global options (``-C <dir>``, ``-c key=val``) so a
# freshness command run with an explicit repo dir — ``git -C <repo> fetch`` — is
# still recognised as freshness (it previously required a bare ``git fetch``).
# A quoted `-C "<path>"` arrives here with its literal BLANKED by policy.shell_code_text
# (#317), so the token after -C may be an empty/space-only quoted string. Accept it:
# without this, `git -C "/abs/repo" commit` — the exact form GIT_FRESHNESS_MESSAGE
# teaches — was never classified as repo work and sailed past the rules-note demand.
# One global-option value: one shell word of unquoted runs and quoted runs in any
# order — a bare token, a blanked quoted literal, or a token with a literal embedded
# (`-c user.name="…"`). Same pattern as rules.py (#413/#414): each character has
# exactly one way to match, so a run of options with no matching verb after fails
# in linear time. The old `\S+(?:"…")?\S*` split each value several ways and
# backtracked ~3^N (6.5 s at 16 `-c a=b`), past the hook timeout (#431).
# shell_code_text leaves a backslash-escaped quote OUTSIDE quotes as-is, so `\\.`
# takes `\'` / `\"` as one escaped character; read as an unclosed quoted run, it
# hid `git -c user.name=O\'Brien commit` from every classifier (#431 review). Each
# alternative starts with a different character, so the match stays linear.
_GIT_OPT_VALUE = r"""(?:\\.|[^\s"'\\]|"[^"]*"|'[^']*')+"""
_GIT_GLOBAL_OPTS = rf"(?:-C[ \t]+{_GIT_OPT_VALUE}[ \t]+|-c[ \t]+{_GIT_OPT_VALUE}[ \t]+)*"
# One git subcommand that ESTABLISHES freshness (a fetch, or an ff-only/rebase
# pull). ``[^|>&;\n]*`` keeps the whole subcommand free of pipes/redirects/chains
# so a piped write (``git fetch | tee x``) is never mistaken for a pure fetch.
_GIT_FRESH_SUB_RE = re.compile(
    rf"^git[ \t]+{_GIT_GLOBAL_OPTS}"
    r"(?:fetch(?:[ \t][^|>&;\n]*)?|pull[^|>&;\n]*(?:--ff-only|--rebase)[^|>&;\n]*)$"
)
# A read-only git subcommand (inspection). Same no-pipe/redirect constraint.
_GIT_READONLY_SUB_RE = re.compile(
    rf"^git[ \t]+{_GIT_GLOBAL_OPTS}"
    r"(?:status|rev-parse|branch|remote|log|show|diff|for-each-ref|symbolic-ref|"
    r"describe|config[ \t]+--get)(?:[ \t][^|>&;\n]*)?$"
)
# GLOBAL (home-anchored) agent config files/dirs. A project-local
# ``<repo>/.claude/settings.json`` is ordinary version-controlled config an
# agent edits routinely and must NOT trip the global-mutation gate — hence the
# resolve-against-$HOME check in :func:`_is_global_config_path`, not a text regex
# that couldn't tell ``~/.claude`` from ``<repo>/.claude``.
_GLOBAL_CONFIG_FILES = frozenset(
    {
        ".codex/AGENTS.md",
        ".codex/hooks.json",
        ".codex/config.toml",
        ".claude/settings.json",
        ".hermes/config.yaml",
        ".hermes/AGENTS.md",
        ".config/opencode/opencode.json",
        ".config/opencode/plugin/omi-guard.js",
        ".gemini/settings.json",
        ".gemini/config/mcp_config.json",
        ".gemini/config/hooks.json",
        ".gemini/config/AGENTS.md",
        ".gemini/antigravity-cli/settings.json",
        ".openclaw/openclaw.json",
        ".openclaw/omind/MEMORY.md",
    }
)
_GLOBAL_CONFIG_DIRS = (".claude/hooks/", ".hermes/hooks/")
_GLOBAL_AUTH_RE = re.compile(
    r"\b(?:"
    r"make|modify|edit|write|install|update|change|patch|apply|fix|add|create|"
    r"remove|delete|configure|enable|disable|wire|register|provision|rename|"
    r"set\s*up|set|do it|go ahead|proceed|send it"
    r")\b",
    re.IGNORECASE,
)
# Negation immediately before an auth verb — "don't change anything", "no need to
# update" — must NOT read as authorization.
_AUTH_NEGATION_RE = re.compile(
    r"\b(?:don'?t|do\s+not|never|without|no\s+need\s+to|avoid|instead\s+of)\s*$",
    re.IGNORECASE,
)
_STRONG_ACTION_AUTH_RE = re.compile(
    r"\b(?:do it|go ahead|proceed|send it|approved|authorized|"
    r"you have (?:my )?(?:permission|authorization)|"
    r"i give you (?:explicit )?(?:permission|authorization))\b",
    re.IGNORECASE,
)
#: "Can/could you ...?" asks whether something is POSSIBLE — the honest answer is
#: an answer, not an action.
#:
#: "Would you ...?" and "Will you ...?" are polite imperatives in practice
#: ("would you add a button", "will you push that"). Both ask about WILLINGNESS,
#: not capability, so neither is treated as interrogatory; they fall through to
#: the ordinary verb-based auth check, which still requires a real authorizing
#: verb ("add"/"fix"/"change"/...) that isn't negated. So "would you mind not
#: touching that" does not become authorization.
#:
#: `will` was grouped with can/could until 2026-08-25, which contradicted the
#: paragraph above and made "will you please implement the fixes?" read as a
#: capability question — hard-blocking every push and merge for the rest of that
#: turn while the work sat finished and unpublishable. CJ: "Will you is asking
#: you to execute, can you is capability."
_CAPABILITY_QUESTION_RE = re.compile(
    r"^\s*(?:\w+[,:]\s+)?(?:hey[, ]+|please[, ]+)?(?:can|could)\s+you\b",
    re.IGNORECASE,
)
# A REAL output redirect to a file: ``> f`` / ``>> f`` — but NOT ``2>&1`` (fd
# dup), NOT ``2>/dev/null``, and NOT ``->`` / ``=>`` (arrows in code/strings).
# Distinguishing these is what stops ``pytest 2>&1 | tail`` from being read as a
# file-writing "side effect" and false-blocking a read-only capability question.
_FILE_REDIRECT_RE = re.compile(r"(?<![-=<>&\d])>>?[ \t]*(?!&)(?!/dev/null\b)[^\s&|>]")
_GLOBAL_MUTATING_BASH_RE = re.compile(
    r"(?:^|[;&|\n(]\s*)(?:"
    r"chmod|chown|cp|dd|ed|ex|install|mv|rm|tee|touch|truncate|"
    r"sed\b[^;&|\n]*\s-i\b|perl\b[^;&|\n]*\s-i\b|"
    r"python3?\b[^;&|\n]*(?:write_text|write_bytes|open\([^;&|\n]*[\"']a|"
    r"open\([^;&|\n]*[\"']w)|"
    r"node\b[^;&|\n]*(?:writeFile|appendFile)"
    r")\b"
)
_SHELL_SIDE_EFFECT_RE = re.compile(
    rf"(?:^|[;&|\n(]\s*)(?:"
    rf"gh\s+(?:issue\s+create|pr\s+(?:create|merge)|release\s+create)|"
    rf"git\s+{_GIT_GLOBAL_OPTS}(?:add|commit|push|merge|rebase|checkout|switch|tag)|"
    r"systemctl\s+(?:restart|reload|stop|start)|"
    r"service\s+\S+\s+(?:restart|reload|stop|start)|"
    r"kubectl\s+(?:apply|delete|rollout\s+restart|scale)|"
    r"docker\s+(?:compose\s+)?(?:up|down|restart|rm)|"
    r"chmod|chown|cp|dd|install|mv|rm|tee|touch|truncate"
    r")\b"
)

#: The subset of :data:`_SHELL_SIDE_EFFECT_RE` that actually needs an explicit
#: go-ahead when the request was phrased as a capability question — things that
#: leave this machine, restart something, destroy data, or change permissions.
#: Notably ABSENT, and deliberately so: `cp`, `mv`, `touch`, `tee`, `install`,
#: `mkdir`, and local `git add`/`commit`/`checkout`. Those are reversible local
#: work, and gating them denied real tasks on a real machine (see
#: :func:`_is_side_effect_action`). `git push` stays — it is the outward one.
_RISKY_SIDE_EFFECT_RE = re.compile(
    rf"(?:^|[;&|\n(]\s*)(?:"
    rf"gh\s+(?:issue\s+create|pr\s+(?:create|merge)|release\s+create)|"
    rf"git\s+{_GIT_GLOBAL_OPTS}push|"
    r"systemctl\s+(?:restart|reload|stop|start)|"
    r"service\s+\S+\s+(?:restart|reload|stop|start)|"
    r"kubectl\s+(?:apply|delete|rollout\s+restart|scale)|"
    r"docker\s+(?:compose\s+)?(?:up|down|restart|rm)|"
    r"chmod|chown|dd|rm|truncate"
    r")\b"
)


# Provably-inert inspection commands, exempt from the consult-gate (#147): no
# filesystem read/write, no repo, no network, no side effect — a memory consult
# could not inform them, so gating them is pure ceremony. Deliberately tiny:
# `cat`/`ls`/`grep`/`find` READ files (repo files included) and stay gated;
# `echo` is excluded because its arguments are arbitrary; `date` only in its
# read forms (`date -s` sets the clock) and `hostname` only bare (an argument
# renames the host).
_INERT_BASH_RE = re.compile(
    r"^(?:pwd|whoami|hostname|true|false|"
    r"id(?:[ \t]+-[A-Za-z]+)*(?:[ \t]+[A-Za-z0-9._-]+)?|"
    r"date(?:[ \t]+\+\S+)?|"
    r"uname(?:[ \t]+-[A-Za-z]+)*|"
    r"which[ \t]+[A-Za-z0-9._+-]+|"
    r"command[ \t]+-v[ \t]+[A-Za-z0-9._+-]+|"
    r"git[ \t]+--version"
    r")$"
)


def _is_inert_command(command: str) -> bool:
    """True only for a single bare inert command. ANY shell metacharacter —
    chain, pipe, redirect, substitution, glob — disqualifies the whole string,
    so an inert command can never carry a passenger (`pwd && rm x`,
    `which $(cmd)`)."""
    command = command.strip()
    if re.search(r"[|&;<>`$\\\n(){}\[\]*?~=]", command):
        return False
    return bool(_INERT_BASH_RE.match(command))


def _split_simple_commands(command: str) -> list[str]:
    """Split a shell command into its ``&&`` / ``||`` / ``;`` / newline parts."""
    return [c.strip() for c in re.split(r"&&|\|\||;|\n", command) if c.strip()]


def _is_freshness_command(command: str) -> bool:
    """True when the command is composed ONLY of safe git read/fetch subcommands
    and includes at least one fetch / ff-pull — so it establishes freshness and
    is itself harmless. Accepts ``git -C <repo> fetch --all --prune`` and
    compound forms like ``git fetch --all --prune && git status -sb`` (the exact
    remediation the block message tells the agent to run). A part that is NOT a
    safe git read (``git fetch && pytest``, ``git fetch | tee x``) disqualifies
    the whole command, so it can never grant freshness to a piggybacked action."""
    parts = _split_simple_commands(command)
    if not parts:
        return False
    fresh = False
    for part in parts:
        if _GIT_FRESH_SUB_RE.match(part):
            fresh = True
        elif not _GIT_READONLY_SUB_RE.match(part):
            return False
    return fresh


def _is_readonly_git_command(command: str) -> bool:
    """True when every part of the command is a safe git read/fetch (so it needs
    no note-read / freshness of its own)."""
    parts = _split_simple_commands(command)
    return bool(parts) and all(
        _GIT_FRESH_SUB_RE.match(p) or _GIT_READONLY_SUB_RE.match(p) for p in parts
    )


def _is_global_config_path(raw: str) -> bool:
    """True only for a GLOBAL (home-anchored) agent config file — never a
    project-local ``<repo>/.claude/…`` even when the repo lives under $HOME."""
    try:
        p = Path(raw).expanduser()
        if not p.is_absolute():
            p = Path.cwd() / p
    except (OSError, RuntimeError):
        return False
    candidates = {p}
    with contextlib.suppress(OSError):
        candidates.add(p.resolve())
    homes = {Path.home()}
    with contextlib.suppress(OSError):
        homes.add(Path.home().resolve())
    for cand in candidates:
        for home in homes:
            try:
                rel = cand.relative_to(home).as_posix()
            except ValueError:
                continue
            if rel in _GLOBAL_CONFIG_FILES or any(rel.startswith(d) for d in _GLOBAL_CONFIG_DIRS):
                return True
    return False


def _command_targets_global_config(command: str) -> bool:
    """True when a shell command references a global config path via ``~/`` or the
    absolute home dir (a project-relative path in the command does not count)."""
    haystack = command.replace("\\", "/")
    home = str(Path.home())
    targets = [*_GLOBAL_CONFIG_FILES, *_GLOBAL_CONFIG_DIRS]
    return any(f"~/{t}" in haystack or f"{home}/{t}" in haystack for t in targets)


def _opt_in_satisfied(opt_in: str, command: str) -> bool:
    """Strict opt-in matcher — implementation lives in :mod:`omind.policy` so
    the compliance detector (Layer E) shares the one definition that can't be
    forged by a bare substring (2026-08-27 review)."""
    return policy.opt_in_satisfied(opt_in, command)


def _action_path(action: dict[str, Any]) -> str:
    for key in ("file_path", "path"):
        value = action.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


#: Programs whose operands after ``[options] host`` run on ANOTHER machine
#: (#394), with the single-letter options that take a separate argument. Only
#: ssh: its remote command is the documented case, and anything it runs acts on
#: the remote host's repos, never the local one.
_REMOTE_LAUNCHERS: dict[str, frozenset[str]] = {"ssh": frozenset("BbcDEeFIiJLlmOopQRSWw")}
#: Shells whose ``-c`` body runs LOCALLY: their bodies are code, not data, so
#: they are unwrapped and walked like the outer command (#394 review).
_LOCAL_SHELLS = frozenset({"sh", "bash", "zsh", "dash", "ksh", "mksh", "ash"})
#: git global options that take a separate argument (``git -C <dir> push``).
_GIT_OPTS_WITH_ARG = frozenset(
    {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--super-prefix", "--config-env"}
)
#: Nesting limit for ``bash -c '… eval "…"'`` unwrapping; a deeper body is
#: left as one opaque site: its quoted text is judged as code (fails closed).
_MAX_UNWRAP_DEPTH = 4
#: Programs that run an ARGUMENT as shell code the guard does not unwrap
#: (``su -c '…'``, ``fish -c``, ``watch '…'``, ``tmux new '…'``). Their quoted
#: arguments are code, so a repo-scoped rule matching them fails closed
#: (#413 review 3) instead of being read as data.
_OPAQUE_EXECUTORS = frozenset(
    {
        "su",
        "runuser",
        "fish",
        "csh",
        "tcsh",
        "nu",
        "xonsh",
        "elvish",
        "pwsh",
        "powershell",
        "cmd",
        "script",
        "watch",
        "flock",
        "parallel",
        "tmux",
        "screen",
        "expect",
        "nix-shell",
        "chroot",
        "unshare",
        "nsenter",
        "doas",
        "pkexec",
    }
)
#: ssh options whose value runs locally, before or around the connection.
_SSH_LOCAL_EXEC_RE = re.compile(r"(?i)(?:Local|Proxy|KnownHosts)Command|\bexec\b")
#: Script interpreters whose ``-c``/``-e`` body is code: they fail closed only
#: when that body visibly spawns a process (:data:`_EXEC_CALL_RE`). A
#: ``python3 -c "print('git push')"`` is data.
_SCRIPT_INTERPRETER_RE = re.compile(
    r"python[\d.]*|pypy[\d.]*|node|nodejs|deno|bun|ruby|perl|php|lua"
)
_EXEC_CALL_RE = re.compile(
    r"subprocess|os\.(?:system|popen|exec|spawn)|pty\.spawn|child_process|Open3|%x|`"
    r"|\b(?:system|exec|execSync|spawn|spawnSync|popen|qx|shell_exec|passthru|proc_open)\b"
)


@dataclass(frozen=True)
class _ShellSite:
    """One simple command the LOCAL shell runs (#394).

    ``text`` is its raw text from the program word on (wrappers such as
    ``sudo``/``env``/``timeout 60`` skipped), with an ssh remote payload
    blanked. ``cwd`` is the directory it runs in, relative to the shell's
    starting directory unless absolute; ``None`` when a ``cd $VAR`` /
    ``cd -`` made it unknowable (callers then fall back to the start).
    ``opaque`` marks a program that runs a quoted argument as code the guard
    did not unwrap (``su -c``, a too-deep ``bash -c``, a python ``-c`` that
    calls ``os.system``): its quoted text is code, not data."""

    program: str
    text: str
    cwd: Path | None
    opaque: bool = False
    #: A shell, ``source`` or ``-c`` body reading code from stdin: ``text`` is
    #: the pipeline feeding it (``cat <<EOF | bash``, #432). A heredoc fed to
    #: a body-less shell is walked instead of kept in ``text``.
    piped: bool = False


def _chdir(cwd: Path | None, step: str) -> Path | None:
    """``cd step`` from ``cwd``: shell semantics, ``~`` expanded. ``None``
    (unknowable) when ``~user`` names no user: ``expanduser`` raises there,
    and the raise used to discard the whole walk (#432)."""
    try:
        target = Path(step).expanduser()
    except (RuntimeError, KeyError, OSError):
        return None
    if target.is_absolute():
        return target
    return None if cwd is None else cwd / target


#: A redirection operator alone (``>``, ``2>``, ``&>``, ``>&``): its target is
#: the next word.
_REDIRECT_OP_RE = re.compile(r"[0-9]*&?[<>]+[&|]?")
#: A word that starts with a redirection (``>/dev/null``, ``2>&1``, ``<in``).
_REDIRECT_RE = re.compile(r"[0-9]*&?[<>]")


def _without_redirects(tokens: list[str]) -> list[str]:
    """``tokens`` minus redirections and their targets: ``cd X >/dev/null``
    and ``pushd X > /dev/null`` name the same directory as ``cd X``."""
    words: list[str] = []
    skip = False
    for token in tokens:
        if skip:
            skip = False
        elif _REDIRECT_OP_RE.fullmatch(token):
            skip = True
        elif not _REDIRECT_RE.match(token):
            words.append(token)
    return words


def _cd_operand(tokens: list[str] | None) -> str | None:
    """The literal directory a ``cd``/``pushd`` names, or ``None`` when it is
    not statically knowable (``cd -``, ``cd $VAR``, ``pushd +1``,
    unparseable). Redirections are dropped and ``-P``/``-L``/``-e``/``-@``
    and ``--`` skipped first (#413 review 3: ``pushd X >/dev/null``)."""
    if tokens is None:
        return None
    words = _without_redirects(tokens)
    if not words:
        return None
    i = 1
    while i < len(words) and words[i].startswith("-") and words[i] != "-":
        if words[i] == "--":
            i += 1
            break
        if not re.fullmatch(r"-[LPe@]+", words[i]):
            return None
        i += 1
    operands = words[i:]
    if not operands:
        return "~" if words[0] == "cd" else None  # bare `cd` goes home
    step = operands[0]
    if len(operands) != 1 or step == "-" or "$" in step or "`" in step:
        return None
    if words[0] == "pushd" and step[:1] == "+":
        return None  # pushd +N rotates the stack
    return step


def _git_site_cwd(tokens: list[str], cwd: Path | None) -> Path | None:
    """The directory a ``git`` invocation acts on: ``cwd`` moved by its own
    cumulative ``-C`` options (git's semantics: each relative to the last),
    then by ``--work-tree`` or ``--git-dir`` when given (the repo is found
    upward from either)."""
    i, tree = 1, ""
    while i < len(tokens):
        word = tokens[i]
        if not word.startswith("-"):
            break
        name, eq, value = word.partition("=")
        if name in _GIT_OPTS_WITH_ARG and not eq:
            if i + 1 >= len(tokens):
                break
            value = tokens[i + 1]
            i += 1
        if name == "-C":
            cwd = _chdir(cwd, value)
        elif name == "--work-tree" or (name == "--git-dir" and not tree):
            tree = value
        i += 1
    return _chdir(cwd, tree) if tree else cwd


def _env_chdir(prefix: str, cwd: Path | None) -> Path | None:
    """``cwd`` moved by every ``env -C <dir>`` / ``--chdir[=]<dir>`` among the
    wrapper words ``prefix`` that precede a stage's program (#432): ``env
    -C <public> git push`` pushes from ``<public>``. A ``$VAR`` or unparseable
    value makes the directory unknowable (``None``).

    The prefix is read the way :func:`_program_stages` skips wrappers, so
    ``env`` counts only in its own position as a wrapper, never as another
    wrapper's value (``sudo -u env -C 3 git``), and a short cluster reads like
    getopt (``env -iC <dir>``, ``env -iC<dir>``, ``env -u X -C <dir>``)."""
    try:
        words = _shell_tokens(prefix)
    except ValueError:
        return None
    for k in range(len(words) - 1, -1, -1):
        if words[k] in _EXEC_ACTIONS:  # only this stage's own wrappers
            words = words[k + 1 :]
            break
    i = 0
    while i < len(words):
        if re.match(r"[A-Za-z_]\w*=", words[i]):
            i += 1
            continue
        base = _basename(words[i])
        if base not in _STAGE_WRAPPERS:
            break  # not a wrapper chain this reads: the directory stays put
        takes_arg = _STAGE_WRAPPERS[base]
        positional = base in _WRAPPER_POSITIONAL
        i += 1
        while i < len(words):
            word = words[i]
            if word == "--":
                i += 1
                break
            if not word.startswith("-") or len(word) < 2:
                if not positional:
                    break
                positional = False  # `timeout 5`: the duration, then more switches
                i += 1
                continue
            if base == "env":
                value = _env_chdir_value(word, words[i + 1] if i + 1 < len(words) else "")
                if value is not None:
                    literal = value and "$" not in value and "`" not in value
                    cwd = _chdir(cwd, value) if literal else None
            i += _switch_width(word, takes_arg)
    return cwd


def _env_chdir_value(word: str, after: str) -> str | None:
    """The directory the ``env`` switch ``word`` changes to (``after`` is the
    next word), or ``None`` when it changes none. ``-C`` may end or sit inside
    a short cluster (``-iC <dir>``, ``-iC<dir>``). A letter before it that
    takes a value (``-uC`` unsets ``C``), or ``-S`` (its rest is a command),
    ends the cluster first."""
    if word in ("-C", "--chdir"):
        return after
    if word.startswith("--chdir="):
        return word.partition("=")[2]
    if word.startswith("--"):
        return None
    for k in range(1, len(word)):
        if word[k] == "C":
            return word[k + 1 :] or after
        if word[k] == "S" or "-" + word[k] in _STAGE_WRAPPERS["env"]:
            return None
    return None


def _shell_body(tokens: list[str]) -> str | None:
    """The ``-c`` body of ``sh/bash/zsh … -c '<body>'`` (``-lc``, ``-ec`` and
    ``-o opt`` before it included), or ``None`` when there is no ``-c``."""
    i = _shell_body_index(tokens)
    return None if i is None else tokens[i]


def _shell_body_index(tokens: list[str]) -> int | None:
    """Where :func:`_shell_body` finds the body in ``tokens``."""
    has_c = False
    i = 1
    while i < len(tokens):
        word = tokens[i]
        if word in ("-o", "+o", "-O", "+O"):
            i += 2
            continue
        if word == "--":
            i += 1
            continue
        if len(word) > 1 and word[0] in "-+":
            has_c = has_c or (word[0] == "-" and "c" in word[1:])
            i += 1
            continue
        return i if has_c else None
    return None


def _shell_sites(
    command: str,
    cwd: Path | None = Path("."),
    dirs: tuple[Path | None, ...] = (),
    depth: int = 0,
    bodies: list[str] | None = None,
) -> tuple[list[_ShellSite], Path | None, tuple[Path | None, ...], str]:
    """Walk ``command`` as the local shell would and list every simple command
    it runs, each with the directory it runs in (#394). Every ``-c``/``eval``
    body the walk unwraps, at any depth, is appended to ``bodies`` when given.

    Returns ``(sites, cwd, dirs, local_text)``: the final ``cwd`` and
    ``pushd`` stack (an ``eval`` body shares them with its caller), and
    ``command`` with every ssh remote payload blanked (length-preserving).

    Stages come from :func:`_program_stages`, so wrappers (``sudo``, ``env
    X=1``, ``timeout 60``, ``xargs`` …) are already skipped. ``cd`` /
    ``pushd`` / ``popd`` move the directory; ``( … )`` and ``$( … )`` scope a
    move to the subshell; a ``cd`` piped or backgrounded with a single ``|`` /
    ``&`` runs in its own subshell and moves nothing. ``sh/bash/zsh -c`` and
    ``eval`` bodies are code this machine runs, so they are walked too (a
    ``-c`` body in a child shell whose ``cd`` does not leak out; an ``eval`` in
    this one). An ssh remote command, quoted or not, runs elsewhere and is
    blanked. ``a || b`` is read like ``a && b``: a ``cd`` before ``||`` is
    assumed to have run, which is wrong only when that ``cd`` failed.

    A ``cd``/``pushd`` operand is read past redirections and ``-P``/``-L``/
    ``--`` (``pushd X >/dev/null``). A site whose quoted arguments are code
    the walk did not follow (``su -c``, a too-deep or positional-arg ``bash
    -c``, ssh ``LocalCommand``) is marked ``opaque`` so rules fail closed on it.
    A shell with no ``-c`` body is opaque with the pipeline feeding it as its
    text, and a heredoc that pipeline feeds it is walked as a child shell
    (``cat <<EOF | bash``); an interpreter's own heredoc is part of its text;
    ``env -C <dir>`` moves the directory of the command it runs (#432).

    Never raises on shell text: an unparseable word list keeps the site at the
    current directory; an unknowable ``cd`` makes the directory ``None``.
    """
    raw = command.replace("\\\n", "  ")
    code = policy.shell_code_text(command).replace("\\\n", "  ")
    local = list(raw)
    stages = _program_stages(code, raw)
    events: list[tuple[int, int, Any]] = [
        (m.start(), 0, m.group()) for m in re.finditer(r"[()]", code)
    ]
    events += [(stage[2], 1, stage) for stage in stages]
    # Each stage's text stops at the next stage's start, so a long `find
    # -exec … {} +` chain (no separator) is not rescanned to the end for
    # every stage: that was quadratic (#432).
    starts = sorted(stage[2] for stage in stages) + [len(code)]
    ends = _stage_ends(code, starts[:-1])
    nth = floor = 0
    sites: list[_ShellSite] = []
    scopes: list[tuple[Path | None, tuple[Path | None, ...]]] = []
    for pos, _kind, item in sorted(events, key=lambda e: (e[0], e[1])):
        if item == "(":
            scopes.append((cwd, dirs))
            continue
        if item == ")":
            if scopes:
                cwd, dirs = scopes.pop()
            continue
        program = item[0]
        while starts[nth] <= pos:
            nth += 1
        end = ends[pos]
        text = raw[pos:end].rstrip()
        try:
            tokens: list[str] | None = _shell_tokens(text)
        except ValueError:
            tokens = None
        if program in ("cd", "pushd", "popd"):
            # Read around the stage by offset, not by copying the text before
            # and after it, which was quadratic in a run of `cd`s (#445).
            last = pos - 1
            while last >= 0 and code[last].isspace():
                last -= 1
            in_subshell = (
                (code.startswith("|", end) and not code.startswith("||", end))
                or (code.startswith("&", end) and not code.startswith("&&", end))
                or (last >= 0 and code[last] == "|" and not (last >= 1 and code[last - 1] == "|"))
            )
            if not in_subshell:
                if program == "popd":
                    if tokens is None or len(_without_redirects(tokens)) != 1:
                        cwd = None
                    elif dirs:
                        cwd, dirs = dirs[0], dirs[1:]
                else:
                    operand = _cd_operand(tokens)
                    moved = None if operand is None else _chdir(cwd, operand)
                    if program == "pushd":
                        dirs = (cwd, *dirs)
                    cwd = moved
            sites.append(_ShellSite(program, text, cwd))
            continue
        if program in _REMOTE_LAUNCHERS:
            takes_arg = _REMOTE_LAUNCHERS[program]
            words = list(re.finditer(r"\S+", code[pos:end]))
            i = 1
            while i < len(words):
                word = words[i].group()
                if word.startswith("-") and len(word) > 1:
                    i += 2 if len(word) == 2 and word[1] in takes_arg else 1
                    continue
                cut = pos + words[i].end()  # everything after the host runs remotely
                local[cut:end] = [" "] * (end - cut)
                text = raw[pos:cut]
                break
            # `-o LocalCommand=…` / `ProxyCommand` / `KnownHostsCommand` /
            # `Match exec` run on THIS machine: their quoted values are code.
            sites.append(_ShellSite(program, text, cwd, bool(_SSH_LOCAL_EXEC_RE.search(text))))
            continue
        here = cwd
        # This stage's wrapper words: back to the previous stage's start or
        # separator, whichever is nearer (bounded, so a chain stays linear).
        lo = starts[nth - 2] if nth >= 2 else 0
        lo = max([lo - 1] + [code.rfind(ch, lo, pos) for ch in ";&|\n()`"]) + 1
        if re.search(r"(?:^|[\s/])env\s", raw[lo:pos]):
            here = _env_chdir(raw[lo:pos], cwd)  # `env -C <dir> git push`
        body: str | None = None
        positional = stdin_body = False
        if tokens and depth < _MAX_UNWRAP_DEPTH:
            if program in _LOCAL_SHELLS:
                at_body = _shell_body_index(tokens)
                if at_body is not None:
                    body = tokens[at_body]
                    positional = at_body + 1 < len(tokens)
                    stdin_body = _body_reads_stdin(tokens, at_body)
            elif program == "eval" and len(tokens) > 1:
                body = " ".join(tokens[1:])
        if body is not None:
            if stdin_body:
                # The body reads its stdin as code (`eval "$(cat)"`, `.
                # /dev/stdin`, a nested `sh`): the pipeline and any heredoc
                # feeding it are code too (#432 review). The fed heredoc is
                # walked as shell code, and the site keeps it in its opaque
                # text, since the body may hand it to something else.
                head = _pipeline_head(code, pos, floor)
                floor = end
                fed = _owned_heredocs(raw, code, head, end)
                if fed:
                    sites.extend(_shell_sites(fed, here, dirs, depth + 1, bodies)[0])
                piped_text = f"{raw[head:end].rstrip()}\n{fed}".rstrip()
                sites.append(_ShellSite(program, piped_text, here, True, True))
            if positional:
                # Words after the body become $0, $1 ..., which the body can
                # run (`bash -c '"$@"' _ <cmd>`): code the walk does not
                # follow, so the whole command fails closed.
                sites.append(_ShellSite(program, text, here, True))
            if bodies is not None:
                bodies.append(body)
            inner, inner_cwd, inner_dirs, inner_local = _shell_sites(
                body, here, dirs, depth + 1, bodies
            )
            sites.extend(inner)
            if program == "eval":
                cwd, dirs = inner_cwd, inner_dirs
            at = raw.find(body, pos, end)
            if at >= 0:  # carry the body's blanked remote payloads outward
                local[at : at + len(body)] = list(inner_local)
            continue
        site_cwd = _git_site_cwd(tokens, here) if program == "git" and tokens else here
        piped = False
        sources_stdin = program in ("source", ".") and _sources_stdin(tokens)
        if (
            (program in _LOCAL_SHELLS or sources_stdin)
            and tokens is not None
            and depth < _MAX_UNWRAP_DEPTH
        ):
            # No `-c` body: it runs a script or reads code from stdin (`cat
            # <<EOF | bash`, `echo … | sh`, `… | source /dev/stdin`). The code
            # is in the pipeline that feeds it, which is judged as this site's
            # text (#432: the text was just `bash`, so a piped heredoc push
            # passed). A heredoc that pipeline feeds it is shell code, walked
            # in a child shell; once walked, it is left out of this site's
            # opaque text, so a `cd` inside it counts (#432 review). A bare
            # shell's own heredoc (`bash <<EOF`) is code the walk already
            # reads in place; `source /dev/stdin <<EOF`'s is masked, so fed.
            head = _pipeline_head(code, pos, floor)
            floor = end
            fed = _owned_heredocs(raw, code, head, end if sources_stdin else pos)
            text, piped = raw[head:end].rstrip(), True
            if fed:
                sites.extend(_shell_sites(fed, site_cwd, dirs, depth + 1, bodies)[0])
        elif _SCRIPT_INTERPRETER_RE.fullmatch(program):
            # The heredoc it owns (`python3 - <<EOF`) is its code, judged like
            # its `-c` twin (#432); `end` stopped at the line break before it.
            # When it reads its program from stdin, the pipeline feeding it
            # and that pipeline's heredoc are its code too (`cat <<EOF |
            # python3 -`, #432 review).
            head = pos
            if tokens is not None and _interpreter_reads_stdin(tokens):
                head = _pipeline_head(code, pos, floor)
                floor = end
            fed = _owned_heredocs(raw, code, head, end)
            text = f"{raw[head:end].rstrip()}\n{fed}".rstrip()
        opaque = (
            program in _OPAQUE_EXECUTORS
            or sources_stdin
            or program in _LOCAL_SHELLS  # a shell body not unwrapped (too deep, unparseable)
            or program == "eval"
            or raw[pos] in "'\""  # the program word itself is quoted: `'git push' x`
            or (
                bool(_SCRIPT_INTERPRETER_RE.fullmatch(program)) and bool(_EXEC_CALL_RE.search(text))
            )
        )
        sites.append(_ShellSite(program, text, site_cwd, opaque, piped))
    return sites, cwd, dirs, "".join(local)


def _stage_ends(code: str, starts: list[int]) -> dict[int, int]:
    """Where each stage starting at an offset in ``starts`` (sorted) ends: at
    the first separator after it that is not part of a redirection, or at the
    next stage's start, whichever comes first (#432). One backward pass finds
    every next separator, so a long chain with no separator (a `find -exec …
    {} +` run, with or without `2>&1` in each clause) stays linear.

    A stage that starts right after a redirection's ``&``/``|`` (the ``1`` of
    ``2>&1 …``, the target of ``&>f``) is the stage splitter cutting at a
    redirect, not a new command: it does not bound the stage before it."""
    n = len(code)
    # Separator offsets in one regex pass, not a Python loop per character
    # (#445); each stage looks up the first one at or after it.
    seps = [
        m.start() for m in _STAGE_SEP_RE.finditer(code) if not _is_redirect_char(code, m.start())
    ]
    seps.append(n)
    real = [s for s in starts if not _after_redirect(code, s)] + [n]
    ends: dict[int, int] = {}
    following = [*starts[1:], n]  # built once: per stage it was quadratic (#445)
    r = 0
    for idx, pos in enumerate(starts):
        while real[r] <= pos:
            r += 1
        # A split-off redirect target ends at the next stage of any kind.
        bound = real[r] if not _after_redirect(code, pos) else following[idx]
        ends[pos] = min(seps[bisect.bisect_left(seps, pos)], bound)
    return ends


_STAGE_SEP_RE = re.compile(r"[;&|\n()`]")
#: One simple command's text between separators; an escaped `\;` (find's
#: -exec terminator) is a word, not a separator (#419). Runs of plain
#: characters are taken whole, not one alternation per character (#445).
_SEGMENT_RE = re.compile(r"(?:[^;&|\n(`)\\]+|\\.)+")


def _after_redirect(code: str, pos: int) -> bool:
    """Whether the stage at ``pos`` follows a redirection's ``&``/``|``
    (blanks between), so the stage splitter cut a redirection there."""
    k = pos - 1
    while k >= 0 and code[k] in " \t":
        k -= 1
    return k >= 0 and code[k] in "&|" and _is_redirect_char(code, k)


#: A ``-c`` body that reads its stdin as code (#432 review): a ``$(cat)`` /
#: backtick ``cat``, ``/dev/stdin``, ``/dev/fd/0``, ``source -``, or a shell,
#: interpreter, ``read`` or ``xargs`` in command position, any of which can
#: run what the pipeline feeding the outer shell writes.
_BODY_READS_STDIN_RE = re.compile(
    r"/dev/stdin|/dev/fd/0|\$\(\s*cat\b|`\s*cat\b"
    r"|(?:^|[\s;&|(`{])(?:source|\.)\s+-(?=\s|$)"
    r"|(?:^|[\s;&|(`{])(?:(?:ba|z|da|k|mk|a)?sh|python[\d.]*|pypy[\d.]*|node|nodejs"
    r"|deno|bun|ruby|perl|php|lua|read|xargs)(?=[\s;&|)`}]|$)"
)

#: Operands that make ``source``/``.`` (or a shell) read code from stdin.
_STDIN_OPERANDS = frozenset({"/dev/stdin", "/dev/fd/0", "-"})


def _is_stdin_switch(word: str) -> bool:
    """Whether the shell switch ``word`` is a short cluster holding ``-s``
    (``-s``, ``-xs``): the shell reads its code from stdin. A long option
    (``--posix``, ``--restricted``) is not one (#444 review)."""
    return len(word) > 1 and word[0] == "-" and word[1] != "-" and "s" in word[1:]


def _body_reads_stdin(tokens: list[str], at_body: int) -> bool:
    """Whether the local shell ``tokens`` (whose ``-c`` body is
    ``tokens[at_body]``) runs code from its stdin: an ``-s`` among its
    switches, or a body :data:`_BODY_READS_STDIN_RE` matches."""
    if any(_is_stdin_switch(w) for w in tokens[1:at_body]):
        return True
    return bool(_BODY_READS_STDIN_RE.search(tokens[at_body]))


def _source_words(tokens: list[str] | None) -> list[str]:
    """``source``/``.`` ``tokens`` minus redirections and the ``--`` that
    ends its options (``source -- <(…)``, #444 review)."""
    words = _without_redirects(tokens or [])
    return words[:1] + words[2:] if words[1:2] == ["--"] else words


def _sources_stdin(tokens: list[str] | None) -> bool:
    """Whether ``source``/``.`` ``tokens`` reads its stdin as code: its file
    operand is ``/dev/stdin``, ``/dev/fd/0`` or ``-`` (#432 review)."""
    words = _source_words(tokens)
    return len(words) > 1 and words[1] in _STDIN_OPERANDS


#: Interpreter switches whose value is the program itself (``python3 -c``,
#: ``node -e``, ``perl -E``, ``php -r``) or a module (``python3 -m``).
_INTERPRETER_CODE_SWITCHES = frozenset({"-c", "-m", "-e", "-E", "-p", "-r", "--eval", "--print"})
#: Interpreter switches that take a separate, non-code value.
_INTERPRETER_VALUE_SWITCHES = frozenset({"-W", "-X", "--require"})


def _interpreter_reads_stdin(tokens: list[str]) -> bool:
    """Whether interpreter ``tokens`` reads its program from stdin: a ``-``
    or ``/dev/stdin`` operand, or no script and no ``-c``/``-e`` program at
    all (``… | python3``). A script operand reads a file (#432 review)."""
    words = _without_redirects(tokens)
    i = 1
    while i < len(words):
        word = words[i]
        if word in _STDIN_OPERANDS:
            return True
        if word == "--":
            return i + 1 >= len(words) or words[i + 1] in _STDIN_OPERANDS
        if not word.startswith("-"):
            return False  # a script file
        if word in _INTERPRETER_CODE_SWITCHES or (
            not word.startswith("--") and word[-1] in "cmeEpr"
        ):
            return False  # `-c '…'`, `-uc '…'`: the program is an argument
        i += 2 if word in _INTERPRETER_VALUE_SWITCHES else 1
    return True


def _owned_heredocs(raw: str, code: str, start: int, stop: int) -> str:
    """The bodies of the heredocs whose ``<<`` operator lies in
    ``code[start:stop]`` (``code`` is ``raw`` masked), read from the lines
    after ``stop``'s line. A heredoc opened earlier on that line, by another
    command, is skipped: its body comes first and is not this one's (#432)."""
    if "<<" not in code[start:stop]:
        return ""
    newline = _next_line_break(raw, stop)
    if newline < 0:
        return ""  # no line after it: every body is empty (#445)
    breaks = _line_breaks(raw)
    before = bisect.bisect_left(breaks, start)
    line = breaks[before - 1] + 1 if before else 0
    heredocs = _heredoc_ops_between(code, line, stop)
    if not any(m.start() >= start for m in heredocs):
        return ""
    return _heredoc_bodies(raw, newline, heredocs, start)


@functools.lru_cache(maxsize=8)
def _line_breaks(text: str) -> tuple[int, ...]:
    """Every newline offset in ``text``, in order: found once per text, so a
    line holding thousands of stages is not rescanned per stage (#445). A
    tuple: the memo is shared, so no caller may change it."""
    return tuple(m.start() for m in re.finditer("\n", text))


def _next_line_break(text: str, at: int) -> int:
    """``text.find("\\n", at)`` by bisection over :func:`_line_breaks`."""
    breaks = _line_breaks(text)
    k = bisect.bisect_left(breaks, at)
    return breaks[k] if k < len(breaks) else -1


@functools.lru_cache(maxsize=8)
def _heredoc_ops(code: str) -> tuple[list[int], list[re.Match[str]]]:
    """Every ``policy._HEREDOC_RE`` match in ``code``, and their offsets."""
    found = list(policy._HEREDOC_RE.finditer(code))
    return [m.start() for m in found], found


def _heredoc_ops_between(code: str, line: int, stop: int) -> list[re.Match[str]]:
    """``list(policy._HEREDOC_RE.finditer(code, line, stop))`` from the matches
    found once per text (#445). Where a match straddles ``line`` or ``stop``,
    a search bounded there could match differently, so it searches as before."""
    starts, found = _heredoc_ops(code)
    lo = bisect.bisect_left(starts, line)
    hi = bisect.bisect_left(starts, stop)
    if (lo and found[lo - 1].end() > line) or (hi and found[hi - 1].end() > stop):
        return list(policy._HEREDOC_RE.finditer(code, line, stop))
    return found[lo:hi]


def _is_redirect_char(code: str, k: int) -> bool:
    """Whether the ``&``/``|`` at ``code[k]`` belongs to a redirection
    (``2>&1``, ``&>f``, ``>|f``), not a separator ending the simple command."""
    ch = code[k]
    if ch == "&":
        return (k > 0 and code[k - 1] in "<>") or (k + 1 < len(code) and code[k + 1] == ">")
    return ch == "|" and k > 0 and code[k - 1] == ">"


def _git_dash_c_path(command: str) -> Path | None:
    """The directory the command's first ``git`` invocation acts on: its
    ``cd``/``pushd`` context (#394) plus its own ``-C``/``--work-tree``/
    ``--git-dir`` options (#147). With no git, where the command's ``cd``
    steps leave the shell. Only a literal ``git`` program honors ``-C``, so
    ``make -C``/``tar -C`` are never misread. A relative result resolves
    against the cwd in the caller; ``None`` (fall back to cwd) when the
    directory is not statically knowable. Never raises.

    Only the FIRST git is reported (freshness and classification want one repo
    per command). Note rules judge every git separately, through
    :func:`_rules_command_view`."""
    try:
        sites, cwd, _local, _bodies = _shell_walk(command)
    except Exception:
        return None
    for site in sites:
        if site.program == "git":
            return site.cwd
    return cwd


@functools.lru_cache(maxsize=8)
def _shell_walk(
    command: str,
) -> tuple[tuple[_ShellSite, ...], Path | None, str, tuple[str, ...]]:
    """:func:`_shell_sites` from the start directory, memoised: the repo
    resolver, the note-rule view and the hard rules (#430) walk the same
    command in one check (#413 review 3). Pure in ``command``: sites hold
    paths relative to the start. The last item is every unwrapped
    ``-c``/``eval`` body."""
    bodies: list[str] = []
    sites, cwd, _dirs, local = _shell_sites(command, bodies=bodies)
    return tuple(sites), cwd, local, tuple(bodies)


def _windows_shell() -> bool:
    """Whether hooks run on Windows. A function so tests can drive
    :func:`_shell_tokens` down its Windows branch on any platform."""
    return os.name == "nt"


def _shell_tokens(part: str) -> list[str]:
    """shlex tokens for one simple command. POSIX shlex treats every backslash
    as an escape and turns an unquoted Windows path such as ``C:\\repo`` into
    ``C:repo``. PowerShell/cmd do not use backslashes that way, so retain them
    for a Windows shell or an explicit drive path. Quotes still follow POSIX
    rules there: Windows agents run hooks under Git Bash, where a quote opens
    mid-word, so ``<<<'x y'`` and ``--split-string='x y'`` are one unquoted
    word each. (Non-POSIX shlex split them at the blank and kept the quotes,
    so the code in them was never judged; #430.) Raises ``ValueError`` on an
    unbalanced quote.

    The split is :func:`_split_words`, which yields exactly what
    ``shlex.split`` (or the escape-free lexer) would, in C-speed regex passes:
    shlex reads one character at a time in Python, which made a 2 MB command
    take seconds to judge (#445)."""
    escape = not (_windows_shell() or (":\\" in part and re.search(r"(?<!\w)[A-Za-z]:\\", part)))
    return list(_split_words(part, escape))


#: One shell word as POSIX ``shlex`` (``whitespace_split``, no commenters)
#: reads it: runs of plain characters, ``\\x`` escapes, ``'…'`` and ``"…"``
#: (where a backslash escapes the next character). Each alternative starts on
#: a distinct character, so the match never backtracks (#445).
_SHLEX_WORD_RE = re.compile(r"""(?:[^ \t\r\n'"\\]+|\\.|'[^']*'|"(?:[^"\\]|\\.)*")+""", re.S)
_SHLEX_PIECE_RE = re.compile(r"""\\(.)|'([^']*)'|"((?:[^"\\]|\\.)*)\"""", re.S)
#: The same with no escape character (``lexer.escape = ""``): a backslash is
#: an ordinary character everywhere, quotes included.
_SHLEX_RAW_WORD_RE = re.compile(r"""(?:[^ \t\r\n'"]+|'[^']*'|"[^"]*")+""")
_SHLEX_RAW_PIECE_RE = re.compile(r"""'([^']*)'|"([^"]*)\"""")
_SHLEX_BLANKS = " \t\r\n"


def _shlex_piece(match: re.Match[str]) -> str:
    """One quoted or escaped piece of a word, unquoted the way shlex does: in
    double quotes a backslash escapes only ``"`` and itself."""
    if match.group(1) is not None:
        return match.group(1)
    if match.group(2) is not None:
        return match.group(2)
    return re.sub(
        r"\\(.)",
        lambda m: m.group(1) if m.group(1) in '"\\' else m.group(0),
        match.group(3),
        flags=re.S,
    )


@functools.lru_cache(maxsize=16)
def _split_words(text: str, escape: bool = True) -> tuple[str, ...]:
    """``shlex.split(text)`` (``escape``) or the escape-free POSIX lexer,
    token for token, without its per-character Python loop (#445). Raises
    ``ValueError`` where shlex would: an unclosed quote, a trailing escape.
    Memoised: the walk and the hard rules split the same stage text."""
    word_re = _SHLEX_WORD_RE if escape else _SHLEX_RAW_WORD_RE
    if word_re.sub("", text).strip(_SHLEX_BLANKS):
        raise ValueError("No closing quotation")  # a quote or escape no word took
    words = word_re.findall(text)
    if not words:
        return ()
    # Unquote every word in one pass over them joined by a character none
    # holds: a word's quotes pair up within it, so no piece spans two words.
    # With no escapes, the pieces are bare quotes, which a template unquotes
    # without a Python call per piece.
    sep = _absent_char(text)
    joined = sep.join(words)
    if escape and "\\" in joined:
        joined = _SHLEX_PIECE_RE.sub(_shlex_piece, joined)
    else:
        joined = _SHLEX_RAW_PIECE_RE.sub(r"\1\2", joined)
    return tuple(joined.split(sep))


def _absent_char(text: str) -> str:
    """A character ``text`` does not hold, and not a quote or backslash (it
    joins words that :func:`_split_words` then unquotes): NUL, unless the text
    holds one."""
    if "\0" not in text:
        return "\0"
    present = set(text) | set("'\"\\")
    return next(ch for ch in map(chr, range(1, 0x110000)) if ch not in present)


def _action_command(action: dict[str, Any]) -> str:
    cmd = str(action.get("command") or "")
    if not cmd and isinstance(action.get("tool_input"), dict):
        cmd = str(action["tool_input"].get("command") or action["tool_input"].get("cmd") or "")
    return cmd


def _action_base_dir(action: dict[str, Any]) -> Path:
    """The adapter passes the hook event's cwd (the agent's shell cwd), which
    can differ from this process's (#394: a worktree commit judged against the
    main checkout). Fall back to the process cwd when absent or bogus."""
    base = Path.cwd()
    with contextlib.suppress(Exception):
        event_cwd = action.get("cwd")
        if isinstance(event_cwd, str) and event_cwd and Path(event_cwd).is_dir():
            base = Path(event_cwd)
    return base


def _enclosing_repo(candidates: list[Path]) -> Path | None:
    """The git worktree enclosing the first candidate that has one."""
    for candidate in candidates:
        try:
            cur = candidate.resolve()
        except ValueError:
            # An embedded NUL (`cd /tm\0p`) names no directory; it raised
            # past every rule (#413 review 3). Fall back to the next candidate,
            # which ends at the cwd.
            continue
        except OSError:
            cur = candidate.absolute()
        for parent in (cur, *cur.parents):
            if _is_worktree_root(parent):
                return parent
    return None


def _is_worktree_root(path: Path) -> bool:
    """Whether ``path`` is the top of a git worktree. A real worktree has
    either a .git pointer file or a directory containing HEAD. Merely finding
    an empty directory named .git (for example a sandbox mount marker) must
    not turn every child path into a repository and demand an impossible
    freshness fetch."""
    marker = path / ".git"
    return marker.is_file() or (marker.is_dir() and (marker / "HEAD").is_file())


def _dir_repo(path: Path, memo: dict[Path, Path | None]) -> Path | None:
    """The worktree enclosing the resolved ``path``, walked without a bound as
    :func:`_enclosing_repo` walks the Write tool's path (#448). ``memo`` is
    shared by every target of one command, so each directory is checked once."""
    visited: list[Path] = []
    found: Path | None = None
    for parent in (path, *path.parents):
        if parent in memo:
            found = memo[parent]
            break
        visited.append(parent)
        if _is_worktree_root(parent):
            found = parent
            break
    for directory in visited:
        memo[directory] = found
    return found


def _repo_root_for_action(action: dict[str, Any]) -> Path | None:
    candidates: list[Path] = []
    raw_path = _action_path(action)
    if raw_path:
        p = Path(raw_path).expanduser()
        candidates.append(p if p.is_dir() else p.parent)
    else:
        base = _action_base_dir(action)
        # A Bash action carries no file path, so the repo was previously always
        # the shell's cwd, which misattributed `git -C <other-repo> fetch` (and
        # `git -C <other-repo> commit`) to the cwd repo (#147). Honor `-C` and
        # `cd` for git commands; a target outside any repo falls through to cwd.
        with contextlib.suppress(Exception):
            dash_c = _git_dash_c_path(_action_command(action))
            if dash_c is not None:
                candidates.append(base / dash_c)
        candidates.append(base)
    return _enclosing_repo(candidates)


def _rules_command_view(action: dict[str, Any]) -> rules_mod.CommandView | None:
    """The Bash command as note rules should see it (#394): ssh remote payloads
    blanked, and every simple command the local shell runs paired with the
    repo it runs in, so a repo-scoped rule judges EACH matching git invocation
    against its own repo and refspec. ``None`` when there is no command or the
    walk fails; rules then judge the raw command against the command-level
    repo, as before #394, which is never more permissive."""
    command = _action_command(action)
    if not command or _action_path(action):
        return None
    try:
        from omind import rules as rules_mod

        base = _action_base_dir(action)
        sites, _cwd, local_text, _bodies = _shell_walk(command)
        repos: dict[Path | None, Path | None] = {}  # 1,600 chained pushes share one cwd
        for site in sites:
            if site.cwd not in repos:
                cands = [base] if site.cwd is None else [base / site.cwd, base]
                repos[site.cwd] = _enclosing_repo(cands)
        return rules_mod.CommandView(
            local_text=local_text,
            sites=tuple(
                rules_mod.CommandSite(text=site.text, repo=repos[site.cwd], opaque=site.opaque)
                for site in sites
            ),
        )
    except Exception:
        return None


def _repo_has_remote(repo: Path) -> bool:
    """True when the repo has at least one configured remote — i.e. there is an
    upstream its local base could be stale against, so a freshness check is
    meaningful. A brand-new ``git init`` repo with no remote has nothing to
    fetch: a bare ``git fetch`` errors (*No remote repository specified*) and
    ``git pull --ff-only`` errors (*no tracking information*), so demanding a
    same-turn freshness check there locks the agent out of its own new repo
    (#149). Such a repo is treated as vacuously fresh (the caller waives the
    freshness demand only — the rules-note consult still applies).

    Subprocess-free (this runs inside the PreToolUse hot path) and deliberately
    CONSERVATIVE: it returns ``True`` on any doubt — a ``.git`` that is a
    linked-worktree / submodule pointer *file* (whose remotes live in the shared
    config, not here), an unreadable config, or a resolution error — so freshness
    is waived ONLY when we positively read the repo's own config and find zero
    ``[remote "…"]`` stanzas. This makes the change a pure correctness fix for
    new local repos and never a loosening for a repo that has a remote. Never
    raises."""
    try:
        gitdir = repo / ".git"
        if not gitdir.is_dir():
            # `.git` is a pointer file (worktree/submodule) or absent — don't
            # guess the shared config; keep the freshness demand.
            return True
        text = (gitdir / "config").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return True
    return bool(re.search(r"(?m)^[ \t]*\[remote[ \t]", text))


def _has_consulted_git_rules(session: str) -> bool:
    needle = GIT_RULES_NOTE.lower()
    for consult in consults(session):
        if consult.get("failed"):
            # The read errored (not-found, locked vault…): the agent has read
            # zero rules, so crediting it enforces nothing (#358).
            continue
        target = str(consult.get("target") or "")
        if needle not in target.lower():
            continue
        # Only an actual READ of the note counts (#392): a search-vault query,
        # backlinks or graph-neighbors naming it returns none of its rules.
        # Records without a tool (internal callers) fall back to their kind.
        tool = str(consult.get("tool") or "")
        if not (is_note_read(tool, target) if tool else consult.get("kind") == "read"):
            continue
        # A truncated read of the demanded note is not a consult of it — the
        # overriding exceptions live below the fold (#239). The marker in the
        # tool result names the exact re-read that clears this.
        return incomplete_consult(session) != needle
    return False


# A ``git commit`` at command position, tolerating the ``-C <dir>`` / ``-c k=v``
# global opts before the verb (so ``git -C <repo> commit`` still matches).
_GIT_COMMIT_RE = re.compile(rf"(?:^|[;&|\n(]\s*)git[ \t]+{_GIT_GLOBAL_OPTS}commit\b")


def _is_commit_action(action: dict[str, Any]) -> bool:
    """True only when a Bash command runs ``git commit``. Freshness is demanded
    ONLY here: a commit is the moment work is recorded onto the local base, so a
    stale base is what gets committed if it was never refreshed. Edits, tests,
    reads, and even pushes do NOT trip the freshness gate (a push of an
    already-fresh-based commit is not stale-prone). Those still require the
    git-rules consult via :func:`_is_repo_sensitive_action` — only the freshness
    demand is narrowed to commits."""
    if str(action.get("tool") or "") != "Bash":
        return False
    # policy.shell_code_text (#317): a `git commit` inside an ssh payload, a
    # heredoc body, or a string literal is not a commit onto THIS machine's
    # base, so demanding a local fetch for it certifies nothing. A `bash -c` /
    # `eval` body is code this shell runs, so it is searched too (#434).
    return any(
        _GIT_COMMIT_RE.search(code)
        for text in _local_code_texts(str(action.get("command") or ""))
        for code in _stage_code_texts(text)
    )


def _stage_code_texts(raw: str) -> list[str]:
    """``raw`` as code (``policy.shell_code_text``), plus each simple command
    in it rebuilt from its program on: leading ``VAR=x``, wrappers
    (``chronic``, ``stdbuf -oL``, ``nice -n 5`` …) and keywords peeled, and
    the program reduced to its basename (``.venv/bin/python3`` → ``python3``).
    The command-anchored patterns then see ``git``/``gh``/``pytest`` behind a
    wrapper too (#434 review). Just the code when the split fails."""
    code = policy.shell_code_text(raw)
    stages = _safe_program_stages(code, raw)
    return [code, *(" ".join([program, *args]) for program, args, _s, _e in stages)]


def _safe_program_stages(code: str, raw: str) -> list[tuple[str, list[str], int, int]]:
    """:func:`_program_stages`, or no stages at all when the split fails:
    classification fails open (AGENTS.md invariant 2)."""
    try:
        return _program_stages(code, raw)
    except Exception:
        return []


#: A python interpreter's basename: ``python``, ``python3``, ``python3.12``.
_PYTHON_PROGRAM_RE = re.compile(r"python[\d.]*")
#: The modules whose ``-m`` run is a test run (#434 review).
_PYTHON_TEST_MODULES = frozenset({"pytest", "unittest", "tox", "nox"})
#: Interpreter switches whose value is the rest of the cluster or the next word.
_PYTHON_VALUE_SWITCHES = frozenset("XW")
_PYTHON_LONG_VALUE_SWITCHES = frozenset({"--check-hash-based-pycs"})


def _python_runs_tests(args: list[str]) -> bool:
    """Whether a python interpreter given ``args`` runs tests or a script file
    (#434 review): ``-m pytest|unittest|tox|nox`` (flags may come first, and
    ``-mpytest`` is one word), or a first operand that is a path
    (``tests/test_x.py``, ``run.py``). ``-c``, any other ``-m`` module, ``-``
    and stdin (a redirect or heredoc) are not: what such code does is the
    script-write and commit detectors' business. One rule for every spelling,
    ``python`` and ``python3`` alike."""
    i = 0
    while i < len(args):
        tok = args[i]
        i += 1
        if _REDIRECT_RE.match(tok):
            # `<`, `2>`: the target is the next word; `<in`, `<<'EOF'` carry it.
            if _REDIRECT_OP_RE.fullmatch(tok):
                i += 1
            continue
        if tok == "-":
            return False
        if tok == "--":
            continue
        if tok.startswith("--"):
            if tok in _PYTHON_LONG_VALUE_SWITCHES:
                i += 1
            continue
        if tok.startswith("-"):
            for pos, ch in enumerate(tok[1:], start=1):
                rest = tok[pos + 1 :]
                if ch == "c":
                    return False
                if ch == "m":
                    module = rest or (args[i] if i < len(args) else "")
                    return module in _PYTHON_TEST_MODULES
                if ch in _PYTHON_VALUE_SWITCHES:
                    if not rest:
                        i += 1
                    break
            continue
        return True
    return False


def _runs_test_runner(code: str, stages: list[tuple[str, list[str], int, int]]) -> bool:
    """Whether ``code`` runs a test runner: a :data:`_REPO_TEST_RE` word at
    command position, or a python stage that :func:`_python_runs_tests`. A
    run wrapper's word (``uv run python3 -c …``, ``hatch run python …``) yields
    to the python rule for the interpreter it runs, so every python spelling is
    judged alike (#434 review)."""
    for m in _REPO_TEST_RE.finditer(code):
        if m.group("prog") in _RUN_WRAPPERS:
            nxt = next((s for s in stages if s[2] >= m.end()), None)
            if (
                nxt is not None
                and _PYTHON_PROGRAM_RE.fullmatch(nxt[0])
                and not re.search(r"[;&|\n()`]", code[m.end() : nxt[2]])
            ):
                continue
        return True
    return any(
        _PYTHON_PROGRAM_RE.fullmatch(program) and _python_runs_tests(args)
        for program, args, _s, _e in stages
    )


def _local_code_texts(command: str) -> list[str]:
    """``command`` plus every ``sh/bash/zsh -c`` and ``eval`` body the shell
    walk unwraps from it, at any depth (#434): each is code this machine runs,
    so each is classified on its own. Just ``[command]`` when the walk fails.

    Also what a local shell runs that the walk does not unwrap, found by the
    hard rules' :func:`_local_shell_subjects` (#449): the words a positional
    body runs (``bash -c '"$@"' _ git commit``), the producer text and heredoc
    piped into a shell reading stdin (``printf 'git commit' | sh``, ``cat
    <<EOF | bash``), and a here-string given to one. When that search fails,
    the command and its bodies are still classified."""
    try:
        texts = [command, *_shell_walk(command)[3]]
    except Exception:
        return [command]
    try:
        code: list[str] = []
        words: list[str] = []
        for text in texts:
            _local_shell_subjects(text, code, words)
    except Exception:
        return texts
    return [*texts, *code, *words]


def _is_repo_sensitive_action(action: dict[str, Any]) -> bool:
    """Whether ``action`` is repo work. A Bash file write counts when its
    target lies in any repo, each target resolving its own (#448)."""
    tool = str(action.get("tool") or "")
    # Classify LOCAL repo work against code this shell actually runs (#317).
    # `ssh host 'cd /p && git commit …'` supplied the separator from inside its
    # payload, so a remote commit was judged local — and the repo was then
    # resolved from the local cwd, making the freshness fetch it demanded
    # vacuous: it refreshed an unrelated repo and recorded a false attestation.
    raw = str(action.get("command") or "")
    path = _action_path(action)
    if tool in _WRITE_TOOLS or tool in _READ_REVIEW_TOOLS:
        return True
    if tool == "Bash":
        if _is_readonly_git_command(policy.shell_code_text(raw)):
            return False
        # A `bash -c '…'` / `eval '…'` body is blanked as a string literal in
        # its caller, but this shell runs it: classify each body like the
        # command itself (#434).
        if any(_runs_repo_work(text) for text in _local_code_texts(raw)):
            return True
        if _writes_into_repo(action):
            return True
    return bool(path)


def _runs_repo_work(raw: str) -> bool:
    """Whether one piece of shell code runs a git write verb, a ``gh``
    pr/release/repo command, a test runner, an in-place editor, or a script
    that writes files. Quoted literals, heredoc bodies and ssh payloads are
    blanked first (#317)."""
    command = policy.shell_code_text(raw)
    if _is_readonly_git_command(command):
        return False
    stages = _safe_program_stages(command, raw)
    # Each simple command is also matched from its program on, so a wrapper
    # (`chronic git commit`, `nice -n 5 python3 -m pytest`) hides nothing
    # (#434 review).
    peeled = [" ".join([program, *args]) for program, args, _s, _e in stages]
    if _runs_test_runner(command, stages):
        return True
    for code in (command, *peeled):
        # Tolerate the ``-C <dir>``/``-c k=v`` global opts before the verb —
        # without this, ``git -C <repo> commit`` was never classified as repo
        # work at all and sailed past the rules-note + freshness checks (#147).
        if re.search(
            rf"(?:^|[;&|\n(]\s*)git[ \t]+{_GIT_GLOBAL_OPTS}"
            r"(?:add|commit|push|merge|rebase|checkout|switch)\b",
            code,
        ):
            return True
        if re.search(r"(?:^|[;&|\n(]\s*)gh\s+(?:pr|release|repo)\b", code):
            return True
    # A wrapped test runner (`nice -n 5 pytest`): python stages were judged above.
    if any(_REPO_TEST_RE.search(code) for code in peeled):
        return True
    return _runs_in_place_edit(stages) or _runs_script_write(stages, raw)


#: An output redirection operator in shell code: ``>``, ``>>``, ``>|``,
#: ``N>``, ``&>``, ``&>>``. The second ``>`` of ``>>`` and the ``>`` of
#: ``<>`` (read-write, never a truncating write) are not matched again, nor
#: is an escaped ``\>`` (``[ a \> b ]`` compares strings; #434 review).
_OUT_REDIRECT_RE = re.compile(r"(?<![<>&\d\\])(?:\d+|&)?>>?\|?")
#: One shell word: unquoted and quoted runs, ending at a blank or operator.
_SHELL_WORD_RE = re.compile(r"""(?:\\.|[^\s"'\\;&|<>()`]|"[^"]*"|'[^']*')+""")
#: A run of blanks, possibly empty: always matches.
_BLANK_RUN_RE = re.compile(r"[ \t]*")


def _skip_blanks(text: str, at: int) -> int:
    """The offset of the first non-blank (space/tab) at or after ``at``."""
    blanks = _BLANK_RUN_RE.match(text, at)
    return at if blanks is None else blanks.end()


#: File operations whose operands are written or removed (#434, #450):
#: ``tee``, ``rm``, ``truncate`` and ``touch`` write/remove every operand,
#: ``mv`` removes its sources and writes its destination, ``cp``, ``install``
#: and ``ln`` write only their destination (``install -d`` every operand),
#: ``dd`` writes only its ``of=`` operand, and ``rsync`` writes its last
#: operand when that is a local path.
_FILE_OPS = frozenset(
    {"tee", "cp", "mv", "rm", "install", "dd", "truncate", "touch", "ln", "rsync"}
)
#: Per program, the switches that take a SEPARATE value: short option letters
#: (also valid at the end of a cluster, ``-dm755`` / ``-m 755``) and long
#: options (``--opt=value`` is one word whatever the table says). GNU
#: spellings; ``-t``/``--target-directory`` is read as the destination. A long
#: option may be any unambiguous prefix of its name (:func:`_long_option`).
_FILE_OP_VALUE_SWITCHES: dict[str, tuple[str, frozenset[str]]] = {
    "cp": ("tS", frozenset({"--target-directory", "--suffix"})),
    "mv": ("tS", frozenset({"--target-directory", "--suffix"})),
    "install": (
        "gmotS",
        frozenset({"--group", "--mode", "--owner", "--target-directory", "--suffix"}),
    ),
    "ln": ("tS", frozenset({"--target-directory", "--suffix"})),
    "truncate": ("sr", frozenset({"--size", "--reference"})),
    "touch": ("dtr", frozenset({"--date", "--reference", "--time"})),
    "rsync": (
        "efBTM@",
        frozenset(
            {
                "--rsh", "--rsync-path", "--filter", "--exclude", "--include",
                "--exclude-from", "--include-from", "--files-from", "--temp-dir",
                "--compare-dest", "--copy-dest", "--link-dest", "--backup-dir",
                "--suffix", "--chmod", "--chown", "--usermap", "--groupmap",
                "--timeout", "--contimeout", "--port", "--sockopts", "--log-file",
                "--log-file-format", "--out-format", "--password-file", "--bwlimit",
                "--max-size", "--min-size", "--max-delete", "--partial-dir",
                "--block-size", "--compress-choice", "--compress-level",
                "--checksum-choice", "--skip-compress", "--modify-window",
                "--remote-option", "--iconv", "--info", "--debug", "--write-batch",
                "--only-write-batch", "--read-batch", "--protocol", "--checksum-seed",
                "--address", "--stop-after", "--stop-at", "--outbuf",
                "--max-alloc", "--early-input", "--copy-as",
            }
        ),
    ),
}
#: Per program, the long FLAGS (no value) the classifier must recognise by
#: prefix: the ones it acts on (``install --directory``, ``rsync
#: --remove-source-files``), and the ones whose full name is a prefix of a
#: value option above, so an exact ``--partial`` is never read as an
#: abbreviated ``--partial-dir`` that swallows the next word (#450 review).
_FILE_OP_LONG_FLAGS: dict[str, frozenset[str]] = {
    "install": frozenset({"--directory"}),
    "rsync": frozenset({"--remove-source-files", "--partial", "--backup", "--group"}),
}
#: Programs whose ``-t DIR`` / ``--target-directory`` names the destination
#: (``touch -t`` is a timestamp, not a directory).
_TARGET_DIR_OPS = frozenset({"cp", "mv", "install", "ln"})
#: rsync options whose value is a path rsync writes on the RECEIVING side
#: (#450 review): backups, temp files and partial transfers. A relative one is
#: really resolved against the destination; reading it from the cwd can only
#: over-gate, the safe direction.
_RSYNC_RECEIVER_WRITES = frozenset({"--backup-dir", "--temp-dir", "--partial-dir"})
#: rsync options whose value is a path written LOCALLY whatever the direction.
_RSYNC_LOCAL_WRITES = frozenset({"--log-file"})
#: ``find -exec`` placeholders and terminators: never a file operand (#434
#: review). The walk leaves a lone ``\`` where ``\;`` ended the site.
_EXEC_PLACEHOLDERS = frozenset({"{}", "+", ";", "\\;", "\\"})


def _redirect_targets(text: str) -> list[str]:
    """The files one simple command's output redirections write, as raw shell
    words. A descriptor duplication (``2>&1``, ``>&2``, ``>&-``) and a
    process substitution (``>(cmd)``) name no file; a quoted ``>`` is data.
    ``>&word`` / ``>& word`` with a non-numeric word writes that file."""
    code = policy.shell_code_text(text)
    targets: list[str] = []
    if ">" not in code:
        return targets  # every output redirection has one (#445)
    for m in _OUT_REDIRECT_RE.finditer(code):
        # Read on from the operator by offset: slicing off the rest of the
        # text per redirection was quadratic in a long run of them (#445).
        at = _skip_blanks(text, m.end())
        if text.startswith("&", at):
            at = _skip_blanks(text, at + 1)
            if text[at : at + 1].isdigit() or text.startswith("-", at):
                continue
        if text.startswith("(", at):
            continue
        word = _SHELL_WORD_RE.match(text, at)
        if word is not None:
            try:
                targets.append("".join(_shell_tokens(word.group())))
            except ValueError:
                continue
    return targets


def _file_op_targets(program: str, text: str) -> list[str]:
    """The operands a :data:`_FILE_OPS` site writes or removes (#434, #450)."""
    # A `find -exec … \;` site ends at the `\` of its terminator; a lone
    # trailing backslash would make shlex reject the whole site.
    text = text.rstrip()
    if text.endswith("\\") and not text.endswith("\\\\"):
        text = text[:-1]
    try:
        words = _without_redirects(_shell_tokens(text))[1:]
    except ValueError:
        return []
    short_values, long_values = _FILE_OP_VALUE_SWITCHES.get(program, ("", frozenset()))
    long_known = long_values | _FILE_OP_LONG_FLAGS.get(program, frozenset())
    operands: list[str] = []
    target_dir: list[str] = []
    #: Option name -> its values, for the options whose value is written.
    values: dict[str, list[str]] = {}
    flags: set[str] = set()
    i, options = 0, True
    while i < len(words):
        word = words[i]
        i += 1
        if options and word == "--":
            options = False
        elif options and word.startswith("--"):
            given, eq, value = word.partition("=")
            name = _long_option(given, long_known)
            if name in long_values and not eq:
                # A value switch at the very end has no value: nothing to add.
                value = words[i] if i < len(words) else ""
                i += 1
            if value:
                values.setdefault(name, []).append(value)
            flags.add(name)
        elif options and word.startswith("-") and len(word) > 1:
            for j, letter in enumerate(word[1:], start=1):
                if letter not in short_values:
                    flags.add(letter)
                    continue
                value = word[j + 1 :]
                if not value:
                    value = words[i] if i < len(words) else ""
                    i += 1
                if value:
                    values.setdefault("-" + letter, []).append(value)
                break
        elif word not in _EXEC_PLACEHOLDERS:
            operands.append(word)
    if program in _TARGET_DIR_OPS:
        target_dir = values.get("-t", []) + values.get("--target-directory", [])
    if program == "dd":
        return [w[3:] for w in operands if w.startswith("of=")]
    if program == "install":
        if "d" in flags or "--directory" in flags:
            return operands
        return target_dir or operands[-1:]
    if program == "ln":
        if target_dir:
            return target_dir
        if len(operands) == 1:  # `ln TARGET` links into the cwd
            return [re.split(r"[/\\]", operands[0].rstrip("/\\"))[-1]]
        return operands[-1:]
    if program == "cp":
        return target_dir or operands[-1:]
    if program == "rsync":
        return _rsync_targets(operands, values, flags)
    return target_dir + operands


def _rsync_targets(operands: list[str], values: dict[str, list[str]], flags: set[str]) -> list[str]:
    """The local paths an ``rsync`` writes or removes (#450): its destination
    when that is local, with the receiver-side ``--backup-dir``/``--temp-dir``/
    ``--partial-dir``; its local sources under ``--remove-source-files``; and
    a ``--log-file`` always."""
    targets = [v for name in _RSYNC_LOCAL_WRITES for v in values.get(name, [])]
    if len(operands) < 2:
        return targets
    *sources, dest = operands
    if not _rsync_remote(dest):
        targets.append(dest)
        targets += values.get("-T", [])
        targets += [v for name in _RSYNC_RECEIVER_WRITES for v in values.get(name, [])]
    if "--remove-source-files" in flags:
        targets += [s for s in sources if not _rsync_remote(s)]
    return targets


def _long_option(given: str, known: frozenset[str]) -> str:
    """The long option ``given`` names among ``known``: itself on an exact
    match, else the one option it is an unambiguous prefix of, as GNU getopt
    accepts (``--target`` for ``--target-directory``, #450 review). An unknown
    or ambiguous ``given`` comes back as is: a flag the classifier ignores."""
    if given in known or len(given) < 3:
        return given
    matches = [name for name in known if name.startswith(given)]
    return matches[0] if len(matches) == 1 else given


def _rsync_remote(word: str) -> bool:
    """Whether an rsync operand names a remote path: ``rsync://…``, or a colon
    before any slash (``host:path``, ``user@host::module``). A Windows drive
    (``C:/x``, ``C:\\x``) is local."""
    if word.startswith("rsync://"):
        return True
    if re.match(r"[A-Za-z]:[/\\]", word):
        return False
    colon = word.find(":")
    return colon > 0 and "/" not in word[:colon]


def _write_target(word: str, cwd: Path | None) -> Path | None:
    """The resolved path the literal word ``word`` names, used from ``cwd``
    (``None``: not knowable, so only an absolute path is judged). A word with
    a parameter or command substitution is not a literal and names nothing,
    nor does a null device (``/dev/null``, Windows ``NUL``)."""
    if not word or "$" in word or "`" in word:
        return None
    if word.startswith("/dev/") or word.lower() in ("nul", "nul:"):
        return None
    try:
        target = Path(word).expanduser()
        if not target.is_absolute():
            if cwd is None:
                return None
            target = cwd / target
        return target.resolve()
    except (OSError, RuntimeError, ValueError):
        return None


#: A ``[[ … ]]`` test or a ``(( … ))`` / ``$(( … ))`` arithmetic expression on
#: one line with no quote in it: a ``>`` there compares, it does not redirect.
_COMPARE_EXPR_RE = re.compile(r"""\[\[[^'"\n]*?\]\]|\(\([^'"\n]*?\)\)""")


def _without_comparisons(command: str) -> str:
    """``command`` with every ``>`` inside a ``[[ … ]]`` test or a ``(( … ))``
    arithmetic expression blanked, so the walk never reads ``[[ a > b ]]`` or
    ``(( n > 3 ))`` as a redirect to ``b``/``3`` (#434 review). A region that
    holds a quote is left alone: ``echo '((' > f; echo '))'`` stays a write."""
    return _COMPARE_EXPR_RE.sub(lambda m: m.group().replace(">", " "), command)


def _writes_into_repo(action: dict[str, Any]) -> bool:
    """Whether a Bash command writes a file inside a repo through an output
    redirection, ``tee``, ``cp``, ``mv``, ``install``, ``dd of=``,
    ``truncate``, ``touch``, ``ln``, a local ``rsync`` destination or an
    in-place ``sed``/``perl``/``ruby``, or removes one with ``rm`` or ``mv``
    (#434, #450, #448). See :func:`_bash_write_repo`. ``False`` on any
    failure: classification fails open."""
    return _bash_write_repo(action) is not None


def _bash_write_repo(action: dict[str, Any]) -> Path | None:
    """The repo a Bash command writes into (#434, #450, #448), or ``None``.
    Every simple command the local shell runs, ``-c``/``eval`` bodies
    included, is judged from the directory it runs in, and each write target
    resolves its OWN enclosing repo, as the Write tool's path does: the repo
    the command's ``cd``/``git -C`` lead to says nothing about where a
    redirect earlier in the line landed (#448). A target in no repo
    (``>/dev/null``, ``2>&1``, ``> /tmp/log``) is not repo work. ``None`` on
    any failure: classification fails open."""
    try:
        base = _action_base_dir(action)
        memo: dict[Path, Path | None] = {}
        for site in _shell_walk(_without_comparisons(_action_command(action)))[0]:
            cwd = None if site.cwd is None else base / site.cwd
            targets = _redirect_targets(site.text)
            if site.program in _FILE_OPS:
                targets += _file_op_targets(site.program, site.text)
            elif site.program in _EDITOR_SWITCHES:
                targets += _in_place_edit_targets(site.program, site.text)
            for word in targets:
                target = _write_target(word, cwd)
                found = None if target is None else _dir_repo(target, memo)
                if found is not None:
                    return found
    except Exception:
        return None
    return None


def _in_place_edit_targets(program: str, text: str) -> list[str]:
    """The files one in-place ``sed``/``perl``/``ruby`` site rewrites, as raw
    shell words (#448): the operands :func:`_in_place_edit_operands` finds
    after the script. ``[]`` when it edits nothing in place or won't parse."""
    # A `find -exec … \;` site ends at the `\` of its terminator.
    text = text.rstrip()
    if text.endswith("\\") and not text.endswith("\\\\"):
        text = text[:-1]
    try:
        words = _without_redirects(_shell_tokens(text))[1:]
    except ValueError:
        return []
    # A quoted `find -exec` terminator (`';'`, or `+` after `{}`) ends the
    # editor's words: find's own expression follows (`-iname` is not `-i`).
    for i, word in enumerate(words):
        if word == ";" or (word == "+" and i and words[i - 1] == "{}"):
            words = words[:i]
            break
    operands = _in_place_edit_operands(program, words) or []
    return [word for word in operands if word not in _EXEC_PLACEHOLDERS]


#: Wrappers and keywords a stage skips to reach its program, with their
#: value-taking switches. The table lives in policy so the hard rules'
#: command-position anchor is derived from the same list (#430).
_STAGE_WRAPPERS = policy.STAGE_WRAPPERS
#: Wrappers that take one positional argument before the command (``timeout 5``).
_WRAPPER_POSITIONAL = policy.WRAPPER_POSITIONAL
#: Tools whose ``run`` subcommand execs the next word (``poetry run python …``,
#: #419). The value is every switch that takes a SEPARATE argument, before
#: ``run`` (``poetry -C sub run``, ``uv --directory . run``) or after it
#: (``uv run --extra dev``, ``conda run -n env``). A ``--opt=value`` word is
#: one word whatever the table says. The uv set is every value-taking option
#: ``uv run --help`` lists (uv 0.12); pipx's is ``pipx run --help``'s; the
#: others come from each tool's documented CLI.
_RUN_WRAPPERS: dict[str, frozenset[str]] = {
    "poetry": frozenset({"-C", "--directory", "-P", "--project"}),
    "pipx": frozenset(
        {
            "--spec",
            "--python",
            "--python-args",
            "--with",
            "--pip-args",
            "--index-url",
            "-i",
            "--fetch-python",
            "--cooldown",
            "--backend",
        }
    ),
    "uv": frozenset(
        {
            "--extra",
            "--no-extra",
            "--group",
            "--no-group",
            "--only-group",
            "--no-editable-package",
            "--env-file",
            "-w",
            "--with",
            "--with-editable",
            "--with-requirements",
            "--package",
            "--python-platform",
            "--index",
            "--default-index",
            "-i",
            "--index-url",
            "--extra-index-url",
            "-f",
            "--find-links",
            "--index-strategy",
            "--keyring-provider",
            "-P",
            "--upgrade-package",
            "--upgrade-group",
            "--resolution",
            "--prerelease",
            "--prerelease-package",
            "--fork-strategy",
            "--exclude-newer",
            "--exclude-newer-package",
            "--no-sources-package",
            "--reinstall-package",
            "--link-mode",
            "-C",
            "--config-setting",
            "--config-settings-package",
            "--no-build-isolation-package",
            "--no-build-package",
            "--no-binary-package",
            "--cache-dir",
            "--refresh-package",
            "-p",
            "--python",
            "--color",
            "--allow-insecure-host",
            "--directory",
            "--project",
            "--config-file",
        }
    ),
    "pipenv": frozenset({"--python", "--pypi-mirror"}),
    "pdm": frozenset({"-c", "--config", "-p", "--project", "--venv", "--skip"}),
    # `hatch run [ENV:]CMD`: the `ENV:` prefix is stripped from the program.
    "hatch": frozenset({"-e", "--env", "-p", "--project", "--data-dir", "--cache-dir", "--config"}),
    "conda": frozenset({"-n", "--name", "-p", "--prefix", "--cwd"}),
}
#: A quoted ``find -exec`` terminator (``';'``, ``";"``). ``shell_code_text``
#: blanks its body, so it is recognised in the raw text at the same offset.
_QUOTED_EXEC_END = ("';'", '";"')
#: ``find`` actions whose following words are a command of their own.
_EXEC_ACTIONS = frozenset({"-exec", "-execdir", "-ok", "-okdir"})
#: Per editor: (switches meaning in-place — BSD ``sed -I`` too; switches after
#: which the rest of a cluster is that switch's ARGUMENT, as in
#: ``perl -MList::Util`` / ``sed -fscript``; the subset that, as the LAST char
#: of a cluster, consume the NEXT word — in ``sed -e -i f`` that ``-i`` is the
#: script, not the flag).
_EDITOR_SWITCHES: dict[str, tuple[frozenset[str], frozenset[str], frozenset[str]]] = {
    "sed": (frozenset("iI"), frozenset("efl"), frozenset("efl")),
    "gsed": (frozenset("i"), frozenset("efl"), frozenset("efl")),
    "perl": (frozenset("i"), frozenset("dDeEIMmx"), frozenset("eE")),
    "ruby": (frozenset("i"), frozenset("CeEFIrx"), frozenset("CeEIr")),
}
_EDITOR_LONG_ARG = frozenset({"--expression", "--file", "--line-length"})
#: Per editor, the switches whose value IS the script, so that every
#: positional word is a file operand (#448).
_EDITOR_SCRIPT_SWITCHES: dict[str, frozenset[str]] = {
    "sed": frozenset("ef"),
    "gsed": frozenset("ef"),
    "perl": frozenset("eE"),
    "ruby": frozenset("e"),
}
_EDITOR_SCRIPT_LONG_ARG = frozenset({"--expression", "--file"})
_INTERPRETER_RE = re.compile(r"python[\d.]*|node|nodejs|ruby")
_WRITE_MODE = r"""\\?['"](?:[bt]*[wax][bt+]*|r[bt]*\+[bt]*)\\?['"]"""
#: A script writing or deleting files. Call syntax only, so the bare word in a
#: printed string or a grep pattern does not count.
_SCRIPT_WRITE_RE = re.compile(
    r"\b(?:write_text|write_bytes|writeFile|writeFileSync|appendFile|appendFileSync)\s*\("
    r"|\bFile\.write\s*\("
    r"|\.unlink(?:Sync)?\s*\(|\bos\.(?:remove|replace|renames?|rmdir)\s*\("
    r"|\bshutil\.(?:rmtree|move)\s*\("
    # rm/rmdir/rename count only on a file-system receiver: `df.rename(…)`
    # (pandas) and `db.rename('t')` are not file writes (#419 review).
    r"|\.(?:rm|rmdir|rename)Sync\s*\("
    r"|\b(?:fs(?:\.promises)?|fsPromises)\.(?:rm|rmdir|rename)\s*\("
    r"""|\brequire\s*\(\s*\\?['"](?:node:)?fs(?:/promises)?\\?['"]\s*\)"""
    r"(?:\.promises)?\.(?:rm|rmdir|rename)\s*\("
    r"|\bPath\s*\([^()\n]*\)\.(?:rename|rmdir)\s*\("
    r"|\b(?:File|FileUtils|Dir)\.(?:rename|rm|rmdir|mv|delete)\s*\("
    # open(p, 'w'|'a'|'x'|'r+'…), open(p, mode="wb"), Path(p).open("w")
    rf"|\bopen\s*\([^)\n]*,\s*(?:mode\s*=\s*)?{_WRITE_MODE}"
    rf"|\.open\s*\(\s*(?:mode\s*=\s*)?{_WRITE_MODE}"
)


#: A pathlib object held in a variable (``p = Path('a'); p.rename('b')``): its
#: ``.rename(``/``.rmdir(`` counts only in a script that uses pathlib, so a
#: pandas ``df.rename(…)`` elsewhere stays read-only (#419 review).
_PATHLIB_RE = re.compile(r"\bpathlib\b|\bPath\s*\(")
_PATHLIB_OP_RE = re.compile(r"\.(?:rename|rmdir)\s*\(")


def _switch_width(word: str, takes_arg: frozenset[str]) -> int:
    """How many words the wrapper switch ``word`` spans (1 or 2), given the
    switches in ``takes_arg`` that take a value. A short cluster reads like
    getopt (#432 review): the first value-taking letter takes the rest of the
    word, or the next word when it ends the cluster (``env -iC <dir>``,
    ``sudo -iu bob``), so that next word is never read as the program."""
    if word in takes_arg:
        return 2
    if word.startswith("--") or len(word) < 3:
        return 1
    for k in range(1, len(word)):
        if "-" + word[k] in takes_arg:
            return 2 if k == len(word) - 1 else 1
    return 1


def _basename(word: str) -> str:
    base = re.split(r"[/\\]", word)[-1]
    return base[:-4] if base.lower().endswith(".exe") else base


def _program_stages(code: str, raw: str = "") -> list[tuple[str, list[str], int, int]]:
    """Split shell ``code`` into simple commands: ``(program, args, start, end)``.
    See :func:`_program_stages_cached`; each call gets its own ``args`` lists."""
    return [
        (program, list(args), start, end)
        for program, args, start, end in _program_stages_cached(code, raw)
    ]


#: A leading ``VAR=value`` word.
_ASSIGNMENT_WORD_RE = re.compile(r"[A-Za-z_]\w*=")


def _word_offset(text: str, words: list[str], offsets: list[int], k: int) -> int:
    """Where ``words[k]`` (``text.split()``) starts in ``text``. ``offsets``
    caches the words located so far, so a segment's words are found once each
    and only up to the last one a stage asks for (#445)."""
    while len(offsets) <= k:
        last = len(offsets) - 1
        prev = offsets[last] + len(words[last]) if offsets else 0
        offsets.append(text.find(words[last + 1], prev))
    return offsets[k]


@functools.lru_cache(maxsize=16)
def _program_stages_cached(
    code: str, raw: str = ""
) -> tuple[tuple[str, tuple[str, ...], int, int], ...]:
    """Split shell ``code`` into simple commands: ``(program, args, start, end)``.
    Memoised: the walk, the hard rules and the repo-work classifier split the
    same command (#445).

    ``code`` is ``policy.shell_code_text`` output — the same length as the raw
    command, so ``start:end`` slices the raw text too. Backslash-newline
    continuations are joined; stages break at unescaped ``; & | ( ) `` ` `` and
    newlines (``)`` ends a ``case`` arm's pattern), and again at a ``find
    -exec``, whose command runs to its ``\\;`` or ``{} +``. Leading ``VAR=x``
    assignments, wrappers (``poetry run`` too) and keywords are skipped, so
    ``program`` is the basename (``.exe`` stripped) of what actually runs.
    ``end`` extends over trailing blanked text: the heredoc body that stage
    owns. ``raw`` (the unmasked command, same length) lets a quoted ``';'``
    terminator end an ``-exec`` too; ``shell_code_text`` blanked its ``;``."""
    code = code.replace("\\\n", "  ")
    n = len(code)
    # A raw text of another length cannot be read at the same offsets: ignore it.
    raw = raw.replace("\\\n", "  ") if len(raw) == n else ""
    stages: list[tuple[str, tuple[str, ...], int, int]] = []
    # An escaped `\;` (find's -exec terminator) is a word, not a separator (#419).
    for seg in _SEGMENT_RE.finditer(code):
        end = seg.end()
        while end < n and code[end].isspace():
            end += 1
        # Words by `str.split` (the same Unicode blanks as `\S+`), offsets
        # only where a stage needs one, and the next `-exec` by bisection, so
        # one long segment is not walked word by word in Python (#445).
        text = seg.group()
        base_at = seg.start()
        words = text.split()
        offsets: list[int] = []
        ntok = len(words)
        execs = (
            []
            if _EXEC_ACTIONS.isdisjoint(words)
            else [k for k, w in enumerate(words) if w in _EXEC_ACTIONS]
        )
        execs.append(ntok)
        in_exec = False
        i = 0
        while i < ntok:
            j = i
            env_prefix = False
            while j < ntok:
                word = words[j]
                if _ASSIGNMENT_WORD_RE.match(word):
                    j += 1
                    continue
                base = _basename(word)
                if base in _RUN_WRAPPERS:
                    # The tool's global options may sit before `run`
                    # (`poetry -C sub run`, `uv --directory . run`).
                    k = j + 1
                    while k < ntok and words[k].startswith("-"):
                        k += 2 if words[k] in _RUN_WRAPPERS[base] else 1
                    if k < ntok and words[k] == "run":
                        j = k + 1
                        while j < ntok and words[j].startswith("-"):
                            j += 2 if words[j] in _RUN_WRAPPERS[base] else 1
                        env_prefix = base == "hatch"
                        continue
                    break
                if base not in _STAGE_WRAPPERS:
                    break
                j += 1
                while j < ntok and words[j].startswith("-"):
                    j += _switch_width(words[j], _STAGE_WRAPPERS[base])
                if base in _WRAPPER_POSITIONAL and j < ntok:
                    # Switches and `--` may follow the duration too
                    # (`timeout 5s -k 2s cmd`, `timeout 5 -- cmd`; #430 review).
                    j += 1
                    while j < ntok and words[j].startswith("-"):
                        j += _switch_width(words[j], _STAGE_WRAPPERS[base])
            if j >= ntok:
                break
            # The stage runs to the next find action at most.
            limit = execs[bisect.bisect_left(execs, j + 1)]
            cut = j + 1
            while in_exec and cut < limit:
                w = words[cut]
                if (
                    w == "\\;"
                    or (w == "+" and words[cut - 1] == "{}")
                    or (
                        raw
                        and raw.startswith(
                            _QUOTED_EXEC_END, base_at + _word_offset(text, words, offsets, cut)
                        )
                    )
                ):
                    break
                cut += 1
            if not in_exec:
                cut = limit
            program = words[j].split(":", 1)[-1] if env_prefix else words[j]
            start = base_at + _word_offset(text, words, offsets, j)
            stages.append((_basename(program), tuple(words[j + 1 : cut]), start, end))
            # An -exec command ended at its `\;`/`+`: what follows is find's own
            # expression up to its next -exec (`-iname` there is not sed's `-i`).
            cut = execs[bisect.bisect_left(execs, cut)]
            i = cut + 1
            in_exec = True
    return tuple(stages)


def _runs_in_place_edit(stages: list[tuple[str, list[str], int, int]]) -> bool:
    """True when a ``sed``/``perl``/``ruby`` invocation carries ITS OWN in-place
    flag (``-i``, ``-i ''``, ``-i.bak``, ``-pi``, ``-Ei``, BSD ``sed -I``,
    ``--in-place[=SUF]``).

    ``stages`` come from :func:`_program_stages` over ``shell_code_text``
    output, so quoted scripts are already blank. Each simple command is read on
    its own: a ``-i`` belonging to another program in the same command
    (``grep -iE``, ``ls -i``) used to satisfy a bare ``" -i" in command`` test
    and turned read-only listings into repo work (#391)."""
    return any(
        _in_place_edit_operands(program, args) is not None for program, args, _start, _end in stages
    )


def _in_place_edit_operands(program: str, args: list[str]) -> list[str] | None:
    """The file operands of one editor invocation that carries its own
    in-place flag, else ``None`` (#391, #448). They are the positional words
    after the script, or all of them when ``-e``/``-f`` (``--expression``,
    ``--file``) supplied it. BSD ``sed -i ''`` takes the empty word as its
    backup suffix, not as the script."""
    switches = _EDITOR_SWITCHES.get(program)
    if switches is None:
        return None
    inplace_chars, stops, takes_next = switches
    script_chars = _EDITOR_SCRIPT_SWITCHES.get(program, frozenset())
    inplace = script_given = options_done = False
    skip = suffix_next = False
    positional: list[str] = []
    for tok in args:
        if skip or (suffix_next and tok == ""):
            skip = suffix_next = False
            continue
        suffix_next = False
        if options_done or tok == "-" or not tok.startswith("-"):
            positional.append(tok)
        elif tok == "--":
            options_done = True
        elif tok == "--in-place" or tok.startswith("--in-place="):
            inplace = True
        elif tok.startswith("--"):
            script_given = script_given or tok.partition("=")[0] in _EDITOR_SCRIPT_LONG_ARG
            skip = tok in _EDITOR_LONG_ARG
        else:
            for pos, ch in enumerate(tok[1:], start=1):
                if ch in inplace_chars:
                    # The rest of the cluster is the backup suffix (`-i.bak`).
                    inplace = True
                    suffix_next = pos == len(tok) - 1 and program in ("sed", "gsed")
                    break
                if ch in stops:
                    script_given = script_given or ch in script_chars
                    skip = pos == len(tok) - 1 and ch in takes_next
                    break
    if not inplace:
        return None
    return positional if script_given else positional[1:]


def _runs_script_write(stages: list[tuple[str, list[str], int, int]], raw: str) -> bool:
    """True when a python/node/ruby stage's OWN text — its ``-c``/``-e``
    payload or heredoc body, which ``shell_code_text`` blanked — calls a
    file-writing or file-deleting API. Only the raw slice of the interpreter's
    stage is searched, so ``grep "write_text" src | python3 -m json.tool`` is
    not a script write (#391)."""
    return any(
        _INTERPRETER_RE.fullmatch(program)
        and (
            _SCRIPT_WRITE_RE.search(raw[start:end])
            or (_PATHLIB_RE.search(raw[start:end]) and _PATHLIB_OP_RE.search(raw[start:end]))
        )
        for program, _args, start, end in stages
    )


def _is_global_config_mutation(action: dict[str, Any]) -> bool:
    tool = str(action.get("tool") or "")
    command = str(action.get("command") or "")
    if tool in _WRITE_TOOLS:
        # A write tool targets exactly one file — resolve it and gate only a
        # GLOBAL (home-anchored) config, not a project-local <repo>/.claude/….
        return _is_global_config_path(_action_path(action))
    if tool != "Bash":
        return False
    # Bash: require a home-anchored global-config path in the command AND a
    # mutating verb or a real file redirect (a plain read is not a mutation).
    if not _command_targets_global_config(command):
        return False
    return bool(_GLOBAL_MUTATING_BASH_RE.search(command) or _FILE_REDIRECT_RE.search(command))


def _turn_authorization_text(action: dict[str, Any], session: str) -> str:
    parts = []
    for key in ("prompt", "user_prompt", "current_prompt", "turn_prompt"):
        value = action.get(key)
        if isinstance(value, str) and value.strip():
            parts.append(value.strip())
    task = turn_task(session)
    if task:
        parts.append(task)
    return "\n".join(parts)


#: How much of the transcript's tail to scan for mid-turn messages. A turn's
#: recent history is what matters; a long session's transcript runs to many MiB
#: and this sits on the PreToolUse path (only reached when a block would fire).
_MIDTURN_TAIL_BYTES = 1024 * 1024


def _is_turn_opener(entry: dict[str, Any]) -> bool:
    """A transcript ``user`` entry that OPENS a turn — typed text, not the
    tool_result envelope the harness also files under ``type: user``."""
    if entry.get("type") != "user" or entry.get("isSidechain") or entry.get("isMeta"):
        return False
    message = entry.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        return bool(content.strip())
    if isinstance(content, list):
        kinds = {b.get("type") for b in content if isinstance(b, dict)}
        return "tool_result" not in kinds and "text" in kinds
    return False


def midturn_user_messages(transcript_path: object) -> list[str]:
    """What the HUMAN typed while the current turn was already running (#290).

    Claude Code delivers such a message alongside a tool result, as a
    ``queued_command`` attachment — no new turn, no ``UserPromptSubmit``, so the
    turn-task file the authorization classifier reads never hears about it.

    Only ``commandMode == "prompt"`` with ``origin.kind == "human"`` counts. The
    same attachment type carries ``task-notification`` entries whose text is
    machine- and agent-authored; crediting one would let a subagent's output
    authorize its parent's side effects. An entry that does not positively say
    a human typed it is ignored — missing authorization is the safe failure.

    Oldest first; ``[]`` on any problem (no transcript, other harness, parse
    error). Never raises.
    """
    try:
        if not isinstance(transcript_path, str) or not transcript_path:
            return []
        path = Path(transcript_path).expanduser()
        with path.open("rb") as handle:
            size = handle.seek(0, os.SEEK_END)
            handle.seek(max(0, size - _MIDTURN_TAIL_BYTES))
            lines = handle.read().decode("utf-8", errors="replace").splitlines()
        if size > _MIDTURN_TAIL_BYTES and lines:
            lines = lines[1:]  # the seek landed mid-line
        found: list[str] = []
        for line in reversed(lines):
            if '"queued_command"' not in line and '"user"' not in line:
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if not isinstance(entry, dict):
                continue
            if _is_turn_opener(entry):
                break  # everything older belongs to an earlier turn
            attachment = entry.get("attachment")
            if entry.get("type") != "attachment" or not isinstance(attachment, dict):
                continue
            origin = attachment.get("origin")
            prompt = attachment.get("prompt")
            if (
                attachment.get("type") == "queued_command"
                and attachment.get("commandMode") == "prompt"
                and isinstance(origin, dict)
                and origin.get("kind") == "human"
                and isinstance(prompt, str)
                and prompt.strip()
            ):
                found.append(prompt.strip())
        found.reverse()
        return found
    except Exception:
        return []


def _midturn_authorizes(action: dict[str, Any]) -> bool:
    """True when the human's LATEST mid-turn message authorizes acting (#290).

    Only the latest one: "fix it" followed by "wait, don't push" must not leave
    the earlier go-ahead standing. It must carry positive authorization — a
    strong phrase, or a non-negated authorizing verb in a message that is not
    itself a capability question — so an aside ("hm, interesting") lifts
    nothing. This can only LIFT the two turn-level authorization blocks; the
    destructive hard rules never consult it."""
    messages = midturn_user_messages(action.get("transcript_path"))
    if not messages:
        return False
    latest = messages[-1]
    if _has_strong_action_auth(latest):
        return True
    return not _is_capability_question(latest) and _has_global_auth(latest)


def _has_strong_action_auth(text: str) -> bool:
    return bool(_STRONG_ACTION_AUTH_RE.search(text))


def _is_capability_question(text: str) -> bool:
    return bool(_CAPABILITY_QUESTION_RE.search(text))


def _has_global_auth(text: str) -> bool:
    """True when the turn text contains an authorizing verb that is NOT negated
    just before it — so "don't change anything" / "no need to update" do not read
    as authorization, while the expanded verb set ("fix"/"add"/"create"/...) does."""
    for m in _GLOBAL_AUTH_RE.finditer(text):
        if not _AUTH_NEGATION_RE.search(text[: m.start()]):
            return True
    return False


def _turn_has_explicit_global_auth(action: dict[str, Any], session: str) -> bool:
    text = _turn_authorization_text(action, session)
    if _is_capability_question(text):
        authorized = _has_strong_action_auth(text)
    else:
        authorized = _has_global_auth(text)
    # The transcript is read only when the opening message would block (#290).
    return authorized or _midturn_authorizes(action)


def _is_side_effect_action(action: dict[str, Any]) -> bool:
    """Does this action carry a consequence worth an explicit go-ahead?

    Deliberately NARROWER than "has any effect". This gate previously counted
    every Write/Edit, every `cp`/`mv`/`touch`/`tee`, and any `>` redirect as a
    side effect, so phrasing a request as "can you …" blocked ordinary work:
    `mkdir -p … && cp …` and a `sed -i` on a scratch file were both denied on a
    real machine. A gate that stops legitimate work teaches people to route
    around it, which costs more safety than it buys.

    What still requires explicit authorization is what a user cannot casually
    undo: reaching OUTSIDE this machine (opening a PR, pushing, cutting a
    release), restarting services, destroying data, changing permissions, or
    editing global agent config. Local, reversible edits are the work itself —
    they are covered by the destructive deny-set and the consult gate, not here.
    """
    if _is_global_config_mutation(action):
        return True
    command = str(action.get("command") or "")
    if str(action.get("tool") or "") == "Bash" or command:
        return bool(_RISKY_SIDE_EFFECT_RE.search(command))
    return False


def _is_unauthorized_capability_side_effect(action: dict[str, Any], session: str) -> bool:
    text = _turn_authorization_text(action, session)
    return (
        _is_capability_question(text)
        and not _has_strong_action_auth(text)
        and _is_side_effect_action(action)
        # Last, so the transcript is read only when a block would fire (#290).
        and not _midturn_authorizes(action)
    )


#: A here-string attached to its operator: ``<<<'sudo id'``, ``0<<<x``.
_HERE_STRING_RE = re.compile(r"\d*<<<(.*)", re.DOTALL)
#: A body that runs its positional words: ``"$@"``, ``$*``, ``$0``, ``${1}``.
_POSITIONAL_REF_RE = re.compile(r"\$\{?[@*0-9]")
#: A redirection operator at the start of a word (``<<<``, ``2>``, ``>``).
_LEADING_REDIRECT_RE = re.compile(r"^\d*&?[<>]+[&|]?")


def _shell_code_source(words: list[str]) -> tuple[str, list[str]]:
    """Where a local shell (``words[0]``) gets the code it runs, and any
    here-string it is given: ``("arg", [])`` for a ``-c`` body, ``("file",
    [])`` for a script operand or a ``<`` redirect (``bash x.sh 'arg'``: its
    arguments are data), else ``("stdin", here_strings)`` (``… | bash``,
    ``bash -s``, ``bash <<< '…'``).

    A redirection's target is not a script operand (``| bash > log``, ``|
    bash 2> err``), nor is a ``-o``/``-O`` value (``| bash -o pipefail``):
    switches are read the way :func:`_shell_body_index` reads them."""
    if _shell_body_index(words) is not None:
        return "arg", []
    here: list[str] = []
    reads_stdin = False
    i = 1
    while i < len(words):
        word = words[i]
        attached = _HERE_STRING_RE.fullmatch(word)
        if attached and attached.group(1):
            here.append(attached.group(1))
        elif _REDIRECT_OP_RE.fullmatch(word):
            if word.endswith("<<<"):
                here.extend(words[i + 1 : i + 2])
            elif re.fullmatch(r"\d*<", word):
                return "file", []  # `bash < x.sh` runs the file
            i += 2
            continue
        elif _REDIRECT_RE.match(word):
            if re.match(r"\d*<[^<&(]", word):
                return "file", []  # `bash <x.sh`
        elif word in ("-o", "+o", "-O", "+O"):
            i += 2
            continue
        elif reads_stdin:
            pass  # with `-s`, operands are positional words, not a script
        elif word in _STDIN_OPERANDS:
            reads_stdin = True  # `bash /dev/stdin`, `bash -` (#432 review)
        elif word == "--":
            if i + 1 < len(words) and words[i + 1] not in _STDIN_OPERANDS:
                return "file", []  # `bash -- x.sh`
        elif len(word) > 1 and word[0] in "-+":
            reads_stdin = _is_stdin_switch(word)
        else:
            return "file", []
        i += 1
    return "stdin", here


def _here_strings(words: list[str]) -> list[str]:
    """The here-strings among ``words`` (``<<< '…'``, ``<<<'…'``): what a
    shell whose ``-c`` body reads its stdin is given as code (#432 review)."""
    here: list[str] = []
    if "<<<" not in "\0".join(words):
        return here  # none at all: skip the per-word scan (#445)
    i = 0
    while i < len(words):
        attached = _HERE_STRING_RE.fullmatch(words[i])
        if attached and attached.group(1):
            here.append(attached.group(1))
        elif words[i].endswith("<<<") and _REDIRECT_OP_RE.fullmatch(words[i]):
            here.extend(words[i + 1 : i + 2])
            i += 1
        i += 1
    return here


def _pipeline_head(code: str, at: int, floor: int = 0) -> int:
    """Where the pipeline whose last stage starts at offset ``at`` of ``code``
    starts, so ``code[head:at]`` is the stages that write into it. Scans back
    past ``|`` and ``|&`` to the previous ``;``/``&``/``&&``/``||``/newline,
    or to the ``(``/``{`` that opens the enclosing group. A group or
    subshell inside the pipeline (``{ …; } | bash``) is taken whole:
    everything it prints is the shell's stdin. Never scans below ``floor``
    (the end of an earlier shell, whose own producers are judged for it), so
    a chain of ``… | bash | … | bash`` stays linear."""
    depth = 0
    i = at - 1
    while i >= floor:
        ch = code[i]
        if ch in ")}":
            depth += 1
        elif ch in "({":
            if depth == 0:
                return i + 1
            depth -= 1
        elif depth == 0 and not _is_redirect_char(code, i):
            if ch in ";\n":
                return i + 1
            if ch == "&" and not (i > 0 and code[i - 1] == "|"):
                return i + 1
            if ch == "|" and i > 0 and code[i - 1] == "|":
                return i + 1
        i -= 1
    return floor


def _words_in_command_position(text: str) -> str:
    """``text`` (a pipeline's producer stages) with every shell word on its
    own line, so each one is in command position: ``echo sudo id | bash``
    runs ``sudo``. A quoted word stays one word, so ``echo 'never use sudo
    here' | bash`` still runs ``never``; inside it, quotes and ``\\n``
    escapes become newlines (``printf 'ls\\nsudo id' | sh``). A leading
    redirection (``<<<'sudo id'``) is dropped. Unparseable text splits on
    every blank and quote, which judges the most text."""
    try:
        words = shlex.split(text)
    except ValueError:
        words = re.split(r"[\s'\"]+", text)
    lines = (_LEADING_REDIRECT_RE.sub("", word) for word in words)
    return "\n".join(re.sub(r"['\"]|\\n", "\n", line) for line in lines)


def _heredoc_bodies(
    text: str, newline: int, heredocs: list[re.Match[str]], keep_from: int = 0
) -> str:
    """The bodies of ``heredocs`` (``policy._HEREDOC_RE`` matches), read from
    the line after offset ``newline`` up to each one's delimiter line, in
    order. Reads only those lines. Empty when there is no next line. Only
    the bodies of heredocs opened at or after ``keep_from`` are returned."""
    body: list[str] = []
    start = newline + 1 if newline >= 0 else len(text)
    for match in heredocs:
        if start >= len(text):
            break  # no lines left for this body or any after it (#445)
        delimiter = match.group(3) or match.group(4)
        while start < len(text):
            end = text.find("\n", start)
            end = len(text) if end < 0 else end
            line = text[start:end]
            start = end + 1
            if (line.lstrip("\t") if match.group(1) else line).strip() == delimiter:
                break
            if match.start() >= keep_from:
                body.append(line)
    return "\n".join(body)


@dataclass(frozen=True)
class _HardSubjects:
    """The texts a hard rule is tested against (#430), by how each is judged.

    ``code``: shell code, judged by :meth:`policy.Rule.matches` as usual; an
    opt-in inside one of them counts for a match there. ``words``: text whose
    every line may be a command (the stages piped into a shell, the
    positional words a ``bash -c '"$@"'`` body runs), searched as is.
    ``quoted``: an opaque site's text with its quotes turned into newlines; a
    command there must be followed by a blank or the end of the text
    (``watch 'sudo id'``), so ``tmux new -s 'sudo-test'`` stays data."""

    code: tuple[str, ...]
    words: tuple[str, ...] = ()
    quoted: tuple[str, ...] = ()


def _without_substitution(masked: str, stop: int, tokens: list[str]) -> list[str] | None:
    """The stage ``tokens`` (ending at offset ``stop`` of ``masked``) without
    the ``<`` of the process substitution ``<(`` it ends in, and without the
    ``<``/``0<`` that makes that substitution its stdin (``bash < <(…)``).
    ``None`` when the stage does not end in a process substitution."""
    if stop >= len(masked) or masked[stop] != "(" or tokens[-1:] != ["<"]:
        return None
    before = tokens[:-1]
    if len(before) > 1 and re.fullmatch(r"0?<", before[-1]):
        before = before[:-1]
    return before


def _process_substitution_code(
    masked: str, text: str, stop: int, program: str, tokens: list[str]
) -> str | None:
    """The producer of a process substitution that the local shell or
    ``source``/``.`` stage ``tokens`` (ending at offset ``stop`` of ``text``)
    reads as code (#444): ``bash <(echo sudo id)`` and ``source <(…)`` run it
    as their script, ``bash < <(…)`` as their stdin. ``None`` when the stage
    does not end in one, or reads its code from elsewhere (``bash x.sh
    <(…)``, ``bash -c '…' <(…)``, ``diff <(…)`` is not a shell at all).

    ``masked`` is :func:`policy.shell_code_text` of ``text``, so a paren in a
    quoted string never closes the substitution; an unclosed one runs to the
    end of the text."""
    before = _without_substitution(masked, stop, tokens)
    if before is None:
        return None
    as_stdin = len(before) < len(tokens) - 1  # `bash < <(…)`, `bash 0< <(…)`
    if program not in _LOCAL_SHELLS:
        if not (_sources_stdin(before) if as_stdin else len(_source_words(before)) == 1):
            return None
    elif _shell_body_index(before) is not None or _shell_code_source(before)[0] != "stdin":
        return None
    elif not as_stdin and any(
        word in _STDIN_OPERANDS or _is_stdin_switch(word) for word in before[1:]
    ):
        return None  # `bash -s <(…)`, `bash - <(…)`: the substitution is a positional word
    depth = 0
    for k in range(stop, len(masked)):
        if masked[k] == "(":
            depth += 1
        elif masked[k] == ")":
            depth -= 1
            if depth == 0:
                return text[stop + 1 : k]
    return text[stop + 1 :]


def _local_shell_subjects(text: str, code: list[str], words: list[str]) -> None:
    """Add to ``code``/``words`` what each local shell in ``text`` runs that
    the walk does not unwrap (#430 review): a ``bash -c`` body's positional
    words, a here-string, and the stages of the pipeline that feed a shell
    reading code from stdin, with a heredoc they own. Only those stages, so
    ``curl … | sh && git commit -m 'sudo: drop'`` judges the curl alone. Also
    every ``env -S`` value, which env splits and runs as a command."""
    masked = policy.shell_code_text(text)
    stages = _program_stages(masked, text)
    # Each stage stops at the next stage's start too, so a `find -exec sh x
    # {} +` chain is not rescanned to the end per shell (#432 review).
    ends = _stage_ends(masked, sorted(stage[2] for stage in stages))
    floor = 0
    for program, _args, pos, _end in stages:
        if program not in _LOCAL_SHELLS and program not in ("source", "."):
            continue
        stop = ends[pos]
        try:
            tokens = _shell_tokens(text[pos:stop])
        except ValueError:
            tokens = [program]
        producer = _process_substitution_code(masked, text, stop, program, tokens)
        if producer is not None:
            # `bash <(echo sudo id)`, `source <(…)`, `bash < <(…)`: what the
            # substitution prints is the code the shell runs (#444).
            if producer.strip():
                words.append(_words_in_command_position(producer))
            continue
        # The substitution is a positional word (`… | bash -s <(:)`), not a
        # `<` redirect: drop it, so the pipeline producer is judged.
        tokens = _without_substitution(masked, stop, tokens) or tokens
        if program not in _LOCAL_SHELLS and not _sources_stdin(tokens):
            continue
        outer_floor = floor
        head = _pipeline_head(masked, pos, floor)
        floor = stop
        own = pos  # where a heredoc the shell reads may open (its own: masked)
        if program not in _LOCAL_SHELLS:
            # `… | source /dev/stdin`, `. /dev/stdin <<EOF`, `. /dev/stdin <<< '…'`
            here: list[str] = _here_strings(tokens[1:])
            own = stop
        else:
            at_body = _shell_body_index(tokens)
            if at_body is not None:
                if at_body + 1 < len(tokens) and _POSITIONAL_REF_RE.search(tokens[at_body]):
                    # `bash -c '"$@"' _ sudo id`: $0 is `_`, so judge from both.
                    words.append(" ".join(tokens[at_body + 1 :]))
                    words.append(" ".join(tokens[at_body + 2 :]))
                if not _body_reads_stdin(tokens, at_body):
                    continue
                # `… | bash -c 'eval "$(cat)"'`: the body runs its stdin.
                here = _here_strings(tokens[at_body + 1 :])
                own = stop
            else:
                source, here = _shell_code_source(tokens)
                if source != "stdin":
                    continue
        code.extend(here)
        producer_start, producer_end = head, pos
        if not text[head:pos].strip() and head >= 2 and masked[head - 2 : head] == ">(":
            # `echo sudo id > >(bash)`, `… | tee >(bash)`: the shell reads
            # what the command writing into its output substitution prints.
            producer_end = head - 2
            producer_start = _pipeline_head(masked, producer_end, outer_floor)
        if text[producer_start:producer_end].strip():
            words.append(_words_in_command_position(text[producer_start:producer_end]))
        # A producer's heredoc body starts on the next line (`cat <<EOF |
        # bash`), past this stage: it is the code the shell reads.
        heredocs = list(policy._HEREDOC_RE.finditer(masked, head, own))
        if heredocs:
            code.append(_heredoc_bodies(text, _next_line_break(text, own), heredocs))
    for segment in _SEGMENT_RE.finditer(masked):
        if not re.search(r"(?:^|[\s/])env\s", segment.group() + " "):
            continue
        try:
            tokens = _shell_tokens(text[segment.start() : segment.end()])
        except ValueError:
            continue
        for k, token in enumerate(tokens):
            if token in ("-S", "--split-string"):
                code.append(" ".join(tokens[k + 1 :]))
            elif token.startswith("--split-string="):
                code.append(token.partition("=")[2])
            elif token.startswith("-S") and len(token) > 2:
                code.append(token[2:])


def _hard_rule_subjects(command: str) -> _HardSubjects:
    """The texts a hard rule is tested against (#430).

    ``code`` is the command plus every ``bash -c``/``eval`` body the cached
    shell walk unwrapped, the here-strings and ``env -S`` values in them, and
    a heredoc a pipeline feeds to a shell. ``words`` is what a shell runs from
    its positional words or from the pipeline feeding its stdin (see
    :func:`_local_shell_subjects`). ``quoted`` is each opaque site the walk
    could not follow (``su -c '…'``, ``watch '…'``, a too-deep ``bash -c``):
    its quoted text is code, so it fails closed, like an opaque site for note
    rules.

    Pure and never raises: on any walk error only the command itself is
    judged, which is the pre-#430 behaviour."""
    try:
        sites, _cwd, _local, bodies = _shell_walk(command)
        code: list[str] = [command, *bodies]
        words: list[str] = []
        for text in (command, *bodies):
            _local_shell_subjects(text, code, words)
        quoted: list[str] = []
        for site in sites:
            if not site.opaque:
                continue
            if site.piped:
                continue  # it reads stdin or a file: judged above
            if site.program in _LOCAL_SHELLS:
                try:
                    has_body = _shell_body_index(_shell_tokens(site.text)) is not None
                except ValueError:
                    has_body = True
                if not has_body:
                    continue  # it reads stdin or a file: judged above
            quoted.append(re.sub(r"['\"]|\\n", "\n", site.text))
        return _HardSubjects(tuple(code), tuple(words), tuple(quoted))
    except Exception:
        return _HardSubjects((command,))


@functools.lru_cache(maxsize=64)
def _quoted_code_re(pattern: str) -> re.Pattern[str]:
    """A ``match="command"`` ``pattern`` whose command word must end at a
    blank or at the end of the text (see :class:`_HardSubjects`)."""
    return re.compile(policy._CMD_POSITION + r"(?=\S*(?:[ \t]|\Z))(?:" + pattern + r")")


def _hard_rule_hits(rule: policy.Rule, command: str, subjects: _HardSubjects) -> list[str]:
    """The subjects ``rule`` matches. A match in ``words``/``quoted`` is
    reported as ``command``: only an opt-in on the command itself covers it.

    Raises :class:`policy.SearchBudgetExceededError` when nothing matched but a
    subject was too costly to judge (#445): the hard-rule callers deny that
    with its own reason. May raise on a malformed rule; the callers skip that
    rule."""
    over_budget = False

    def found(regex: re.Pattern[str] | None, text: str) -> bool:
        nonlocal over_budget
        if regex is None:
            judged = rule.judge(text)
        elif rule.match == "command":
            # A command-position search is bounded (#445).
            try:
                judged = policy.command_search(regex, rule.pattern, text)
            except policy.SearchBudgetExceededError:
                judged = None
        else:
            judged = regex.search(text) is not None
        over_budget = over_budget or judged is None
        return judged is True

    hits = [text for text in subjects.code if found(None, text)]
    if hits:
        return hits
    compiled = rule.compiled()
    quoted = _quoted_code_re(rule.pattern) if rule.match == "command" else compiled
    if any(found(compiled, text) for text in subjects.words):
        return [command]
    if any(found(quoted, text) for text in subjects.quoted):
        return [command]
    if over_budget:
        raise policy.SearchBudgetExceededError(rule.pattern)
    return []


def _hard_rule_opted_in(rule: policy.Rule, command: str, hits: list[str]) -> bool:
    """The opt-in counts where it really takes effect: on the command, or
    inside every body the rule matched (``bash -c 'OMI_…=1 git push …'``).
    One body's opt-in never covers a match elsewhere."""
    if not rule.opt_in:
        return False
    return _opt_in_satisfied(rule.opt_in, command) or all(
        _opt_in_satisfied(rule.opt_in, text) for text in hits
    )


#: The reason a hard rule denies a command too costly to judge (#445).
BUDGET_EXCEEDED_MESSAGE = "command too large/complex to judge safely — split it or shorten it"


def _log_budget_exceeded(rule: policy.Rule, command: str, session: str) -> None:
    """Record a budget denial as its own compliance event, so the audit trail
    tells it from a real match (#445). Never raises."""
    with contextlib.suppress(Exception):
        compliance.log_event(
            compliance.KIND_BUDGET_EXCEEDED,
            session=session,
            tool="Bash",
            command=command,
            rule_id=rule.id,
            severity=rule.severity,
            outcome="deny",
        )


def _hard_policy_verdict(command: str, session: str = "") -> Verdict | None:
    """The deny for the first ``hard`` policy rule ``command`` matches, else None.

    A rule is tested against every text :func:`_hard_rule_subjects` returns, so
    a body run through ``bash -c``/``eval``/a piped shell is judged too (#430).

    The github_push tier is skipped when the command carries its opt-in token (a
    deliberate Codeberg mirror). Soft rules never block here (Layer E records
    them). The opt-in only skips its own rule, so it can never bypass a
    destructive rule a command also matches.

    The SEED rules live in code and never depend on the state dir: if loading
    the full policy raises for any reason (e.g. no resolvable home directory),
    the seed rules are evaluated on their own rather than lost (#420).

    A command whose command-position search could run past
    :data:`policy.CMD_SEARCH_BUDGET`, and which holds a hard rule's keyword,
    is denied with :data:`BUDGET_EXCEEDED_MESSAGE` and logged as a
    ``budget-exceeded`` event under ``session`` (#445): the hard rules fail
    CLOSED there, and only they do.
    """
    try:
        rules: list[policy.Rule] = list(policy.load_policy())
    except Exception:
        rules = list(policy.SEED_RULES)
    subjects = _hard_rule_subjects(command)
    for rule in rules:
        if rule.severity != policy.SEVERITY_HARD:
            continue
        # A single malformed rule must never brick the guard on EVERY tool call:
        # a pattern that fails to compile / errors mid-match is skipped, not
        # raised, and skipping it leaves every other hard rule in force.
        # (Learned rules are also validated at load; this is the belt to that
        # suspenders, covering a bad seed rule or a catastrophic pattern.) Any
        # exception, not just ``re.error``: one rule raising must not disable
        # the rules after it.
        try:
            hits = _hard_rule_hits(rule, command, subjects)
        except policy.SearchBudgetExceededError:
            # Too costly to judge, and the rule's keyword is in it: a hard
            # rule fails CLOSED, with a reason that says why (#445).
            if _hard_rule_opted_in(rule, command, [command]):
                continue
            _log_budget_exceeded(rule, command, session)
            return Verdict(
                allow=False,
                reason=f"omi-guard ({rule.label()}): {BUDGET_EXCEEDED_MESSAGE}",
                rule_id=rule.id,
            )
        except Exception:
            continue
        if not hits or _hard_rule_opted_in(rule, command, hits):
            continue
        return Verdict(
            allow=False,
            reason=f"omi-guard ({rule.label()}): {rule.message}",
            rule_id=rule.id,
        )
    return None


def decide(action: dict[str, Any], *, git_rules_missing: bool = False) -> Verdict:
    """The harness-agnostic policy. See the module docstring for the schema.

    ``git_rules_missing`` (#358) waives ONLY the git-rules read demand, for a
    vault that positively lacks the note; every other gate still runs."""
    session = str(action.get("session") or "")
    command = str(action.get("command") or "")
    repo = _repo_root_for_action(action)

    # 1) Consulting OMI sets the per-turn sentinel and is always allowed. When
    # the adapter knows what was consulted, record it (with target) so the
    # verifier can judge relevance; otherwise just mark the gate consulted.
    if action.get("is_omi_consult"):
        target = str(action.get("consult_target") or "")
        if target:
            record_consult(
                session,
                kind=str(action.get("consult_kind") or "consult"),
                target=target,
                tool=str(action.get("tool") or ""),
            )
        else:
            mark_consulted(session)
        return Verdict(allow=True)

    # 2) Hard blocks — every ``hard`` rule in the data-driven policy.
    hard = _hard_policy_verdict(command, session)
    if hard is not None:
        return hard

    if repo is not None and _is_freshness_command(command):
        # Recorded optimistically here (PreToolUse cannot know the exit code);
        # a FAILED fetch is retracted by guard.record_freshness_outcome on
        # PostToolUse, so a fetch that exits 1 no longer satisfies the
        # commit-time freshness gate (2026-08-27 review).
        _record_git_freshness(session, repo, command)
        return Verdict(allow=True)

    # 2.5) Tool-schema loading (e.g. ToolSearch) is never gated. It already
    # passed the hard blocks above; skip the gate WITHOUT satisfying it (loading
    # a schema is not a consult), so a deferred OMI tool can be loaded and then
    # actually consulted to clear the gate — otherwise the turn deadlocks.
    if str(action.get("tool") or "") in _GATE_EXEMPT_TOOLS:
        return Verdict(allow=True)

    if _is_unauthorized_capability_side_effect(action, session):
        record_pending(session, command or _action_path(action))
        return Verdict(
            allow=False,
            reason=f"omi-guard (hard): {CAPABILITY_SIDE_EFFECT_MESSAGE}",
            rule_id="capability-question-explicit-auth",
        )

    # 2.6) Operator pause (`omind guard pause --for ...`): a time-boxed fast window
    # that skips the consult-gate + verifier for mission-critical speed / token
    # savings. ONLY the gate — the HARD destructive blocks above already ran, so a
    # pause can never green-light a repo-delete / discretionary push / raw sudo. It
    # auto-expires (see :func:`pause_remaining`); engaging it is logged for audit.
    if gate_paused():
        return Verdict(allow=True)

    if _is_global_config_mutation(action) and not _turn_has_explicit_global_auth(action, session):
        return Verdict(
            allow=False,
            reason=f"omi-guard (hard): {GLOBAL_MUTATION_MESSAGE}",
            rule_id="global-config-explicit-auth",
        )

    # A Bash command run from outside any repo can still write into one
    # (`cat > ~/src/x/y.py <<EOF` from /tmp). The Write tool takes its repo
    # from its path; such a write takes it from its target (#448). Only when
    # the command resolves no repo of its own: a commit's freshness stays
    # keyed to the repo its `cd`/`git -C` lead to. The written repo only opens
    # the consult gate: a commit lands where the command's own cwd says, so
    # freshness is judged only when that repo resolves, as before #448.
    gate_repo = repo
    if gate_repo is None and str(action.get("tool") or "") == "Bash":
        gate_repo = _bash_write_repo(action)
    if gate_repo is not None and _is_repo_sensitive_action(action):
        if not git_rules_missing and not _has_consulted_git_rules(session):
            record_pending(session, command or _action_path(action))
            # Name the demanded note so the verifier credits the obeying read
            # as relevant instead of re-closing the gate over it (#148).
            record_demanded_note(session, GIT_RULES_NOTE)
            return Verdict(
                allow=False,
                reason=f"omi-guard (hard): {GIT_RULES_MESSAGE}",
                rule_id="repo-work-read-git-rules",
            )
        # Freshness is demanded ONLY before a commit — that is when a stale local
        # base actually gets recorded; edits, tests, reads, and pushes are not
        # gated on it. A repo with no configured remote has nothing to fetch and
        # no upstream to be stale against, so the check is vacuous there too —
        # waive it rather than lock the agent out of a brand-new `git init` repo
        # (#149).
        if (
            repo is not None
            and _is_commit_action(action)
            and not _git_fresh_for_repo(session, repo)
            and _repo_has_remote(repo)
        ):
            record_pending(session, command or _action_path(action))
            return Verdict(
                allow=False,
                reason=f"omi-guard (hard): {GIT_FRESHNESS_MESSAGE}",
                rule_id="repo-work-fresh-base",
            )

    # 2.7) Provably-inert inspection commands (bare `pwd`, `whoami`, ...) skip
    # the consult-gate (#147): they can't touch a repo, a file, the network, or
    # any state, so no consult could inform them. They deliberately do NOT set
    # the sentinel — the first real action still requires its consult.
    if command and _is_inert_command(command):
        return Verdict(allow=True)

    # 3) The gate — block until OMI was consulted this turn.
    if consulted_this_turn(session):
        return Verdict(allow=True)
    # Record what we were about to do (#96): the verifier scores the next consult
    # against this, so the FIRST consult after a work-transition clears even when the
    # captured task + recent activity are both still cold. (Bash block path; the
    # non-Bash gate-block records it via `guard suggest`.)
    record_pending(session, command)
    return Verdict(allow=False, reason=f"omi-gate: {GATE_MESSAGE}", rule_id="omi-gate")


def _note_rules_verdict(action: dict[str, Any], omi_dir: Path | None) -> Verdict | None:
    """Deterministic operator note rules (#240), evaluated before everything.

    Rules a hook can decide must never depend on model attention. A ``deny``
    hit blocks with the rule's message (compliance-logged by the caller like
    any other hard deny); a ``warn`` or an unknown-visibility miss logs a
    decision event and falls through. Never raises — a broken rule table must
    never brick the guard (fail-open like every other layer).
    """
    if omi_dir is None:
        return None
    try:
        from omind import rules

        hit = rules.evaluate(
            action, omi_dir, _repo_root_for_action(action), view=_rules_command_view(action)
        )
        if hit is None:
            return None
        session = str(action.get("session") or "")
        if hit.outcome == rules.ACTION_DENY:
            return Verdict(
                allow=False,
                reason=(
                    f"omi-guard (hard): note rule '{hit.rule.id}' "
                    f"[{hit.rule.note}]: {hit.rule.message}"
                ),
                rule_id=f"note-rule:{hit.rule.id}",
            )
        compliance.log_event(
            compliance.KIND_DECISION,
            session=session,
            tool=str(action.get("tool") or ""),
            command=str(action.get("command") or ""),
            rule_id=f"note-rule:{hit.rule.id}",
            severity="soft",
            outcome=hit.outcome,
            detail=(hit.detail or hit.rule.message)[:200],
        )
        return None
    except Exception:
        return None


#: Hard ceiling for the embedded excerpt so a huge note can't bloat every deny.
_EXCERPT_CAP = 1_600


def _governing_excerpt(omi_dir: Path | str, note: str) -> str:
    """Summary + leading excerpt of ``note``, capped, for embedding in a deny
    message (#241). Best-effort: any failure returns ``""`` — the deny still
    stands on its demand sentence alone."""
    try:
        from omind import recall

        memory = recall.compact_recall(omi_dir, note, max_chars=1_200, organic=False)
        summary = str(memory.get("summary") or "").strip()
        content = str(memory.get("content") or "").strip()
        text = "\n\n".join(part for part in (summary, content) if part)
        return text[:_EXCERPT_CAP]
    except Exception:
        return ""


#: ``rule_id`` of the compliance event a fail-open logs (#420).
GUARD_ERROR_RULE = "guard-internal-error"


def _rule_severity(rule_id: str) -> str:
    """The severity a deny under ``rule_id`` is logged with (#420).

    A policy rule's own severity when it is one; the consult gate is soft
    friction; any other deny is hard, as :func:`check_action` logs it.
    """
    if rule_id.startswith("omi-gate"):
        return policy.SEVERITY_SOFT
    with contextlib.suppress(Exception):
        for rule in policy.load_policy():
            if rule.id == rule_id:
                return rule.severity
    return policy.SEVERITY_HARD


def _fail_open_verdict(
    action: dict[str, Any],
    exc: Exception,
    decided: Verdict | None,
    *,
    stage: str = "check",
    decided_logged: bool = False,
) -> Verdict:
    """The verdict when :func:`check_action` hits an unexpected exception (#420).

    A deny decided before the exception is a deliberate block and stands. An
    undecided action fails OPEN — enforcement degrades, never raises into the
    agent — except that the static hard-policy rules are re-checked on their
    own, so a crash in an unrelated classifier (repo detection, note rules)
    cannot wave through a command a hard rule plainly names. The error goes to
    stderr and the compliance log so the hole is visible, not silent.

    Two compliance events are written: the internal error itself
    (``guard-internal-error``, outcome ``fail-open`` when the action is allowed,
    ``error`` when a deny still stands), and — for a deny — the deny under its
    real rule id and severity, so it still counts in the recidivism ladder and
    doctor's ``top_rules``. Both writes are best-effort: logging resolves the
    state dir (``Path.home()``), which can itself raise, and the handler must
    never raise the error it is handling back into the agent.

    ``stage`` names the step that failed in the stderr line (``check``, or an
    adapter step such as ``adapter render``). ``decided_logged`` says the caller
    already logged ``decided`` (the adapter, whose :func:`check_action` logs its
    own deny before rendering): the deny is then not logged a second time, so
    one attempt never counts twice toward ``learn.escalate``.
    """
    verdict = Verdict(allow=True)
    if decided is not None and not decided.allow:
        verdict = decided
    else:
        with contextlib.suppress(Exception):
            hard = _hard_policy_verdict(
                str(action.get("command") or ""), str(action.get("session") or "")
            )
            if hard is not None:
                verdict = hard
    session = ""
    tool = ""
    command = ""
    with contextlib.suppress(Exception):
        session = str(action.get("session") or "")
        tool = str(action.get("tool") or "")
        command = str(action.get("command") or "")
    with contextlib.suppress(Exception):
        what = "allowing this action (fail-open)" if verdict.allow else "a deny still applies"
        sys.stderr.write(
            f"omi-guard: internal error in guard {stage} ({type(exc).__name__}: {exc}); {what}\n"
        )
    with contextlib.suppress(Exception):
        compliance.log_event(
            compliance.KIND_DECISION,
            session=session,
            tool=tool,
            command=command,
            rule_id=GUARD_ERROR_RULE,
            severity=policy.SEVERITY_SOFT,
            outcome="fail-open" if verdict.allow else "error",
            detail=f"{type(exc).__name__}: {exc}",
        )
    already_logged = decided_logged and verdict is decided
    if not verdict.allow and verdict.rule_id and not already_logged:
        with contextlib.suppress(Exception):
            compliance.log_event(
                compliance.KIND_DECISION,
                session=session,
                tool=tool,
                command=command,
                rule_id=verdict.rule_id,
                severity=_rule_severity(verdict.rule_id),
                outcome="deny",
            )
    return verdict


def check_action(action: dict[str, Any], omi_dir: Path | None = None) -> Verdict:
    """Decide an action and log a real policy-rule deny to the compliance log.

    The shared core behind ``omind guard check`` and the per-harness adapters
    (:mod:`omind.adapters`), so every harness logs + decides identically. The
    routine ``omi-gate`` "you didn't consult" deny is friction, not logged.

    Never raises (#420, invariant 2): an unexpected exception from any
    classifier fails OPEN via :func:`_fail_open_verdict`. A deny already
    decided before the exception still stands — only an undecided action is
    waved through.
    """
    verdict: Verdict | None = None
    try:
        verdict = _note_rules_verdict(action, omi_dir)
        if verdict is None:
            session = str(action.get("session") or "")
            # #358: once a turn has established the git-rules note is absent, the
            # marker waives the demand for the rest of it — one log line per turn,
            # not one per tool call.
            known_missing = demanded_note(session) == _GIT_RULES_MISSING_MARK
            verdict = decide(action, git_rules_missing=known_missing)
            if (
                not verdict.allow
                and verdict.rule_id == "repo-work-read-git-rules"
                and omi_dir is not None
                and demanded_note_missing(omi_dir)
            ):
                # The demanded note does not exist, so the read this deny asks for
                # cannot succeed. Degrade LOUDLY instead of demanding a ceremony:
                # log the gap where `omind doctor` / an audit can find it, tell the
                # operator the fix, and re-decide with only that demand waived.
                record_demanded_note(session, _GIT_RULES_MISSING_MARK)
                compliance.log_event(
                    compliance.KIND_DECISION,
                    session=session,
                    tool=str(action.get("tool") or ""),
                    command=str(action.get("command") or ""),
                    rule_id="demanded-note-missing",
                    severity="soft",
                    outcome="allowed",
                    detail=f"missing note: {GIT_RULES_NOTE}",
                )
                print(GIT_RULES_MISSING_MESSAGE, file=sys.stderr)
                verdict = decide(action, git_rules_missing=True)
        if verdict.allow:
            # #296: an allowed action still counts against the turn's budget, and at
            # the budget the core may re-arm the gate around an unseen relevant note.
            rearm = budget_verdict(action, omi_dir)
            if rearm is not None:
                verdict = rearm
        if (
            not verdict.allow
            and verdict.rule_id == "repo-work-read-git-rules"
            and omi_dir is not None
        ):
            # #241: place the governing rule text adjacent to the action it blocks.
            # The demand sentence stays first — the recall ceremony still runs and
            # feeds consult telemetry — but the rule itself rides along, because an
            # instruction next to the action wins attention that one injected 200
            # turns earlier has lost.
            excerpt = _governing_excerpt(omi_dir, GIT_RULES_NOTE)
            if excerpt:
                verdict = Verdict(
                    allow=False,
                    reason=(f"{verdict.reason}\n\n--- Governing memory (excerpt) ---\n{excerpt}"),
                    rule_id=verdict.rule_id,
                )
        if not verdict.allow and verdict.rule_id == "omi-gate" and omi_dir is not None:
            from omind import retrieve

            session = str(action.get("session") or "")
            verdict = Verdict(
                allow=False,
                reason=f"omi-gate: {retrieve.suggest_message(turn_task(session), omi_dir)}",
                rule_id=verdict.rule_id,
            )
        if not verdict.allow and verdict.rule_id and not verdict.rule_id.startswith("omi-gate"):
            compliance.log_event(
                compliance.KIND_DECISION,
                session=str(action.get("session") or ""),
                tool=str(action.get("tool") or ""),
                command=str(action.get("command") or ""),
                rule_id=verdict.rule_id,
                severity=policy.SEVERITY_HARD,
                outcome="deny",
            )
        return verdict
    except Exception as exc:
        return _fail_open_verdict(action, exc, verdict)


def _load(stream: TextIO) -> dict[str, Any]:
    # Reading an interactive terminal blocks forever — and a by-hand recovery run
    # (`omind guard reset` typed at a shell) has no piped payload. Treat a TTY
    # stdin as empty rather than hang. Hook input is always piped (never a TTY),
    # so the live path is unchanged; only a human running the command benefits.
    try:
        if stream.isatty():
            return {}
    except (AttributeError, ValueError, OSError):
        pass
    try:
        data = json.loads(stream.read() or "{}")
    except (ValueError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def run_guard(
    action_name: str,
    stream: TextIO | None = None,
    *,
    omi_dir: Path | None = None,
    harness: str = "claude",
    limit: int = 20,
    command: str = "",
    explain: bool = False,
    duration: str = "",
) -> int:
    """CLI entry for ``omind guard <action>``. Returns the process exit code.

    ``check`` reads an action descriptor on stdin and prints the deny reason to
    stderr when blocking. ``reset`` clears the session's per-turn sentinel.
    ``learn`` compiles a violation (stdin JSON) into a soft rule + OMI note;
    ``escalate`` walks the recidivism ladder. Unknown actions are a no-op (exit
    0) — a guard must never wedge the agent.
    """
    src = stream if stream is not None else sys.stdin
    if action_name == "reset":
        data = _load(src)
        session = str(data.get("session") or data.get("session_id") or "")
        # Compliance-log every reset. This verb is agent-reachable (the session
        # id is echoed by hooks, and an empty session clears EVERY gate on the
        # box), so an unlogged clear is an invisible gate bypass; the recidivism
        # reader should see who cleared what, when (2026-08-27 review).
        with contextlib.suppress(Exception):
            compliance.log_event(
                compliance.KIND_GATE_RESET,
                session=session or "(all sessions)",
                tool="guard",
                command="omind guard reset",
                outcome="cleared",
            )
        if session:
            clear_gate(session)
            begin_turn(session, str(data.get("prompt") or ""))
        else:
            # No session id — a human running `omind guard reset` by hand to
            # recover a wedged gate. Clear every gate, since they can't know which
            # session is stuck. (The hook path always supplies a session.)
            clear_all_gates()
        return 0
    if action_name == "preflight":
        return _run_preflight(_load(src), omi_dir, harness=harness)
    if action_name == "learn":
        return _run_learn(_load(src), omi_dir)
    if action_name == "escalate":
        return _run_escalate()
    if action_name == "log":
        return _run_log(limit)
    if action_name == "policy":
        return _run_policy()
    if action_name == "explain":
        return _run_explain(command)
    if action_name == "status":
        return _run_status()
    if action_name == "pause":
        return _run_pause(duration)
    if action_name == "resume":
        return _run_resume()
    if action_name == "repair":
        return _run_repair(omi_dir)
    if action_name == "suggest":
        return _run_suggest(_load(src), omi_dir)
    if action_name == "verify":
        return _run_verify(_load(src), omi_dir, explain)
    if action_name == "adapter":
        from omind import adapters

        return adapters.run_adapter(src, omi_dir=omi_dir, harness=harness)
    if action_name == "selftest":
        from omind import harness as harness_mod

        results = harness_mod.run_selftest()
        for r in results:
            mark = "ok" if r["ok"] else "FAIL"
            sys.stdout.write(
                f"[{mark}] {r['harness']:8} {r['format']:12} "
                f"blocked={r['blocked']} :: {r['command']}\n"
            )
        return 0 if all(r["ok"] for r in results) else 1
    if action_name == "export-corpus":
        from omind import corpus

        count = corpus.export_corpus(sys.stdout)
        sys.stderr.write(f"exported {count} corpus example(s)\n")
        return 0
    if action_name == "check":
        verdict = check_action(_load(src), omi_dir=omi_dir)
        if not verdict.allow:
            # The exit code carries the block; a failed stderr write must not
            # turn it into a traceback.
            with contextlib.suppress(Exception):
                sys.stderr.write(f"BLOCKED by {verdict.reason}\n")
        return verdict.exit_code
    return 0


def _miss_strict() -> bool:
    return bool(os.environ.get(MISS_STRICT_ENV))


def preflight_mode() -> str:
    """How this turn's preflight delivers memory: ``hint``/``inject``/``off``.

    An unrecognised value falls back to the default rather than failing a hook.
    """
    value = str(os.environ.get(PREFLIGHT_MODE_ENV, "")).strip().lower()
    return value if value in PREFLIGHT_MODES else DEFAULT_PREFLIGHT_MODE


def looks_stale(*texts: str) -> bool:
    """True when a note announces its own supersession/correction (#321)."""
    return any(_STALE_MARKER_RE.search(text or "") for text in texts)


def strip_action_items(text: str) -> str:
    """Drop unchecked ``- [ ]`` lines from an excerpt bound for the context
    window. An automatic injection is background, not an assignment (#321)."""
    cleaned = _ACTION_ITEM_RE.sub("", text)
    return re.sub(r"\n{3,}", "\n\n", cleaned).strip()


def hard_rule_notes(omi_dir: Path | str) -> set[str]:
    """Filenames of notes that carry a compiled ``omind-rule`` block.

    This is the curated always-binds surface: enforcement, not recall. These
    keep the firm framing and the full excerpt no matter what mode or budget
    the recall path is under (#321, fix 2 — "keep firm framing only for the
    small set of true hard rules"). Best-effort; a failure means no carve-out.
    """
    try:
        from omind import rules

        return {rule.note for rule in rules.load_rules(omi_dir) if rule.note}
    except Exception:
        return set()


def session_context_chars(omi_dir: Path | str, session: str) -> int:
    """omind context already pushed into ``session``, from omind's own ledger.

    The telemetry that measured #321 was being written and never read; this is
    what reads it. Since #387 only the PUSH channel counts — priming, preflight
    and name hints — so the agent's own ``recall-note``/``search-vault`` reads no
    longer switch hints off. ``OMIND_SPLIT_BUDGET=0`` restores the old sum.
    Best-effort — budgeting must never break a hook.
    """
    if not session:
        return 0
    try:
        from omind import ai_usage

        if ai_usage.split_budget_enabled():
            return ai_usage.session_context_chars(omi_dir, session, channel=ai_usage.PUSH)
        return ai_usage.session_context_chars(omi_dir, session)
    except Exception:
        return 0


#: Appended to the over-budget notice when the budget counts push only (#387),
#: so the agent knows its own deliberate reads did not cause the taper.
_PULL_NOT_COUNTED = "; your own OMI reads are not counted"


def _split_budget() -> bool:
    try:
        from omind import ai_usage

        return ai_usage.split_budget_enabled()
    except Exception:
        return False


#: Turns that look like an action rather than a conversation (#241). Fixed by
#: design — an env knob here would be one more thing that silently degrades.
_ACTION_TURN_RE = re.compile(
    r"\b(git|push|commit|merge|deploy|release|sudo|rm|delete|publish|provision)\b",
    re.IGNORECASE,
)


#: Name timelines shown with one preflight hint (#390). Each is at most
#: ``timeline.MAX_CHARS``, so the hint stays under ~2,300 chars.
_PREFLIGHT_TIMELINES = 2


def _name_timelines(omi_dir: Path | str, names: list[str]) -> list[Any]:
    """Dated histories for the rare names that cleared this turn (#390).

    Empty when there are none, the flag is off, or anything fails.
    """
    if not names:
        return []
    try:
        from omind import timeline

        return timeline.for_names(omi_dir, names, limit=_PREFLIGHT_TIMELINES)
    except Exception:
        return []


def _second_title_line(omi_dir: Path | str, relevant: list[tuple[str, str]], first: str) -> str:
    """Stored filename + summary of the runner-up preflight match, never a
    full body (#241). Skipped on the economy profile, where the preflight
    budget is too tight for a second note. Best-effort — a failure adds
    nothing."""
    if len(relevant) < 2:
        return ""
    try:
        from omind import ai_usage, recall

        if ai_usage.policy(omi_dir).preflight_chars < 2_000:
            return ""
        # #416: the filename retrieval ranked, not a re-resolved title — that
        # can open a newer note sharing the title or stem, or nothing at all.
        filename = relevant[1][1]
        if not filename or filename == first:
            return ""
        memory = recall.compact_recall(
            omi_dir, filename, max_chars=recall.MIN_RECALL_CHARS, organic=False
        )
        summary = str(memory.get("summary") or "").strip()
        line = f"\n\nAlso possibly relevant: [[{filename}]]"
        return line + (f" — {summary}" if summary else "")
    except Exception:
        return ""


def preflight_turn(data: dict[str, Any], omi_dir: Path | None) -> str:
    """Prepare one turn with compact relevant memory and satisfy the soft gate.

    Hard policy prerequisites still run before the soft gate, so a general
    preflight memory cannot bypass the specific git-rules/freshness controls.

    A genuine MISS — the vault was searched and nothing scored as relevant to
    ``task`` — auto-clears the gate instead of forcing a manual consult (unless
    ``MISS_STRICT_ENV`` opts back in): reading an arbitrary note that is, by
    construction, not relevant to the turn buys nothing and only costs tokens.
    An EMPTY task (nothing captured to search with) is not a miss — it means we
    never ran the search at all, so it stays strict; we can't judge "nothing is
    relevant" without having looked.
    """
    session = str(data.get("session_id") or data.get("session") or "")
    task = str(data.get("prompt") or data.get("user_prompt") or "")
    now = time.time()
    last = _read_last_turn(session)
    # #296: an identical continuation-shaped prompt inside the retry window is
    # the harness's own API auto-retry (a burst of bare "retry" turns), not new
    # work — carry the turn's gate state and injected memory instead of
    # resetting and re-judging. A substantive prompt re-sent verbatim is a human
    # re-asking and gets a normal (summary-only, cheap) preflight.
    if (
        task
        and str(last.get("prompt") or "") == task
        and now - float(last.get("ts") or 0.0) <= RETRY_WINDOW_SECS
        and is_continuation_prompt(task)
    ):
        _write_last_turn(session, prompt=task, task=str(last.get("task") or task), ts=now)
        _log_continuity(
            session,
            tool="UserPromptSubmit",
            command="",
            rule_id=GATE_CARRY_RULE,
            outcome="carry",
            detail=f"task={task[:100]!r}",
        )
        return ""
    # Read the activity trail BEFORE the reset: it lives in the sentinel.
    activity = _activity_text(session, omi_dir) if task else ""
    clear_gate(session)
    # #296: a continuation prompt ("retry", "go ahead", a task notification)
    # carries no signal of its own — resolve it against the prior turn's task
    # and what the agent has been doing, so the gate only auto-clears when the
    # vault genuinely has nothing for the work in progress.
    prior_task = str(last.get("task") or "")
    continuation = bool(task) and is_continuation_prompt(task)
    retrieval_task = (
        " ".join(p for p in (task, prior_task, activity) if p) if continuation else task
    )
    begin_turn(session, retrieval_task)
    _write_last_turn(
        session,
        prompt=task,
        task=prior_task if continuation and prior_task else task,
        ts=now,
    )
    if gate_paused() or omi_dir is None:
        return ""

    from omind import ai_usage, recall, retrieve

    relevant = retrieve.relevant_notes(retrieval_task, omi_dir, limit=2) if task else []
    # #416: the stored filename retrieval ranked first. Re-resolving its title
    # picks the NEWEST note whose title, filename or stem matches — a decoy.
    filename = (relevant[0][1] or None) if relevant else None
    if filename is None:
        if task and not relevant and not _miss_strict():
            record_consult(session, kind="no-match", target="", relevant=False)
            compliance.log_event(
                compliance.KIND_DECISION,
                session=session,
                tool="UserPromptSubmit",
                rule_id=GATE_NO_MATCH_RULE,
                severity="soft",
                outcome="auto-clear",
                detail=f"task={task[:120]!r}",
            )
            return (
                "OMI turn preflight searched the vault and found nothing relevant "
                "to this turn's task. Consult gate cleared for this turn — "
                f"proceeding without a forced read (set {MISS_STRICT_ENV}=1 to "
                "require one anyway)."
            )
        return (
            "OMI turn preflight found no confident memory match. The consult gate "
            "remains armed. Before any non-memory tool, call OMI MCP `search-vault` "
            "with a focused query, then `recall-note` on one result."
        )

    memory = recall.compact_recall(
        omi_dir,
        filename,
        max_chars=ai_usage.policy(omi_dir).preflight_chars,
        organic=False,
    )
    # #257: the ranking surfaces the best candidate even when "best" is a single
    # shared word (a bare "retry" turn pulling an unrelated note). Require a
    # minimum absolute term overlap before an unsolicited injection; a weak
    # match is treated like a miss (auto-clear unless MISS_STRICT opts back in).
    min_terms = retrieve.preflight_min_terms()
    # #386: a rare identifier the note mentions clears the threshold on its own.
    # Such a turn is only ever HINTED (titles), even under OMIND_PREFLIGHT=inject:
    # one shared name says which notes to look at, not that their body belongs
    # in this turn's context.
    rare: list[str] = []
    haystack = " ".join(str(memory.get(key) or "") for key in ("title", "summary", "content"))
    if min_terms and retrieve.matched_terms(retrieval_task, haystack) < min_terms:
        rare = retrieve.rare_identifier_hits(retrieval_task, omi_dir, filename)
        if not rare:
            if not _miss_strict():
                record_consult(session, kind="weak-match", target=filename, relevant=False)
                compliance.log_event(
                    compliance.KIND_DECISION,
                    session=session,
                    tool="UserPromptSubmit",
                    rule_id=GATE_WEAK_MATCH_RULE,
                    severity="soft",
                    outcome="auto-clear",
                    detail=f"note={filename!r} task={task[:100]!r}",
                )
                return (
                    "OMI turn preflight found only a weak memory match (fewer "
                    f"than {min_terms} task terms shared) — not injecting it. "
                    "Consult gate cleared for this turn — proceeding without a "
                    f"forced read (set {MISS_STRICT_ENV}=1 to require one anyway)."
                )
            return (
                "OMI turn preflight found no confident memory match. The consult "
                "gate remains armed. Before any non-memory tool, call OMI MCP "
                "`search-vault` with a focused query, then `recall-note` on one "
                "result."
            )
    title = str(memory.get("title") or Path(filename).stem)
    summary = str(memory.get("summary") or "").strip()
    excerpt = str(memory.get("content") or "").strip()
    # The curated always-binds surface: notes carrying a compiled omind-rule
    # block. Everything below that treats recall as optional exempts these —
    # they are enforcement, and enforcement does not get to be probabilistic.
    hard_rule = filename in hard_rule_notes(omi_dir)

    # #390: a turn cleared by a rare name whose facts changed gets that name's
    # dated history — superseded and corrected notes marked, not hidden.
    timelines = _name_timelines(omi_dir, rare)

    # #321 fix 3: never auto-inject a note that announces its own correction.
    # The pipeline used to ship facts, retractions and supersessions in
    # arbitrary order, each stamped "the memory governs". This governs TOPIC
    # matches; a name timeline (#390) shows the correction as part of the
    # history instead, and is only ever a titles-only hint.
    if not hard_rule and not timelines and looks_stale(summary, excerpt):
        record_consult(session, kind="stale-note", target=filename, relevant=False)
        compliance.log_event(
            compliance.KIND_DECISION,
            session=session,
            tool="UserPromptSubmit",
            rule_id=GATE_STALE_NOTE_RULE,
            severity="soft",
            outcome="auto-clear",
            detail=f"note={filename!r} task={task[:100]!r}",
        )
        return (
            f"OMI turn preflight's best match [[{filename}]] carries a supersession "
            "or correction marker, so it was not injected. Consult gate cleared "
            "— call OMI MCP `recall-note` on it if this turn needs it, and read "
            "the correction with the claim."
        )

    # #321 fix 6: budget the session, not just the turn. Two observed sessions
    # accumulated ~207K tokens of pure recall injection each; at that volume
    # omind is not adding context to the session, it substantially IS it.
    spent = session_context_chars(omi_dir, session)
    if not hard_rule and spent >= SESSION_INJECTION_BUDGET_CHARS:
        record_consult(session, kind="budget-spent", target=filename, relevant=False)
        compliance.log_event(
            compliance.KIND_DECISION,
            session=session,
            tool="UserPromptSubmit",
            rule_id=GATE_BUDGET_RULE,
            severity="soft",
            outcome="auto-clear",
            detail=f"chars={spent} budget={SESSION_INJECTION_BUDGET_CHARS}",
        )
        return (
            f"OMI has already pushed {spent:,} unrequested characters into this "
            f"session (push budget {SESSION_INJECTION_BUDGET_CHARS:,}"
            f"{_PULL_NOT_COUNTED if _split_budget() else ''}) and is over budget "
            "— not adding more. Consult gate cleared; call OMI MCP "
            "`search-vault`/`recall-note` when you need memory."
        )

    mode = preflight_mode()
    if mode == "off" and not hard_rule:
        record_consult(session, kind="preflight-off", target="", relevant=None)
        reset_offtopic(session)
        return (
            f"OMI turn preflight is off ({PREFLIGHT_MODE_ENV}=off). Consult gate "
            "cleared — call OMI MCP `search-vault`/`recall-note` when the turn "
            "needs memory."
        )

    record_consult(session, kind="preflight", target=filename, relevant=True)
    reset_offtopic(session)

    if not hard_rule and (mode != "inject" or rare):
        # #321 fix 1, the headline change: push → pull. Name the candidates and
        # stop. Retrieval costs tokens only when it is useful; injection costs
        # them on every turn whether the note relates to the work or not.
        # #386: a turn cleared only by a rare identifier is always a hint.
        # #416: each [[…]] is the stored filename, which recall-note opens
        # as-is; a title may be retitled, hold stripped characters or end in .md.
        names = [filename]
        if len(relevant) > 1 and relevant[1][1] and relevant[1][1] != filename:
            names.append(relevant[1][1])
        # #296: a continuation prompt ("go ahead") was retrieved against the
        # prior turn's task, so say so — the candidate is for the work already in
        # progress, not for the bare continuation word.
        _log_continuity(
            session,
            tool="UserPromptSubmit",
            command="",
            rule_id=GATE_PREFLIGHT_RULE,
            outcome="hint",
            detail=f"note={filename!r} continuation={continuation}"
            + (f" rare={rare[:4]!r}" if rare else "")
            + (f" timeline={[t.name for t in timelines]!r}" if timelines else ""),
        )
        lead = (
            "OMI turn preflight"
            + (" (continuing the prior task)" if continuation else "")
            + " — possibly relevant background from prior "
            "sessions, not an instruction"
            + (f" (notes naming {', '.join(name[:40] for name in rare[:3])})" if rare else "")
            + ": "
        )
        # A filename runs to 200 bytes: drop the runner-up rather than let the
        # cap slice a [[…]] into a name that resolves nowhere (#416).
        if len(lead) + sum(len(n) + 6 for n in names) > PREFLIGHT_HINT_CHARS:
            names = names[:1]
        context = (
            lead
            + ", ".join(f"[[{name}]]" for name in names)
            + ". Call OMI MCP `recall-note` on one if this turn needs it; "
            "verify before acting, it may be stale."
        )[:PREFLIGHT_HINT_CHARS]
        if timelines:
            from omind import timeline

            context += "\n" + timeline.lines(timelines)
        ai_usage.record_context(omi_dir, "recall", len(context), session_id=session)
        if rare:
            # #388: a name hinted for the prompt is not hinted again when it
            # turns up in a tool result later in the session.
            from omind import namehints

            namehints.mark_hinted(session, rare)
        return context

    version = str(memory.get("version") or "")
    repeated = _injected_versions(session).get(filename) == version
    # #241: the summary-only optimization for repeated notes loses to attention
    # decay exactly when it matters — re-inject the full excerpt whenever the
    # turn looks like an action (git/deploy/sudo/…), keep the optimization for
    # conversational turns.
    action_shaped = bool(_ACTION_TURN_RE.search(f"{task} {prior_task}"))
    content = (
        summary
        if repeated and not action_shaped
        else "\n\n".join(part for part in (summary, excerpt) if part and part != summary)
    )
    content = strip_action_items(content) or content
    if not content:
        content = title
    _record_injected(session, filename, version)
    _log_continuity(
        session,
        tool="UserPromptSubmit",
        command="",
        rule_id=GATE_PREFLIGHT_RULE,
        outcome="inject",
        detail=f"note={filename!r} continuation={continuation}",
    )
    framing = (
        ". This note compiles a hard operational rule — apply it unless the "
        "user's current message explicitly overrides it. Silence is not an "
        "override.\n\n"
        if hard_rule
        else ". Possibly relevant background from prior sessions — verify "
        "before acting on it; it may be stale.\n\n"
    )
    context = (
        "OMI turn preflight"
        + (" (continuing the prior task)" if continuation else "")
        + f" recalled [[{filename}]]"
        + (
            " (full excerpt already injected earlier this session)"
            if repeated and not action_shaped
            else ""
        )
        + framing
        + content
    )
    context += _second_title_line(omi_dir, relevant, filename)
    ai_usage.record_context(omi_dir, "recall", len(context), session_id=session)
    return context


def _run_preflight(data: dict[str, Any], omi_dir: Path | None, *, harness: str = "claude") -> int:
    """UserPromptSubmit adapter: inject preflight beside the user prompt, in the
    calling harness's output shape (Claude camelCase by default; Poolside's
    snake_case twin under ``--harness poolside``, whose event is also
    translated first so a prompt missing from the payload is recovered).

    Fails open (#420): an unexpected exception injects nothing and exits 0,
    reported to stderr and the compliance log, rather than a traceback that a
    harness may treat as a blocked prompt."""
    from omind import harness as harness_mod

    try:
        data = harness_mod.translate_event(harness, data)
        context = preflight_turn(data, omi_dir)
    except Exception as exc:
        with contextlib.suppress(Exception):
            sys.stderr.write(
                f"omi-guard: internal error in guard preflight ({type(exc).__name__}: {exc}); "
                "continuing without preflight memory (fail-open)\n"
            )
        # Best-effort: logging resolves the state dir, which can raise the very
        # error being handled (no home directory) — it must not escape.
        with contextlib.suppress(Exception):
            compliance.log_event(
                compliance.KIND_DECISION,
                session=str(data.get("session_id") or data.get("session") or ""),
                tool="UserPromptSubmit",
                rule_id=GUARD_ERROR_RULE,
                severity=policy.SEVERITY_SOFT,
                outcome="fail-open",
                detail=f"{type(exc).__name__}: {exc}",
            )
        return 0
    if context:
        sys.stdout.write(harness_mod.render_context(harness, "UserPromptSubmit", context))
    return 0


def _run_learn(data: dict[str, Any], omi_dir: Path | None) -> int:
    """``omind guard learn``: compile a violation descriptor into enforcement."""
    from omind import learn

    pattern = str(data.get("pattern") or "").strip()
    message = str(data.get("message") or "").strip()
    if not pattern or not message:
        sys.stderr.write("guard learn: 'pattern' and 'message' are required\n")
        return 1
    result = learn.learn_violation(
        pattern=pattern,
        message=message,
        rule_id=(str(data["rule_id"]).strip() if data.get("rule_id") else None),
        omi_dir=omi_dir,
        note_title=(str(data["note_title"]) if data.get("note_title") else None),
        note_summary=str(data.get("note_summary") or ""),
        note_body=str(data.get("note_body") or ""),
    )
    msg = f"learned rule {result.rule_id}"
    if result.note_action:
        msg += f"; OMI note {result.note_action}"
    sys.stdout.write(msg + "\n")
    return 0


def _run_escalate() -> int:
    """``omind guard escalate``: apply the recidivism ladder to learned rules."""
    from omind import learn

    changes = learn.escalate()
    if not changes:
        sys.stdout.write("no learned rules crossed an escalation threshold\n")
        return 0
    for change in changes:
        verifier = " + verifier" if change.verify else ""
        sys.stdout.write(
            f"escalated {change.rule_id}: {change.from_severity} -> "
            f"{change.to_severity}{verifier} ({change.count} hits)\n"
        )
    return 0


def _action_intent(event: dict[str, Any]) -> str:
    """A short text of what an action is about — the file path / command / query the
    tool input carries — for recording the gate-blocked intent (#96)."""
    ti = event.get("tool_input")
    ti = ti if isinstance(ti, dict) else {}
    for key in ("command", "file_path", "query", "pattern", "path", "url", "prompt"):
        val = ti.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return ""


def _run_suggest(data: dict[str, Any], omi_dir: Path | None) -> int:
    """``omind guard suggest``: print the gate-deny message naming the notes
    relevant to this turn's task (Phase 3.2). Prints to STDOUT and exits 0 so the
    bash adapter can capture it and emit the actual exit-2 deny itself.

    NOTE for anyone reading a terminal: this is the hook's message *generator*,
    not a rule suggester and not a thing that can be blocked. Run bare, it reads
    an empty event from stdin and prints "BLOCKED by omi-gate: …" as its normal
    output — which reads exactly like the command itself was refused. It is not.
    The learning loop's commands are ``guard learn`` and ``guard escalate``.
    """
    session = str(data.get("session_id") or data.get("session") or "")
    # The non-Bash gate-block path (Read/Edit/Write/…) reaches the core only here;
    # record what the agent was about to do (#96) so the verifier can judge the next
    # consult against it. (The Bash block path records it in decide().)
    record_pending(session, _action_intent(data))
    task = turn_task(session)
    if omi_dir is not None:
        from omind import retrieve

        message = retrieve.suggest_message(task, omi_dir)
    else:
        message = GATE_MESSAGE
    sys.stdout.write(f"BLOCKED by omi-gate: {message}\n")
    return 0


def _run_verify(data: dict[str, Any], omi_dir: Path | None, explain: bool = False) -> int:
    """``omind guard verify``: judge an OMI-consult event's relevance (manual /
    test entry; the live path runs inside the PostToolUse hook). ``--explain``
    prints the score/thresholds/band/verdict diagnostic without side effects."""
    if omi_dir is None:
        sys.stdout.write("not-a-consult\n")
        return 0
    from omind import verify

    if explain:
        info = verify.explain_consult(data, omi_dir)
        sys.stdout.write((json.dumps(info, indent=2) if info else "not-a-consult") + "\n")
        return 0
    verdict = verify.verify_consult(data, omi_dir)
    sys.stdout.write((verdict or "not-a-consult") + "\n")
    return 0


def _run_log(limit: int) -> int:
    """``omind guard log``: human view of the compliance log + a rollup."""
    summary = compliance.summary()
    sys.stdout.write(
        f"compliance log: {summary['total']} event(s), {summary['denies']} deny, "
        f"{summary['violations']} violation(s)"
        + (f"; last {summary['last_ts']}" if summary["last_ts"] else "")
        + "\n"
    )
    if summary["top_rules"]:
        top = ", ".join(f"{rid}×{n}" for rid, n in summary["top_rules"])
        sys.stdout.write(f"top rules: {top}\n")
    for event in compliance.read_events(limit=limit):
        sys.stdout.write(
            f"  {event.get('ts', ''):19}  {str(event.get('kind', '')):9} "
            f"{str(event.get('outcome', '')):9} {str(event.get('rule_id', '')):24} "
            f"{event.get('command', '')}\n"
        )
    return 0


def _run_policy() -> int:
    """``omind guard policy``: list the active deny set (seed + learned)."""
    rules = policy.load_policy()
    for rule in rules:
        flag = " [verify]" if rule.verify else ""
        sys.stdout.write(
            f"  [{rule.severity:4}] {rule.tier:11} {rule.source:7} "
            f"hits={rule.hits:<3} {rule.id}{flag}\n"
        )
    learned = sum(1 for rule in rules if rule.source == "learned")
    sys.stdout.write(f"{len(rules)} rule(s): {len(rules) - learned} seed + {learned} learned\n")
    return 0


def _run_explain(command: str) -> int:
    """``omind guard explain --command "<cmd>"``: which policy rules a command
    hits + the verdict, WITHOUT touching the gate/sentinel (a pure dry-run)."""
    if not command:
        sys.stderr.write('guard explain: pass --command "<cmd>"\n')
        return 1
    # The same subjects `check` judges (#430 review): a body run through
    # `bash -c`, a piped shell or a wrapper is explained, not just the text.
    subjects = _hard_rule_subjects(command)
    matched: list[tuple[policy.Rule, bool]] = []
    for rule in policy.load_policy():
        try:
            hits = _hard_rule_hits(rule, command, subjects)
        except policy.SearchBudgetExceededError:
            if rule.severity == policy.SEVERITY_HARD:
                sys.stdout.write(f"  [{rule.severity}] {rule.id}: {BUDGET_EXCEEDED_MESSAGE}\n")
                hits = [command]
            else:
                continue  # not judged: no match
        except Exception:
            continue
        if hits:
            matched.append((rule, _hard_rule_opted_in(rule, command, hits)))
    if not matched:
        sys.stdout.write(f"ALLOW (no policy rule matches): {command}\n")
        return 0
    for rule, opted_in in matched:
        state = "opt-in→allow" if opted_in else rule.severity
        sys.stdout.write(f"  [{state}] {rule.id} ({rule.tier}): {rule.message}\n")
    blocking = [r for r, opted in matched if r.severity == policy.SEVERITY_HARD and not opted]
    sys.stdout.write(("DENY" if blocking else "ALLOW") + f": {command}\n")
    return 0


#: ``30m`` / ``2h`` / ``90s`` / a bare ``45`` (minutes). Anchored so a malformed
#: value is rejected, never silently pausing for a surprising length.
_DURATION_RE = re.compile(r"^\s*(\d+)\s*([smh]?)\s*$", re.IGNORECASE)


def _parse_duration(text: str) -> int | None:
    """Seconds for a duration string, or ``None`` if malformed. A bare number is
    minutes (the natural unit for a work-burst pause)."""
    match = _DURATION_RE.match(text or "")
    if not match:
        return None
    return int(match.group(1)) * {"s": 1, "m": 60, "h": 3600, "": 60}[match.group(2).lower()]


def _fmt_secs(secs: int) -> str:
    if secs >= 3600:
        return f"{secs // 3600}h{(secs % 3600) // 60:02d}m"
    if secs >= 60:
        return f"{secs // 60}m{secs % 60:02d}s"
    return f"{secs}s"


#: Public alias: the SessionStart priming banner formats the remaining pause.
fmt_secs = _fmt_secs


def _run_pause(duration: str) -> int:
    """``omind guard pause [--for 30m]``: skip the consult-gate + verifier for a
    time-boxed fast window (mission-critical speed / token savings). The HARD
    destructive blocks stay on; the window auto-resumes; the engagement is logged."""
    seconds = _DEFAULT_PAUSE_SECONDS if not duration else _parse_duration(duration)
    if seconds is None:
        sys.stderr.write(f"guard pause: bad --for {duration!r} (use 30m / 2h / 90s / 45)\n")
        return 1
    if seconds <= 0:
        resume_gate()
        sys.stdout.write("consult-gate re-armed (pause duration was 0).\n")
        return 0
    capped = min(seconds, _MAX_PAUSE_SECONDS)
    if capped != seconds:
        sys.stdout.write(
            f"guard pause: {_fmt_secs(seconds)} exceeds the {_fmt_secs(_MAX_PAUSE_SECONDS)} "
            f"cap — pausing for {_fmt_secs(capped)} instead.\n"
        )
        seconds = capped
    pause_gate(seconds)
    compliance.log_event(
        compliance.KIND_DECISION,
        session="",
        tool="guard",
        command=f"pause --for {_fmt_secs(seconds)}",
        rule_id="gate-paused",
        severity=policy.SEVERITY_SOFT,
        outcome="paused",
    )
    sys.stdout.write(
        f"consult-gate + verifier PAUSED for {_fmt_secs(seconds)} (auto-resumes). "
        "HARD destructive blocks stay ON. Run `omind guard resume` to re-arm now.\n"
    )
    return 0


def _run_resume() -> int:
    """``omind guard resume``: re-arm the consult-gate immediately."""
    was = pause_remaining()
    resume_gate()
    if was > 0:
        sys.stdout.write(f"consult-gate re-armed ({_fmt_secs(was)} of pause discarded).\n")
    else:
        sys.stdout.write("consult-gate already armed (no active pause).\n")
    return 0


def _config_protection() -> list[tuple[str, bool]]:
    """The guard's own config files and whether each is writable by THIS user — the
    kill-shot surface the red-team found (clear the gate once, then edit the hook /
    settings to disable the guard). Surfaced by ``guard status`` so the exposure is
    visible; the real mitigation (root-owned + immutable config) lives outside the
    agent. Best-effort; never raises."""
    out: list[tuple[str, bool]] = []
    with contextlib.suppress(Exception):
        from omind import provision

        candidates: list[tuple[str, Path]] = [
            ("PreToolUse hook", provision._omi_guard_dest()),
            ("learned policy", policy.policy_path()),
            ("Claude settings", Path.home() / ".claude" / "settings.json"),
        ]
        for label, path in candidates:
            if path.exists():
                out.append((label, os.access(path, os.W_OK)))
    return out


def _run_status() -> int:
    """``omind guard status``: the harnesses omind can guard + their capability."""
    from omind import harness as harness_mod

    remaining = pause_remaining()
    if remaining > 0:
        sys.stdout.write(
            f"  PAUSED: consult-gate + verifier off for {_fmt_secs(remaining)} more "
            "(hard blocks still on) — `omind guard resume` to re-arm\n"
        )
    for name, spec in harness_mod.HARNESSES.items():
        sys.stdout.write(
            f"  {name:10} capability={spec.capability:11} "
            f"format={spec.block_format:12} — {spec.description}\n"
        )
    from omind import embed

    es = embed.status()
    if es["available"]:
        sys.stdout.write(f"  semantic relevance: ON (model {es['model']})\n")
    else:
        sys.stdout.write(f"  semantic relevance: off (keyword path) — {es['reason']}\n")
    protection = _config_protection()
    writable = [label for label, can_write in protection if can_write]
    if writable:
        sys.stdout.write(
            "  self-protection: guard config is AGENT-WRITABLE (" + ", ".join(writable) + ") — "
            "clearing the gate once is enough to edit it and disable the guard. Harden by "
            "making these root-owned + immutable (chattr +i), outside the agent's reach.\n"
        )
    elif protection:
        sys.stdout.write("  self-protection: guard config is write-protected\n")
    return 0


def _run_repair(omi_dir: Path | None) -> int:
    """``omind guard repair``: re-provision the OMI guard hook-set, fixing a
    clobbered/stale settings hook path or OMI_DIR mismatch (the wedge we hit)."""
    from omind.provision import heal_omi_guard

    vault = omi_dir.parent if omi_dir is not None else None
    folder = omi_dir.name if omi_dir is not None else "OMI"
    changed = heal_omi_guard(vault=vault, folder=folder, log=print)
    sys.stdout.write(
        "repaired the OMI guard hook-set\n"
        if changed
        else "OMI guard already healthy (nothing to repair)\n"
    )
    return 0
