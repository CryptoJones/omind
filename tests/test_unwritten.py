# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Aaron K. Clark
"""Tests for the unwritten-work detector (#221).

The detector reads omind's OWN journal — never a transcript — and nudges when a
session did real work and recorded none of it. The bar for firing is
deliberately high: a false nudge trains people to ignore the true one.
"""

from __future__ import annotations

import pytest

from omind import unwritten


def _line(session: str, tool: str) -> str:
    return f"- 09:14 [session {session}] PostToolUse {tool} -> thing (ok)"


def _journal(session: str, tools: list[str]) -> str:
    return "\n".join(["# Worklog", ""] + [_line(session, t) for t in tools])


def test_silent_when_the_session_wrote_a_note() -> None:
    text = _journal("abc12345", ["Bash"] * 20 + ["mcp__omi__create-note"])
    assert unwritten.notice(text, "abc12345") is None


def test_fires_when_work_happened_and_nothing_was_written() -> None:
    text = _journal("abc12345", ["Bash"] * 20)
    message = unwritten.notice(text, "abc12345")
    assert message is not None
    assert "20 actions" in message
    assert "OMIND_NO_UNWRITTEN" in message  # the nudge states its own off switch


def test_silent_for_a_small_session() -> None:
    """Answering one question and stopping is not a lost memory."""
    assert unwritten.notice(_journal("abc12345", ["Bash"] * 3), "abc12345") is None


def test_reading_alone_is_not_work_worth_recording() -> None:
    text = _journal("abc12345", ["mcp__omi__search-vault", "mcp__omi__recall-note"] * 15)
    assert unwritten.notice(text, "abc12345") is None


def test_only_counts_the_session_asked_about() -> None:
    """A busy neighbour must not make a quiet session look guilty."""
    mine = _journal("aaaaaaaa", ["Bash"] * 2)
    theirs = _journal("bbbbbbbb", ["Bash"] * 40)
    assert unwritten.notice(mine + "\n" + theirs, "aaaaaaaa") is None


def test_stop_marker_is_not_an_action() -> None:
    text = "\n".join(
        [_line("abc12345", "Bash")] * 5
        + ["- 09:20 [session abc12345] Stop -> turn ended"] * 30
    )
    assert unwritten.notice(text, "abc12345") is None


def test_summarize_counts_actions_and_writes() -> None:
    text = _journal("abc12345", ["Bash", "Edit", "mcp__omi__create-note"])
    assert unwritten.summarize(text, "abc12345") == (3, 1)


def test_disabled_by_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMIND_NO_UNWRITTEN", "1")
    assert unwritten.disabled() is True
    assert unwritten.check("/nonexistent", "abc12345") is None


def test_check_never_raises_on_a_missing_vault() -> None:
    """This runs on the Stop path; breaking the ability to stop is unacceptable."""
    assert unwritten.check("/definitely/not/a/vault", "abc12345") is None
