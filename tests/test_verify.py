# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Aaron K. Clark
"""Tests for the verifier — Layer C."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from omind import compliance, guard, verify


def _omi(tmp_path: Path) -> Path:
    omi = tmp_path / "OMI"
    omi.mkdir(parents=True, exist_ok=True)
    return omi


def test_consult_target_extraction(tmp_path: Path) -> None:
    omi = _omi(tmp_path)
    assert verify.consult_target(
        {"tool_name": "mcp__omi__search-vault", "tool_input": {"query": "codeberg"}}, omi
    ) == ("search", "codeberg")
    assert verify.consult_target(
        {"tool_name": "mcp__omi__read-note", "tool_input": {"filename": "Note.md"}}, omi
    ) == ("read", "Note.md")
    note = omi / "Note.md"
    note.write_text("# Note\n", encoding="utf-8")
    assert verify.consult_target(
        {"tool_name": "Read", "tool_input": {"file_path": str(note)}}, omi
    ) == ("read", str(note))
    # A Read outside the OMI folder, and a non-consult tool, are not consults.
    outside = {"tool_name": "Read", "tool_input": {"file_path": "/etc/hosts"}}
    assert verify.consult_target(outside, omi) is None
    bash = {"tool_name": "Bash", "tool_input": {"command": "ls"}}
    assert verify.consult_target(bash, omi) is None


def test_judge_prefilter_high_and_low_skip_the_model(monkeypatch: pytest.MonkeyPatch) -> None:
    # If the model were called these would blow up; the prefilter must short-circuit.
    monkeypatch.setattr(verify, "_ask_model", lambda *a, **k: (_ for _ in ()).throw(AssertionError))
    assert verify.judge("codeberg release push", "codeberg release push mirror") is True
    assert verify.judge("codeberg release push", "banana mango smoothie") is False
    # No task / no text -> fail open (relevant).
    assert verify.judge("", "anything") is True
    assert verify.judge("task", "") is True


def test_judge_middle_band_consults_the_model(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, str]] = []

    def fake_model(task: str, text: str) -> bool | None:
        calls.append((task, text))
        return False

    monkeypatch.setattr(verify, "_ask_model", fake_model)
    # ~one of three task terms overlap -> middle band -> model decides.
    assert verify.judge("codeberg release push", "codeberg notes about other things") is False
    assert calls  # the model was actually consulted


def test_ask_model_fails_open_without_claude(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(verify.shutil, "which", lambda _name: None)
    assert verify._ask_model("t", "x") is None  # no binary -> None -> caller fails open


def test_verify_consult_relevant_records_no_violation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(verify, "_ask_model", lambda *a, **k: None)
    omi = _omi(tmp_path)
    note = omi / "Codeberg.md"
    note.write_text("# Codeberg\n\ncodeberg release push mirror workflow\n", encoding="utf-8")
    guard.begin_turn("v1", "how to codeberg release push")
    guard.mark_consulted("v1")  # gate open (as the bash touch would)
    out = io.StringIO()
    verdict = verify.verify_consult(
        {"tool_name": "Read", "session_id": "v1", "tool_input": {"file_path": str(note)}},
        omi,
        out=out,
    )
    assert verdict == "relevant"
    assert compliance.read_events() == []  # nothing logged
    assert guard.consults("v1")[0]["relevant"] is True


def test_verify_consult_irrelevant_warn_logs_but_keeps_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(verify, "_ask_model", lambda *a, **k: None)
    omi = _omi(tmp_path)
    note = omi / "Smoothie.md"
    note.write_text("# Smoothie\n\nbanana mango ice recipe\n", encoding="utf-8")
    guard.begin_turn("v2", "how to codeberg release push")
    guard.mark_consulted("v2")
    out = io.StringIO()
    verdict = verify.verify_consult(
        {"tool_name": "Read", "session_id": "v2", "tool_input": {"file_path": str(note)}},
        omi,
        require=False,
        out=out,
    )
    assert verdict == "irrelevant"
    assert compliance.read_events()[-1]["rule_id"] == verify.OFF_TOPIC_RULE
    assert "off-topic" in out.getvalue()
    assert guard.consulted_this_turn("v2")  # WARN mode leaves the gate alone


def test_verify_consult_require_mode_recloses_the_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(verify, "_ask_model", lambda *a, **k: None)
    omi = _omi(tmp_path)
    note = omi / "Smoothie.md"
    note.write_text("# Smoothie\n\nbanana mango ice recipe\n", encoding="utf-8")
    guard.begin_turn("v3", "how to codeberg release push")
    guard.mark_consulted("v3")
    verify.verify_consult(
        {"tool_name": "Read", "session_id": "v3", "tool_input": {"file_path": str(note)}},
        omi,
        require=True,
        out=io.StringIO(),
    )
    assert not guard.consulted_this_turn("v3")  # REQUIRE re-closed the gate


def test_verify_consult_require_keeps_gate_when_a_relevant_consult_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(verify, "_ask_model", lambda *a, **k: None)
    omi = _omi(tmp_path)
    good = omi / "Codeberg.md"
    good.write_text("# Codeberg\n\ncodeberg release push mirror\n", encoding="utf-8")
    bad = omi / "Smoothie.md"
    bad.write_text("# Smoothie\n\nbanana mango\n", encoding="utf-8")
    guard.begin_turn("v4", "how to codeberg release push")
    guard.mark_consulted("v4")
    base = {"tool_name": "Read", "session_id": "v4"}
    verify.verify_consult({**base, "tool_input": {"file_path": str(good)}}, omi, require=True)
    verify.verify_consult(
        {**base, "tool_input": {"file_path": str(bad)}}, omi, require=True, out=io.StringIO()
    )
    assert guard.consulted_this_turn("v4")  # a relevant consult exists -> gate stays open


def test_verify_consult_ignores_non_consult_events(tmp_path: Path) -> None:
    omi = _omi(tmp_path)
    assert verify.verify_consult(
        {"tool_name": "Bash", "session_id": "v5", "tool_input": {"command": "ls"}}, omi
    ) is None


def test_run_guard_verify_action(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(verify, "_ask_model", lambda *a, **k: None)
    omi = _omi(tmp_path)
    note = omi / "Codeberg.md"
    note.write_text("# Codeberg\n\ncodeberg release push\n", encoding="utf-8")
    guard.begin_turn("v6", "codeberg release push")
    guard.mark_consulted("v6")
    event = io.StringIO(
        json.dumps(
            {"tool_name": "Read", "session_id": "v6", "tool_input": {"file_path": str(note)}}
        )
    )
    assert guard.run_guard("verify", event, omi_dir=omi) == 0
