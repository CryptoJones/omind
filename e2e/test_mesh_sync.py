# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Aaron K. Clark
"""Two real nodes peer over ssh and converge — the mesh's core promise."""

from __future__ import annotations

import shlex

from e2e.nodes import (
    OMI_DIR,
    VAULT,
    add_peers_full_mesh,
    install_omind,
    interconnect,
    note_digests,
    setup_vault,
    sync,
    write_note,
)


def _converged(a, b) -> None:
    da, db = note_digests(a), note_digests(b)
    assert da == db, f"vaults diverged:\n{a.name}: {sorted(da)}\n{b.name}: {sorted(db)}"


def test_two_nodes_converge_over_ssh(nodes, wheel) -> None:
    a, b = nodes(2)
    for node in (a, b):
        install_omind(node, wheel)
        setup_vault(node)
    interconnect([a, b])
    add_peers_full_mesh([a, b])

    write_note(a, "Written On A", "alpha facts")
    write_note(b, "Written On B", "bravo facts")

    # A pushes its outbox to B and merges what B has; then B does the same;
    # one more pass on A picks up B's regenerated state.
    sync(a)
    sync(b)
    sync(a)

    assert "alpha facts" in b.run(f"cat {OMI_DIR}/'Written On A.md'").stdout
    assert "bravo facts" in a.run(f"cat {OMI_DIR}/'Written On B.md'").stdout
    _converged(a, b)


def test_concurrent_edits_field_merge(nodes, wheel) -> None:
    a, b = nodes(2)
    for node in (a, b):
        install_omind(node, wheel)
        setup_vault(node)
    interconnect([a, b])
    add_peers_full_mesh([a, b])

    write_note(a, "Shared Note", "base details")
    sync(a)
    sync(b)

    # Concurrent, non-overlapping edits while both nodes hold the note:
    # A adds a tag, B appends different details. Field-level merge must keep both.
    a.run(
        f"omind note --title 'Shared Note' --tags from-a --details 'base details' "
        f"--vault {VAULT}"
    )
    b_details = shlex.quote("base details\nplus a line from B")
    b.run(f"omind note --title 'Shared Note' --details {b_details} --vault {VAULT}")
    sync(a)
    sync(b)
    sync(a)

    for node in (a, b):
        text = node.run(f"cat {OMI_DIR}/'Shared Note.md'").stdout
        assert "from-a" in text, f"{node.name} lost A's tag"
        assert "plus a line from B" in text, f"{node.name} lost B's details line"
        assert "<<<<<<<" not in text, f"{node.name} has raw conflict markers"
    _converged(a, b)
