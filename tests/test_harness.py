# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Aaron K. Clark
"""Tests for the declarative harness specs + decision renderer + selftest."""

from __future__ import annotations

import io
import json

from omind import guard, harness


def test_specs_and_fallback() -> None:
    assert harness.spec_for("hermes").block_format == harness.FMT_CLAUDE_JSON
    assert harness.spec_for("opencode").block_format == harness.FMT_JSON_SIGNAL
    assert harness.spec_for("claude").block_format == harness.FMT_EXIT2
    assert harness.spec_for("unknown-harness").name == "claude"  # safe fallback
    assert all(s.can_block() for s in harness.HARNESSES.values())


def _render(verdict: guard.Verdict, fmt: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    code = harness.render_decision(verdict, fmt, out, err)
    return code, out.getvalue(), err.getvalue()


def test_render_exit2() -> None:
    deny = guard.Verdict(allow=False, reason="omi-guard (hard): no")
    code, out, err = _render(deny, harness.FMT_EXIT2)
    assert code == 2 and "BLOCKED by omi-guard (hard): no" in err and out == ""
    code, out, err = _render(guard.Verdict(allow=True), harness.FMT_EXIT2)
    assert code == 0 and out == "" and err == ""


def test_render_claude_json_for_hermes() -> None:
    code, out, err = _render(
        guard.Verdict(allow=False, reason="omi-guard (hard): nope"), harness.FMT_CLAUDE_JSON
    )
    assert code == 0  # the block is in the JSON, not the exit code
    assert json.loads(out) == {"decision": "block", "reason": "omi-guard (hard): nope"}
    code, out, err = _render(guard.Verdict(allow=True), harness.FMT_CLAUDE_JSON)
    assert code == 0 and out == ""  # allow -> no decision emitted


def test_render_json_signal_for_opencode() -> None:
    deny = guard.Verdict(allow=False, reason="r", rule_id="gh-pr-create-merge")
    code, out, _ = _render(deny, harness.FMT_JSON_SIGNAL)
    assert code == 2
    assert json.loads(out) == {"allow": False, "reason": "r", "rule_id": "gh-pr-create-merge"}
    code, out, _ = _render(guard.Verdict(allow=True), harness.FMT_JSON_SIGNAL)
    assert code == 0 and json.loads(out)["allow"] is True


def test_selftest_all_pass() -> None:
    results = harness.run_selftest()
    assert {r["harness"] for r in results} == {"claude", "hermes", "opencode"}
    assert all(r["ok"] for r in results)
    assert all(r["blocked"] for r in results)  # every canned case is a hard rule
    # the rendered block carries the right shape per harness
    by = {r["harness"]: r for r in results}
    assert by["hermes"]["format"] == harness.FMT_CLAUDE_JSON
    assert by["opencode"]["format"] == harness.FMT_JSON_SIGNAL


def test_run_guard_selftest_action() -> None:
    assert guard.run_guard("selftest") == 0
