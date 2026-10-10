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
FMT_OPENCLAW = "openclaw"  # RETIRED (#425): no OpenClaw gateway reads this; {allow,reason,rule_id}
# Poolside pool hook: snake_case hook_specific_output deny JSON, exit 0
FMT_POOLSIDE = "poolside"
# Antigravity (agy) PreToolUse: {"decision":"deny","reason"} / {"decision":"allow"}, exit 0
FMT_AGY = "agy"
# Grok Build PreToolUse: {"decision":"deny","reason"} on stdout, exit 0.
# Exit 2 is also a deny, but Grok shows only the first stderr line as the
# reason, so a progress bar printed before the BLOCKED line hides the guard
# message. The JSON decision is honored regardless of exit code.
FMT_GROK = "grok"


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
    "agy": HarnessSpec("agy", CAP_HARD_BLOCK, FMT_AGY, "Antigravity CLI PreToolUse hook"),
    "antigravity": HarnessSpec(
        "antigravity", CAP_HARD_BLOCK, FMT_AGY, "Antigravity CLI PreToolUse hook"
    ),
    # Grok Build (the `grok` CLI) speaks Claude-shaped hook names but a camelCase
    # payload (`toolName`, `toolInput`, `sessionId`, `hookEventName`). It also
    # runs Claude Code's hooks via compatibility and feeds them that same
    # payload. PreToolUse hard-blocks on stdout `{"decision":"deny","reason"}`.
    "grok": HarnessSpec("grok", CAP_HARD_BLOCK, FMT_GROK, "Grok Build PreToolUse hook"),
    # RETIRED (#425): the "POST /hooks/agent gateway" this described does not
    # exist. OpenClaw has no shell-command hooks (tool gating is the in-process
    # plugin hook before_tool_call) and rejects the hooks.agent entry omind used
    # to write, so setup no longer wires it and nothing invokes this adapter.
    # Kept only because `omind guard selftest` and the adapter's last-resort
    # tables still list it; a real OpenClaw guard would be a plugin.
    "openclaw": HarnessSpec(
        "openclaw",
        CAP_DETECT_ONLY,
        FMT_OPENCLAW,
        "retired: OpenClaw has no shell-command hooks; nothing invokes this (#425)",
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
        # RETIRED (#425): no OpenClaw gateway reads this; setup no longer wires
        # it (see the "openclaw" HarnessSpec). Kept advisory (exit 0) so the
        # selftest row stays stable.
        out.write(
            json.dumps(
                {"allow": verdict.allow, "reason": verdict.reason, "rule_id": verdict.rule_id}
            )
            + "\n"
        )
        return 0
    if fmt == FMT_AGY:
        # Antigravity (agy) PreToolUse hook reads a JSON decision on stdout (exit 0).
        # On deny emit {"decision":"deny","reason":...}; on allow emit {"decision":"allow"}.
        if verdict.allow:
            out.write(json.dumps({"decision": "allow"}) + "\n")
        else:
            out.write(json.dumps({"decision": "deny", "reason": verdict.reason}) + "\n")
        return 0
    if fmt == FMT_GROK:
        # Grok reads a top-level decision on stdout (exit 0). `deny` drops the
        # call and `reason` is shown to the model. Empty stdout + exit 0 = allow.
        # Do not also exit 2: a non-JSON crash must fail open, and exit 2 would
        # surface the first stderr line instead of this reason.
        if not verdict.allow:
            out.write(
                json.dumps({"decision": "deny", "reason": f"OMI guard: {verdict.reason}"}) + "\n"
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


def agy_last_prompt(transcript_path: object) -> str:
    """The most recent user query in an Antigravity transcript, or ``""``. Best-effort."""
    if not isinstance(transcript_path, str) or not transcript_path:
        return ""
    try:
        path = Path(transcript_path)
        if not path.is_file():
            return ""
        size = path.stat().st_size
        with path.open("rb") as fh:
            if size > 4 * 1024 * 1024:
                fh.seek(size - 4 * 1024 * 1024)
            tail = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return ""
    for line in reversed(tail.splitlines()):
        if '"USER_INPUT"' not in line:
            continue
        try:
            record = json.loads(line)
            if record.get("type") == "USER_INPUT":
                content = record.get("content")
                if isinstance(content, str) and content.strip():
                    return content.strip()
        except ValueError:
            continue
    return ""


#: Grok's ``hookEventName`` values (snake_case). Claude and Codex send PascalCase
#: on ``hook_event_name`` and do not set this key.
_GROK_HOOK_EVENTS = frozenset(
    {
        "pre_tool_use",
        "post_tool_use",
        "post_tool_use_failure",
        "user_prompt_submit",
        "session_start",
        "session_end",
        "stop",
        "stop_failure",
        "stop_cancelled",
        "notification",
        "subagent_start",
        "subagent_stop",
        "pre_compact",
        "post_compact",
        "permission_denied",
    }
)
_GROK_EVENT_TO_CLAUDE = {
    "pre_tool_use": "PreToolUse",
    "post_tool_use": "PostToolUse",
    "post_tool_use_failure": "PostToolUseFailure",
    "user_prompt_submit": "UserPromptSubmit",
    "session_start": "SessionStart",
    "session_end": "SessionEnd",
    "stop": "Stop",
    "subagent_start": "SubagentStart",
    "subagent_stop": "SubagentStop",
    "pre_compact": "PreCompact",
    "post_compact": "PostCompact",
    "notification": "Notification",
    "permission_denied": "PermissionDenied",
}
#: Grok's built-in tool names, mapped onto the names the guard policy already
#: classifies (shell, write, read). MCP tools are handled separately.
_GROK_TOOL_ALIASES = {
    "run_terminal_command": "Bash",
    "read_file": "Read",
    "search_replace": "Edit",
    "write": "Write",
    "grep": "Grep",
    "list_dir": "LS",
    "web_search": "WebSearch",
    "spawn_subagent": "Task",
}
_GROK_DISPATCHERS = frozenset({"use_tool", "CallMcpTool"})
_GROK_BARE_OMI_TOOLS = frozenset(
    {
        "search-vault",
        "recall-note",
        "read-note",
        "create-note",
        "edit-note",
        "list-notes",
        "help",
    }
)


def payload_is_grok(event: dict[str, Any]) -> bool:
    """True when ``event`` is a Grok Build hook payload.

    Grok loads Claude Code's hooks and sends them this shape, so the check is
    on the payload, not only on ``--harness grok``. Claude and Codex events
    stay snake_case (``tool_name`` / ``tool_input``) and do not match.
    """
    if not isinstance(event, dict):
        return False
    name = event.get("hookEventName")
    if isinstance(name, str) and name in _GROK_HOOK_EVENTS:
        return True
    return "toolName" in event or "toolInput" in event


def _agy_cwd(tool_call: object) -> str:
    """The ``Cwd`` argument of an Antigravity ``run_command``, or ``""``.

    Antigravity's transcript logs record argument values JSON-encoded, with
    literal double quotes around the path. The hook may send either form, so
    a quoted value is decoded.
    """
    if not isinstance(tool_call, dict):
        return ""
    args = tool_call.get("args")
    value = args.get("Cwd") if isinstance(args, dict) else None
    if not isinstance(value, str):
        return ""
    value = value.strip()
    if len(value) >= 2 and value.startswith('"') and value.endswith('"'):
        try:
            decoded = json.loads(value)
        except (ValueError, RecursionError):
            return value
        return decoded if isinstance(decoded, str) else ""
    return value


def _first_dict_arg(*values: object) -> dict[str, Any] | None:
    """The first non-empty dispatcher argument: an object, or a JSON string of one.

    An OpenAI-style ``use_tool`` sends ``arguments`` as a JSON string. Without
    parsing it, the consult target would be the dispatcher wrapper instead of
    the note name. ``omi-guard.sh`` parses the same keys in the same order.
    """
    for value in values:
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except (ValueError, RecursionError):
                continue
        if isinstance(value, dict) and value:
            return dict(value)
    return None


def _translate_grok(event: dict[str, Any]) -> dict[str, Any]:
    """Map a Grok hook event onto the Claude-shaped event the rest of omind uses."""
    out = dict(event)
    if not out.get("tool_name"):
        name = out.get("toolName")
        if isinstance(name, str):
            out["tool_name"] = name
    if not isinstance(out.get("tool_input"), dict) and isinstance(out.get("toolInput"), dict):
        out["tool_input"] = dict(out["toolInput"])
    if not out.get("session_id") and isinstance(out.get("sessionId"), str):
        out["session_id"] = out["sessionId"]
    if not out.get("transcript_path") and isinstance(out.get("transcriptPath"), str):
        out["transcript_path"] = out["transcriptPath"]
    if not str(out.get("prompt") or "").strip():
        for key in ("userPrompt", "message"):
            value = out.get(key)
            if isinstance(value, str) and value.strip():
                out["prompt"] = value
                break
    if "tool_response" not in out and "toolResult" in out:
        out["tool_response"] = out["toolResult"]
    if not out.get("hook_event_name"):
        mapped = _GROK_EVENT_TO_CLAUDE.get(str(out.get("hookEventName") or ""))
        if mapped:
            out["hook_event_name"] = mapped

    tool = str(out.get("tool_name") or "")
    raw_input = out.get("tool_input")
    tool_input: dict[str, Any] = raw_input if isinstance(raw_input, dict) else {}
    if tool in _GROK_DISPATCHERS:
        inner = tool_input.get("tool_name") or tool_input.get("toolName") or tool_input.get("name")
        if isinstance(inner, str) and inner:
            tool = inner
            nested = _first_dict_arg(
                tool_input.get("tool_input"),
                tool_input.get("arguments"),
                tool_input.get("toolInput"),
            )
            if nested is not None:
                out["tool_input"] = nested
            out["tool_name"] = tool
    if tool in _GROK_TOOL_ALIASES:
        tool = _GROK_TOOL_ALIASES[tool]
        out["tool_name"] = tool
    if tool.startswith("omi__") or ("__" in tool and not tool.startswith("mcp__")):
        out["tool_name"] = "mcp__" + tool
    elif tool.startswith("omi_"):
        out["tool_name"] = "mcp__omi__" + tool[4:]
    elif tool in _GROK_BARE_OMI_TOOLS:
        out["tool_name"] = "mcp__omi__" + tool
    return out


def _claude_event_uses_grok_tool(event: dict[str, Any]) -> bool:
    """True when a snake_case Claude-hook event is one of Grok's own tool names.

    Grok's Claude-compatible hook sometimes sends ``tool_name`` / ``tool_input``
    and no camelCase keys, so :func:`payload_is_grok` is false. ``use_tool``
    still hides the MCP tool, and ``run_terminal_command`` is not ``Bash``.
    A plain Claude event (``Bash``, ``Edit``, ``mcp__omi__…``, ``omi__…``) does
    not match, so :func:`translate_event` can return that same object.
    """
    tool = event.get("tool_name")
    return isinstance(tool, str) and (tool in _GROK_DISPATCHERS or tool in _GROK_TOOL_ALIASES)


def translate_event(harness: str, event: dict[str, Any]) -> dict[str, Any]:
    """Map a harness's raw hook event onto the Claude-shaped event the rest of
    omind consumes (journal, verifier, accounting, guard). Identity for every
    harness but Poolside, Antigravity, and Grok:

    - Poolside: MCP tools are ``<server>__<tool>`` with no ``mcp__`` prefix,
      shell tool carries ``cmd``, ``PostToolUse`` carries ``tool_output``,
      prompt recovered from trajectory.
    - Antigravity: toolCall carries ``name`` and ``args`` (with ``CommandLine`` /
      ``TargetFile`` / ``AbsolutePath``), session in ``conversationId``, prompt
      recovered from transcript.
    - Grok: camelCase ``toolName`` / ``toolInput`` / ``sessionId``, built-in
      tool names aliased onto Claude's, MCP tools ``<server>__<tool>``. A
      Grok-shaped payload is translated even when the hook was installed as
      Claude's, because Grok runs those hooks with its own event shape. A
      snake_case ``use_tool`` / ``CallMcpTool`` or a Grok built-in name on the
      Claude harness is translated too (#472). ``omi-guard.sh`` normalizes the
      same shapes before its consult case.
    """
    if not isinstance(event, dict):
        return event
    if harness == "grok" or payload_is_grok(event):
        return _translate_grok(event)
    spec = spec_for(harness)
    if spec.block_format == FMT_AGY:
        out = dict(event)
        tool_call = out.get("toolCall")
        if isinstance(tool_call, dict):
            out["tool_name"] = str(tool_call.get("name") or "")
            args = tool_call.get("args")
            if isinstance(args, dict):
                agy_tool_input = dict(args)
                if "CommandLine" in agy_tool_input and "command" not in agy_tool_input:
                    agy_tool_input["command"] = agy_tool_input["CommandLine"]
                if "TargetFile" in agy_tool_input and "file_path" not in agy_tool_input:
                    agy_tool_input["file_path"] = agy_tool_input["TargetFile"]
                elif "AbsolutePath" in agy_tool_input and "file_path" not in agy_tool_input:
                    agy_tool_input["file_path"] = agy_tool_input["AbsolutePath"]
                out["tool_input"] = agy_tool_input
        if "session_id" not in out and "conversationId" in out:
            out["session_id"] = out["conversationId"]
        workspaces = out.get("workspacePaths")
        root = ""
        if isinstance(workspaces, list) and workspaces and isinstance(workspaces[0], str):
            root = workspaces[0]
        # run_command's own Cwd is where the command runs, so it wins over a
        # top-level cwd and the workspace root: a `git commit` run in a repo
        # outside the root is judged against that repo (#473). A relative Cwd
        # is taken from the root, not from wherever this hook process runs.
        agy_cwd = _agy_cwd(tool_call)
        if agy_cwd:
            if root and not Path(agy_cwd).is_absolute():
                agy_cwd = str(Path(root) / agy_cwd)
            out["cwd"] = agy_cwd
        elif "cwd" not in out and root:
            out["cwd"] = root
        if "prompt" not in out:
            prompt = agy_last_prompt(out.get("transcriptPath"))
            if prompt:
                out["prompt"] = prompt
        tool = str(out.get("tool_name") or "")
        if tool.startswith("omi__"):
            out["tool_name"] = "mcp__" + tool
        elif tool.startswith("omi_"):
            out["tool_name"] = "mcp__omi__" + tool[4:]
        elif tool in (
            "search-vault",
            "recall-note",
            "read-note",
            "create-note",
            "edit-note",
            "list-notes",
        ):
            out["tool_name"] = "mcp__omi__" + tool
        return out
    if harness == "claude" and _claude_event_uses_grok_tool(event):
        return _translate_grok(event)
    if spec.name != "poolside":
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
    ``SessionStart``/``UserPromptSubmit``/``PreInvocation``-style hook: Claude's camelCase
    ``hookSpecificOutput`` by default, Poolside's snake_case twin, or Antigravity's
    ``injectSteps`` structure."""
    if spec_for(harness).block_format == FMT_AGY:
        payload: dict[str, Any] = {
            "injectSteps": [
                {
                    "ephemeralMessage": text,
                }
            ]
        }
    elif spec_for(harness).name == "poolside":
        payload = {
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
    ``continue: true`` plus ``additional_context``; Antigravity honours
    ``{"decision": "continue", "reason": reason}``."""
    if spec_for(harness).block_format == FMT_AGY:
        payload: dict[str, Any] = {
            "decision": "continue",
            "reason": reason,
        }
    elif spec_for(harness).name == "poolside":
        payload = {
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
    (
        "agy",
        {
            "toolCall": {
                "name": "run_command",
                "args": {"CommandLine": "gh repo delete acme/widget"},
            },
            "conversationId": "st",
        },
        True,
    ),
    (
        "grok",
        {
            "hookEventName": "pre_tool_use",
            "hook_event_name": "PreToolUse",
            "sessionId": "st",
            "toolName": "run_terminal_command",
            "toolInput": {"command": "gh repo delete acme/widget"},
        },
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
