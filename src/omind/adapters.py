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

import sys
from pathlib import Path
from typing import Any, TextIO

from omind import guard

#: Tool-name prefixes that denote an OMI consult across harnesses (the omind MCP
#: server is registered under the same tool namespace everywhere).
_OMI_CONSULT_PREFIXES = ("mcp__omi__",)


def _first_str(data: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = data.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def normalize_action(event: dict[str, Any]) -> dict[str, Any]:
    """Map a harness pre-action event into the guard's action schema.

    Tolerant of the field-name variations across Claude Code (``tool_name`` +
    ``tool_input.command`` + ``session_id``), Hermes, OpenClaw, and OpenCode
    (``tool``/``name`` + ``command``/``args`` + ``session``), so every harness
    funnels into the same decision.
    """
    tool = _first_str(event, ("tool", "tool_name", "name"))
    tool_input = event.get("tool_input")
    tool_input = tool_input if isinstance(tool_input, dict) else {}
    command = (
        _first_str(event, ("command",))
        or _first_str(tool_input, ("command",))
        or _first_str(event, ("args", "input"))
    )
    session = _first_str(event, ("session", "session_id"))
    is_consult = tool.startswith(_OMI_CONSULT_PREFIXES) or bool(event.get("is_omi_consult"))
    return {
        "tool": tool,
        "command": command,
        "session": session,
        "is_omi_consult": is_consult,
    }


def run_adapter(
    stream: TextIO | None = None, *, omi_dir: Path | None = None, harness: str = "claude"
) -> int:
    """Read a harness event on stdin, normalize it, decide, and render the verdict
    in ``harness``'s block-output format (exit-2 for shell harnesses, a
    ``{"decision":"block"}`` JSON for Hermes, an ``{allow,reason}`` signal for the
    OpenCode plugin). Returns the exit code the adapter should exit with."""
    from omind import harness as harness_mod

    src = stream if stream is not None else sys.stdin
    action = normalize_action(guard._load(src))
    verdict = guard.check_action(action)
    spec = harness_mod.spec_for(harness)
    return harness_mod.render_decision(verdict, spec.block_format, sys.stdout, sys.stderr)
