# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Aaron K. Clark
"""Tests for the learning loop: auto-compile + recidivism escalation."""

from __future__ import annotations

import io
import json
from pathlib import Path

from omind import compliance, guard, learn, policy


def test_derive_rule_id_is_stable_and_pattern_qualified() -> None:
    a = learn.derive_rule_id(r"\bfoo\b", "no foo allowed")
    assert a == learn.derive_rule_id(r"\bfoo\b", "no foo allowed")  # deterministic
    assert a.startswith("learned-no-foo-allowed-")
    # Same message, different pattern -> different id (no collision).
    assert a != learn.derive_rule_id(r"\bbar\b", "no foo allowed")


def test_learn_violation_appends_soft_rule_without_a_note() -> None:
    result = learn.learn_violation(pattern=r"\brm\s+-rf\b", message="no rm -rf")
    learned = policy.load_learned()
    assert [r.id for r in learned] == [result.rule_id]
    assert learned[0].severity == policy.SEVERITY_SOFT
    assert learned[0].tier == policy.TIER_LEARNED
    assert result.note_action is None  # no omi_dir -> no note


def test_learn_violation_writes_an_omi_note(tmp_path: Path) -> None:
    result = learn.learn_violation(
        pattern=r"\bcurl\s+\|\s*sh\b",
        message="never pipe curl into a shell",
        omi_dir=tmp_path,
    )
    assert result.note_action == "created"
    note = tmp_path / f"OMI enforcement lesson — {result.rule_id}.md"
    assert note.is_file()
    assert "never pipe curl into a shell" in note.read_text(encoding="utf-8")


def test_learn_violation_is_idempotent_by_id() -> None:
    learn.learn_violation(pattern=r"\bx\b", message="m", rule_id="fixed")
    learn.learn_violation(pattern=r"\bx\b", message="m2", rule_id="fixed")
    learned = policy.load_learned()
    assert [r.id for r in learned] == ["fixed"]
    assert learned[0].message == "m2"


def test_escalate_soft_to_hard_then_verifier() -> None:
    learn.learn_violation(pattern=r"\bdanger\b", message="m", rule_id="esc")
    # 3 hits -> hard.
    for _ in range(learn.SOFT_TO_HARD):
        compliance.log_event(compliance.KIND_VIOLATION, rule_id="esc", outcome="observed")
    changes = learn.escalate()
    assert len(changes) == 1 and changes[0].to_severity == policy.SEVERITY_HARD
    assert not changes[0].verify
    rule = next(r for r in policy.load_learned() if r.id == "esc")
    assert rule.severity == policy.SEVERITY_HARD and not rule.verify

    # More hits push past the verifier threshold.
    for _ in range(learn.HARD_TO_VERIFY - learn.SOFT_TO_HARD):
        compliance.log_event(compliance.KIND_VIOLATION, rule_id="esc", outcome="observed")
    changes = learn.escalate()
    assert changes and changes[0].verify
    assert next(r for r in policy.load_learned() if r.id == "esc").verify is True


def test_escalate_never_touches_seed_rules() -> None:
    for _ in range(learn.HARD_TO_VERIFY + 2):
        compliance.log_event(
            compliance.KIND_VIOLATION, rule_id="gh-pr-create-merge", outcome="escaped"
        )
    assert learn.escalate() == []  # seed rules are immutable code


def test_escalate_noop_below_threshold() -> None:
    learn.learn_violation(pattern=r"\by\b", message="m", rule_id="low")
    compliance.log_event(compliance.KIND_VIOLATION, rule_id="low", outcome="observed")
    assert learn.escalate() == []


def test_run_guard_learn_and_escalate_actions(tmp_path: Path) -> None:
    descriptor = json.dumps({"pattern": r"\bzap\b", "message": "no zapping", "rule_id": "zap"})
    assert guard.run_guard("learn", io.StringIO(descriptor), omi_dir=tmp_path) == 0
    assert any(r.id == "zap" for r in policy.load_learned())
    # Missing fields -> exit 1, nothing learned.
    assert guard.run_guard("learn", io.StringIO("{}"), omi_dir=tmp_path) == 1

    for _ in range(learn.SOFT_TO_HARD):
        compliance.log_event(compliance.KIND_VIOLATION, rule_id="zap", outcome="observed")
    assert guard.run_guard("escalate") == 0
    assert next(r for r in policy.load_learned() if r.id == "zap").severity == policy.SEVERITY_HARD
