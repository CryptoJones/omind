# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Aaron K. Clark
"""Tests for machine-local dynamic core membership."""

from __future__ import annotations

from pathlib import Path

import pytest

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


# -- usefulness feedback (item #2) -----------------------------------------


def test_only_organic_reads_feed_the_usefulness_signal(tmp_path: Path) -> None:
    omi = tmp_path / "OMI"
    omi.mkdir()
    now = 2_000_000_000.0
    access.record(omi, "Organic.md", session="s1", now=now)
    access.record(omi, "Injected.md", organic=False, now=now)
    weights = access.usefulness_weights(omi, now=now)
    assert weights["Organic.md"].useful_reads == 1
    assert weights["Organic.md"].filtered_reads == 0
    # A hook/preflight read never counts as usefulness, but is accounted for.
    assert weights["Injected.md"].useful_reads == 0
    assert weights["Injected.md"].filtered_reads == 1
    assert weights["Injected.md"].weight == 1.0  # never organically read -> untouched


def test_rereads_in_one_session_count_once(tmp_path: Path) -> None:
    omi = tmp_path / "OMI"
    omi.mkdir()
    now = 2_000_000_000.0
    access.record(omi, "N.md", session="s1", now=now)
    access.record(omi, "N.md", session="s1", now=now)  # same session -> filtered
    access.record(omi, "N.md", session="s2", now=now)  # new session -> counts
    signal = access.usefulness_weights(omi, now=now)["N.md"]
    assert signal.useful_reads == 2
    assert signal.filtered_reads == 1


def test_usefulness_decays_only_after_onset_and_floors(tmp_path: Path) -> None:
    omi = tmp_path / "OMI"
    omi.mkdir()
    now = 2_000_000_000.0
    day = 86_400.0
    access.record(omi, "Fresh.md", session="s", now=now - 10 * day)
    access.record(omi, "Stale.md", session="s", now=now - 60 * day)
    access.record(omi, "Ancient.md", session="s", now=now - 200 * day)
    weights = access.usefulness_weights(omi, now=now)
    assert weights["Fresh.md"].weight == 1.0  # inside the 30-day onset -> no penalty
    assert 0.7 < weights["Stale.md"].weight < 1.0  # decaying toward the floor
    assert weights["Ancient.md"].weight == pytest.approx(0.7)  # floored, never buried


def test_usefulness_decay_is_monotone_never_a_boost(tmp_path: Path) -> None:
    omi = tmp_path / "OMI"
    omi.mkdir()
    now = 2_000_000_000.0
    day = 86_400.0
    for name, age in (("A.md", 40), ("B.md", 70), ("C.md", 100)):
        access.record(omi, name, session="s", now=now - age * day)
    weights = access.usefulness_weights(omi, now=now)
    # Strictly decreasing with staleness, and never above 1.0 (no self-reinforce).
    assert 1.0 > weights["A.md"].weight > weights["B.md"].weight > weights["C.md"].weight
    assert all(w.weight <= 1.0 for w in weights.values())


def test_core_members_still_counts_every_read(tmp_path: Path) -> None:
    """#173's scorer shares the table but is untouched by the organic split."""
    omi = tmp_path / "OMI"
    omi.mkdir()
    now = 2_000_000_000.0
    access.record(omi, "Hooked.md", organic=False, now=now)  # not a usefulness signal
    access.record(omi, "Hooked.md", organic=False, now=now)  # ...but still a read for #173
    access.record(omi, "Organic.md", session="s", now=now)
    assert access.core_members(omi, now=now)[0] == "Hooked.md"
