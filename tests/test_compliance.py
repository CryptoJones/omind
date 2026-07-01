# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Aaron K. Clark
"""Tests for the compliance log + the PostToolUse violation detector (Layer E)."""

from __future__ import annotations

import io
import json

from omind import compliance, guard, hooks, policy


def test_log_event_appends_parseable_jsonl() -> None:
    compliance.log_event(
        compliance.KIND_DECISION,
        session="s",
        tool="Bash",
        command="gh pr create",
        rule_id="gh-pr-create-merge",
        severity="hard",
        outcome="deny",
    )
    lines = compliance.compliance_log_path().read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["rule_id"] == "gh-pr-create-merge"
    assert record["outcome"] == "deny"
    assert record["ts"]  # timestamped


def test_read_events_skips_bad_lines_and_honors_limit() -> None:
    path = compliance.compliance_log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        '{"rule_id": "a"}\nnot json\n\n{"rule_id": "b"}\n{"rule_id": "c"}\n',
        encoding="utf-8",
    )
    events = compliance.read_events()
    assert [e["rule_id"] for e in events] == ["a", "b", "c"]
    assert [e["rule_id"] for e in compliance.read_events(limit=2)] == ["b", "c"]


def test_recidivism_counts_exclude_the_gate() -> None:
    for rid in ("r1", "r1", "r2", "omi-gate"):
        compliance.log_event(compliance.KIND_DECISION, rule_id=rid, outcome="deny")
    assert compliance.recidivism("r1") == 2
    counts = compliance.recidivism_counts()
    assert counts["r1"] == 2 and counts["r2"] == 1
    assert "omi-gate" not in counts


def test_summary_rollup() -> None:
    compliance.log_event(compliance.KIND_DECISION, rule_id="r1", outcome="deny")
    compliance.log_event(compliance.KIND_VIOLATION, rule_id="r1", outcome="observed")
    summ = compliance.summary()
    assert summ["total"] == 2
    assert summ["denies"] == 1
    assert summ["violations"] == 1
    assert summ["last_ts"]
    assert ("r1", 2) in summ["top_rules"]


def test_detector_flags_hard_rule_escape() -> None:
    event = {
        "tool_name": "Bash",
        "session_id": "x",
        "tool_input": {"command": "gh repo delete x/y"},
    }
    assert compliance.record_post_tool(event) == 1
    rec = compliance.read_events()[-1]
    assert rec["rule_id"] == "gh-repo-delete"
    assert rec["outcome"] == "escaped"  # the block-path let a hard rule through
    assert rec["severity"] == "hard"


def test_detector_records_soft_rule_as_observed() -> None:
    policy.append_learned_rule(
        policy.Rule(
            id="soft-obs",
            pattern=r"\bnpm\s+publish\b",
            message="m",
            severity=policy.SEVERITY_SOFT,
            tier=policy.TIER_LEARNED,
        )
    )
    event = {"tool_name": "Bash", "session_id": "x", "tool_input": {"command": "npm publish"}}
    assert compliance.record_post_tool(event) == 1
    assert compliance.read_events()[-1]["outcome"] == "observed"


def test_detector_honors_github_push_opt_in_and_skips_non_bash() -> None:
    optin = {
        "tool_name": "Bash",
        "session_id": "x",
        "tool_input": {"command": "OMI_PUSH_GITHUB=1 git push github main"},
    }
    assert compliance.record_post_tool(optin) == 0  # deliberate mirror, not a violation
    read = {"tool_name": "Read", "session_id": "x", "tool_input": {"file_path": "/tmp/x"}}
    assert compliance.record_post_tool(read) == 0  # non-Bash has no command to scan


def test_guard_check_logs_policy_deny_but_not_gate_deny() -> None:
    # A bare unconsulted action is an omi-gate deny — friction, not logged.
    guard.run_guard("check", io.StringIO(json.dumps({"command": "ls", "session": "g"})))
    assert compliance.read_events() == []
    # A hard policy rule deny IS logged.
    guard.mark_consulted("g")
    guard.run_guard(
        "check", io.StringIO(json.dumps({"command": "gh repo delete x/y", "session": "g"}))
    )
    events = compliance.read_events()
    assert len(events) == 1
    assert events[0]["rule_id"] == "gh-repo-delete"
    guard.clear_gate("g")


def test_post_tool_hook_runs_the_detector(tmp_path: object) -> None:
    event = json.dumps(
        {"tool_name": "Bash", "session_id": "h", "tool_input": {"command": "gh repo delete a/b"}}
    )
    hooks.run_hook("PostToolUse", tmp_path, stdin=io.StringIO(event))  # type: ignore[arg-type]
    assert compliance.read_events()[-1]["rule_id"] == "gh-repo-delete"


def test_read_events_survives_a_torn_non_utf8_line() -> None:
    """A single bad byte in the log must not crash every consumer forever."""
    path = compliance.compliance_log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    good = json.dumps({"rule_id": "ok", "outcome": "observed"})
    path.write_bytes(good.encode() + b"\n\xff\xfe torn\n" + good.encode() + b"\n")
    events = compliance.read_events()  # must not raise
    assert [e.get("rule_id") for e in events] == ["ok", "ok"]
