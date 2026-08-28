# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Aaron K. Clark
"""Tests for the scoped-write interlock (item #5)."""

from __future__ import annotations

import pytest

from omind import scope_guard


def test_unset_process_scope_fails_open() -> None:
    # The common case — no OMIND_SCOPE — never blocks, even a scoped note.
    assert scope_guard.check_write("buzz", env={}) is None


def test_unscoped_note_is_always_writable() -> None:
    assert scope_guard.check_write("", env={"OMIND_SCOPE": "buzz"}) is None


def test_matching_scope_is_allowed() -> None:
    assert scope_guard.check_write("buzz", env={"OMIND_SCOPE": "buzz"}) is None


def test_scope_labels_are_normalised_before_comparison() -> None:
    # "#Buzz" and "buzz" are the same scope, like store._clean_scope.
    assert scope_guard.check_write("#Buzz", env={"OMIND_SCOPE": "buzz"}) is None


def test_out_of_scope_write_is_denied_by_default() -> None:
    with pytest.raises(scope_guard.ScopeViolationError) as excinfo:
        scope_guard.check_write("antigua", env={"OMIND_SCOPE": "buzz"})
    message = str(excinfo.value)
    assert "buzz" in message and "antigua" in message
    assert "OMIND_SCOPE_MODE=warn" in message  # remediation is spelled out
    assert "not a security boundary" in message.lower()


def test_warn_mode_downgrades_deny_to_a_returned_warning() -> None:
    warning = scope_guard.check_write(
        "antigua", env={"OMIND_SCOPE": "buzz", "OMIND_SCOPE_MODE": "warn"}
    )
    assert warning is not None
    assert "antigua" in warning


def test_process_scope_reads_and_normalises_the_env() -> None:
    assert scope_guard.process_scope({"OMIND_SCOPE": "#Buzz "}) == "buzz"
    assert scope_guard.process_scope({}) == ""
