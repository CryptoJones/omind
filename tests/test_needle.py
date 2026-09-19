# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Aaron K. Clark
"""Tests for the A/B needle replay harness (#321). Everything up to the model
call is deterministic; the model is a function the tests supply."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from omind import ai_usage, compliance, needle
from omind.store import NoteFields, OmiStore


def _vault(tmp_path: Path) -> Path:
    omi = tmp_path / "OMI"
    omi.mkdir()
    OmiStore(omi).create_note(
        NoteFields(
            title="Release signing procedure",
            summary="How release signing works and why the signing key rotation failed.",
            details="Release signing uses the offline signing key; rotation failed in August.",
            tags=["release", "signing"],
        )
    )
    return omi


def _transcript(tmp_path: Path, turns: int = 12) -> Path:
    path = tmp_path / "session.jsonl"
    rows: list[dict[str, object]] = []
    for i in range(turns):
        rows.append({"type": "user", "message": {"content": f"why did release signing fail {i}"}})
        filler = {"type": "text", "text": f"Investigating step {i}. " * 20}
        rows.append({"type": "assistant", "message": {"content": [filler]}})
        rows.append(
            {
                "type": "user",
                "message": {"content": [{"type": "tool_result", "content": "x" * 5_000}]},
            }
        )
    rows.append({"type": "user", "isMeta": True, "message": {"content": "meta noise"}})
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return path


def test_needles_are_deterministic_and_distinct_per_depth_and_trial() -> None:
    assert needle.make_needle(0, 20) == needle.make_needle(0, 20)
    made = {needle.make_needle(t, d) for t in range(3) for d in needle.DEPTHS}
    assert len(made) == 9
    assert all(n.passphrase in n.text and n.ack in n.text for n in made)


def test_load_transcript_keeps_prompts_and_caps_tool_results(tmp_path: Path) -> None:
    segments = needle.load_transcript(_transcript(tmp_path, turns=2))
    assert [s.role for s in segments] == ["user", "assistant", "tool"] * 2  # meta dropped
    assert max(len(s.text) for s in segments if s.role == "tool") == 2_000
    with pytest.raises(ValueError, match="no usable content"):
        empty = tmp_path / "empty.jsonl"
        empty.write_text("\n", encoding="utf-8")
        needle.load_transcript(empty)


def test_needle_lands_at_the_requested_depth(tmp_path: Path) -> None:
    segments = needle.load_transcript(_transcript(tmp_path))
    for depth in needle.DEPTHS:
        planted = needle.make_needle(0, depth)
        prompt, injected = needle.build_arm(segments, planted, depth)
        assert injected == 0
        position = 100.0 * prompt.index(planted.passphrase) / len(prompt)
        assert abs(position - depth) < 12  # segment-boundary granularity
        assert prompt.rstrip().endswith(planted.question)


def test_on_arm_replays_preflight_after_each_user_prompt(tmp_path: Path) -> None:
    segments = needle.load_transcript(_transcript(tmp_path, turns=3))
    planted = needle.make_needle(0, 50)
    off, _ = needle.build_arm(segments, planted, 50)
    on, injected = needle.build_arm(segments, planted, 50, inject=lambda _p: "MEMORY PUSH")
    assert on.count("MEMORY PUSH") == 3 and injected == 3 * len("MEMORY PUSH")
    assert "MEMORY PUSH" not in off and len(on) > len(off)


def test_scoring_needs_the_fact_and_the_final_line_instruction() -> None:
    planted = needle.make_needle(1, 80)
    assert needle.score(f"It is {planted.passphrase}.\n{planted.ack}", planted) == (True, True)
    assert needle.score(f"{planted.ack}\nIt is {planted.passphrase}.", planted) == (True, False)
    assert needle.score("I could not find it.", planted) == (False, False)
    assert needle.score(None, planted) == (False, False)


def test_run_reports_per_depth_recall_and_the_on_off_delta(tmp_path: Path) -> None:
    omi = _vault(tmp_path)

    def model(prompt: str) -> str:
        # A model that context-rots: it only finds the needle when no preflight
        # memory was pushed into the transcript.
        import re

        if "[memory]" in prompt:
            return "not sure"
        found = re.search(r"passphrase for project \w+ is '([^']+)'.*?exact line (ACK-\w+)", prompt)
        assert found
        return f"{found.group(1)}\n{found.group(2)}"

    report = needle.run_needle(omi, _transcript(tmp_path), mode="inject", model=model)
    by = {m.name: m for m in report.measurements}
    assert by["replayed preflight (inject)"].value > 0  # the replica actually spoke
    for depth in needle.DEPTHS:
        assert by[f"recall @{depth}%, preflight off"].value == 100.0
        assert by[f"recall @{depth}%, preflight on"].value == 0.0
    assert by["recall delta (on - off)"].value == -100.0
    assert by["adherence delta (on - off)"].value == -100.0


def test_without_a_model_the_arms_are_built_and_nothing_is_scored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    omi = _vault(tmp_path)
    monkeypatch.setattr(ai_usage, "resolve_model_backend", lambda: None)
    out = tmp_path / "arms"
    report = needle.run_needle(omi, _transcript(tmp_path), emit=out)
    names = [m.name for m in report.measurements]
    assert "model answers" in names and not any(n.startswith("recall") for n in names)
    assert len(list(out.glob("*.txt"))) == len(needle.DEPTHS) * 2


def test_the_replay_is_read_only(tmp_path: Path) -> None:
    omi = _vault(tmp_path)
    before = sorted((p.name, p.read_bytes()) for p in omi.glob("*.md"))
    needle.run_needle(omi, _transcript(tmp_path), mode="inject", model=lambda _p: "x")
    assert sorted((p.name, p.read_bytes()) for p in omi.glob("*.md")) == before
    assert compliance.read_events() == []
    assert ai_usage.read_events(omi) == []


def test_cli_requires_a_transcript(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from omind import cli

    vault = tmp_path / "vault"
    (vault / "OMI").mkdir(parents=True)
    argv = ["bench", "--needle", "--vault", str(vault), "--folder", "OMI"]
    assert cli.main(argv) == 2
    assert "--transcript" in capsys.readouterr().err
    assert cli.main([*argv, "--transcript", str(tmp_path / "missing.jsonl")]) == 2
