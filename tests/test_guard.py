# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Aaron K. Clark
"""Tests for the harness-agnostic OMI-compliance guard decision engine."""

from __future__ import annotations

import io
import json

from omind import guard


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
