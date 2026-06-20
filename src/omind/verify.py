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
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from omind import compliance, guard, retrieve

#: Overlap at/above this is relevant with no model call; at/below the low mark is
#: irrelevant with no model call; the band between is referred to the model.
_HIGH = 0.5
_LOW = 0.1
_NOTE_EXCERPT_CAP = 4000
_DEFAULT_TIMEOUT = 15
_REQUIRE_ENV = "OMI_VERIFY_REQUIRE"
_TIMEOUT_ENV = "OMI_VERIFY_TIMEOUT"

#: Synthetic rule id for an off-topic consult in the compliance log.
OFF_TOPIC_RULE = "off-topic-consult"


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


def judge(task: str, text: str) -> bool:
    """Relevance verdict for a single consult. Deterministic prefilter first, the
    model only for the ambiguous middle, fail-open everywhere else."""
    if not task or not text:
        return True  # can't judge without both sides -> fail open (relevant)
    score = retrieve.overlap_score(task, text)
    if score >= _HIGH:
        return True
    if score <= _LOW:
        return False
    verdict = _ask_model(task, text)
    return True if verdict is None else verdict


def _require_mode(require: bool | None) -> bool:
    if require is not None:
        return require
    return bool(os.environ.get(_REQUIRE_ENV))


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
    relevant = judge(task, _consult_text(kind, target, omi_dir))
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
        guard.clear_gate(session)
    return "irrelevant"
