# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Aaron K. Clark
"""Tests for the verifier — Layer C."""

from __future__ import annotations

import io
import json
from datetime import datetime
from pathlib import Path

import pytest

from omind import compliance, guard, hooks, verify


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


def test_index_and_scaffolding_reads_are_not_consults(tmp_path: Path) -> None:
    """The vault TOC (index.md), MEMORY.md and the template are 'relevant to
    everything' — reading one is the gate-dodge, not a consult. consult_target
    must NOT recognize them (otherwise the PostToolUse verifier's record_consult
    re-creates the sentinel and re-clears the gate a bash-only fix kept closed)."""
    omi = _omi(tmp_path)
    for name in ("index.md", "MEMORY.md", "Memory Template.md"):
        (omi / name).write_text("## Recent Memories\n", encoding="utf-8")
        event = {"tool_name": "Read", "tool_input": {"file_path": str(omi / name)}}
        assert verify.consult_target(event, omi) is None, name
    # a real content note under the OMI folder is still a consult
    note = omi / "Codeberg.md"
    note.write_text("# Codeberg\n", encoding="utf-8")
    assert verify.consult_target(
        {"tool_name": "Read", "tool_input": {"file_path": str(note)}}, omi
    ) == ("read", str(note))


def test_verify_consult_index_read_does_not_clear_the_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end: reading the index with the gate CLOSED must not satisfy it via
    the PostToolUse path — verify_consult returns None, records nothing, and leaves
    the gate closed; a real content note read still clears it (the control)."""
    monkeypatch.setattr(verify, "_ask_model", lambda *a, **k: None)
    omi = _omi(tmp_path)
    (omi / "index.md").write_text("## Recent Memories\n- [[Whatever]]\n", encoding="utf-8")
    guard.begin_turn("idx", "how to codeberg release push")
    guard.clear_gate("idx")  # gate CLOSED — turn start, nothing consulted yet
    verdict = verify.verify_consult(
        {
            "tool_name": "Read",
            "session_id": "idx",
            "tool_input": {"file_path": str(omi / "index.md")},
        },
        omi,
    )
    assert verdict is None  # not a consult
    assert not guard.consulted_this_turn("idx")  # gate STILL closed — dodge defeated
    assert guard.consults("idx") == []  # nothing recorded
    # control: a real content note read DOES clear the gate
    note = omi / "Codeberg.md"
    note.write_text("# Codeberg\n\ncodeberg release push mirror workflow\n", encoding="utf-8")
    verify.verify_consult(
        {
            "tool_name": "Read",
            "session_id": "idx",
            "tool_input": {"file_path": str(note)},
        },
        omi,
    )
    assert guard.consulted_this_turn("idx")  # a real consult clears the gate


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
    monkeypatch.setenv("OMI_VERIFY_OFFTOPIC_ESCALATE", "1")  # #98: enforce from the 1st off-topic
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


def test_verify_require_caps_recloses_so_it_cannot_deadlock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A terse/abstract task scores ~0 against every note, so naive REQUIRE would
    re-close the gate on every consult forever — an unbreakable wedge. The per-turn
    cap breaks it: past the cap the verifier degrades to WARN (gate stays open) and
    logs the floor. A verifier must never deadlock the agent."""
    monkeypatch.setattr(verify, "_ask_model", lambda *a, **k: None)
    monkeypatch.setenv("OMI_VERIFY_MAX_RECLOSE", "1")
    monkeypatch.setenv("OMI_VERIFY_OFFTOPIC_ESCALATE", "1")  # #98: enforce from the 1st off-topic
    omi = _omi(tmp_path)
    note = omi / "Smoothie.md"
    note.write_text("# Smoothie\n\nbanana mango ice recipe\n", encoding="utf-8")
    guard.begin_turn("cap", "how to codeberg release push")  # zero the re-close counter
    payload = {"tool_name": "Read", "session_id": "cap", "tool_input": {"file_path": str(note)}}

    # 1st off-topic consult: within the cap -> re-closes the gate (forces a retry).
    guard.mark_consulted("cap")
    verify.verify_consult(payload, omi, require=True, out=io.StringIO())
    assert not guard.consulted_this_turn("cap")

    # 2nd off-topic consult: past the cap -> WARN, gate STAYS open, floor logged.
    guard.mark_consulted("cap")
    verify.verify_consult(payload, omi, require=True, out=io.StringIO())
    assert guard.consulted_this_turn("cap")  # the agent is never deadlocked
    assert compliance.read_events()[-1]["rule_id"] == verify.NO_RELEVANT_FLOOR_RULE


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


# -- 2.41.1: tunable thresholds, always-relevant allowlist, explain, past mistakes --


def test_tunable_thresholds(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*a: object, **k: object) -> bool:
        raise AssertionError("model should not be called")

    monkeypatch.setattr(verify, "_ask_model", boom)
    # zero overlap -> deterministic irrelevant by default
    assert verify.judge("codeberg release", "banana mango smoothie") is False
    # lower HIGH to 0 -> any score is "high" -> relevant, still no model call
    monkeypatch.setenv("OMI_VERIFY_HIGH", "0.0")
    assert verify.judge("codeberg release", "banana mango smoothie") is True


def test_always_relevant_allowlist(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(verify, "_ask_model", lambda *a, **k: None)
    omi = _omi(tmp_path)
    note = omi / "codeberg-authoritative.md"
    note.write_text("# Codeberg\n\nhosting order mirror push remote\n", encoding="utf-8")
    guard.begin_turn("ar", "cut the release please")  # terse -> would score low/irrelevant
    guard.mark_consulted("ar")
    monkeypatch.setenv("OMI_VERIFY_ALWAYS_RELEVANT", "codeberg-authoritative")
    verdict = verify.verify_consult(
        {"tool_name": "Read", "session_id": "ar", "tool_input": {"file_path": str(note)}},
        omi,
        require=True,
        out=io.StringIO(),
    )
    assert verdict == "relevant"  # allowlisted -> never re-closes the gate
    assert guard.consulted_this_turn("ar")


def test_past_mistakes_context() -> None:
    assert verify._past_mistakes_context() == ""  # none yet
    compliance.log_event(
        compliance.KIND_VIOLATION, rule_id=verify.OFF_TOPIC_RULE, command="Smoothie.md",
        outcome="irrelevant",
    )
    ctx = verify._past_mistakes_context()
    assert "Smoothie.md" in ctx and "off-topic" in ctx.lower()


def test_explain_consult(tmp_path: Path) -> None:
    omi = _omi(tmp_path)
    note = omi / "Codeberg.md"
    note.write_text("# Codeberg\n\ncodeberg release push mirror workflow\n", encoding="utf-8")
    guard.begin_turn("ex", "how to codeberg release push mirror")
    info = verify.explain_consult(
        {"tool_name": "Read", "session_id": "ex", "tool_input": {"file_path": str(note)}}, omi
    )
    assert info is not None
    assert info["kind"] == "read" and info["score"] >= 0.5
    assert "high" in info["band"] and info["verdict"] is True
    bash = {"tool_name": "Bash", "tool_input": {"command": "ls"}}
    assert verify.explain_consult(bash, omi) is None


def test_run_guard_verify_explain(tmp_path: Path) -> None:
    omi = _omi(tmp_path)
    note = omi / "Codeberg.md"
    note.write_text("# Codeberg\n\ncodeberg release push\n", encoding="utf-8")
    guard.begin_turn("ge", "codeberg release push")
    payload = {"tool_name": "Read", "session_id": "ge", "tool_input": {"file_path": str(note)}}
    event = io.StringIO(json.dumps(payload))
    assert guard.run_guard("verify", event, omi_dir=omi, explain=True) == 0


# -- 2.43.2: a consult that addresses the task in different WORD FORMS is judged
#    relevant on the first try, deterministically (no model, no re-close) --


def test_word_form_variant_consult_is_relevant_first_try(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The model must NOT be needed: stemming lifts the deterministic score into
    # the relevant band even though task and note share no exact token.
    monkeypatch.setattr(
        verify, "_ask_model", lambda *a, **k: (_ for _ in ()).throw(AssertionError)
    )
    omi = _omi(tmp_path)
    note = omi / "Verifier.md"
    note.write_text(
        "# Verifier\n\nthe verifier scores how relevant each consult is\n", encoding="utf-8"
    )
    # Chatty task in different word forms than the note ("scoring" vs "scores",
    # "relevance" vs "relevant", "consults" vs "consult").
    guard.begin_turn("wf", "please fix the verifier relevance scoring before we move on")
    guard.mark_consulted("wf")
    verdict = verify.verify_consult(
        {"tool_name": "Read", "session_id": "wf", "tool_input": {"file_path": str(note)}},
        omi,
        require=True,
        out=io.StringIO(),
    )
    assert verdict == "relevant"
    assert guard.consulted_this_turn("wf")  # never re-closed: relevant on first try


# -- #95: score relevance against what the agent is DOING, not only the last
#    user prompt — delegated/background work must not re-close the gate --


def _journal(omi: Path, session: str, *details: str, now: datetime | None = None) -> None:
    """Write today's session journal with one bullet per ``details`` entry, in the
    exact shape ``verify.recent_activity`` parses."""
    when = now or datetime.now()
    directory = hooks.journal_dir(omi)
    directory.mkdir(parents=True, exist_ok=True)
    sid = hooks.short_session_id(session)
    lines = [f"- 09:0{i} [session {sid}] PostToolUse {d}" for i, d in enumerate(details)]
    (directory / hooks.journal_name(when)).write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_recent_activity_filters_by_session_and_drops_omi_consults(tmp_path: Path) -> None:
    omi = _omi(tmp_path)
    _journal(
        omi,
        "build1",
        "Read -> /src/matcher/merge.rs (ok)",
        "Bash -> `cargo build -p matcher` (ok)",
        f"Read -> {omi}/Some Note.md (ok)",  # an OMI read: must be excluded
    )
    # A different session's bullets must not leak in.
    other = hooks.journal_dir(omi) / hooks.journal_name(datetime.now())
    other_sid = hooks.short_session_id("other9")
    other.write_text(
        other.read_text(encoding="utf-8")
        + f"- 09:09 [session {other_sid}] PostToolUse Read -> /x/secret.py (ok)\n",
        encoding="utf-8",
    )
    activity = verify.recent_activity("build1", omi)
    assert "matcher" in activity and "cargo" in activity
    assert "Some Note" not in activity  # OMI consult excluded (no relevance bootstrap)
    assert "secret" not in activity  # other session excluded
    assert "PostToolUse" not in activity and "(ok)" not in activity  # scaffolding stripped


def test_empty_journal_falls_back_to_task_only(tmp_path: Path) -> None:
    omi = _omi(tmp_path)
    assert verify.recent_activity("nojournal", omi) == ""  # no journal -> no signal


def test_delegated_work_consult_is_relevant_via_activity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The model must never be needed: the deterministic activity overlap alone
    # lifts the consult into the relevant band.
    monkeypatch.setattr(
        verify, "_ask_model", lambda *a, **k: (_ for _ in ()).throw(AssertionError)
    )
    omi = _omi(tmp_path)
    note = omi / "Matcher Merge.md"
    note.write_text(
        "# Matcher Merge\n\nthe matcher crate merge and bsim signature context\n",
        encoding="utf-8",
    )
    # The user delegated background work: the captured turn task is about virus
    # samples / guard rails, but the agent has actually been building the matcher.
    task = "pull virus samples and test the guard rails"
    guard.begin_turn("build1", task)
    guard.mark_consulted("build1")
    _journal(
        omi,
        "build1",
        "Read -> /src/matcher/merge.rs (ok)",
        "Grep -> matcher merge bsim signature (ok)",
    )
    # Pre-fix: judged against the user line alone, this consult is OFF-topic.
    assert verify.judge(task, note.read_text(encoding="utf-8")) is False
    # Post-fix: blending in what the agent is DOING makes it relevant.
    verdict = verify.verify_consult(
        {"tool_name": "Read", "session_id": "build1", "tool_input": {"file_path": str(note)}},
        omi,
        require=True,
        out=io.StringIO(),
    )
    assert verdict == "relevant"
    assert guard.consulted_this_turn("build1")  # gate not re-closed


def test_consult_off_topic_to_both_task_and_activity_still_irrelevant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The widening must not defeat the gate: a consult matching NEITHER the task
    # nor the agent's activity is still irrelevant.
    monkeypatch.setattr(
        verify, "_ask_model", lambda *a, **k: (_ for _ in ()).throw(AssertionError)
    )
    omi = _omi(tmp_path)
    note = omi / "Banana.md"
    note.write_text("# Banana\n\nbanana mango smoothie recipe\n", encoding="utf-8")
    task = "pull virus samples and test the guard rails"
    guard.begin_turn("build2", task)
    guard.mark_consulted("build2")
    _journal(omi, "build2", "Read -> /src/matcher/merge.rs (ok)")
    verdict = verify.verify_consult(
        {"tool_name": "Read", "session_id": "build2", "tool_input": {"file_path": str(note)}},
        omi,
        require=True,
        out=io.StringIO(),
    )
    assert verdict == "irrelevant"


# -- #96: blend the gate-blocked action (pending intent) so the FIRST consult after
#    a work-transition clears — task + activity both cold, but the blocked action hot --


def test_gate_block_records_pending_intent_and_turn_start_clears_it() -> None:
    guard.begin_turn("pi", "some task")
    # Not consulted yet: the gate blocks a (benign) Bash action and records its command.
    verdict = guard.decide(
        {"tool": "Bash", "command": "cargo test -p scylla-merge", "session": "pi"}
    )
    assert not verdict.allow and verdict.rule_id == "omi-gate"
    assert guard.pending_intent("pi") == "cargo test -p scylla-merge"
    guard.begin_turn("pi", "next turn")  # turn start resets the per-turn pending intent
    assert guard.pending_intent("pi") == ""


def test_transition_consult_relevant_via_pending_intent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The model must never be needed: the pending-action overlap alone lifts the
    # consult into the relevant band. Task + activity are both COLD (previous thread).
    monkeypatch.setattr(
        verify, "_ask_model", lambda *a, **k: (_ for _ in ()).throw(AssertionError)
    )
    omi = _omi(tmp_path)
    note = omi / "Matcher.md"
    note.write_text(
        "# Matcher\n\nthe matcher crate merge and bsim signature context\n", encoding="utf-8"
    )
    guard.begin_turn("xfer", "go back to looping the work")  # terse, off-topic to the note
    guard.mark_consulted("xfer")
    _journal(omi, "xfer", "Read -> /docs/resume.tex (ok)", "Bash -> `git push` (ok)")  # prev thread
    # The agent pivots to matcher work; the gate blocked that action, recording its intent.
    guard.record_pending("xfer", "grep -rn matcher merge bsim signature crates/scylla-merge")
    verdict = verify.verify_consult(
        {"tool_name": "Read", "session_id": "xfer", "tool_input": {"file_path": str(note)}},
        omi,
        require=True,
        out=io.StringIO(),
    )
    assert verdict == "relevant"
    assert guard.consulted_this_turn("xfer")  # not re-closed: the pending intent matched


def test_consult_off_topic_to_task_activity_and_pending_still_irrelevant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The third signal must not defeat the gate either: a consult matching NONE of
    # task / activity / pending is still irrelevant.
    monkeypatch.setattr(
        verify, "_ask_model", lambda *a, **k: (_ for _ in ()).throw(AssertionError)
    )
    omi = _omi(tmp_path)
    note = omi / "Banana.md"
    note.write_text("# Banana\n\nbanana mango smoothie recipe\n", encoding="utf-8")
    guard.begin_turn("b3", "pull virus samples and test the guard rails")
    guard.mark_consulted("b3")
    _journal(omi, "b3", "Read -> /src/matcher/merge.rs (ok)")
    guard.record_pending("b3", "cargo build -p scylla-merge")
    verdict = verify.verify_consult(
        {"tool_name": "Read", "session_id": "b3", "tool_input": {"file_path": str(note)}},
        omi,
        require=True,
        out=io.StringIO(),
    )
    assert verdict == "irrelevant"  # banana matches none of task / activity / pending


# -- #98: the GRADUATED gate — off-topic is a WARNING until a sustained streak crosses
#    the threshold; a relevant consult resets it; only then does REQUIRE re-close --


def test_offtopic_streak_warns_below_threshold_then_enforces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(verify, "_ask_model", lambda *a, **k: None)
    monkeypatch.setenv("OMI_VERIFY_OFFTOPIC_ESCALATE", "2")  # warn once, enforce on the 2nd
    omi = _omi(tmp_path)
    note = omi / "Smoothie.md"
    note.write_text("# Smoothie\n\nbanana mango ice recipe\n", encoding="utf-8")
    guard.begin_turn("g1", "how to codeberg release push")
    payload = {"tool_name": "Read", "session_id": "g1", "tool_input": {"file_path": str(note)}}

    # 1st off-topic: WARN only — the gate is left open (the agent isn't taxed yet).
    guard.mark_consulted("g1")
    verify.verify_consult(payload, omi, require=True, out=io.StringIO())
    assert guard.consulted_this_turn("g1")
    assert guard.offtopic_count("g1") == 1

    # 2nd off-topic: streak hits the threshold -> REQUIRE re-closes, escalation logged.
    guard.mark_consulted("g1")
    verify.verify_consult(payload, omi, require=True, out=io.StringIO())
    assert not guard.consulted_this_turn("g1")
    assert compliance.read_events()[-1]["rule_id"] == verify.OFFTOPIC_ESCALATED_RULE


def test_relevant_consult_resets_the_offtopic_streak(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(verify, "_ask_model", lambda *a, **k: None)
    monkeypatch.setenv("OMI_VERIFY_OFFTOPIC_ESCALATE", "2")
    omi = _omi(tmp_path)
    good = omi / "Codeberg.md"
    good.write_text("# Codeberg\n\ncodeberg release push mirror\n", encoding="utf-8")
    bad = omi / "Smoothie.md"
    bad.write_text("# Smoothie\n\nbanana mango\n", encoding="utf-8")
    base = {"tool_name": "Read", "session_id": "g2"}
    guard.begin_turn("g2", "how to codeberg release push")
    guard.mark_consulted("g2")

    verify.verify_consult(
        {**base, "tool_input": {"file_path": str(bad)}}, omi, require=True, out=io.StringIO()
    )
    assert guard.offtopic_count("g2") == 1  # off-topic bumps the streak
    verify.verify_consult({**base, "tool_input": {"file_path": str(good)}}, omi, require=True)
    assert guard.offtopic_count("g2") == 0  # a RELEVANT consult resets it


def test_verify_consult_short_circuits_when_gate_paused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """While `omind guard pause` is engaged, an off-topic consult that would
    normally log a violation + re-close (REQUIRE mode) is judged relevant with no
    model call, no violation, and no re-close — the gate is open anyway."""
    monkeypatch.setattr(verify, "_ask_model", lambda *a, **k: None)
    omi = _omi(tmp_path)
    note = omi / "Smoothie.md"
    note.write_text("# Smoothie\n\nbanana mango ice recipe\n", encoding="utf-8")
    guard.begin_turn("vpause", "how to codeberg release push")  # off-topic vs the note
    guard.mark_consulted("vpause")
    before = len(compliance.read_events())
    guard.pause_gate(60)
    verdict = verify.verify_consult(
        {"tool_name": "Read", "session_id": "vpause", "tool_input": {"file_path": str(note)}},
        omi,
        require=True,
    )
    assert verdict == "relevant"  # paused: judged relevant, enforcement skipped
    assert guard.consulted_this_turn("vpause")  # gate not re-closed
    assert len(compliance.read_events()) == before  # no off-topic violation logged
    guard.resume_gate()


def test_semantic_blend_rescues_a_keyword_poor_consult(monkeypatch: pytest.MonkeyPatch) -> None:
    """3.0.0: a consult that is on-topic in MEANING but shares no keywords with the
    task is off-topic under keyword overlap alone, but relevant once the semantic
    backend rates it close — the false-negative friction the blend targets."""
    task = "publish a new version to the git remote"
    text = "the steps to cut a release and ship it to the forge"
    # keyword-only (no backend): no shared terms -> judged irrelevant, as in 2.x
    monkeypatch.setattr(verify.embed, "similarity", lambda a, b: None)
    assert verify.judge(task, text) is False
    # with a semantic backend rating them close, the blend lifts it to relevant
    monkeypatch.setattr(verify.embed, "similarity", lambda a, b: 0.82)
    assert verify.judge(task, text) is True
