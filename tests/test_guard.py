# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Aaron K. Clark
"""Tests for the harness-agnostic OMI-compliance guard decision engine."""

from __future__ import annotations

import importlib.resources
import io
import json
from pathlib import Path

import pytest

from omind import guard, paths


def test_omi_consult_is_allowed_and_sets_the_per_turn_sentinel() -> None:
    guard.clear_gate("s1")
    assert guard.decide({"is_omi_consult": True, "session": "s1"}).allow
    assert guard.consulted_this_turn("s1")
    guard.clear_gate("s1")


def test_hard_block_fires_even_when_consulted() -> None:
    guard.mark_consulted("s2")  # gate is satisfied, yet a hard rule still wins
    verdict = guard.decide({"tool": "Bash", "command": "gh pr merge 9", "session": "s2"})
    assert not verdict.allow
    assert "hard" in verdict.reason
    guard.clear_gate("s2")


def test_gate_blocks_until_consulted_then_re_arms() -> None:
    guard.clear_gate("s3")
    assert not guard.decide({"command": "ls", "session": "s3"}).allow  # nothing consulted
    guard.decide({"is_omi_consult": True, "session": "s3"})  # consult
    assert guard.decide({"command": "ls", "session": "s3"}).allow  # cleared for the turn
    guard.clear_gate("s3")  # turn-start reset
    assert not guard.decide({"command": "ls", "session": "s3"}).allow  # re-armed


def test_full_destructive_set_is_blocked() -> None:
    guard.mark_consulted("s4")
    for cmd in (
        "gh auth setup-git",
        "git push https://github.com/x/y.git main",
        "git push github main",
        "gh pr create --title x",
        "gh repo delete x/y",
        "gh api -X DELETE repos/x/y",
    ):
        assert not guard.decide({"command": cmd, "session": "s4"}).allow, cmd
    guard.clear_gate("s4")


def test_codeberg_push_is_allowed_after_consult() -> None:
    guard.mark_consulted("s5")
    cmd = "git push git@codeberg.org:CryptoJones/omind.git main"
    assert guard.decide({"command": cmd, "session": "s5"}).allow
    guard.clear_gate("s5")


def test_github_push_is_opt_in_not_hard() -> None:
    guard.mark_consulted("s7")
    bare = "git push https://x@github.com/CryptoJones/omind.git main"
    assert not guard.decide({"command": bare, "session": "s7"}).allow  # blocked by default
    optin = "OMI_PUSH_GITHUB=1 " + bare
    assert guard.decide({"command": optin, "session": "s7"}).allow  # deliberate push allowed
    # the opt-in does NOT bypass the absolute hard rules
    assert not guard.decide(
        {"command": "OMI_PUSH_GITHUB=1 gh pr create --title x", "session": "s7"}
    ).allow
    assert not guard.decide(
        {"command": "OMI_PUSH_GITHUB=1 gh repo delete x/y", "session": "s7"}
    ).allow
    guard.clear_gate("s7")


def test_run_guard_check_and_reset_exit_codes() -> None:
    guard.clear_gate("s6")
    blocked = guard.run_guard("check", io.StringIO(json.dumps({"command": "ls", "session": "s6"})))
    assert blocked == 2
    ok = guard.run_guard(
        "check", io.StringIO(json.dumps({"is_omi_consult": True, "session": "s6"}))
    )
    assert ok == 0
    assert guard.run_guard("reset", io.StringIO(json.dumps({"session": "s6"}))) == 0
    assert not guard.consulted_this_turn("s6")


def test_clear_gate_reaps_legacy_tmp_sentinels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(guard, "_LEGACY_SENTINEL_DIRS", (tmp_path,))
    legacy = tmp_path / "omi-gate-deadbeef"
    legacy.write_text("")
    unrelated = tmp_path / "keep.txt"
    unrelated.write_text("x")
    guard.clear_gate("sReap")
    assert not legacy.exists()  # stale prototype sentinel reaped
    assert unrelated.exists()  # unrelated files untouched


def test_sentinel_path_lives_in_state_dir() -> None:
    assert guard._sentinel_path("abc.def") == paths.state_dir() / "gate-abc.def"


def test_guard_and_reset_adapters_share_one_sentinel_path() -> None:
    """Regression for the /tmp-vs-state-dir drift: the guard and reset adapters
    must compute the same per-turn sentinel path, and the guard must never use
    the legacy /tmp path (only the reset reaps it)."""
    files = importlib.resources.files("omind")
    guard_sh = files.joinpath("omi-guard.sh").read_text(encoding="utf-8")
    reset_sh = files.joinpath("omi-gate-reset.sh").read_text(encoding="utf-8")
    state_expr = "${XDG_STATE_HOME:-$HOME/.local/state}/omind"
    assert state_expr in guard_sh and "gate-$sid" in guard_sh
    assert state_expr in reset_sh and "gate-$sid" in reset_sh
    assert "/tmp/omi-gate" not in guard_sh


def test_turn_task_capture_roundtrip() -> None:
    guard.begin_turn("t1", "fix the codeberg release workflow")
    assert guard.turn_task("t1") == "fix the codeberg release workflow"
    assert guard.turn_task("never-set") == ""  # never raises on a missing turn file


def test_reset_clears_gate_and_captures_task() -> None:
    guard.mark_consulted("t2")
    assert guard.consulted_this_turn("t2")
    guard.run_guard(
        "reset", io.StringIO(json.dumps({"session_id": "t2", "prompt": "do the thing"}))
    )
    assert not guard.consulted_this_turn("t2")  # gate re-armed
    assert guard.turn_task("t2") == "do the thing"  # task captured for the verifier


def test_record_consult_accumulates_and_survives_a_bash_touch(tmp_path: Path) -> None:
    guard.record_consult("t3", kind="read", target="A.md", relevant=True)
    guard.record_consult("t3", kind="search", target="codeberg", relevant=None)
    recorded = guard.consults("t3")
    assert [c["target"] for c in recorded] == ["A.md", "codeberg"]
    assert recorded[0]["relevant"] is True
    # An empty file (as the bash adapter's `touch` leaves it) reads as no consults,
    # never a crash.
    guard._sentinel_path("t4").parent.mkdir(parents=True, exist_ok=True)
    guard._sentinel_path("t4").write_text("", encoding="utf-8")
    assert guard.consults("t4") == []
    assert guard.consulted_this_turn("t4")


def test_is_omi_consult_with_target_is_recorded() -> None:
    guard.clear_gate("t5")
    guard.decide(
        {
            "is_omi_consult": True,
            "session": "t5",
            "consult_target": "Note.md",
            "consult_kind": "read",
        }
    )
    assert guard.consults("t5")[0]["target"] == "Note.md"
    guard.clear_gate("t5")
