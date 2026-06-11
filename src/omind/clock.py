# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Aaron K. Clark
"""Logical versioning for the memory mesh: per-note Lamport revisions.

A revision is ``<counter>@<node-id>`` (e.g. ``12@laptop-3f9a2c``), stamped
into a note's ``## Metadata`` section. The counter is a per-note Lamport
clock: every write stamps one past the highest revision observed for that
note, so causally-later edits always compare greater. Ordering is the
``(counter, node_id)`` tuple — the node-id breaks ties between concurrent
edits deterministically on every node.

Wall-clock time is never trusted across machines (see docs/mesh.md); this
module is the cross-node ordering truth. It is pure — the counter lives in
the note itself, never in module state.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_REV_RE = re.compile(r"^(\d+)@([A-Za-z0-9][A-Za-z0-9._-]*)$")


@dataclass(frozen=True)
class Rev:
    """One stamped revision: a Lamport counter qualified by the writing node."""

    counter: int
    node_id: str

    def __str__(self) -> str:
        return f"{self.counter}@{self.node_id}"

    @classmethod
    def parse(cls, raw: str) -> Rev | None:
        """Parse ``<counter>@<node-id>``; ``None`` for empty or malformed input.

        Legacy notes carry no revision at all, so an unparseable rev is not an
        error — it sorts below every stamped revision (see :meth:`sort_key`).
        """
        match = _REV_RE.match((raw or "").strip())
        if not match:
            return None
        return cls(counter=int(match.group(1)), node_id=match.group(2))

    def sort_key(self) -> tuple[int, str]:
        return (self.counter, self.node_id)

    def newer_than(self, other: Rev | None) -> bool:
        """True when this revision wins last-writer-wins against ``other``.

        ``None`` (a legacy, never-stamped note) loses to any stamped revision:
        the stamped edit is provably later in causal order.
        """
        if other is None:
            return True
        return self.sort_key() > other.sort_key()


def next_rev(current: Rev | None, node_id: str) -> Rev:
    """Lamport tick: one past the highest revision observed for the note."""
    counter = (current.counter if current is not None else 0) + 1
    return Rev(counter=counter, node_id=node_id)
