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

import contextlib
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
from typing import Any, TextIO

from omind import compliance, filelock, paths, policy

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
GIT_RULES_MESSAGE = (
    "ACTION BLOCKED. Next call OMI MCP `recall-note` with "
    '`{"name":"Operational Rules - Git Repos and Secrets", "max_chars": 8000}`, '
    "then retry. Repo work requires that specific memory this turn — read it "
    "in full: a truncated read does not clear this gate."
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
    "current turn; answer questions first instead of inferring permission."
)
CAPABILITY_SIDE_EFFECT_MESSAGE = (
    "side-effect actions require explicit imperative authorization; answer capability "
    "questions like `can you ...?` without acting until the user says to proceed."
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


def record_incomplete_consult(session: str, note: str) -> None:
    """Mark a demanded note as read-but-truncated; the gate stays armed until a
    full read (or a best-possible one) lands. Best-effort, never raises."""
    with contextlib.suppress(OSError):
        path = _incomplete_path(session)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(note.strip().lower(), encoding="utf-8")


def clear_incomplete_consult(session: str) -> None:
    with contextlib.suppress(OSError):
        _incomplete_path(session).unlink()


def incomplete_consult(session: str) -> str:
    """The demanded note whose only read this turn was truncated, or ``""``."""
    try:
        return _incomplete_path(session).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


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
        repos = _fresh_repos(session)
        repos[str(repo)] = int(time.time())
        payload = {"repos": repos, "command": command}
        path.write_text(json.dumps(payload), encoding="utf-8")


def _git_fresh_for_repo(session: str, repo: Path) -> bool:
    return str(repo) in _fresh_repos(session)


def _retract_git_freshness(session: str, repo: Path) -> None:
    """Remove one repo from this turn's fresh set (a fetch that failed)."""
    if not session:
        return
    with contextlib.suppress(OSError, ValueError):
        path = _git_fresh_path(session)
        repos = _fresh_repos(session)
        if str(repo) not in repos:
            return
        del repos[str(repo)]
        if repos:
            path.write_text(json.dumps({"repos": repos}), encoding="utf-8")
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
        repo = _repo_root_for_action(event)
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


def record_consult(session: str, *, kind: str, target: str, relevant: bool | None = None) -> None:
    """Append one OMI consult (note read / search) to the turn's sentinel with
    its relevance verdict (``None`` = not yet judged), and mark the gate
    consulted. Never raises."""

    def _record(data: dict[str, Any]) -> dict[str, Any]:
        existing = data.get("consults")
        consult_list = existing if isinstance(existing, list) else []
        consult_list.append({"kind": kind, "target": target, "relevant": relevant})
        data["consults"] = consult_list
        data["actions"] = 0
        return data

    _mutate_sentinel(session, _record)


def consults(session: str) -> list[dict[str, Any]]:
    """The consults recorded this turn (each ``{kind, target, relevant}``)."""
    raw = _read_sentinel(session).get("consults")
    return [c for c in raw if isinstance(c, dict)] if isinstance(raw, list) else []


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
_GATE_EXEMPT_TOOLS = frozenset({"ToolSearch"})
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
_REPO_TEST_RE = re.compile(
    r"(?:^|[;&|\n(]\s*)(?:uv|pytest|python|tox|nox|hatch|npm|pnpm|yarn|cargo|go|make)\b"
)
# Optional leading git global options (``-C <dir>``, ``-c key=val``) so a
# freshness command run with an explicit repo dir — ``git -C <repo> fetch`` — is
# still recognised as freshness (it previously required a bare ``git fetch``).
_GIT_GLOBAL_OPTS = r"(?:-C[ \t]+\S+[ \t]+|-c[ \t]+\S+[ \t]+)*"
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


def _git_dash_c_path(command: str) -> Path | None:
    """The cumulative ``-C <dir>`` target of the command's first simple command,
    when that command is ``git`` — the repo a ``git -C <dir> …`` actually acts
    on. Scoped to a literal leading ``git`` token so ``make -C``/``tar -C`` are
    never misread. Repeated ``-C`` chains relative to the previous one (git's
    own semantics); a relative result resolves against cwd in the caller. Any
    parse trouble returns ``None`` (fall back to cwd) — never raises."""
    try:
        parts = _split_simple_commands(command)
        if not parts:
            return None
        # POSIX shlex treats every backslash as an escape and turns an unquoted
        # Windows path such as ``C:\\repo`` into ``C:repo``.  PowerShell/cmd do
        # not use backslashes that way, so retain them for a Windows shell or
        # an explicit drive path. Non-POSIX shlex keeps surrounding quotes;
        # remove only a matching outer pair.
        windows_style = os.name == "nt" or re.search(r"(?<!\w)[A-Za-z]:\\", parts[0])
        tokens = shlex.split(parts[0], posix=not windows_style)
        if windows_style:
            tokens = [
                token[1:-1]
                if len(token) >= 2 and token[0] == token[-1] and token[0] in {'"', "'"}
                else token
                for token in tokens
            ]
    except ValueError:
        return None
    if not tokens or tokens[0] != "git":
        return None
    target: Path | None = None
    i = 1
    while i < len(tokens) - 1:
        if tokens[i] == "-C":
            step = Path(tokens[i + 1]).expanduser()
            target = step if target is None or step.is_absolute() else target / step
            i += 2
        elif tokens[i] == "-c":
            i += 2
        else:
            break
    return target


def _repo_root_for_action(action: dict[str, Any]) -> Path | None:
    candidates: list[Path] = []
    raw_path = _action_path(action)
    if raw_path:
        p = Path(raw_path).expanduser()
        candidates.append(p if p.is_dir() else p.parent)
    else:
        # A Bash action carries no file path, so the repo was previously always
        # the shell's cwd — which misattributed `git -C <other-repo> fetch` (and
        # `git -C <other-repo> commit`) to the cwd repo (#147). Honor `-C` for
        # git commands; a `-C` that lands outside any repo falls through to cwd.
        with contextlib.suppress(Exception):
            dash_c = _git_dash_c_path(str(action.get("command") or ""))
            if dash_c is not None:
                candidates.append(dash_c)
        candidates.append(Path.cwd())
    for candidate in candidates:
        try:
            cur = candidate.resolve()
        except OSError:
            cur = candidate.absolute()
        for parent in (cur, *cur.parents):
            marker = parent / ".git"
            # A real worktree has either a .git pointer file or a directory
            # containing HEAD. Merely finding an empty directory named .git
            # (for example a sandbox mount marker) must not turn every child
            # path into a repository and demand an impossible freshness fetch.
            if marker.is_file() or (marker.is_dir() and (marker / "HEAD").is_file()):
                return parent
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
        target = str(consult.get("target") or "").lower()
        if needle in target:
            # A truncated read of the demanded note is not a consult of it —
            # the overriding exceptions live below the fold (#239). The marker
            # in the tool result names the exact re-read that clears this.
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
    return bool(_GIT_COMMIT_RE.search(str(action.get("command") or "")))


def _is_repo_sensitive_action(action: dict[str, Any]) -> bool:
    tool = str(action.get("tool") or "")
    command = str(action.get("command") or "")
    path = _action_path(action)
    if tool in _WRITE_TOOLS or tool in _READ_REVIEW_TOOLS:
        return True
    if tool == "Bash":
        if _is_readonly_git_command(command):
            return False
        # Tolerate the ``-C <dir>``/``-c k=v`` global opts before the verb —
        # without this, ``git -C <repo> commit`` was never classified as repo
        # work at all and sailed past the rules-note + freshness checks (#147).
        if re.search(
            rf"(?:^|[;&|\n(]\s*)git[ \t]+{_GIT_GLOBAL_OPTS}"
            r"(?:add|commit|push|merge|rebase|checkout|switch)\b",
            command,
        ):
            return True
        if re.search(r"(?:^|[;&|\n(]\s*)gh\s+(?:pr|release|repo)\b", command):
            return True
        if _REPO_TEST_RE.search(command):
            return True
        if re.search(r"(?:^|[;&|\n(]\s*)(?:sed|perl|python|python3|node|ruby)\b", command) and (
            " -i" in command or "write_text" in command or "Path(" in command
        ):
            return True
    return bool(path)


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
        return _has_strong_action_auth(text)
    return _has_global_auth(text)


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
    )


def decide(action: dict[str, Any]) -> Verdict:
    """The harness-agnostic policy. See the module docstring for the schema."""
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
                session, kind=str(action.get("consult_kind") or "consult"), target=target
            )
        else:
            mark_consulted(session)
        return Verdict(allow=True)

    # 2) Hard blocks — every ``hard`` rule in the data-driven policy. The
    # github_push tier is skipped when the command carries its opt-in token (a
    # deliberate Codeberg mirror). Soft rules never block here (Layer E records
    # them). The opt-in only skips its own rule, so it can never bypass a
    # destructive rule a command also matches.
    for rule in policy.load_policy():
        if rule.severity != policy.SEVERITY_HARD:
            continue
        # A single malformed rule must never brick the guard on EVERY tool call:
        # a pattern that fails to compile / errors mid-match is skipped, not
        # raised. (Learned rules are also validated at load; this is the belt to
        # that suspenders, covering a bad seed rule or a catastrophic pattern.)
        try:
            if not rule.compiled().search(command):
                continue
        except re.error:
            continue
        if rule.opt_in and _opt_in_satisfied(rule.opt_in, command):
            continue
        return Verdict(
            allow=False,
            reason=f"omi-guard ({rule.label()}): {rule.message}",
            rule_id=rule.id,
        )

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

    if repo is not None and _is_repo_sensitive_action(action):
        if not _has_consulted_git_rules(session):
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
            _is_commit_action(action)
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

        hit = rules.evaluate(action, omi_dir, _repo_root_for_action(action))
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


def check_action(action: dict[str, Any], omi_dir: Path | None = None) -> Verdict:
    """Decide an action and log a real policy-rule deny to the compliance log.

    The shared core behind ``omind guard check`` and the per-harness adapters
    (:mod:`omind.adapters`), so every harness logs + decides identically. The
    routine ``omi-gate`` "you didn't consult" deny is friction, not logged.
    """
    verdict = _note_rules_verdict(action, omi_dir)
    if verdict is None:
        verdict = decide(action)
    if verdict.allow:
        # #296: an allowed action still counts against the turn's budget, and at
        # the budget the core may re-arm the gate around an unseen relevant note.
        rearm = budget_verdict(action, omi_dir)
        if rearm is not None:
            verdict = rearm
    if not verdict.allow and verdict.rule_id == "repo-work-read-git-rules" and omi_dir is not None:
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
        return _run_preflight(_load(src), omi_dir)
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
            sys.stderr.write(f"BLOCKED by {verdict.reason}\n")
        return verdict.exit_code
    return 0


def _miss_strict() -> bool:
    return bool(os.environ.get(MISS_STRICT_ENV))


#: Turns that look like an action rather than a conversation (#241). Fixed by
#: design — an env knob here would be one more thing that silently degrades.
_ACTION_TURN_RE = re.compile(
    r"\b(git|push|commit|merge|deploy|release|sudo|rm|delete|publish|provision)\b",
    re.IGNORECASE,
)


def _second_title_line(omi_dir: Path | str, titles: list[str], first: str) -> str:
    """Title + summary of the runner-up preflight match, never a full body
    (#241). Skipped on the economy profile, where the preflight budget is too
    tight for a second note. Best-effort — a failure adds nothing."""
    if len(titles) < 2:
        return ""
    try:
        from omind import ai_usage, recall

        if ai_usage.policy(omi_dir).preflight_chars < 2_000:
            return ""
        filename = recall.filename_for_title(omi_dir, titles[1])
        if filename is None or filename == first:
            return ""
        memory = recall.compact_recall(
            omi_dir, filename, max_chars=recall.MIN_RECALL_CHARS, organic=False
        )
        title = str(memory.get("title") or Path(filename).stem)
        summary = str(memory.get("summary") or "").strip()
        line = f"\n\nAlso possibly relevant: [[{title}]]"
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

    titles = retrieve.relevant_titles(retrieval_task, omi_dir, limit=2) if task else []
    filename = recall.filename_for_title(omi_dir, titles[0]) if titles else None
    if filename is None:
        if task and not titles and not _miss_strict():
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
    if min_terms:
        haystack = " ".join(str(memory.get(key) or "") for key in ("title", "summary", "content"))
        if retrieve.matched_terms(retrieval_task, haystack) < min_terms:
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
    version = str(memory.get("version") or "")
    repeated = _injected_versions(session).get(filename) == version
    # #241: the summary-only optimization for repeated notes loses to attention
    # decay exactly when it matters — re-inject the full excerpt whenever the
    # turn looks like an action (git/deploy/sudo/…), keep the optimization for
    # conversational turns.
    action_shaped = bool(_ACTION_TURN_RE.search(f"{task} {prior_task}"))
    summary = str(memory.get("summary") or "").strip()
    excerpt = str(memory.get("content") or "").strip()
    content = (
        summary
        if repeated and not action_shaped
        else "\n\n".join(part for part in (summary, excerpt) if part and part != summary)
    )
    if not content:
        content = str(memory.get("title") or filename)
    record_consult(session, kind="preflight", target=filename, relevant=True)
    reset_offtopic(session)
    _record_injected(session, filename, version)
    _log_continuity(
        session,
        tool="UserPromptSubmit",
        command="",
        rule_id=GATE_PREFLIGHT_RULE,
        outcome="inject",
        detail=f"note={filename!r} continuation={continuation}",
    )
    context = (
        "OMI turn preflight"
        + (" (continuing the prior task)" if continuation else "")
        + f" recalled [[{memory.get('title') or Path(filename).stem}]]"
        + (
            " (full excerpt already injected earlier this session)"
            if repeated and not action_shaped
            else ""
        )
        + ". This is a standing operator instruction/memory relevant to this "
        "turn — apply it unless the user's current message explicitly "
        "overrides it. Silence is not an override.\n\n" + content
    )
    context += _second_title_line(omi_dir, titles, filename)
    ai_usage.record_context(omi_dir, "recall", len(context), session_id=session)
    return context


def _run_preflight(data: dict[str, Any], omi_dir: Path | None) -> int:
    """Claude UserPromptSubmit adapter: inject preflight beside the user prompt."""
    context = preflight_turn(data, omi_dir)
    if context:
        payload = {
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": context,
            }
        }
        sys.stdout.write(json.dumps(payload) + "\n")
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
    matched: list[tuple[policy.Rule, bool]] = []
    for rule in policy.load_policy():
        if rule.compiled().search(command):
            opted_in = bool(rule.opt_in and _opt_in_satisfied(rule.opt_in, command))
            matched.append((rule, opted_in))
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
