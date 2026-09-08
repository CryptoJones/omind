# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Aaron K. Clark
"""Declarative per-harness adapter specs — Phase 4 of the enforcement roadmap.

The guard DECISION (:mod:`omind.guard`) is harness-agnostic. What differs per
harness is only three things: (1) how its pre-action event is shaped, (2) whether
it can **hard-block** an action at all, and (3) how a block is signalled back.
Capturing those as DATA — a :class:`HarnessSpec` — keeps each new harness a
described, tested unit instead of a bespoke adapter, and lets the core **degrade
gracefully** where a harness can only detect (log/warn), not block.

So a rule learned under Claude Code enforces identically under Hermes and
OpenCode, once each harness's hook is wired to pipe its event to
``omind guard adapter --harness <name>``.
"""

from __future__ import annotations

import io
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

from omind.guard import Verdict

#: Capability: can this harness HARD-BLOCK an action, or only DETECT it (log/warn)?
CAP_HARD_BLOCK = "hard-block"
CAP_DETECT_ONLY = "detect-only"

#: Block-output format the adapter renders a deny in.
FMT_EXIT2 = "exit2"  # Claude Code / shell hook: stderr reason + exit 2
FMT_CLAUDE_JSON = "claude_json"  # Hermes pre_tool_call: {"decision":"block","reason"} on stdout
FMT_JSON_SIGNAL = "json_signal"  # OpenCode plugin reads {allow, reason} JSON and throws in JS
FMT_CODEX_HOOK = "codex_hook"  # Codex PreToolUse/PermissionRequest: hookSpecificOutput deny JSON
FMT_GEMINI = "gemini"  # Gemini CLI BeforeTool: {"decision":"deny","reason"} on stdout, exit 0
FMT_OPENCLAW = "openclaw"  # OpenClaw gateway: {allow,reason,rule_id} JSON (detect-only, exit 0)
FMT_POOLSIDE = "poolside"  # Poolside pool hook: snake_case hook_specific_output deny JSON, exit 0


@dataclass(frozen=True)
class HarnessSpec:
    """How one harness is wired to the harness-agnostic guard core."""

    name: str
    capability: str
    block_format: str
    description: str = ""

    def can_block(self) -> bool:
        return self.capability == CAP_HARD_BLOCK


HARNESSES: dict[str, HarnessSpec] = {
    "claude": HarnessSpec("claude", CAP_HARD_BLOCK, FMT_EXIT2, "Claude Code PreToolUse('*')"),
    "hermes": HarnessSpec("hermes", CAP_HARD_BLOCK, FMT_CLAUDE_JSON, "Hermes pre_tool_call hook"),
    "opencode": HarnessSpec(
        "opencode", CAP_HARD_BLOCK, FMT_JSON_SIGNAL, "OpenCode plugin tool.execute.before"
    ),
    "codex": HarnessSpec(
        "codex", CAP_HARD_BLOCK, FMT_CODEX_HOOK, "Codex PreToolUse/PermissionRequest hook"
    ),
    "gemini": HarnessSpec("gemini", CAP_HARD_BLOCK, FMT_GEMINI, "Gemini CLI BeforeTool hook"),
    # DSH (DeepSeek Harness) hard-blocks at the `tools/pre-execute` waterfall via
    # its Cordis guard plugin (omind-guard.dsh.js), which denies before dispatch.
    # Uses json_signal (exit-code + {allow,reason,rule_id} JSON) like OpenCode,
    # so the JS plugin can enforce both hard-rule denies AND the consult-gate.
    "deepseek": HarnessSpec(
        "deepseek", CAP_HARD_BLOCK, FMT_JSON_SIGNAL, "DSH tools/pre-execute guard plugin"
    ),
    # Poolside's `pool` CLI (>= 1.0.16) ships Claude-shaped lifecycle hooks under
    # a `hooks:` key in settings.yaml (#311; supersedes the ACP-proxy plan in
    # #304). PreToolUse hard-blocks: a snake_case `hook_specific_output` deny on
    # stdout (exit 0) drops the tool call and the reason is shown to the model —
    # live-verified against pool 1.0.16 on 2026-09-08.
    "poolside": HarnessSpec(
        "poolside", CAP_HARD_BLOCK, FMT_POOLSIDE, "Poolside pool PreToolUse hook"
    ),
    # Detect-only until a live gateway is confirmed to enforce a deny (issue #88):
    # the verdict is rendered + sent, but we don't yet CLAIM hard-block capability.
    "openclaw": HarnessSpec(
        "openclaw", CAP_DETECT_ONLY, FMT_OPENCLAW, "OpenClaw POST /hooks/agent gateway"
    ),
}


def spec_for(harness: str) -> HarnessSpec:
    """The spec for ``harness`` (falls back to the Claude/exit-2 contract)."""
    return HARNESSES.get(harness, HARNESSES["claude"])


def render_decision(
    verdict: Verdict, fmt: str, out: TextIO, err: TextIO, *, event: str = ""
) -> int:
    """Render a guard :class:`~omind.guard.Verdict` in a harness's block-output
    format; return the process exit code the adapter should exit with.

    ``event`` is the harness's hook-event name (only Codex needs it — its deny
    shape differs between ``PreToolUse`` and ``PermissionRequest``).
    """
    if fmt == FMT_CODEX_HOOK:
        # Codex reads a camelCase `hookSpecificOutput` JSON on stdout (exit 0; the
        # deny lives in the JSON, NOT the exit code). Empty stdout + exit 0 = allow.
        # PreToolUse and PermissionRequest take different deny shapes.
        if verdict.allow:
            return 0
        reason = f"OMI guard: {verdict.reason}"
        if event == "PermissionRequest":
            payload = {
                "hookSpecificOutput": {
                    "hookEventName": "PermissionRequest",
                    "decision": {"behavior": "deny", "message": reason},
                }
            }
        else:  # PreToolUse (the primary mount) or unknown → block at the tool call
            payload = {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": reason,
                }
            }
        out.write(json.dumps(payload) + "\n")
        return 0
    if fmt == FMT_CLAUDE_JSON:
        # Hermes reads the hook's stdout JSON. Emit a block decision on deny; on
        # allow emit nothing (Hermes treats an absent decision as allow).
        if not verdict.allow:
            out.write(json.dumps({"decision": "block", "reason": verdict.reason}) + "\n")
        return 0
    if fmt == FMT_JSON_SIGNAL:
        # The OpenCode JS plugin reads this {allow, reason, rule_id} and throws on
        # deny. rule_id lets the plugin enforce only HARD-RULE denies (not the
        # consult-gate) where the gate's turn/consult signals aren't verified.
        out.write(
            json.dumps(
                {"allow": verdict.allow, "reason": verdict.reason, "rule_id": verdict.rule_id}
            )
            + "\n"
        )
        return verdict.exit_code
    if fmt == FMT_GEMINI:
        # Gemini CLI's BeforeTool hook reads a JSON decision on stdout (exit 0; the
        # deny rides in the JSON). Emit ONLY the decision JSON on deny — any other
        # stdout breaks Gemini's parser; on allow emit nothing (= proceed).
        if not verdict.allow:
            out.write(json.dumps({"decision": "deny", "reason": verdict.reason}) + "\n")
        return 0
    if fmt == FMT_POOLSIDE:
        # Poolside reads a snake_case decision object on stdout (exit 0; the deny
        # rides in the JSON). `permission_decision: deny` drops the call and the
        # reason is shown to the model verbatim. Empty stdout + exit 0 = allow.
        # (Exit 2 + stderr also blocks, but the JSON path is the documented
        # PreToolUse contract and cannot be confused with a crashed hook.)
        if verdict.allow:
            return 0
        out.write(
            json.dumps(
                {
                    "hook_specific_output": {
                        "hook_event_name": "PreToolUse",
                        "permission_decision": "deny",
                        "permission_decision_reason": f"BLOCKED by {verdict.reason}",
                    }
                }
            )
            + "\n"
        )
        return 0
    if fmt == FMT_OPENCLAW:
        # OpenClaw's gateway reads a JSON verdict. DETECT-ONLY for now (issue #88):
        # we can't live-verify the gateway enforces a deny, so always exit 0 and
        # let the verdict be advisory until hard-block is proven against a gateway.
        out.write(
            json.dumps(
                {"allow": verdict.allow, "reason": verdict.reason, "rule_id": verdict.rule_id}
            )
            + "\n"
        )
        return 0
    # FMT_EXIT2 (default): stderr reason + exit 2 — the Claude/shell contract.
    if not verdict.allow:
        err.write(f"BLOCKED by {verdict.reason}\n")
    return verdict.exit_code


# -- per-harness event + output shapes ------------------------------------------

#: Poolside's ``UserPromptSubmit`` payload can arrive without ``prompt`` (it did
#: under ``pool exec`` 1.0.16); the user query is still recoverable from the
#: session's trajectory file, whose inference records wrap it in these tags.
_POOLSIDE_QUERY_OPEN = "<user_query>"
_POOLSIDE_QUERY_CLOSE = "</user_query>"
#: Read at most this much of the trajectory tail when recovering the prompt.
_POOLSIDE_TRAJECTORY_TAIL = 4 * 1024 * 1024


def poolside_last_prompt(trajectory_path: object) -> str:
    """The most recent user query in a Poolside trajectory (``.ndjson``), or
    ``""``. Best-effort and never raises: a missing prompt only means the turn
    preflight runs without a task (the gate then stays strict, as it does under
    Claude Code when nothing was captured)."""
    if not isinstance(trajectory_path, str) or not trajectory_path:
        return ""
    try:
        path = Path(trajectory_path)
        size = path.stat().st_size
        with path.open("rb") as fh:
            if size > _POOLSIDE_TRAJECTORY_TAIL:
                fh.seek(size - _POOLSIDE_TRAJECTORY_TAIL)
            tail = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return ""
    for line in reversed(tail.splitlines()):
        if _POOLSIDE_QUERY_OPEN not in line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue  # a partial first line of the tail window
        found = _last_user_content(record)
        if found:
            return found
    return ""


def _last_user_content(node: Any) -> str:
    """Walk a trajectory record for the last ``{"role": "user", "content": ...}``
    and return its query text (the ``<user_query>`` body when wrapped)."""
    result = ""
    if isinstance(node, dict):
        content = node.get("content")
        if node.get("role") == "user" and isinstance(content, str):
            result = _unwrap_user_query(content) or result
        for value in node.values():
            result = _last_user_content(value) or result
    elif isinstance(node, list):
        for value in node:
            result = _last_user_content(value) or result
    return result


def _unwrap_user_query(content: str) -> str:
    start = content.rfind(_POOLSIDE_QUERY_OPEN)
    if start < 0:
        return content.strip()
    start += len(_POOLSIDE_QUERY_OPEN)
    end = content.find(_POOLSIDE_QUERY_CLOSE, start)
    return content[start : end if end >= 0 else None].strip()


def translate_event(harness: str, event: dict[str, Any]) -> dict[str, Any]:
    """Map a harness's raw hook event onto the Claude-shaped event the rest of
    omind consumes (journal, verifier, accounting, guard). Identity for every
    harness but Poolside, whose payload differs in exactly four places (all
    live-verified against pool 1.0.16, #311):

    - MCP tools are ``<server>__<tool>`` with no ``mcp__`` prefix, so the omi
      consult detectors (``mcp__omi__…``) would miss every vault read;
    - the ``shell`` tool carries its command line as ``tool_input.cmd``;
    - ``PostToolUse`` carries ``tool_output`` (text) instead of ``tool_response``;
    - ``UserPromptSubmit`` may omit ``prompt``; it is recovered from the trajectory.
    """
    if spec_for(harness).name != "poolside" or not isinstance(event, dict):
        return event
    out = dict(event)
    tool = str(out.get("tool_name") or "")
    if "__" in tool and not tool.startswith("mcp__"):
        out["tool_name"] = "mcp__" + tool
    tool_input = out.get("tool_input")
    if isinstance(tool_input, dict) and "command" not in tool_input:
        cmd = tool_input.get("cmd")
        if isinstance(cmd, str) and cmd:
            out["tool_input"] = {**tool_input, "command": cmd}
    if "tool_response" not in out and "tool_output" in out:
        out["tool_response"] = out["tool_output"]
    if out.get("hook_event_name") == "UserPromptSubmit" and not out.get("prompt"):
        prompt = poolside_last_prompt(out.get("trajectory_path"))
        if prompt:
            out["prompt"] = prompt
    return out


def render_context(harness: str, event_name: str, text: str) -> str:
    """The stdout line that injects ``text`` as additional context for a
    ``SessionStart``/``UserPromptSubmit``-style hook: Claude's camelCase
    ``hookSpecificOutput`` by default, Poolside's snake_case twin."""
    if spec_for(harness).name == "poolside":
        payload: dict[str, Any] = {
            "hook_specific_output": {
                "hook_event_name": event_name,
                "additional_context": text,
            }
        }
    else:
        payload = {
            "hookSpecificOutput": {
                "hookEventName": event_name,
                "additionalContext": text,
            }
        }
    return json.dumps(payload) + "\n"


def render_stop_block(harness: str, reason: str) -> str:
    """The stdout line a ``Stop`` hook emits to refuse the stop (the loop guard).
    Claude Code honours ``{"decision": "block"}``; Poolside's Stop hook honours
    ``continue: true`` plus ``additional_context`` (bounded by its
    ``stop_hook_max_continuations`` setting)."""
    if spec_for(harness).name == "poolside":
        payload: dict[str, Any] = {
            "continue": True,
            "reason": reason,
            "hook_specific_output": {
                "hook_event_name": "Stop",
                "additional_context": reason,
            },
        }
    else:
        payload = {"decision": "block", "reason": reason}
    return json.dumps(payload) + "\n"


#: (harness, event, expect_blocked) — each command is a hard rule, so it blocks
#: regardless of the per-turn gate, making the self-test deterministic + side-effect
#: free (it calls ``decide`` directly, never the logging path).
_SELFTEST_CASES: tuple[tuple[str, dict[str, Any], bool], ...] = (
    (
        "claude",
        {"tool_name": "Bash", "tool_input": {"command": "gh pr create -t x"}, "session_id": "st"},
        True,
    ),
    (
        "hermes",
        {
            "hook_event_name": "pre_tool_call",
            "tool": "shell",
            "tool_input": {"command": "gh repo delete a/b"},
            "session_id": "st",
        },
        True,
    ),
    (
        "opencode",
        {"tool": "bash", "tool_input": {"command": "gh auth setup-git"}, "session_id": "st"},
        True,
    ),
    (
        "codex",
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "gh repo delete acme/widget"},
            "session_id": "st",
        },
        True,
    ),
    (
        "gemini",
        {
            "hook_event_name": "BeforeTool",
            "tool_name": "run_shell_command",
            "tool_input": {"command": "gh pr merge 5"},
            "session_id": "st",
        },
        True,
    ),
    (
        "deepseek",
        {
            "tool_name": "bash",
            "tool_input": {"command": "gh repo delete acme/widget"},
            "session_id": "st",
        },
        True,
    ),
    (
        "poolside",
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "shell",
            "tool_input": {"cmd": "gh repo delete acme/widget"},
            "session_id": "st",
        },
        True,
    ),
    (
        "openclaw",
        {"tool": "shell", "command": "gh repo delete a/b", "session": "st"},
        True,
    ),
)


def run_selftest() -> list[dict[str, Any]]:
    """Replay canned per-harness events through normalize → decide → render and
    report whether each produced the expected block decision. Side-effect free
    (uses :func:`omind.guard.decide` directly, not the logging check path), so it
    validates wiring **without** any live harness running."""
    from omind import adapters, guard

    results: list[dict[str, Any]] = []
    for name, event, expect_blocked in _SELFTEST_CASES:
        action = adapters.normalize_action(translate_event(name, event))
        verdict = guard.decide(action)
        spec = spec_for(name)
        out, err = io.StringIO(), io.StringIO()
        render_decision(
            verdict, spec.block_format, out, err, event=str(event.get("hook_event_name") or "")
        )
        blocked = not verdict.allow
        results.append(
            {
                "harness": name,
                "command": action["command"],
                "blocked": blocked,
                "format": spec.block_format,
                "rendered": (out.getvalue() or err.getvalue()).strip(),
                "ok": blocked == expect_blocked,
            }
        )
    return results
