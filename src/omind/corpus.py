# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Aaron K. Clark
"""Export the compliance log as fine-tuning data — the long-game groundwork.

The roadmap's Phase 4 end state is fine-tuning a model on the accumulated
violation corpus (the only true in-weights fix). The training run itself needs a
sizable corpus + GPU and is out of scope here, but the *pipeline* that turns the
compliance log into instruction-tuning examples is not — and it is what makes the
corpus accumulate into something trainable. ``omind guard export-corpus`` emits
one JSON chat example per recorded violation/deny:

    {"messages": [
       {"role": "system", "content": "<the guard's job>"},
       {"role": "user", "content": "Action — Bash: gh pr create ... May the agent run this?"},
       {"role": "assistant", "content": "DENY — <the rule's reason>"}],
     "meta": {"rule_id": ..., "severity": ..., "outcome": ...}}

The routine ``omi-gate`` "you didn't consult" deny is excluded (it is friction,
not a teachable policy violation).
"""

from __future__ import annotations

import json
from typing import Any, TextIO

from omind import compliance, policy, verify

SYSTEM_PROMPT = (
    "You are an OMI-compliance guard for an AI coding agent. Given an action the "
    "agent wants to take, answer ALLOW or DENY on the first word and give the "
    "one-line reason grounded in the OMI rules."
)


def _rule_messages() -> dict[str, str]:
    """Map rule id -> human reason, for the assistant target. Includes the
    synthetic off-topic-consult rule the verifier logs."""
    messages = {rule.id: rule.message for rule in policy.load_policy()}
    messages.setdefault(
        verify.OFF_TOPIC_RULE,
        "that OMI consult was not relevant to the task — consult a note that "
        "matches what you are working on.",
    )
    return messages


def corpus_examples() -> list[dict[str, Any]]:
    """Build chat-format training examples from the compliance log (newest last).

    Every recorded policy violation/deny becomes one DENY example; the gate-only
    deny is skipped. Never raises.
    """
    rule_messages = _rule_messages()
    examples: list[dict[str, Any]] = []
    for event in compliance.read_events():
        rule_id = str(event.get("rule_id") or "")
        if not rule_id or rule_id == "omi-gate":
            continue
        tool = str(event.get("tool") or "action")
        command = str(event.get("command") or "")
        reason = rule_messages.get(rule_id, "this action violates an OMI-compliance rule.")
        action = f"Action — {tool}: {command}" if command else f"Action — {tool}"
        examples.append(
            {
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": f"{action}\nMay the agent run this?"},
                    {"role": "assistant", "content": f"DENY — {reason}"},
                ],
                "meta": {
                    "rule_id": rule_id,
                    "severity": str(event.get("severity") or ""),
                    "outcome": str(event.get("outcome") or ""),
                },
            }
        )
    return examples


def export_corpus(out: TextIO) -> int:
    """Write the corpus as JSONL to ``out``; return the number of examples."""
    examples = corpus_examples()
    for example in examples:
        out.write(json.dumps(example) + "\n")
    return len(examples)
