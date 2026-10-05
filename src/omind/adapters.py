# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Aaron K. Clark
"""Harness-agnostic guard adapter — Phase 4 of the enforcement roadmap.

The decision core (:mod:`omind.guard`) is already harness-agnostic; the roadmap's
Phase 4 is to give every *other* agent (Hermes Agent, OpenClaw, OpenCode) the
same thin front the Claude Code adapter (``omi-guard.sh``) has, so a rule learned
under one agent enforces under all of them. Rather than a bespoke script per
harness, this module normalizes any harness's pre-action event into the single
action schema ``omind guard check`` consumes, then delegates to that one path
(hard blocks + per-turn gate + compliance logging live in ONE place).

A harness wires this by piping its pre-action event JSON to ``omind guard
adapter`` before it runs a tool / makes an LLM call, and treating a non-zero exit
as "blocked" (exit 2) — exactly how the Claude PreToolUse hook treats
``omind guard check``. Installing that call into each *live* harness is the
documented follow-up (it needs the harness's own hook config); the adapter
itself — the part that has to enforce identically everywhere — lives here and is
exercised by the test-suite against each harness's event shape.
"""

from __future__ import annotations

import contextlib
import json
import sys
from pathlib import Path
from typing import Any, TextIO

from omind import guard

#: Tool-name prefixes that denote an OMI consult across harnesses. Most harnesses
#: namespace MCP tools as ``mcp__<server>__<tool>`` (double underscore); the Gemini
#: CLI uses ``mcp_<server>_<tool>`` (single underscore); Antigravity / pool use
#: ``omi_`` or ``omi__``, so all forms are listed.
_OMI_CONSULT_PREFIXES = ("mcp__omi__", "mcp__omi_", "mcp_omi_", "omi__", "omi_")


def _first_str(data: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = data.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def _derive_command(event: dict[str, Any], tool_input: dict[str, Any]) -> str:
    """Best-effort command text from a harness event.

    Accepts the common argv-as-list shape (``"args": ["repo", "delete", "a/b"]``)
    and an ``input``/``args`` object, not just a plain string — otherwise the
    guard saw an empty command and no hard rule could match the real payload.
    """
    # Poolside's ``shell`` tool carries the command line as ``cmd``;
    # Antigravity uses ``CommandLine``.
    for source in (
        event.get("command"),
        tool_input.get("command"),
        tool_input.get("CommandLine"),
        tool_input.get("cmd"),
    ):
        if isinstance(source, str) and source:
            return source
    for container in (event, tool_input):
        for key in ("args", "input"):
            val = container.get(key)
            if isinstance(val, str) and val:
                return val
            if isinstance(val, list):
                joined = " ".join(str(x) for x in val if x is not None).strip()
                if joined:
                    return joined
            if isinstance(val, dict):
                inner = _first_str(val, ("command", "CommandLine", "cmd"))
                if inner:
                    return inner
    return ""


def normalize_action(event: dict[str, Any]) -> dict[str, Any]:
    """Map a harness pre-action event into the guard's action schema.

    Tolerant of the field-name variations across Claude Code (``tool_name`` +
    ``tool_input.command`` + ``session_id``), Hermes, OpenClaw, OpenCode,
    and Antigravity (``toolCall.name`` + ``toolCall.args.CommandLine`` +
    ``conversationId``), so every harness funnels into the same decision.
    """
    tool = _first_str(event, ("tool", "tool_name", "name"))
    if not tool and isinstance(event.get("toolCall"), dict):
        tool = _first_str(event["toolCall"], ("name", "tool"))
    tool_input = event.get("tool_input")
    if not isinstance(tool_input, dict) and isinstance(event.get("toolCall"), dict):
        args = event["toolCall"].get("args")
        if isinstance(args, dict):
            tool_input = args
    tool_input = tool_input if isinstance(tool_input, dict) else {}
    command = _derive_command(event, tool_input)
    file_path = _first_str(
        tool_input, ("file_path", "path", "TargetFile", "AbsolutePath")
    ) or _first_str(event, ("file_path", "path", "TargetFile", "AbsolutePath"))
    session = _first_str(
        event, ("session", "session_id", "conversationId", "conversation_id")
    )
    prompt = _first_str(event, ("prompt", "user_prompt", "current_prompt", "turn_prompt"))
    is_consult = tool.startswith(_OMI_CONSULT_PREFIXES) or bool(event.get("is_omi_consult"))
    consult_target = (
        _first_str(tool_input, ("name", "query", "q", "file_path", "path", "pattern"))
        or _first_str(event, ("consult_target",))
    )
    return {
        "tool": tool,
        "command": command,
        "session": session,
        "is_omi_consult": is_consult,
        "file_path": file_path,
        "prompt": prompt,
        # #290: lets the guard see messages the human sent mid-turn.
        "transcript_path": _first_str(event, ("transcript_path", "transcriptPath")),
        "consult_target": consult_target,
        "consult_kind": "read" if "read" in tool.lower() else "search",
        # #394: the agent's shell cwd, so the target repo is resolved from where
        # the command runs, not from wherever this hook process started.
        "cwd": _first_str(event, ("cwd",)),
    }


def run_adapter(
    stream: TextIO | None = None, *, omi_dir: Path | None = None, harness: str = "claude"
) -> int:
    """Read a harness event on stdin, normalize it, decide, and render the verdict
    in ``harness``'s block-output format (exit-2 for shell harnesses, a
    ``{"decision":"block"}`` JSON for Hermes, an ``{allow,reason}`` signal for the
    OpenCode plugin, a snake_case ``hook_specific_output`` deny for Poolside).
    Returns the exit code the adapter should exit with."""
    from omind import harness as harness_mod

    src = stream if stream is not None else sys.stdin
    spec = harness_mod.spec_for(harness)
    try:
        if src.isatty():  # a by-hand invocation with no piped event: nothing to guard
            return 0
    except (AttributeError, ValueError, OSError):
        pass
    raw = src.read()
    if not raw.strip():
        return 0  # no event (the shell adapter also allows empty input)
    try:
        event = json.loads(raw)
        if not isinstance(event, dict):
            raise ValueError("event is not a JSON object")
    except (ValueError, TypeError):
        # A mangled/truncated event in an enforcement component must FAIL CLOSED:
        # a destructive command must never be waved through because its event
        # didn't parse. Emit the harness's block verdict.
        blocked = guard.Verdict(
            allow=False,
            reason="omi-guard: unparseable guard event — blocking (fail-closed)",
            rule_id="adapter-parse-error",
        )
        return harness_mod.render_decision(
            blocked, spec.block_format, sys.stdout, sys.stderr, event=""
        )
    # Grok runs Claude's hooks and sends a camelCase payload. Deny with Grok's
    # JSON decision so the reason is the guard text, not the first stderr line.
    fmt = spec.block_format
    if harness == "grok" or harness_mod.payload_is_grok(event):
        fmt = harness_mod.FMT_GROK
    # Poolside's event shape differs from Claude's in three places (tool naming,
    # ``cmd``, ``tool_output``); translate ONCE here so the guard, verifier, and
    # accounting all see the Claude-shaped event they were written against.
    #
    # For Codex, Gemini, Poolside, agy and Windows Claude this IS the check
    # dispatch, so it fails OPEN the way ``guard.check_action`` does (#420): an
    # exception translating, normalizing or rendering is reported and logged,
    # and the action is allowed (static hard-policy rules still apply), rendered
    # in the harness's own format.
    action: dict[str, Any] | None = None
    verdict: guard.Verdict | None = None
    hook_event = ""
    stage = "adapter translate"
    try:
        event = harness_mod.translate_event(harness, event)
        # Codex's deny shape depends on which hook fired (PreToolUse vs
        # PermissionRequest); pass the event name through (ignored by others).
        hook_event = str(event.get("hook_event_name") or "")
        stage = "adapter normalize"
        action = normalize_action(event)
        stage = "adapter check"
        verdict = guard.check_action(action, omi_dir=omi_dir)
        stage = "adapter render"
        return harness_mod.render_decision(
            verdict, fmt, sys.stdout, sys.stderr, event=hook_event
        )
    except Exception as exc:
        # ``check_action`` logs its own decision (and never raises), so a verdict
        # it returned is already on the compliance record: only the internal
        # error is logged here, never the deny a second time.
        fallback = _fail_open_verdict(event, action, exc, verdict, stage=stage)
        try:
            return harness_mod.render_decision(
                fallback, fmt, sys.stdout, sys.stderr, event=hook_event
            )
        except Exception:
            return _render_last_resort(fmt, fallback.allow, hook_event)


#: Fixed literal outputs for when :func:`omind.harness.render_decision` raises
#: twice (#420). Empty stdout is NOT a safe universal deny: OpenCode reads it as
#: ``{}`` (= allow), and agy/Hermes read silence as proceed, so a standing deny
#: is written in each harness's own shape: ``(stdout, stderr, exit code)``.
_LAST_RESORT_REASON = "omi-guard: internal error rendering the verdict; a hard rule denies this"
_LAST_RESORT_DENY: dict[str, tuple[str, str, int]] = {
    "exit2": ("", f"BLOCKED by {_LAST_RESORT_REASON}\n", 2),
    "claude_json": (
        json.dumps({"decision": "block", "reason": _LAST_RESORT_REASON}) + "\n",
        "",
        0,
    ),
    "json_signal": (
        json.dumps(
            {"allow": False, "reason": _LAST_RESORT_REASON, "rule_id": guard.GUARD_ERROR_RULE}
        )
        + "\n",
        "",
        2,
    ),
    "codex_hook": (
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": f"OMI guard: {_LAST_RESORT_REASON}",
                }
            }
        )
        + "\n",
        "",
        0,
    ),
    "codex_hook:PermissionRequest": (
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PermissionRequest",
                    "decision": {
                        "behavior": "deny",
                        "message": f"OMI guard: {_LAST_RESORT_REASON}",
                    },
                }
            }
        )
        + "\n",
        "",
        0,
    ),
    "gemini": (json.dumps({"decision": "deny", "reason": _LAST_RESORT_REASON}) + "\n", "", 0),
    "poolside": (
        json.dumps(
            {
                "hook_specific_output": {
                    "hook_event_name": "PreToolUse",
                    "permission_decision": "deny",
                    "permission_decision_reason": f"BLOCKED by {_LAST_RESORT_REASON}",
                }
            }
        )
        + "\n",
        "",
        0,
    ),
    "openclaw": (
        json.dumps(
            {"allow": False, "reason": _LAST_RESORT_REASON, "rule_id": guard.GUARD_ERROR_RULE}
        )
        + "\n",
        "",
        0,
    ),
    "agy": (json.dumps({"decision": "deny", "reason": _LAST_RESORT_REASON}) + "\n", "", 0),
    "grok": (
        json.dumps({"decision": "deny", "reason": f"OMI guard: {_LAST_RESORT_REASON}"}) + "\n",
        "",
        0,
    ),
}
_LAST_RESORT_ALLOW: dict[str, str] = {
    "json_signal": json.dumps({"allow": True, "reason": "", "rule_id": ""}) + "\n",
    "openclaw": json.dumps({"allow": True, "reason": "", "rule_id": ""}) + "\n",
    "agy": json.dumps({"decision": "allow"}) + "\n",
}


def _render_last_resort(fmt: str, allow: bool, hook_event: str) -> int:
    """Write a fixed literal verdict in ``fmt``'s shape; return its exit code.

    Never raises. An unknown format falls back to the exit-2 contract, which is
    also what a deny returns when its literal cannot be written.
    """
    if allow:
        with contextlib.suppress(Exception):
            sys.stdout.write(_LAST_RESORT_ALLOW.get(fmt, ""))
        return 0
    key = f"{fmt}:{hook_event}"
    out, err, code = _LAST_RESORT_DENY.get(
        key, _LAST_RESORT_DENY.get(fmt, _LAST_RESORT_DENY["exit2"])
    )
    try:
        sys.stdout.write(out)
        sys.stderr.write(err)
    except Exception:
        return 2
    return code


def _fail_open_verdict(
    event: Any,
    action: dict[str, Any] | None,
    exc: Exception,
    decided: guard.Verdict | None,
    *,
    stage: str,
) -> guard.Verdict:
    """The adapter's fail-open verdict (#420), via :func:`guard._fail_open_verdict`.

    When the event never normalized, the hard-policy re-check and the compliance
    event get a best-effort action read straight off the raw event. A
    ``decided`` verdict came back from :func:`guard.check_action`, which already
    logged it, so only the internal error is logged for it.
    """
    if action is None:
        action = {}
        try:
            # Claude/Codex/Gemini ``tool_input``, Poolside's ``cmd``, agy's
            # ``toolCall.args.CommandLine``, or a flat ``command``.
            tool_input = event.get("tool_input")
            tool_call = event.get("toolCall")
            args = tool_call.get("args") if isinstance(tool_call, dict) else None
            command = ""
            for source, key in (
                (tool_input, "command"),
                (tool_input, "cmd"),
                (args, "CommandLine"),
                (event, "command"),
            ):
                if isinstance(source, dict) and source.get(key):
                    command = str(source[key])
                    break
            action = {
                "tool": str(event.get("tool_name") or event.get("tool") or ""),
                "command": command,
                "session": str(
                    event.get("session_id")
                    or event.get("conversationId")
                    or event.get("session")
                    or ""
                ),
            }
        except Exception:
            action = {}
    return guard._fail_open_verdict(
        action, exc, decided, stage=stage, decided_logged=decided is not None
    )
