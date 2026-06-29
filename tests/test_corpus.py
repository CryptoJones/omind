# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Aaron K. Clark
"""Tests for the fine-tune corpus export (Phase 4 groundwork)."""

from __future__ import annotations

import io
import json

from omind import compliance, corpus, verify


def test_empty_log_yields_no_examples() -> None:
    assert corpus.corpus_examples() == []
    out = io.StringIO()
    assert corpus.export_corpus(out) == 0
    assert out.getvalue() == ""


def test_examples_carry_deny_and_the_rule_reason() -> None:
    compliance.log_event(
        compliance.KIND_DECISION,
        tool="Bash",
        command="gh repo delete x/y",
        rule_id="gh-repo-delete",
        severity="hard",
        outcome="deny",
    )
    examples = corpus.corpus_examples()
    assert len(examples) == 1
    msgs = examples[0]["messages"]
    assert msgs[0]["role"] == "system"
    assert "gh repo delete" in msgs[1]["content"]
    assert msgs[2]["content"].startswith("DENY —")
    assert "Repos and Secrets" in msgs[2]["content"]  # the seed rule's reason
    assert examples[0]["meta"]["rule_id"] == "gh-repo-delete"


def test_off_topic_consult_gets_a_synthetic_reason() -> None:
    compliance.log_event(
        compliance.KIND_VIOLATION,
        tool="Read",
        command="Smoothie.md",
        rule_id=verify.OFF_TOPIC_RULE,
        severity="soft",
        outcome="irrelevant",
    )
    example = corpus.corpus_examples()[-1]
    assert "relevant" in example["messages"][2]["content"].lower()


def test_gate_only_deny_is_excluded() -> None:
    compliance.log_event(compliance.KIND_DECISION, rule_id="omi-gate", outcome="deny")
    assert corpus.corpus_examples() == []


def test_export_writes_jsonl() -> None:
    compliance.log_event(
        compliance.KIND_DECISION, tool="Bash", command="gh repo delete x/y",
        rule_id="gh-repo-delete", outcome="deny",
    )
    out = io.StringIO()
    assert corpus.export_corpus(out) == 1
    line = out.getvalue().strip()
    assert json.loads(line)["meta"]["rule_id"] == "gh-repo-delete"
