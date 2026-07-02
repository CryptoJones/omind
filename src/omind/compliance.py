# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Aaron K. Clark
"""The compliance log + the violation detector (Layer E of the roadmap).

Every interesting guard decision and every post-hoc rule match is appended to
``state_dir()/compliance.jsonl`` — one JSON object per line. That log is the
learning corpus: :mod:`omind.learn` counts recidivism from it to escalate rules,
``omind doctor`` summarizes it, and ``omind guard export-corpus`` turns it into
fine-tuning data.

Two writers feed it:

* ``omind guard check`` logs a ``decision`` when a **policy rule** denies an
  action (the routine ``omi-gate`` "you didn't consult" deny is *not* logged —
  it is friction, not a violation worth learning from).
* The PostToolUse detector (:func:`record_post_tool`) is Layer E: it re-scans the
  command that actually *ran* against the policy. A ``soft``-rule match is
  evidence the recidivism loop accumulates; a ``hard``-rule match means the
  block-path let something through (an escape) and is logged as such.

Like every omind hook path this is best-effort and never raises into the agent.
"""

from __future__ import annotations

import json
import os
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

from omind import filelock, paths, policy

#: A single command rarely needs more than this to be attributable; cap so the
#: corpus can't be bloated by a pathological one-liner.
_COMMAND_CAP = 400

KIND_DECISION = "decision"
KIND_VIOLATION = "violation"


def compliance_log_path() -> Path:
    """The append-only compliance log: ``$XDG_STATE_HOME/omind/compliance.jsonl``."""
    return paths.state_dir() / "compliance.jsonl"


def _truncate(text: str, limit: int = _COMMAND_CAP) -> str:
    collapsed = " ".join(text.split())
    return collapsed if len(collapsed) <= limit else collapsed[: limit - 1] + "…"


def log_event(
    kind: str,
    *,
    session: str = "",
    tool: str = "",
    command: str = "",
    rule_id: str = "",
    severity: str = "",
    outcome: str = "",
    detail: str = "",
    now: datetime | None = None,
) -> None:
    """Append one record to the compliance log. Never raises.

    ``detail`` carries optional context an after-the-fact audit needs (e.g. the
    verifier's score + the signals an off-topic consult was judged against,
    #148); it is only written when non-empty, so existing readers see the same
    schema they always did.

    Uses ``O_APPEND`` + an advisory ``flock`` so concurrent hook processes
    serialize without interleaving a half-written line (same discipline as the
    journal writer in :mod:`omind.hooks`).
    """
    record = {
        "ts": (now or datetime.now()).isoformat(timespec="seconds"),
        "kind": kind,
        "session": session,
        "tool": tool,
        "command": _truncate(command) if command else "",
        "rule_id": rule_id,
        "severity": severity,
        "outcome": outcome,
    }
    if detail:
        record["detail"] = _truncate(detail)
    try:
        path = compliance_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        binary = getattr(os, "O_BINARY", 0)
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | binary, 0o644)
        try:
            filelock.lock_fd(fd)
            os.write(fd, (json.dumps(record) + "\n").encode("utf-8"))
        finally:
            filelock.unlock_fd(fd)
            os.close(fd)
    except OSError:
        return


def read_events(limit: int | None = None) -> list[dict[str, Any]]:
    """Parse the compliance log into records, newest last. Skips bad lines;
    never raises. ``limit`` keeps only the most recent N records."""
    try:
        # errors="replace": a single torn multibyte sequence (a short os.write
        # under ENOSPC) is a UnicodeDecodeError (a ValueError, NOT an OSError), so
        # strict decoding made read_events raise forever and took down the
        # checkpoint timer / doctor / corpus export until the log was hand-repaired.
        lines = compliance_log_path().read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    events: list[dict[str, Any]] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict):
            events.append(obj)
    return events[-limit:] if limit is not None else events


def recidivism(rule_id: str) -> int:
    """How many times ``rule_id`` has been recorded (decision + violation)."""
    return sum(1 for e in read_events() if e.get("rule_id") == rule_id)


def recidivism_counts() -> Counter[str]:
    """Per-rule occurrence counts across the whole log (drives escalation)."""
    return Counter(
        str(e.get("rule_id"))
        for e in read_events()
        if e.get("rule_id") and e.get("rule_id") != "omi-gate"
    )


def summary() -> dict[str, Any]:
    """A compact rollup for ``omind doctor``: totals + the top recidivist rules."""
    events = read_events()
    counts = recidivism_counts()
    return {
        "total": len(events),
        "denies": sum(1 for e in events if e.get("outcome") == "deny"),
        "violations": sum(1 for e in events if e.get("kind") == KIND_VIOLATION),
        "last_ts": events[-1].get("ts") if events else None,
        "top_rules": counts.most_common(5),
    }


def _bash_command(event: dict[str, Any]) -> str:
    """The shell command from a PostToolUse event, or ``""`` for non-Bash tools.

    The policy patterns match shell commands, so only Bash actions are scannable.
    """
    if str(event.get("tool_name") or "") != "Bash":
        return ""
    tool_input = event.get("tool_input")
    if not isinstance(tool_input, dict):
        return ""
    command = tool_input.get("command")
    return command if isinstance(command, str) else ""


def record_post_tool(event: dict[str, Any], *, now: datetime | None = None) -> int:
    """Layer E: scan the command that actually ran against the policy and log a
    violation per matching rule. Returns the number of matches recorded.

    A ``hard``-rule match here is an *escape* — the block-path should have stopped
    it — and is recorded with ``outcome="escaped"``. A ``soft``-rule match is
    evidence (``outcome="observed"``) the recidivism loop accumulates. The
    github-push opt-in is honored so a deliberate mirror is not a violation.
    Best-effort; never raises into the hook.
    """
    command = _bash_command(event)
    if not command:
        return 0
    session = str(event.get("session_id") or "")
    matched = 0
    try:
        for rule in policy.load_policy():
            if not rule.compiled().search(command):
                continue
            if rule.opt_in and re.search(rule.opt_in, command):
                continue
            outcome = "escaped" if rule.severity == policy.SEVERITY_HARD else "observed"
            log_event(
                KIND_VIOLATION,
                session=session,
                tool="Bash",
                command=command,
                rule_id=rule.id,
                severity=rule.severity,
                outcome=outcome,
                now=now,
            )
            matched += 1
    except re.error:
        return matched
    return matched
