# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Aaron K. Clark
"""Tests for the harness-agnostic guard adapter (Phase 4)."""

from __future__ import annotations

import io
import json

import pytest

from omind import adapters, guard


def test_normalize_claude_shape() -> None:
    action = adapters.normalize_action(
        {"tool_name": "Bash", "tool_input": {"command": "ls"}, "session_id": "s"}
    )
    assert action == {"tool": "Bash", "command": "ls", "session": "s", "is_omi_consult": False}


def test_normalize_other_harness_shapes() -> None:
    # Hermes/OpenCode-ish: top-level tool/command/session.
    action = adapters.normalize_action({"tool": "shell", "command": "gh pr create", "session": "h"})
    assert action["command"] == "gh pr create" and action["session"] == "h"
    # An mcp__omi__ tool is recognized as a consult regardless of harness.
    consult = adapters.normalize_action({"name": "mcp__omi__search-vault", "session": "h"})
    assert consult["is_omi_consult"] is True
    # `args` is accepted as the command when no command/tool_input is present.
    assert adapters.normalize_action({"args": "rm -rf /"})["command"] == "rm -rf /"


def test_run_adapter_hard_block_denies_any_harness() -> None:
    event = io.StringIO(json.dumps({"tool": "shell", "command": "gh pr create", "session": "a1"}))
    assert adapters.run_adapter(event) == 2  # hard rule fires without a consult too


def test_run_adapter_consult_clears_then_gate_allows() -> None:
    guard.clear_gate("a2")
    blocked = io.StringIO(json.dumps({"tool": "shell", "command": "ls", "session": "a2"}))
    assert adapters.run_adapter(blocked) == 2  # gate closed
    consult = io.StringIO(json.dumps({"name": "mcp__omi__read-note", "session": "a2"}))
    assert adapters.run_adapter(consult) == 0  # consult clears the gate
    assert guard.consulted_this_turn("a2")
    allowed = io.StringIO(json.dumps({"tool": "shell", "command": "ls", "session": "a2"}))
    assert adapters.run_adapter(allowed) == 0  # now allowed for the turn
    guard.clear_gate("a2")


def test_run_guard_adapter_action_dispatches() -> None:
    guard.clear_gate("a3")
    event = io.StringIO(json.dumps({"tool": "shell", "command": "ls", "session": "a3"}))
    assert guard.run_guard("adapter", event) == 2
    guard.clear_gate("a3")


# -- 2.41.0: per-harness rendering ------------------------------------------


def test_run_adapter_hermes_renders_claude_json(capsys: pytest.CaptureFixture[str]) -> None:
    event = io.StringIO(json.dumps({"tool": "shell", "command": "gh pr create", "session": "h1"}))
    code = adapters.run_adapter(event, harness="hermes")
    out = capsys.readouterr().out
    assert code == 0  # block is in the JSON, not the exit code
    assert json.loads(out)["decision"] == "block"


def test_run_adapter_opencode_renders_json_signal(capsys: pytest.CaptureFixture[str]) -> None:
    payload = {"tool": "bash", "command": "gh repo delete a/b", "session": "o1"}
    code = adapters.run_adapter(io.StringIO(json.dumps(payload)), harness="opencode")
    out = capsys.readouterr().out
    assert code == 2
    assert json.loads(out)["allow"] is False


# -- 2.41.3: Codex (snake_case stdin, per-event deny shape) ------------------


def test_normalize_codex_shape() -> None:
    # Codex sends Claude-shaped snake_case fields; normalize handles them as-is.
    action = adapters.normalize_action(
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "gh repo delete a/b"},
            "session_id": "cx",
            "tool_use_id": "t1",
        }
    )
    assert action["command"] == "gh repo delete a/b" and action["session"] == "cx"


def test_run_adapter_codex_pretooluse_deny(capsys: pytest.CaptureFixture[str]) -> None:
    payload = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": "gh repo delete a/b"},
        "session_id": "cx1",
    }
    code = adapters.run_adapter(io.StringIO(json.dumps(payload)), harness="codex")
    out = capsys.readouterr().out
    assert code == 0  # the deny rides in the JSON, not the exit code
    hso = json.loads(out)["hookSpecificOutput"]
    assert hso["hookEventName"] == "PreToolUse" and hso["permissionDecision"] == "deny"


def test_run_adapter_codex_permissionrequest_deny(capsys: pytest.CaptureFixture[str]) -> None:
    payload = {
        "hook_event_name": "PermissionRequest",
        "tool_name": "Bash",
        "tool_input": {"command": "gh repo delete a/b"},
        "session_id": "cx2",
    }
    adapters.run_adapter(io.StringIO(json.dumps(payload)), harness="codex")
    hso = json.loads(capsys.readouterr().out)["hookSpecificOutput"]
    assert hso["hookEventName"] == "PermissionRequest"
    assert hso["decision"] == {"behavior": "deny", "message": hso["decision"]["message"]}


def test_run_adapter_codex_allow_emits_nothing(capsys: pytest.CaptureFixture[str]) -> None:
    guard.clear_gate("cx3")
    consult = io.StringIO(json.dumps({"tool_name": "mcp__omi__read-note", "session_id": "cx3"}))
    assert adapters.run_adapter(consult, harness="codex") == 0  # consult clears the gate
    capsys.readouterr()
    payload = {"hook_event_name": "PreToolUse", "tool_name": "Bash",
               "tool_input": {"command": "ls"}, "session_id": "cx3"}
    code = adapters.run_adapter(io.StringIO(json.dumps(payload)), harness="codex")
    assert code == 0 and capsys.readouterr().out == ""  # allow -> empty stdout
    guard.clear_gate("cx3")


# -- 2.44.0: Gemini CLI (BeforeTool, single-underscore MCP names) ------------


def test_normalize_gemini_shape() -> None:
    # Gemini's run_shell_command carries the command in tool_input.
    action = adapters.normalize_action(
        {
            "hook_event_name": "BeforeTool",
            "tool_name": "run_shell_command",
            "tool_input": {"command": "gh pr merge 5"},
            "session_id": "gm",
        }
    )
    assert action["command"] == "gh pr merge 5" and action["session"] == "gm"
    # Gemini namespaces MCP tools with single underscores (mcp_<server>_<tool>);
    # the consult must still be recognized so it can clear the gate.
    consult = adapters.normalize_action(
        {"tool_name": "mcp_omi_search-vault", "session_id": "gm"}
    )
    assert consult["is_omi_consult"] is True


def test_run_adapter_gemini_deny_emits_decision_json(capsys: pytest.CaptureFixture[str]) -> None:
    payload = {
        "hook_event_name": "BeforeTool",
        "tool_name": "run_shell_command",
        "tool_input": {"command": "gh repo delete a/b"},
        "session_id": "gm1",
    }
    code = adapters.run_adapter(io.StringIO(json.dumps(payload)), harness="gemini")
    out = capsys.readouterr().out
    assert code == 0  # deny rides in the JSON, not the exit code
    assert json.loads(out)["decision"] == "deny"


def test_run_adapter_gemini_consult_clears_gate(capsys: pytest.CaptureFixture[str]) -> None:
    guard.clear_gate("gm2")
    consult = io.StringIO(json.dumps({"tool_name": "mcp_omi_read-note", "session_id": "gm2"}))
    assert adapters.run_adapter(consult, harness="gemini") == 0  # Gemini consult clears it
    assert guard.consulted_this_turn("gm2")
    guard.clear_gate("gm2")


# -- 2.44.0: OpenClaw gateway (detect-only) ---------------------------------


def test_run_adapter_openclaw_detect_only(capsys: pytest.CaptureFixture[str]) -> None:
    payload = {"tool": "shell", "command": "gh repo delete a/b", "session": "oc1"}
    code = adapters.run_adapter(io.StringIO(json.dumps(payload)), harness="openclaw")
    out = capsys.readouterr().out
    assert code == 0  # detect-only: the verdict is advisory, never aborts
    body = json.loads(out)
    assert body["allow"] is False and body["rule_id"]  # the deny is still reported
