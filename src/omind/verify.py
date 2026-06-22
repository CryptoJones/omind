# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Aaron K. Clark
"""The verifier — Layer C of the enforcement roadmap.

The per-turn gate only forces a consult to *happen*; it cannot tell whether the
note read was relevant to the task, so an agent clears it by reading an arbitrary
note. The verifier closes that gap. It runs in the **PostToolUse** hook for an
OMI consult (off the synchronous PreToolUse hot path) and judges whether the
consult was relevant to the turn's captured task:

* a fast **deterministic prefilter** (:func:`omind.retrieve.overlap_score`)
  decides the clear cases — high overlap is relevant, ~zero overlap is not — with
  no model call;
* only the ambiguous middle shells out to headless ``claude -p`` (short timeout);
* **any** error, timeout, missing ``claude`` binary, empty task, or unreadable
  note fails **open** (treated relevant) and is logged — a verifier must never
  wedge the agent.

Default mode is **WARN**: an off-topic consult is logged + a stderr nudge names
better notes, but the gate is untouched. Opt-in **REQUIRE**
(``OMI_VERIFY_REQUIRE=1``) re-closes the gate when no relevant consult exists this
turn, forcing one — enforcement stays entirely off the PreToolUse hot path.

The verifier never steers the agent toward credential/auth notes (the nudge
reuses :mod:`omind.retrieve`, which de-prioritizes them).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from omind import compliance, guard, hooks, retrieve

#: Overlap at/above this is relevant with no model call; at/below the low mark is
#: irrelevant with no model call; the band between is referred to the model.
#: Both are tunable via env (widen the model band, or the deterministic-relevant
#: band, for terse-prompt workflows where keyword overlap under-scores).
_HIGH = 0.5
_LOW = 0.1
_NOTE_EXCERPT_CAP = 4000
_DEFAULT_TIMEOUT = 15
_REQUIRE_ENV = "OMI_VERIFY_REQUIRE"
_TIMEOUT_ENV = "OMI_VERIFY_TIMEOUT"
#: REQUIRE mode re-closes the gate when an off-topic consult leaves the turn with
#: no relevant consult — but it must NEVER deadlock. A terse/abstract task scores
#: ~0 keyword-overlap against every note, so the agent could not raise the score
#: no matter what it reads. After this many re-closes in a turn, stop re-closing
#: (degrade to WARN): the lazy single-arbitrary-read shortcut is still re-closed
#: and logged, but a genuinely-stuck agent always escapes. 0 disables re-closing.
_MAX_RECLOSE_ENV = "OMI_VERIFY_MAX_RECLOSE"
_DEFAULT_MAX_RECLOSE = 2
_HIGH_ENV = "OMI_VERIFY_HIGH"
_LOW_ENV = "OMI_VERIFY_LOW"
#: Comma-separated substrings; a consult whose target matches one is ALWAYS
#: relevant (never re-closes the gate) — e.g. release/project notes you always
#: consult. The escape hatch for the REQUIRE-mode false negatives on terse tasks.
_ALWAYS_RELEVANT_ENV = "OMI_VERIFY_ALWAYS_RELEVANT"
#: How many of the most-recent same-session journal bullets feed the "what the
#: agent is doing" signal (issue #95). Tunable for very chatty/quiet sessions.
_ACTIVITY_LIMIT_ENV = "OMI_VERIFY_ACTIVITY"
_DEFAULT_ACTIVITY_LIMIT = 8
#: A journal action bullet, capturing the (short) session id and the action text:
#: ``- HH:MM [session <id>] <label> <tool> -> <target> (<outcome>)``.
_ACTIVITY_BULLET_RE = re.compile(r"^-\s+\d{1,2}:\d{2}\s+\[session\s+([^\]]*)\]\s+(.*)$")
#: The trailing ``(ok)`` / ``(error: …)`` outcome marker on a journal bullet.
_OUTCOME_RE = re.compile(r"\([^)]*\)\s*$")

#: Synthetic rule id for an off-topic consult in the compliance log.
OFF_TOPIC_RULE = "off-topic-consult"

#: Synthetic rule id for the anti-wedge floor: REQUIRE mode hit its per-turn
#: re-close cap with no relevant consult and let the agent proceed anyway. Logged
#: so a chronically off-target task (a verifier blind spot) is visible, not silent.
NO_RELEVANT_FLOOR_RULE = "verify-reclose-floor"


def _threshold(env: str, default: float) -> float:
    try:
        return float(os.environ.get(env) or default)
    except (ValueError, TypeError):
        return default


def _always_relevant(target: str) -> bool:
    """True when ``target`` matches an operator-configured always-relevant
    substring (``OMI_VERIFY_ALWAYS_RELEVANT``)."""
    low = target.lower()
    for pat in os.environ.get(_ALWAYS_RELEVANT_ENV, "").split(","):
        pat = pat.strip().lower()
        if pat and pat in low:
            return True
    return False


def _activity_limit() -> int:
    try:
        return max(0, int(os.environ.get(_ACTIVITY_LIMIT_ENV) or _DEFAULT_ACTIVITY_LIMIT))
    except (ValueError, TypeError):
        return _DEFAULT_ACTIVITY_LIMIT


def recent_activity(
    session: str, omi_dir: Path | str, *, now: datetime | None = None, limit: int | None = None
) -> str:
    """What the agent has recently been *doing* this session, as one scored blob.

    Issue #95: the captured ``turn_task`` is only the user's last prompt, so when
    the user delegates background/parallel work the agent's genuinely on-topic
    consults score off-topic against it and the gate re-closes. Blending in the
    agent's recent **non-OMI** journal bullets (the same per-action trail the
    journal already records) gives the verifier a second, on-topic signal.

    Prior OMI consults are deliberately excluded so an agent can't bootstrap
    relevance by reading an arbitrary note and then citing it as "activity".
    Best-effort: returns ``""`` when the journal is absent/unreadable, so the
    caller falls back to task-only scoring (no behaviour change).
    """
    sid = hooks.short_session_id(session)
    if not sid or sid == "unknown":
        return ""
    when = now or datetime.now()
    omi_marker = str(Path(omi_dir))
    cap = _activity_limit() if limit is None else max(0, limit)
    if cap == 0:
        return ""
    path = hooks.journal_dir(omi_dir) / hooks.journal_name(when)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    details: list[str] = []
    for line in text.splitlines():
        match = _ACTIVITY_BULLET_RE.match(line.strip())
        if not match or match.group(1).strip() != sid:
            continue
        detail = match.group(2)
        if "mcp__omi__" in detail or "mcp_omi_" in detail or omi_marker in detail:
            continue  # don't let prior OMI consults bootstrap relevance
        # Keep the meaningful target/command, dropping the ``<label> <tool> ->``
        # prefix and the trailing ``(outcome)`` so journal scaffolding (tool names,
        # "ok"/"error") doesn't dilute the overlap with the consult.
        if "->" in detail:
            detail = detail.split("->", 1)[1]
        detail = _OUTCOME_RE.sub("", detail).strip()
        if detail:
            details.append(detail)
    return " ".join(details[-cap:])


def _past_mistakes_context(limit: int = 5) -> str:
    """Recent off-topic consults this agent made, to prime the relevance check
    with this agent's past mistakes (roadmap Phase 3). ``""`` when none."""
    recent = [
        str(e.get("command") or "")
        for e in compliance.read_events()
        if e.get("rule_id") == OFF_TOPIC_RULE and e.get("command")
    ][-limit:]
    if not recent:
        return ""
    return (
        "This agent has RECENTLY consulted these off-topic (avoid rewarding the "
        "same mistake):\n" + "\n".join(f"- {c}" for c in recent) + "\n\n"
    )


def _under(path: str, omi_dir: Path | str) -> bool:
    try:
        return Path(omi_dir).expanduser().resolve() in Path(path).expanduser().resolve().parents
    except (OSError, ValueError):
        return str(omi_dir) in path


def consult_target(event: dict[str, Any], omi_dir: Path | str) -> tuple[str, str] | None:
    """``(kind, target)`` for an OMI-consult event, or ``None`` if it isn't one.

    ``kind`` is ``"search"`` (a query) or ``"read"`` (a note path/name).
    """
    tool = str(event.get("tool_name") or "")
    ti = event.get("tool_input")
    ti = ti if isinstance(ti, dict) else {}
    if tool.startswith("mcp__omi__"):
        if "search" in tool:
            return ("search", str(ti.get("query") or ""))
        target = str(
            ti.get("filename") or ti.get("name") or ti.get("note") or ti.get("query") or ""
        )
        return ("read", target)
    if tool == "Read":
        fp = str(ti.get("file_path") or "")
        if fp and _under(fp, omi_dir):
            return ("read", fp)
    return None


def _consult_text(kind: str, target: str, omi_dir: Path | str) -> str:
    """The text to score against the task: a search query verbatim, or the body
    of the consulted note (best-effort; falls back to the note name)."""
    if kind == "search":
        return target
    candidates = [Path(target), Path(omi_dir) / target, Path(omi_dir) / f"{target}.md"]
    for path in candidates:
        try:
            if path.is_file():
                return path.read_text(encoding="utf-8", errors="replace")[:_NOTE_EXCERPT_CAP]
        except OSError:
            continue
    return target


def _parse_verdict(text: str) -> bool | None:
    """Read a relevance verdict from a model reply. ``None`` if unclear (the
    caller fails open)."""
    low = text.strip().lower()
    if not low:
        return None
    if "irrelevant" in low or low.startswith("no"):
        return False
    if "relevant" in low or low.startswith("yes"):
        return True
    return None


def _ask_model(task: str, text: str) -> bool | None:
    """Ask headless ``claude -p`` whether the consult was relevant. ``None`` on
    any unavailability/error/timeout (the caller fails open)."""
    claude = shutil.which("claude")
    if not claude:
        return None
    prompt = (
        "You are an OMI-compliance relevance checker. An agent was told to consult "
        "its memory (OMI) before acting on a task, and it consulted the material "
        "below. Answer with exactly one word — RELEVANT or IRRELEVANT — for whether "
        "that material is relevant to the task.\n\n"
        f"{_past_mistakes_context()}"
        f"TASK:\n{task[:1000]}\n\n"
        f"CONSULTED MATERIAL:\n{text[:2000]}\n"
    )
    try:
        timeout = int(os.environ.get(_TIMEOUT_ENV) or _DEFAULT_TIMEOUT)
    except ValueError:
        timeout = _DEFAULT_TIMEOUT
    try:
        result = subprocess.run(
            [claude, "-p", prompt], capture_output=True, text=True, timeout=timeout
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode != 0:
        return None
    return _parse_verdict(result.stdout or "")


def judge_with_activity(task: str, activity: str, text: str) -> bool:
    """Relevance verdict blending the captured task with the agent's recent
    activity (issue #95): a consult is relevant if it overlaps **either** signal.
    Same deterministic-prefilter → model-fallback ladder as :func:`judge`, scoring
    against ``max`` of the two overlaps so neither dilutes the other (concatenating
    would inflate the recall denominator — the dilution 2.43.2 fought)."""
    if not text or (not task and not activity):
        return True  # can't judge without both sides -> fail open (relevant)
    high = _threshold(_HIGH_ENV, _HIGH)
    low = _threshold(_LOW_ENV, _LOW)
    score = max(
        retrieve.overlap_score(task, text) if task else 0.0,
        retrieve.overlap_score(activity, text) if activity else 0.0,
    )
    if score >= high:
        return True
    if score <= low:
        return False
    verdict = _ask_model(task or activity, text)
    return True if verdict is None else verdict


def judge(task: str, text: str) -> bool:
    """Relevance verdict for a single consult against the turn task alone.
    Deterministic prefilter first (with operator-tunable thresholds), the model
    only for the ambiguous middle, fail-open everywhere else."""
    return judge_with_activity(task, "", text)


def _require_mode(require: bool | None) -> bool:
    if require is not None:
        return require
    return bool(os.environ.get(_REQUIRE_ENV))


def _max_reclose() -> int:
    try:
        return max(0, int(os.environ.get(_MAX_RECLOSE_ENV) or _DEFAULT_MAX_RECLOSE))
    except (ValueError, TypeError):
        return _DEFAULT_MAX_RECLOSE


def _any_relevant(session: str) -> bool:
    return any(c.get("relevant") is True for c in guard.consults(session))


def _nudge(task: str, omi_dir: Path | str, out: Any) -> None:
    titles = retrieve.relevant_titles(task, omi_dir) if task else []
    links = ", ".join(f"[[{t}]]" for t in titles) if titles else "a note on-point for this task"
    out.write(
        f"omind: that OMI consult looks off-topic for your task — more relevant: {links}.\n"
    )


def verify_consult(
    event: dict[str, Any],
    omi_dir: Path | str,
    *,
    require: bool | None = None,
    now: datetime | None = None,
    out: Any = None,
) -> str | None:
    """Judge one PostToolUse event. Returns ``"relevant"`` / ``"irrelevant"``, or
    ``None`` when the event was not an OMI consult. Never raises."""
    target_info = consult_target(event, omi_dir)
    if target_info is None:
        return None
    kind, target = target_info
    session = str(event.get("session_id") or "")
    task = guard.turn_task(session)
    activity = recent_activity(session, omi_dir, now=now)
    relevant = _always_relevant(target) or judge_with_activity(
        task, activity, _consult_text(kind, target, omi_dir)
    )
    guard.record_consult(session, kind=kind, target=target, relevant=relevant)
    if relevant:
        return "relevant"

    compliance.log_event(
        compliance.KIND_VIOLATION,
        session=session,
        tool=str(event.get("tool_name") or ""),
        command=target,
        rule_id=OFF_TOPIC_RULE,
        severity="soft",
        outcome="irrelevant",
        now=now,
    )
    _nudge(task, omi_dir, out if out is not None else sys.stderr)
    if _require_mode(require) and not _any_relevant(session):
        # REQUIRE mode: re-close the gate so the next action is blocked until a
        # relevant consult happens. Enforcement stays off the PreToolUse hot path.
        # BUT a verifier must never deadlock the agent — cap the re-closes per turn
        # so a terse task (which scores ~0 against every note) can't wedge it
        # forever. Past the cap, degrade to WARN: stop re-closing, but record the
        # backstop so the floor is visible in the compliance log.
        if guard.bump_reclose(session) <= _max_reclose():
            guard.clear_gate(session)
        else:
            compliance.log_event(
                compliance.KIND_VIOLATION,
                session=session,
                tool=str(event.get("tool_name") or ""),
                command=target,
                rule_id=NO_RELEVANT_FLOOR_RULE,
                severity="soft",
                outcome="floor",
                now=now,
            )
    return "irrelevant"


def explain_consult(event: dict[str, Any], omi_dir: Path | str) -> dict[str, Any] | None:
    """Diagnose how a consult would be judged WITHOUT side effects — the score,
    the (tunable) thresholds, which band it lands in, the verdict, and the notes
    that would be suggested. Powers ``omind guard verify --explain`` so a REQUIRE-
    mode false negative is debuggable. ``None`` when not an OMI consult."""
    target_info = consult_target(event, omi_dir)
    if target_info is None:
        return None
    kind, target = target_info
    session = str(event.get("session_id") or "")
    task = guard.turn_task(session)
    activity = recent_activity(session, omi_dir, now=None)
    text = _consult_text(kind, target, omi_dir)
    high = _threshold(_HIGH_ENV, _HIGH)
    low = _threshold(_LOW_ENV, _LOW)
    task_score = retrieve.overlap_score(task, text) if task else 0.0
    activity_score = retrieve.overlap_score(activity, text) if activity else 0.0
    score = max(task_score, activity_score)
    if _always_relevant(target):
        band, verdict = "always-relevant (allowlist)", True
    elif (not task and not activity) or not text:
        band, verdict = "fail-open (no task/text)", True
    elif score >= high:
        band, verdict = "high → deterministic relevant", True
    elif score <= low:
        band, verdict = "low → deterministic irrelevant", False
    else:
        band, verdict = "middle → claude -p tiebreaker", None
    return {
        "kind": kind,
        "target": target,
        "task": task[:160],
        "score": round(score, 3),
        "task_score": round(task_score, 3),
        "activity_score": round(activity_score, 3),  # issue #95: what the agent is doing
        "high": high,
        "low": low,
        "band": band,
        "verdict": verdict,  # None = the model would decide
        "suggested_notes": retrieve.relevant_titles(task, omi_dir) if task else [],
    }
