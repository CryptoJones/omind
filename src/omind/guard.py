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
     (the harness's turn-start hook) clears the sentinel.

The policy lives in data, but the seed rules live in code, so the hard blocks
are always enforceable here on the raw command even on a blank machine — they
cannot be skipped by a broken adapter or a missing policy file.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

from omind import compliance, paths, policy

GATE_MESSAGE = (
    "consult OMI before acting this turn — read a note relevant to your task "
    "(an OMI search or read), then retry. One consult clears the rest of the "
    "turn. This is NOT a prompt to open the credential/auth notes."
)
GIT_RULES_NOTE = "Operational Rules - Git Repos and Secrets"
GIT_RULES_MESSAGE = (
    "repo work requires reading OMI note `Operational Rules - Git Repos and Secrets` "
    "this turn; a generic project-memory consult is not enough."
)
GIT_FRESHNESS_MESSAGE = (
    "repo work requires a same-turn freshness check before review/edit/test/commit/push: "
    "run `git fetch --all --prune` or `git pull --ff-only`, then inspect branch status."
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


def begin_turn(session: str, task: str) -> None:
    """Record this turn's task (best-effort, never raises). Written by
    ``omind guard reset``; the Claude adapter writes the same file in pure bash.

    Also resets the per-turn re-close counter and the pending-intent (#96), so the
    verifier's anti-wedge cap and the transition signal are both measured per turn
    (the bash turn-start hook clears the same counter file)."""
    _clear_reclose(session)
    _clear_pending(session)
    _clear_git_freshness(session)
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


def _record_git_freshness(session: str, repo: Path, command: str) -> None:
    if not session:
        return
    with contextlib.suppress(OSError):
        path = _git_fresh_path(session)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"repo": str(repo), "command": command, "ts": int(time.time())}
        path.write_text(json.dumps(payload), encoding="utf-8")


def _git_fresh_for_repo(session: str, repo: Path) -> bool:
    try:
        data = json.loads(_git_fresh_path(session).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(data, dict) and data.get("repo") == str(repo)


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
        path.write_text(json.dumps(data), encoding="utf-8")


def mark_consulted(session: str) -> None:
    """Mark OMI consulted this turn — the sentinel's *existence* is the gate.
    Preserves any consult records already captured this turn."""
    data = _read_sentinel(session)
    data.setdefault("consults", [])
    _write_sentinel(session, data)


def record_consult(
    session: str, *, kind: str, target: str, relevant: bool | None = None
) -> None:
    """Append one OMI consult (note read / search) to the turn's sentinel with
    its relevance verdict (``None`` = not yet judged), and mark the gate
    consulted. Never raises."""
    data = _read_sentinel(session)
    existing = data.get("consults")
    consult_list = existing if isinstance(existing, list) else []
    consult_list.append({"kind": kind, "target": target, "relevant": relevant})
    data["consults"] = consult_list
    _write_sentinel(session, data)


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
    """Increment and return this turn's re-close count. Never raises."""
    nxt = reclose_count(session) + 1
    with contextlib.suppress(OSError):
        path = _reclose_path(session)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(nxt), encoding="utf-8")
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
    """Increment and return the consecutive off-topic streak. Never raises."""
    nxt = offtopic_count(session) + 1
    with contextlib.suppress(OSError):
        path = _offtopic_path(session)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(nxt), encoding="utf-8")
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
    for pattern in ("gate-*", "reclose-*", "pending-*", "offtopic-*", "git-fresh-*"):
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
_GIT_FRESH_ONLY_RE = re.compile(
    r"^\s*git\s+(?:fetch\b[^;&|\n]*|pull\b[^;&|\n]*(?:--ff-only|--rebase)[^;&|\n]*)\s*$"
)
_GIT_STATUS_ONLY_RE = re.compile(r"^\s*git\s+(?:status|rev-parse|branch|remote)\b[^;&|\n]*$")
_GLOBAL_CONFIG_RE = re.compile(
    r"(?:^|[\s'\"=:/])(?:~?/)?(?:"
    r"\.codex/(?:AGENTS\.md|hooks\.json|config\.toml)|"
    r"\.claude/(?:settings\.json|hooks/[^ \t\n'\";]+)|"
    r"\.hermes/(?:config\.yaml|hooks/[^ \t\n'\";]+|AGENTS\.md)|"
    r"\.config/opencode/(?:opencode\.json|plugin/omi-guard\.js)|"
    r"\.gemini/settings\.json|"
    r"\.openclaw/(?:openclaw\.json|omind/MEMORY\.md)"
    r")"
)
_GLOBAL_AUTH_RE = re.compile(
    r"\b(?:please\s+)?(?:"
    r"make|modify|edit|write|install|update|change|patch|apply|do it|go ahead|"
    r"proceed|send it"
    r")\b",
    re.IGNORECASE,
)
_STRONG_ACTION_AUTH_RE = re.compile(
    r"\b(?:do it|go ahead|proceed|send it|approved|authorized|"
    r"you have (?:my )?(?:permission|authorization)|"
    r"i give you (?:explicit )?(?:permission|authorization))\b",
    re.IGNORECASE,
)
_CAPABILITY_QUESTION_RE = re.compile(
    r"^\s*(?:hey[, ]+|please[, ]+)?(?:can|could|would|will)\s+you\b",
    re.IGNORECASE,
)
_GLOBAL_MUTATING_BASH_RE = re.compile(
    r"(?:^|[;&|\n(]\s*)(?:"
    r"chmod|chown|cp|dd|ed|ex|install|mv|rm|tee|touch|truncate|"
    r"sed\b[^;&|\n]*\s-i\b|perl\b[^;&|\n]*\s-i\b|"
    r"python3?\b[^;&|\n]*(?:write_text|write_bytes|open\([^;&|\n]*[\"']a|"
    r"open\([^;&|\n]*[\"']w)|"
    r"node\b[^;&|\n]*(?:writeFile|appendFile)"
    r")\b|>|>>"
)
_SHELL_SIDE_EFFECT_RE = re.compile(
    r"(?:^|[;&|\n(]\s*)(?:"
    r"gh\s+(?:issue\s+create|pr\s+(?:create|merge)|release\s+create)|"
    r"git\s+(?:add|commit|push|merge|rebase|checkout|switch|tag)|"
    r"systemctl\s+(?:restart|reload|stop|start)|"
    r"service\s+\S+\s+(?:restart|reload|stop|start)|"
    r"kubectl\s+(?:apply|delete|rollout\s+restart|scale)|"
    r"docker\s+(?:compose\s+)?(?:up|down|restart|rm)|"
    r"chmod|chown|cp|dd|install|mv|rm|tee|touch|truncate"
    r")\b|>|>>"
)


def _opt_in_satisfied(opt_in: str, command: str) -> bool:
    """True only when the ``VAR=VALUE`` opt-in token appears as a REAL leading
    environment assignment — at the command start, right after a shell separator
    (``;`` / ``&&`` / ``|`` / a NEWLINE), or via ``env`` — so it actually takes effect.

    A bare substring match (the old behaviour) let the token be forged in a comment
    or a string arg (``rm -rf / # OMI_SUDO_OK=1``, ``echo "OMI_SUDO_OK=1"``) and
    silently bypass a hard rule without ever setting the variable. That is not a
    deliberate opt-in, so it must not skip the deny.

    A newline IS a shell command boundary, so a line-leading assignment inside a
    multi-line script (``…\n  OMI_PUSH_GITHUB=1 git push …``) is legitimate and must
    be recognised — omitting ``\\n`` from the separator class wrongly rejected it
    (3.0.2). A plain space is NOT a separator, so a mid-line ``echo OMI_SUDO_OK=1``
    still doesn't count."""
    pattern = r"(?:^|[;&|\n]|\benv)\s*" + re.escape(opt_in) + r"(?=\s|$)"
    return re.search(pattern, command) is not None


def _action_path(action: dict[str, Any]) -> str:
    for key in ("file_path", "path"):
        value = action.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _repo_root_for_action(action: dict[str, Any]) -> Path | None:
    candidates: list[Path] = []
    raw_path = _action_path(action)
    if raw_path:
        p = Path(raw_path).expanduser()
        candidates.append(p if p.is_dir() else p.parent)
    else:
        candidates.append(Path.cwd())
    for candidate in candidates:
        try:
            cur = candidate.resolve()
        except OSError:
            cur = candidate.absolute()
        for parent in (cur, *cur.parents):
            if (parent / ".git").exists():
                return parent
    return None


def _has_consulted_git_rules(session: str) -> bool:
    needle = GIT_RULES_NOTE.lower()
    for consult in consults(session):
        target = str(consult.get("target") or "").lower()
        if needle in target:
            return True
    return False


def _is_repo_sensitive_action(action: dict[str, Any]) -> bool:
    tool = str(action.get("tool") or "")
    command = str(action.get("command") or "")
    path = _action_path(action)
    if tool in _WRITE_TOOLS or tool in _READ_REVIEW_TOOLS:
        return True
    if tool == "Bash":
        if _GIT_FRESH_ONLY_RE.match(command) or _GIT_STATUS_ONLY_RE.match(command):
            return False
        if re.search(
            r"(?:^|[;&|\n(]\s*)git\s+(?:add|commit|push|merge|rebase|checkout|switch)\b",
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
    if tool not in _WRITE_TOOLS and tool != "Bash":
        return False
    haystack = " ".join(
        part for part in (str(action.get("command") or ""), _action_path(action)) if part
    ).replace("\\", "/")
    if not _GLOBAL_CONFIG_RE.search(haystack):
        return False
    if tool in _WRITE_TOOLS:
        return True
    return bool(_GLOBAL_MUTATING_BASH_RE.search(str(action.get("command") or "")))


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


def _turn_has_explicit_global_auth(action: dict[str, Any], session: str) -> bool:
    text = _turn_authorization_text(action, session)
    if _is_capability_question(text):
        return _has_strong_action_auth(text)
    return bool(_GLOBAL_AUTH_RE.search(text))


def _is_side_effect_action(action: dict[str, Any]) -> bool:
    tool = str(action.get("tool") or "")
    if tool in _WRITE_TOOLS:
        return True
    if _is_global_config_mutation(action):
        return True
    command = str(action.get("command") or "")
    if tool == "Bash" or command:
        return bool(_SHELL_SIDE_EFFECT_RE.search(command))
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
        if not rule.compiled().search(command):
            continue
        if rule.opt_in and _opt_in_satisfied(rule.opt_in, command):
            continue
        return Verdict(
            allow=False,
            reason=f"omi-guard ({rule.label()}): {rule.message}",
            rule_id=rule.id,
        )

    if repo is not None and _GIT_FRESH_ONLY_RE.match(command):
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

    if _is_global_config_mutation(action) and not _turn_has_explicit_global_auth(
        action, session
    ):
        return Verdict(
            allow=False,
            reason=f"omi-guard (hard): {GLOBAL_MUTATION_MESSAGE}",
            rule_id="global-config-explicit-auth",
        )

    if repo is not None and _is_repo_sensitive_action(action):
        if not _has_consulted_git_rules(session):
            record_pending(session, command or _action_path(action))
            return Verdict(
                allow=False,
                reason=f"omi-guard (hard): {GIT_RULES_MESSAGE}",
                rule_id="repo-work-read-git-rules",
            )
        if not _git_fresh_for_repo(session, repo):
            record_pending(session, command or _action_path(action))
            return Verdict(
                allow=False,
                reason=f"omi-guard (hard): {GIT_FRESHNESS_MESSAGE}",
                rule_id="repo-work-fresh-base",
            )

    # 3) The gate — block until OMI was consulted this turn.
    if consulted_this_turn(session):
        return Verdict(allow=True)
    # Record what we were about to do (#96): the verifier scores the next consult
    # against this, so the FIRST consult after a work-transition clears even when the
    # captured task + recent activity are both still cold. (Bash block path; the
    # non-Bash gate-block records it via `guard suggest`.)
    record_pending(session, command)
    return Verdict(allow=False, reason=f"omi-gate: {GATE_MESSAGE}", rule_id="omi-gate")


def check_action(action: dict[str, Any]) -> Verdict:
    """Decide an action and log a real policy-rule deny to the compliance log.

    The shared core behind ``omind guard check`` and the per-harness adapters
    (:mod:`omind.adapters`), so every harness logs + decides identically. The
    routine ``omi-gate`` "you didn't consult" deny is friction, not logged.
    """
    verdict = decide(action)
    if not verdict.allow and verdict.rule_id and verdict.rule_id != "omi-gate":
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
        if session:
            clear_gate(session)
            begin_turn(session, str(data.get("prompt") or ""))
        else:
            # No session id — a human running `omind guard reset` by hand to
            # recover a wedged gate. Clear every gate, since they can't know which
            # session is stuck. (The hook path always supplies a session.)
            clear_all_gates()
        return 0
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
        verdict = check_action(_load(src))
        if not verdict.allow:
            sys.stderr.write(f"BLOCKED by {verdict.reason}\n")
        return verdict.exit_code
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
    bash adapter can capture it and emit the actual exit-2 deny itself."""
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
