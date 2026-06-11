# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Aaron K. Clark
"""Tests for omind.clock: Lamport revision parse, ordering, and ticking."""

from __future__ import annotations

import pytest

from omind.clock import Rev, next_rev


def test_str_round_trip() -> None:
    rev = Rev(counter=12, node_id="laptop-3f9a2c")
    assert str(rev) == "12@laptop-3f9a2c"
    assert Rev.parse(str(rev)) == rev


@pytest.mark.parametrize("raw", ["", "  ", "12", "@node", "12@", "x@node", "12@-bad", "1@a b"])
def test_parse_rejects_malformed(raw: str) -> None:
    assert Rev.parse(raw) is None


def test_parse_allows_hostname_characters() -> None:
    assert Rev.parse("3@host.lan_1-x") == Rev(counter=3, node_id="host.lan_1-x")


def test_ordering_by_counter_then_node_id() -> None:
    assert Rev(2, "a").newer_than(Rev(1, "z"))
    assert not Rev(1, "z").newer_than(Rev(2, "a"))
    # Tie-break: same counter, node-id decides — deterministically on every node.
    assert Rev(1, "b").newer_than(Rev(1, "a"))
    assert not Rev(1, "a").newer_than(Rev(1, "b"))
    assert not Rev(1, "a").newer_than(Rev(1, "a"))


def test_stamped_always_beats_legacy() -> None:
    assert Rev(1, "any").newer_than(None)


def test_next_rev_ticks_past_current() -> None:
    assert next_rev(None, "n1") == Rev(1, "n1")
    assert next_rev(Rev(4, "other"), "n1") == Rev(5, "n1")
