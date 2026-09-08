# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Aaron K. Clark
"""Tests for the declarative harness specs + decision renderer + selftest."""

from __future__ import annotations

import io
import json
from pathlib import Path

from omind import guard, harness


def test_specs_and_fallback() -> None:
    assert harness.spec_for("hermes").block_format == harness.FMT_CLAUDE_JSON
    assert harness.spec_for("opencode").block_format == harness.FMT_JSON_SIGNAL
    assert harness.spec_for("claude").block_format == harness.FMT_EXIT2
    assert harness.spec_for("codex").block_format == harness.FMT_CODEX_HOOK
    assert harness.spec_for("gemini").block_format == harness.FMT_GEMINI
    assert harness.spec_for("openclaw").block_format == harness.FMT_OPENCLAW
    assert harness.spec_for("unknown-harness").name == "claude"  # safe fallback
    # Gemini's BeforeTool hook hard-blocks; OpenClaw is detect-only until a live
    # gateway is confirmed to enforce a deny (issue #88).
    assert harness.spec_for("gemini").can_block() is True
    assert harness.spec_for("openclaw").can_block() is False
    assert all(
        s.can_block() for k, s in harness.HARNESSES.items() if k != "openclaw"
    )


def _render(verdict: guard.Verdict, fmt: str, *, event: str = "") -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    code = harness.render_decision(verdict, fmt, out, err, event=event)
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


def test_render_codex_hook() -> None:
    deny = guard.Verdict(allow=False, reason="omi-guard (hard): nope")
    # PreToolUse (default / primary mount) -> permissionDecision deny on stdout, exit 0.
    code, out, err = _render(deny, harness.FMT_CODEX_HOOK, event="PreToolUse")
    assert code == 0 and err == ""
    assert json.loads(out) == {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": "OMI guard: omi-guard (hard): nope",
        }
    }
    # PermissionRequest -> the decision.behavior deny shape instead.
    code, out, _ = _render(deny, harness.FMT_CODEX_HOOK, event="PermissionRequest")
    assert code == 0
    assert json.loads(out) == {
        "hookSpecificOutput": {
            "hookEventName": "PermissionRequest",
            "decision": {"behavior": "deny", "message": "OMI guard: omi-guard (hard): nope"},
        }
    }
    # Unknown event falls back to the PreToolUse shape (block at the earliest point).
    _, out, _ = _render(deny, harness.FMT_CODEX_HOOK, event="")
    assert json.loads(out)["hookSpecificOutput"]["hookEventName"] == "PreToolUse"
    # allow -> empty stdout + exit 0 (Codex treats empty stdout as allow).
    code, out, err = _render(guard.Verdict(allow=True), harness.FMT_CODEX_HOOK, event="PreToolUse")
    assert code == 0 and out == "" and err == ""


def test_render_gemini() -> None:
    # Gemini's BeforeTool hook reads a JSON decision on stdout, exit 0.
    deny = guard.Verdict(allow=False, reason="omi-guard (hard): nope")
    code, out, err = _render(deny, harness.FMT_GEMINI)
    assert code == 0 and err == ""  # the deny rides in the JSON, not the exit code
    assert json.loads(out) == {"decision": "deny", "reason": "omi-guard (hard): nope"}
    # allow -> empty stdout (Gemini proceeds), and we never pollute stdout.
    code, out, err = _render(guard.Verdict(allow=True), harness.FMT_GEMINI)
    assert code == 0 and out == "" and err == ""


def test_render_openclaw_detect_only() -> None:
    # OpenClaw's gateway reads an {allow,reason,rule_id} JSON; detect-only means we
    # always exit 0 (advisory) even on a deny — issue #88.
    deny = guard.Verdict(allow=False, reason="r", rule_id="gh-pr-create-merge")
    code, out, _ = _render(deny, harness.FMT_OPENCLAW)
    assert code == 0  # detect-only: never abort the process
    assert json.loads(out) == {"allow": False, "reason": "r", "rule_id": "gh-pr-create-merge"}
    code, out, _ = _render(guard.Verdict(allow=True), harness.FMT_OPENCLAW)
    assert code == 0 and json.loads(out)["allow"] is True


def test_selftest_all_pass() -> None:
    results = harness.run_selftest()
    assert {r["harness"] for r in results} == {
        "claude",
        "hermes",
        "opencode",
        "codex",
        "gemini",
        "deepseek",
        "poolside",
        "openclaw",
    }
    assert all(r["ok"] for r in results)
    assert all(r["blocked"] for r in results)  # every canned case is a hard rule
    # the rendered block carries the right shape per harness
    by = {r["harness"]: r for r in results}
    assert by["hermes"]["format"] == harness.FMT_CLAUDE_JSON
    assert by["opencode"]["format"] == harness.FMT_JSON_SIGNAL
    assert by["codex"]["format"] == harness.FMT_CODEX_HOOK
    assert "permissionDecision" in by["codex"]["rendered"]  # codex deny shape rendered
    assert by["gemini"]["format"] == harness.FMT_GEMINI
    assert '"decision": "deny"' in by["gemini"]["rendered"]  # gemini deny shape rendered
    assert by["poolside"]["format"] == harness.FMT_POOLSIDE
    assert '"permission_decision": "deny"' in by["poolside"]["rendered"]  # snake_case deny
    assert by["openclaw"]["format"] == harness.FMT_OPENCLAW


def test_run_guard_selftest_action() -> None:
    assert guard.run_guard("selftest") == 0


# -- #311: Poolside pool CLI -------------------------------------------------


def test_poolside_spec_and_render() -> None:
    spec = harness.spec_for("poolside")
    assert spec.block_format == harness.FMT_POOLSIDE and spec.can_block() is True
    deny = guard.Verdict(allow=False, reason="omi-guard (hard): no", rule_id="gh-repo-delete")
    code, out, err = _render(deny, harness.FMT_POOLSIDE)
    assert code == 0 and err == ""  # the deny is in the JSON, not the exit code
    payload = json.loads(out)["hook_specific_output"]
    assert payload == {
        "hook_event_name": "PreToolUse",
        "permission_decision": "deny",
        "permission_decision_reason": "BLOCKED by omi-guard (hard): no",
    }
    # allow -> empty stdout, exit 0 (pool treats empty stdout as "observe only").
    code, out, err = _render(guard.Verdict(allow=True), harness.FMT_POOLSIDE)
    assert code == 0 and out == "" and err == ""


def test_translate_event_maps_poolside_shape_onto_claude() -> None:
    raw = {
        "hook_event_name": "PostToolUse",
        "tool_name": "omi__search-vault",
        "tool_input": {"query": "x", "limit": 1},
        "tool_output": "3 hits",
        "session_id": "s",
    }
    event = harness.translate_event("poolside", raw)
    assert event["tool_name"] == "mcp__omi__search-vault"  # consult detectors match
    assert event["tool_response"] == "3 hits"  # accounting/journal read tool_response
    assert raw["tool_name"] == "omi__search-vault"  # input untouched
    shell = harness.translate_event(
        "poolside", {"tool_name": "shell", "tool_input": {"cmd": "ls", "description": "d"}}
    )
    assert shell["tool_input"] == {"cmd": "ls", "description": "d", "command": "ls"}
    # Native pool tools (single underscore) are not MCP and are left alone; an
    # already-prefixed name is not double-prefixed.
    assert harness.translate_event("poolside", {"tool_name": "todo_action"})["tool_name"] == (
        "todo_action"
    )
    assert (
        harness.translate_event("poolside", {"tool_name": "mcp__omi__help"})["tool_name"]
        == "mcp__omi__help"
    )
    # Identity for every other harness.
    assert harness.translate_event("claude", raw) is raw


def test_translate_event_recovers_poolside_prompt_from_trajectory(tmp_path: Path) -> None:
    traj = tmp_path / "trajectory-standalone_abc.ndjson"
    older = {
        "type": "tool_call.inference.start",
        "tool_call_inference_start": {
            "chat_completion_request": {
                "messages": [
                    {"role": "system", "content": "sys"},
                    {
                        "role": "user",
                        "content": "<context>c</context>\n<user_query>\nold ask\n</user_query>",
                    },
                ]
            }
        },
    }
    newer = json.loads(json.dumps(older))
    newer["tool_call_inference_start"]["chat_completion_request"]["messages"][1]["content"] = (
        "<context>c</context>\n\n<user_query>\nrotate the ronin28 wifi password\n</user_query>"
    )
    traj.write_text(
        json.dumps(older)
        + "\n"
        + json.dumps({"type": "tool_call.result"})
        + "\n"
        + json.dumps(newer)
        + "\n",
        encoding="utf-8",
    )
    event = harness.translate_event(
        "poolside",
        {"hook_event_name": "UserPromptSubmit", "session_id": "s", "trajectory_path": str(traj)},
    )
    assert event["prompt"] == "rotate the ronin28 wifi password"  # the LAST query wins
    # A prompt already present is never overridden; a missing file is not an error.
    kept = harness.translate_event(
        "poolside",
        {"hook_event_name": "UserPromptSubmit", "prompt": "given", "trajectory_path": str(traj)},
    )
    assert kept["prompt"] == "given"
    absent = harness.translate_event(
        "poolside",
        {"hook_event_name": "UserPromptSubmit", "trajectory_path": str(tmp_path / "nope")},
    )
    assert "prompt" not in absent
    assert harness.poolside_last_prompt(None) == ""


def test_render_context_and_stop_block_per_harness() -> None:
    claude = json.loads(harness.render_context("claude", "SessionStart", "hi"))
    assert claude == {
        "hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": "hi"}
    }
    pool = json.loads(harness.render_context("poolside", "UserPromptSubmit", "hi"))
    assert pool == {
        "hook_specific_output": {"hook_event_name": "UserPromptSubmit", "additional_context": "hi"}
    }
    assert json.loads(harness.render_stop_block("claude", "keep going")) == {
        "decision": "block",
        "reason": "keep going",
    }
    stop = json.loads(harness.render_stop_block("poolside", "keep going"))
    assert stop["continue"] is True and stop["reason"] == "keep going"
    assert stop["hook_specific_output"]["additional_context"] == "keep going"
    # Unknown harnesses fall back to the Claude shape, like spec_for().
    assert "hookSpecificOutput" in harness.render_context("mystery", "SessionStart", "x")
