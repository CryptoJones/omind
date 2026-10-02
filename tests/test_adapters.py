# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Aaron K. Clark
"""Tests for the harness-agnostic guard adapter (Phase 4)."""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import pytest

from omind import adapters, guard


def test_normalize_claude_shape() -> None:
    action = adapters.normalize_action(
        {"tool_name": "Bash", "tool_input": {"command": "ls"}, "session_id": "s"}
    )
    assert action == {
        "tool": "Bash",
        "command": "ls",
        "session": "s",
        "is_omi_consult": False,
        "file_path": "",
        "prompt": "",
        "transcript_path": "",
        "consult_target": "",
        "consult_kind": "search",
        "cwd": "",
    }


def test_normalize_other_harness_shapes() -> None:
    # Hermes/OpenCode-ish: top-level tool/command/session.
    action = adapters.normalize_action({"tool": "shell", "command": "gh pr create", "session": "h"})
    assert action["command"] == "gh pr create" and action["session"] == "h"
    # An mcp__omi__ tool is recognized as a consult regardless of harness.
    consult = adapters.normalize_action(
        {"name": "mcp__omi__read-note", "tool_input": {"name": "Operational Rules"}, "session": "h"}
    )
    assert consult["is_omi_consult"] is True
    assert consult["consult_target"] == "Operational Rules"
    # `args` is accepted as the command when no command/tool_input is present.
    assert adapters.normalize_action({"args": "rm -rf /"})["command"] == "rm -rf /"


def test_normalize_accepts_array_shaped_args() -> None:
    """A list argv must reach the guard as a command, not be dropped to ''."""
    action = adapters.normalize_action(
        {"tool": "shell", "args": ["gh", "repo", "delete", "a/b"], "session": "z"}
    )
    assert action["command"] == "gh repo delete a/b"
    inner = adapters.normalize_action({"tool": "shell", "input": {"command": "rm -rf /"}})
    assert inner["command"] == "rm -rf /"


def test_run_adapter_hard_block_denies_any_harness() -> None:
    event = io.StringIO(json.dumps({"tool": "shell", "command": "gh pr create", "session": "a1"}))
    assert adapters.run_adapter(event) == 2  # hard rule fires without a consult too


def test_run_adapter_fails_closed_on_unparseable_event() -> None:
    """A mangled event in an enforcement component must block, not wave through."""
    assert adapters.run_adapter(io.StringIO("{not valid json")) == 2
    assert adapters.run_adapter(io.StringIO("[1, 2, 3]")) == 2  # not an object
    # A genuinely empty stream is not an error — nothing to guard.
    assert adapters.run_adapter(io.StringIO("   ")) == 0


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


def test_normalize_preserves_prompt_context() -> None:
    action = adapters.normalize_action(
        {
            "tool_name": "Bash",
            "tool_input": {"command": "echo hi"},
            "session_id": "cxp",
            "prompt": "I give you explicit permission to make the change.",
        }
    )
    assert action["prompt"] == "I give you explicit permission to make the change."


# -- #311: Poolside pool CLI (PreToolUse hook, snake_case decision JSON) ------


def test_run_adapter_poolside_deny_emits_snake_case_decision(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # pool's shell tool carries the command line as `cmd`, not `command`.
    payload = {
        "hook_api_version": "1.0",
        "hook_event_name": "PreToolUse",
        "tool_name": "shell",
        "tool_input": {"cmd": "gh repo delete a/b", "description": "nuke it"},
        "session_id": "ps1",
        "tool_call_id": "chatcmpl-tool-1",
    }
    code = adapters.run_adapter(io.StringIO(json.dumps(payload)), harness="poolside")
    out = capsys.readouterr().out
    assert code == 0  # the deny rides in the JSON, not the exit code
    decision = json.loads(out)["hook_specific_output"]
    assert decision["permission_decision"] == "deny"
    assert decision["hook_event_name"] == "PreToolUse"
    assert "BLOCKED by" in decision["permission_decision_reason"]


def test_run_adapter_poolside_consult_clears_gate(capsys: pytest.CaptureFixture[str]) -> None:
    # pool names MCP tools `<server>__<tool>` with no `mcp__` prefix; the consult
    # must still be recognized or the gate could never clear under pool.
    guard.clear_gate("ps2")
    consult = io.StringIO(
        json.dumps(
            {
                "hook_event_name": "PreToolUse",
                "tool_name": "omi__search-vault",
                "tool_input": {"query": "poolside hooks", "limit": 1},
                "session_id": "ps2",
            }
        )
    )
    assert adapters.run_adapter(consult, harness="poolside") == 0
    assert capsys.readouterr().out == ""  # allow -> empty stdout
    assert guard.consulted_this_turn("ps2")
    guard.clear_gate("ps2")


def test_normalize_action_reads_poolside_cmd_key() -> None:
    action = adapters.normalize_action(
        {"tool_name": "shell", "tool_input": {"cmd": "gh pr merge 5"}, "session_id": "ps3"}
    )
    assert action["command"] == "gh pr merge 5"


# -- Antigravity (agy) --------------------------------------------------------


def test_normalize_agy_shape() -> None:
    action = adapters.normalize_action(
        {
            "toolCall": {
                "name": "run_command",
                "args": {"CommandLine": "git push origin main --force"},
            },
            "conversationId": "agy-sess-1",
        }
    )
    assert action["command"] == "git push origin main --force"
    assert action["session"] == "agy-sess-1"

    consult = adapters.normalize_action(
        {
            "toolCall": {
                "name": "omi_read-note",
                "args": {"name": "Instructions"},
            },
            "conversationId": "agy-sess-2",
        }
    )
    assert consult["is_omi_consult"] is True
    assert consult["consult_target"] == "Instructions"


def test_run_adapter_agy_deny_emits_json_decision(
    capsys: pytest.CaptureFixture[str],
) -> None:
    payload = {
        "toolCall": {
            "name": "run_command",
            "args": {"CommandLine": "gh repo delete a/b"},
        },
        "conversationId": "agy-deny-1",
    }
    code = adapters.run_adapter(io.StringIO(json.dumps(payload)), harness="agy")
    out = capsys.readouterr().out
    assert code == 0  # Antigravity expects exit code 0
    decision = json.loads(out)
    assert decision["decision"] == "deny"
    assert "omi-guard" in decision["reason"]


def test_run_adapter_agy_consult_clears_gate(
    capsys: pytest.CaptureFixture[str],
) -> None:
    guard.clear_gate("agy-gate-1")
    consult = io.StringIO(
        json.dumps(
            {
                "toolCall": {
                    "name": "omi_search-vault",
                    "args": {"query": "auth"},
                },
                "conversationId": "agy-gate-1",
            }
        )
    )
    assert adapters.run_adapter(consult, harness="agy") == 0
    out = capsys.readouterr().out
    assert json.loads(out) == {"decision": "allow"}
    assert guard.consulted_this_turn("agy-gate-1")

    # Following action in same turn is allowed and renders {"decision": "allow"}
    allowed = io.StringIO(
        json.dumps(
            {
                "toolCall": {
                    "name": "run_command",
                    "args": {"CommandLine": "ls -la"},
                },
                "conversationId": "agy-gate-1",
            }
        )
    )
    assert adapters.run_adapter(allowed, harness="agy") == 0
    out2 = capsys.readouterr().out
    assert json.loads(out2) == {"decision": "allow"}
    guard.clear_gate("agy-gate-1")



def test_normalize_carries_the_transcript_path_for_midturn_authorization() -> None:
    """#290: without it the guard cannot see a message the human sent mid-turn."""
    from omind import adapters

    event = {"tool_name": "Bash", "tool_input": {"command": "ls"}, "transcript_path": "/t.jsonl"}
    assert adapters.normalize_action(event)["transcript_path"] == "/t.jsonl"
    assert adapters.normalize_action({"tool_name": "Bash"})["transcript_path"] == ""


def test_run_adapter_fails_open_when_a_classifier_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#420: every harness reaches the core through check_action, so a crashing
    classifier must render an ALLOW, not escape as a traceback."""

    def boom(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("classifier exploded")

    monkeypatch.setattr(guard, "_repo_root_for_action", boom)
    guard.clear_gate("a420")
    event = io.StringIO(json.dumps({"tool": "shell", "command": "ls", "session": "a420"}))
    assert adapters.run_adapter(event) == 0


def _adapter_error_events(session: str) -> list[dict[str, object]]:
    from omind import compliance

    return [
        e
        for e in compliance.read_events()
        if e.get("rule_id") == guard.GUARD_ERROR_RULE and e.get("session") == session
    ]


def test_run_adapter_agy_fails_open_when_translation_raises(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """#420 review: translate_event runs before check_action. A transcript line
    that is valid JSON but not an object made agy_last_prompt raise
    AttributeError; the agy adapter must render ALLOW in its own format and log."""
    transcript = tmp_path / "agy.jsonl"
    transcript.write_text('["USER_INPUT"]\n', encoding="utf-8")
    payload = {
        "toolCall": {"name": "run_command", "args": {"CommandLine": "ls"}},
        "conversationId": "agy-420",
        "transcriptPath": str(transcript),
    }
    code = adapters.run_adapter(io.StringIO(json.dumps(payload)), harness="agy")
    captured = capsys.readouterr()
    assert code == 0
    assert json.loads(captured.out) == {"decision": "allow"}
    assert "internal error in guard check" in captured.err
    events = _adapter_error_events("agy-420")
    assert len(events) == 1
    assert events[0]["outcome"] == "fail-open"
    assert "AttributeError" in str(events[0]["detail"])


def test_run_adapter_agy_fail_open_still_honours_hard_rules(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    transcript = tmp_path / "agy.jsonl"
    transcript.write_text('["USER_INPUT"]\n', encoding="utf-8")
    payload = {
        "toolCall": {"name": "run_command", "args": {"CommandLine": "gh repo delete a/b"}},
        "conversationId": "agy-420h",
        "transcriptPath": str(transcript),
    }
    code = adapters.run_adapter(io.StringIO(json.dumps(payload)), harness="agy")
    assert code == 0
    assert json.loads(capsys.readouterr().out)["decision"] == "deny"


def test_run_adapter_opencode_fails_open_when_normalize_raises(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A non-exit-code harness (OpenCode's JSON signal) gets an allow signal."""

    def boom(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("normalize exploded")

    monkeypatch.setattr(adapters, "normalize_action", boom)
    payload = {"tool": "bash", "command": "ls", "session": "oc-420"}
    code = adapters.run_adapter(io.StringIO(json.dumps(payload)), harness="opencode")
    captured = capsys.readouterr()
    assert code == 0
    assert json.loads(captured.out)["allow"] is True
    events = _adapter_error_events("oc-420")
    assert len(events) == 1
    assert "RuntimeError: normalize exploded" in str(events[0]["detail"])


def test_run_adapter_fails_open_when_render_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from omind import harness as harness_mod

    real = harness_mod.render_decision
    calls: list[bool] = []

    def flaky(*args: Any, **kwargs: Any) -> int:
        if not calls:
            calls.append(True)
            raise RuntimeError("render exploded")
        return real(*args, **kwargs)

    monkeypatch.setattr(harness_mod, "render_decision", flaky)
    monkeypatch.setattr(guard, "check_action", lambda *_a, **_k: guard.Verdict(allow=True))
    payload = {"tool": "shell", "command": "ls", "session": "r420"}
    assert adapters.run_adapter(io.StringIO(json.dumps(payload))) == 0
    assert len(_adapter_error_events("r420")) == 1
