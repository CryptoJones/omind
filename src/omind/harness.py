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
from dataclasses import dataclass
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
        import json

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
            import json

            out.write(json.dumps({"decision": "block", "reason": verdict.reason}) + "\n")
        return 0
    if fmt == FMT_JSON_SIGNAL:
        # The OpenCode JS plugin reads this {allow, reason, rule_id} and throws on
        # deny. rule_id lets the plugin enforce only HARD-RULE denies (not the
        # consult-gate) where the gate's turn/consult signals aren't verified.
        import json

        out.write(
            json.dumps(
                {"allow": verdict.allow, "reason": verdict.reason, "rule_id": verdict.rule_id}
            )
            + "\n"
        )
        return verdict.exit_code
    # FMT_EXIT2 (default): stderr reason + exit 2 — the Claude/shell contract.
    if not verdict.allow:
        err.write(f"BLOCKED by {verdict.reason}\n")
    return verdict.exit_code


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
)


def run_selftest() -> list[dict[str, Any]]:
    """Replay canned per-harness events through normalize → decide → render and
    report whether each produced the expected block decision. Side-effect free
    (uses :func:`omind.guard.decide` directly, not the logging check path), so it
    validates wiring **without** any live harness running."""
    from omind import adapters, guard

    results: list[dict[str, Any]] = []
    for name, event, expect_blocked in _SELFTEST_CASES:
        action = adapters.normalize_action(event)
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
