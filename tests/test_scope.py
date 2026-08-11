# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Aaron K. Clark
"""Tests for the optional note retrieval scope (#222).

A flat namespace plus full-copy mesh replication means every agent on every
machine sees every note. `Scope:` narrows what RETRIEVAL returns. It is NOT a
security boundary and the tests below pin that: an unscoped query still sees
everything, because the note is plain Markdown on disk either way.
"""

from __future__ import annotations

from pathlib import Path

from omind.store import OmiStore, _clean_scope, parse_note, render_fields


def _write(omi: Path, title: str, body: str, scope: str = "") -> None:
    meta = "- Created: 2026-08-11\n- Tags: #t\n"
    if scope:
        meta += f"- Scope: {scope}\n"
    (omi / f"{title}.md").write_text(
        f"# {title}\n\n## Metadata\n{meta}\n## Summary\n{body}\n", encoding="utf-8"
    )


def test_scope_is_normalised() -> None:
    assert _clean_scope("#Buzz") == "buzz"
    assert _clean_scope("  Antigua  ") == "antigua"
    assert _clean_scope("") == ""


def test_scope_round_trips_and_is_absent_when_unset() -> None:
    md = "# T\n\n## Metadata\n- Created: 2026-08-11\n- Scope: #Buzz\n\n## Summary\ns\n"
    assert parse_note(md).scope == "buzz"
    assert "- Scope: buzz" in render_fields(parse_note(md))
    plain = "# T\n\n## Metadata\n- Created: 2026-08-11\n\n## Summary\ns\n"
    assert parse_note(plain).scope == ""
    assert "- Scope:" not in render_fields(parse_note(plain))


def test_unfiltered_search_sees_everything(tmp_path: Path) -> None:
    """The default must not change. Scope is opt-in at QUERY time."""
    omi = tmp_path / "OMI"
    omi.mkdir()
    _write(omi, "Global", "kubernetes ingress")
    _write(omi, "Scoped", "kubernetes ingress", scope="buzz")
    names = {s.filename for s in OmiStore(omi).search("kubernetes")}
    assert names == {"Global.md", "Scoped.md"}


def test_filtering_hides_other_scopes_but_keeps_unscoped(tmp_path: Path) -> None:
    """Unscoped notes always survive: a vault written before this field existed
    must not go invisible the moment someone filters."""
    omi = tmp_path / "OMI"
    omi.mkdir()
    _write(omi, "Global", "kubernetes ingress")
    _write(omi, "Buzz", "kubernetes ingress", scope="buzz")
    _write(omi, "Antigua", "kubernetes ingress", scope="antigua")
    names = {s.filename for s in OmiStore(omi).search("kubernetes", scope="buzz")}
    assert names == {"Global.md", "Buzz.md"}
    assert "Antigua.md" not in names


def test_scope_filter_accepts_a_hash_prefix(tmp_path: Path) -> None:
    omi = tmp_path / "OMI"
    omi.mkdir()
    _write(omi, "Buzz", "kubernetes ingress", scope="buzz")
    assert {s.filename for s in OmiStore(omi).search("kubernetes", scope="#Buzz")} == {"Buzz.md"}


def test_scope_is_visible_on_results(tmp_path: Path) -> None:
    """A caller that filtered needs to see what it filtered on."""
    omi = tmp_path / "OMI"
    omi.mkdir()
    _write(omi, "Buzz", "kubernetes ingress", scope="buzz")
    hits = OmiStore(omi).search("kubernetes")
    assert hits and hits[0].scope == "buzz"
