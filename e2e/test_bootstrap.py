# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Aaron K. Clark
"""Fresh-box bootstrap: install the wheel, provision the wiring, write a memory."""

from __future__ import annotations

from e2e.nodes import OMI_DIR, VAULT, install_omind, setup_vault, write_note


def test_fresh_box_setup_and_first_note(nodes, wheel) -> None:
    (node,) = nodes(1)
    install_omind(node, wheel)
    setup_vault(node)

    # The seeds landed and the index is the generated one.
    listing = node.run(f"ls {OMI_DIR}").stdout
    assert "index.md" in listing
    assert "Memory Template.md" in listing

    write_note(node, "E2E First Memory", "written on a disposable node")
    note = node.run(f"cat {OMI_DIR}/'E2E First Memory.md'").stdout
    assert "written on a disposable node" in note
    index = node.run(f"cat {OMI_DIR}/index.md").stdout
    assert "[[E2E First Memory]]" in index

    # Re-running setup must converge, and doctor must stay green.
    setup_vault(node)
    node.run(f"omind doctor --vault {VAULT}")
