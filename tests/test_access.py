# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Aaron K. Clark
"""Tests for machine-local dynamic core membership."""

from __future__ import annotations

from pathlib import Path

from omind import access


def test_core_members_balance_frequency_and_recency(tmp_path: Path) -> None:
    omi = tmp_path / "OMI"
    omi.mkdir()
    now = 2_000_000_000.0
    access.record(omi, "Frequent.md", now=now - 86_400)
    access.record(omi, "Frequent.md", now=now - 86_400)
    access.record(omi, "Recent.md", now=now)
    assert access.core_members(omi, now=now) == ["Frequent.md", "Recent.md"]


def test_stale_accesses_demote_out_of_core(tmp_path: Path) -> None:
    omi = tmp_path / "OMI"
    omi.mkdir()
    now = 2_000_000_000.0
    access.record(omi, "Old.md", now=now - 91 * 86_400)
    access.record(omi, "Current.md", now=now)
    assert access.core_members(omi, now=now) == ["Current.md"]


def test_access_state_is_bounded_by_requested_core_size(tmp_path: Path) -> None:
    omi = tmp_path / "OMI"
    omi.mkdir()
    for number in range(10):
        access.record(omi, f"Note {number}.md", now=float(number + 1))
    assert len(access.core_members(omi, limit=3, now=10.0)) == 3
