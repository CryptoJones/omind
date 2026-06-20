# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Aaron K. Clark
"""The learning loop: compile a mistake into enforcement, then escalate repeats.

The guiding principle of the roadmap is that an LLM does not learn in-weights
from a note, so every mistake becomes a MECHANICAL control — the environment is
the learner. Two operations implement that here:

* :func:`learn_violation` — compile a violation into a **soft** learned policy
  rule (so the guard now recognizes the pattern) *and* a structured OMI note (so
  the lesson is human-readable and travels the mesh). Idempotent by rule id.
* :func:`escalate` — read recidivism from the compliance log and walk the
  ``soft → hard → verifier`` ladder: a soft rule seen :data:`SOFT_TO_HARD` times
  becomes a hard block; one seen :data:`HARD_TO_VERIFY` times is additionally
  flagged for Layer C scrutiny. Only *learned* rules move — seed rules are
  immutable code.

Both are best-effort and never raise into a hook.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from omind import compliance, policy

#: Recidivism thresholds for the escalation ladder.
SOFT_TO_HARD = 3
HARD_TO_VERIFY = 5


@dataclass(frozen=True)
class LearnResult:
    rule_id: str
    note_action: str | None  # "created" / "updated" / None when no note written


@dataclass(frozen=True)
class Escalation:
    rule_id: str
    from_severity: str
    to_severity: str
    verify: bool
    count: int


def _slug(text: str, words: int = 5) -> str:
    tokens = re.findall(r"[a-z0-9]+", text.lower())[:words]
    return "-".join(tokens)


def derive_rule_id(pattern: str, message: str) -> str:
    """A stable, readable id for a learned rule: a message slug + a short hash of
    the pattern (so two different patterns never collide on the same slug)."""
    digest = hashlib.sha256(pattern.encode("utf-8")).hexdigest()[:6]
    slug = _slug(message) or "rule"
    return f"learned-{slug}-{digest}"


def _note_body(rule_id: str, pattern: str, message: str) -> str:
    return (
        "Compiled automatically by omind's enforcement learning loop after an "
        "observed OMI-compliance violation. It is enforced mechanically by "
        "`omind guard` so the mistake recurs as a block, not a hope.\n\n"
        f"- Rule id: `{rule_id}`\n"
        f"- Pattern: `{pattern}`\n"
        "- Severity: soft on creation; escalates to a hard block on recidivism "
        f"(soft→hard at {SOFT_TO_HARD} hits, →verifier at {HARD_TO_VERIFY}).\n\n"
        f"{message}"
    )


def learn_violation(
    *,
    pattern: str,
    message: str,
    rule_id: str | None = None,
    omi_dir: Path | str | None = None,
    note_title: str | None = None,
    note_summary: str = "",
    note_body: str = "",
    write_note: bool = True,
    now: datetime | None = None,
) -> LearnResult:
    """Compile a violation into a soft learned rule (+ an OMI note). Idempotent.

    The rule is always recorded; the note is best-effort (a missing/unwritable
    vault must not lose the mechanical control).
    """
    rid = rule_id or derive_rule_id(pattern, message)
    policy.append_learned_rule(
        policy.Rule(
            id=rid,
            pattern=pattern,
            message=message,
            severity=policy.SEVERITY_SOFT,
            tier=policy.TIER_LEARNED,
        ),
        now=now,
    )
    note_action: str | None = None
    if write_note and omi_dir is not None:
        note_action = _write_lesson_note(
            rid, pattern, message, omi_dir, note_title, note_summary, note_body
        )
    return LearnResult(rule_id=rid, note_action=note_action)


def _write_lesson_note(
    rule_id: str,
    pattern: str,
    message: str,
    omi_dir: Path | str,
    note_title: str | None,
    note_summary: str,
    note_body: str,
) -> str | None:
    """Upsert the lesson note through the single-writer path. Never raises."""
    from omind.notes import upsert_note
    from omind.store import NoteError, NoteFields

    title = note_title or f"OMI enforcement lesson — {rule_id}"
    try:
        action, _ = upsert_note(
            omi_dir,
            NoteFields(
                title=title,
                summary=note_summary or message,
                details=note_body or _note_body(rule_id, pattern, message),
                tags=["omi-enforcement", "learned-rule", "guard"],
            ),
        )
        return action
    except (NoteError, OSError):
        return None


def escalate() -> list[Escalation]:
    """Walk the soft→hard→verifier ladder for learned rules by recidivism.

    Returns the escalations performed (empty when nothing crossed a threshold).
    """
    counts = compliance.recidivism_counts()
    learned = {r.id: r for r in policy.load_learned()}
    changes: list[Escalation] = []
    for rule_id, count in counts.items():
        rule = learned.get(rule_id)
        if rule is None:
            continue  # seed rules are immutable code — never escalated here
        if count >= HARD_TO_VERIFY and not rule.verify:
            policy.update_learned_rule(
                rule_id, severity=policy.SEVERITY_HARD, hits=count, verify=True
            )
            changes.append(
                Escalation(rule_id, rule.severity, policy.SEVERITY_HARD, True, count)
            )
        elif count >= SOFT_TO_HARD and rule.severity == policy.SEVERITY_SOFT:
            policy.update_learned_rule(rule_id, severity=policy.SEVERITY_HARD, hits=count)
            changes.append(
                Escalation(rule_id, rule.severity, policy.SEVERITY_HARD, False, count)
            )
    return changes
