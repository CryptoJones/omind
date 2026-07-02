# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Aaron K. Clark
"""Tests for OKF (Open Knowledge Format) conformance + conversion."""

from __future__ import annotations

from pathlib import Path

import pytest

from omind import okf
from omind.cli import main
from omind.store import (
    OmiStore,
    build_okf_frontmatter,
    derive_okf_type,
    parse_frontmatter,
    parse_note,
    render_fields,
)

LEGACY_NOTE = """# Legacy Note

## Metadata
- Created: 2026-06-01
- Tags: #omi #reference

## Summary
A legacy note.

## Details
Body text.

## Connections
[[Other Note]]
"""

OKF_NOTE = """---
type: Playbook
title: Deploy Guide
tags: [ops, deploy]
timestamp: 2026-07-01
---

# Deploy Guide

Steps here.
"""


@pytest.fixture
def store(tmp_path: Path) -> OmiStore:
    omi = tmp_path / "OMI"
    omi.mkdir()
    return OmiStore(omi)


# -- frontmatter parse / build ----------------------------------------------------


def test_derive_type_from_tag_and_default() -> None:
    assert derive_okf_type(["omi", "feedback"]) == "Feedback"
    assert derive_okf_type(["reference"]) == "Reference"
    assert derive_okf_type(["random", "misc"]) == "Memory"  # default when unmapped


def test_render_emits_required_type_frontmatter() -> None:
    md = render_fields(parse_note(LEGACY_NOTE))
    assert md.startswith("---\n")
    fm = parse_frontmatter(okf._leading_frontmatter_block(md))
    assert fm["type"] == "Reference"  # derived from #reference
    assert fm["title"] == "Legacy Note"
    assert fm["tags"] == ["omi", "reference"]
    assert fm["timestamp"] == "2026-06-01"
    # The legacy ## Metadata body is kept so un-upgraded peers still parse it.
    assert "## Metadata" in md


def test_external_okf_note_dual_read() -> None:
    """A frontmatter-only note (no ## Metadata) is read via the frontmatter."""
    f = parse_note(OKF_NOTE)
    assert f.okf_type == "Playbook"
    assert f.title == "Deploy Guide"
    assert f.tags == ["ops", "deploy"]
    assert f.created == "2026-07-01"


def test_unknown_frontmatter_keys_preserved() -> None:
    note = "---\ntype: Reference\naliases:\n- Foo\ncssclass: wide\n---\n# N\n\n## Summary\ns\n"
    rebuilt = build_okf_frontmatter(parse_note(note))
    fm = parse_frontmatter(rebuilt)
    assert fm["type"] == "Reference"
    assert fm["aliases"] == ["Foo"]  # unknown producer key round-trips
    assert fm["cssclass"] == "wide"


def test_render_round_trip_stable() -> None:
    once = render_fields(parse_note(LEGACY_NOTE))
    twice = render_fields(parse_note(once))
    assert once == twice


# -- conformance check ------------------------------------------------------------


def test_check_flags_legacy(store: OmiStore) -> None:
    (store.omi_dir / "Legacy Note.md").write_text(LEGACY_NOTE, encoding="utf-8")
    report = okf.check_conformance(store.omi_dir)
    assert not report.ok
    assert report.concepts == 1
    assert report.conformant == 0
    assert "no parseable YAML frontmatter" in report.problems[0].problem


def test_check_passes_okf(store: OmiStore) -> None:
    (store.omi_dir / "Deploy Guide.md").write_text(OKF_NOTE, encoding="utf-8")
    report = okf.check_conformance(store.omi_dir)
    assert report.ok
    assert report.conformant == 1


def test_check_skips_reserved_scaffolding(store: OmiStore) -> None:
    for name in ("index.md", "MEMORY.md", "Memory Template.md"):
        (store.omi_dir / name).write_text("# scaffolding, no frontmatter\n", encoding="utf-8")
    report = okf.check_conformance(store.omi_dir)
    assert report.concepts == 0
    assert report.ok


def test_missing_type_is_non_conformant(store: OmiStore) -> None:
    (store.omi_dir / "No Type.md").write_text("---\ntitle: X\n---\n# X\n", encoding="utf-8")
    report = okf.check_conformance(store.omi_dir)
    assert not report.ok
    assert "type" in report.problems[0].problem


def test_unterminated_frontmatter_is_non_conformant(store: OmiStore) -> None:
    (store.omi_dir / "Bad.md").write_text("---\ntype: X\n# never closed\n", encoding="utf-8")
    report = okf.check_conformance(store.omi_dir)
    assert not report.ok


# -- conversion -------------------------------------------------------------------


def test_convert_makes_vault_conformant(store: OmiStore) -> None:
    (store.omi_dir / "Legacy Note.md").write_text(LEGACY_NOTE, encoding="utf-8")
    result = okf.convert_vault(store.omi_dir)
    assert result.converted == 1
    assert result.report.ok
    text = (store.omi_dir / "Legacy Note.md").read_text(encoding="utf-8")
    assert text.startswith("---\n")
    assert "\ntype: Reference" in text
    assert "## Summary" in text and "[[Other Note]]" in text  # legacy body preserved


def test_convert_is_idempotent(store: OmiStore) -> None:
    (store.omi_dir / "Legacy Note.md").write_text(LEGACY_NOTE, encoding="utf-8")
    okf.convert_vault(store.omi_dir)
    second = okf.convert_vault(store.omi_dir)
    assert second.converted == 0
    assert second.unchanged == 1
    assert second.report.ok


def test_convert_dry_run_writes_nothing(store: OmiStore) -> None:
    path = store.omi_dir / "Legacy Note.md"
    path.write_text(LEGACY_NOTE, encoding="utf-8")
    before = path.read_text(encoding="utf-8")
    result = okf.convert_vault(store.omi_dir, dry_run=True)
    assert result.converted == 1
    assert path.read_text(encoding="utf-8") == before  # unchanged on disk


# -- CLI --------------------------------------------------------------------------


def test_cli_convert_and_check(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    omi = tmp_path / "OMI"
    omi.mkdir()
    (omi / "Legacy Note.md").write_text(LEGACY_NOTE, encoding="utf-8")
    common = ["--vault", str(tmp_path), "--folder", "OMI"]

    assert main(["convert", "--check", *common]) == 1  # legacy: non-conformant
    capsys.readouterr()
    assert main(["convert", *common]) == 0
    assert "converted 1" in capsys.readouterr().out
    assert main(["convert", "--check", *common]) == 0  # now conformant
