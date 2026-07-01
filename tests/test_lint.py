# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Aaron K. Clark
"""Tests for ``omind lint`` — the vault health check."""

from __future__ import annotations

from pathlib import Path

from omind import lint
from omind.cli import main


def _omi(tmp_path: Path) -> Path:
    omi = tmp_path / "OMI"
    omi.mkdir(parents=True, exist_ok=True)
    return omi


def _write(omi: Path, name: str, body: str) -> None:
    (omi / name).write_text(body, encoding="utf-8")


def test_clean_vault_has_no_issues(tmp_path: Path) -> None:
    omi = _omi(tmp_path)
    _write(omi, "Alpha.md", "# Alpha\n\n## Connections\n- [[Beta]]\n")
    _write(omi, "Beta.md", "# Beta\n\n## Connections\n- [[Alpha]]\n")
    issues = lint.lint_vault(omi)
    assert issues == []
    assert "no issues" in lint.format_report(issues, omi_dir=omi)


def test_broken_wikilink_is_an_error(tmp_path: Path) -> None:
    omi = _omi(tmp_path)
    _write(omi, "Alpha.md", "# Alpha\n\n## Connections\n- [[Ghost]]\n")
    issues = lint.lint_vault(omi)
    broken = [i for i in issues if i.kind == "broken-link"]
    assert len(broken) == 1
    assert broken[0].severity == "error"
    assert broken[0].note == "Alpha.md"
    assert "Ghost" in broken[0].detail


def test_wikilink_resolves_by_title_alias_and_heading(tmp_path: Path) -> None:
    omi = _omi(tmp_path)
    # Beta's *title* differs from its stem; links by title, with alias/heading, resolve.
    _write(omi, "beta-note.md", "# The Beta Note\n\nbody\n")
    _write(
        omi,
        "Alpha.md",
        "# Alpha\n\n## Connections\n- [[The Beta Note|nick]]\n- [[beta-note#section]]\n",
    )
    assert [i for i in lint.lint_vault(omi) if i.kind == "broken-link"] == []


def test_missing_title_is_a_warning(tmp_path: Path) -> None:
    omi = _omi(tmp_path)
    _write(omi, "Untitled.md", "no heading here, just prose\n")
    issues = lint.lint_vault(omi)
    titles = [i for i in issues if i.kind == "missing-title"]
    assert len(titles) == 1 and titles[0].severity == "warn"


def test_isolated_note_is_info(tmp_path: Path) -> None:
    omi = _omi(tmp_path)
    _write(omi, "Lonely.md", "# Lonely\n\nno links in or out\n")
    _write(omi, "A.md", "# A\n\n## Connections\n- [[B]]\n")
    _write(omi, "B.md", "# B\n\n## Connections\n- [[A]]\n")
    issues = lint.lint_vault(omi)
    isolated = [i for i in issues if i.kind == "isolated"]
    assert [i.note for i in isolated] == ["Lonely.md"]
    assert isolated[0].severity == "info"


def test_a_linked_note_is_not_isolated(tmp_path: Path) -> None:
    omi = _omi(tmp_path)
    # Leaf has no outbound links but IS linked to -> not isolated.
    _write(omi, "Leaf.md", "# Leaf\n\nterminal note\n")
    _write(omi, "Hub.md", "# Hub\n\n## Connections\n- [[Leaf]]\n")
    assert [i for i in lint.lint_vault(omi) if i.kind == "isolated"] == []


def test_near_duplicate_titles_flagged_once(tmp_path: Path) -> None:
    omi = _omi(tmp_path)
    _write(omi, "one.md", "# Telesto motherboard order plan\n\n## Connections\n- [[two]]\n")
    _write(omi, "two.md", "# Telesto motherboard order plan v2\n\n## Connections\n- [[one]]\n")
    dups = [i for i in lint.lint_vault(omi) if i.kind == "near-duplicate"]
    assert len(dups) == 1
    assert dups[0].note == "one.md | two.md"  # sorted, single unordered pair


def test_disabled_and_reserved_notes_are_skipped(tmp_path: Path) -> None:
    omi = _omi(tmp_path)
    # Reserved files and a soft-deleted note must not be linted or counted.
    _write(omi, "index.md", "# OMI\n\n- [[Ghost]]\n")  # reserved -> ignored
    _write(omi, "Dead.md", "# Dead\n\n## Metadata\n- Disabled: true\n\n- [[Ghost]]\n")
    issues = lint.lint_vault(omi)
    assert issues == []  # neither the reserved nor the disabled note's link is reported


def test_links_to_reserved_notes_are_not_broken(tmp_path: Path) -> None:
    omi = _omi(tmp_path)
    _write(omi, "A.md", "# A\n\n## Connections\n- [[index]]\n- [[Memory Template]]\n")
    assert [i for i in lint.lint_vault(omi) if i.kind == "broken-link"] == []


def test_lint_cli_reports_and_exit_codes(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    omi = _omi(tmp_path)
    _write(omi, "Alpha.md", "# Alpha\n\n## Connections\n- [[Ghost]]\n")
    rc = main(["lint", "--vault", str(tmp_path), "--folder", "OMI"])
    assert rc == 1  # a broken link is error severity -> non-zero
    assert "broken-link" in capsys.readouterr().out


def test_lint_cli_strict_flags_info(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    omi = _omi(tmp_path)
    _write(omi, "Lonely.md", "# Lonely\n\nno links\n")  # isolated -> info only
    assert main(["lint", "--vault", str(tmp_path), "--folder", "OMI"]) == 0
    assert main(["lint", "--vault", str(tmp_path), "--folder", "OMI", "--strict"]) == 1
    capsys.readouterr()


def test_lint_cli_json(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    import json

    omi = _omi(tmp_path)
    _write(omi, "Alpha.md", "# Alpha\n\n- [[Ghost]]\n")
    main(["lint", "--vault", str(tmp_path), "--folder", "OMI", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert any(i["kind"] == "broken-link" for i in payload)


def test_clean_format_report_on_empty_vault(tmp_path: Path) -> None:
    omi = _omi(tmp_path)
    assert lint.lint_vault(omi) == []
    assert "no issues" in lint.format_report([], omi_dir=omi)


def test_dated_series_is_not_a_near_duplicate(tmp_path: Path) -> None:
    """A daily Worklog series must not be flagged as duplicate memories."""
    omi = _omi(tmp_path)
    for day in ("2026-06-28", "2026-06-29", "2026-06-30"):
        _write(omi, f"Worklog {day}.md", f"# Worklog {day}\n\n- [[Worklog 2026-06-28]]\n")
    dupes = [i for i in lint.lint_vault(omi) if i.kind == "near-duplicate"]
    assert dupes == []


def test_link_to_archived_note_is_not_broken(tmp_path: Path) -> None:
    """A link to a soft-deleted (archived) note is valid, not a broken-link error."""
    omi = _omi(tmp_path)
    _write(omi, "Live.md", "# Live\n\n- [[Archived Note]]\n")
    _write(
        omi,
        "Archived Note.md",
        "# Archived Note\n\n## Metadata\n- Disabled: true\n",
    )
    broken = [i for i in lint.lint_vault(omi) if i.kind == "broken-link"]
    assert broken == []


def test_link_into_journal_subfolder_is_not_broken(tmp_path: Path) -> None:
    """A wikilink to a Journal/ rollup note must resolve, not error."""
    omi = _omi(tmp_path)
    _write(omi, "Ref.md", "# Ref\n\n- [[Session Journal Rollup 2026-W26]]\n")
    (omi / "Journal").mkdir()
    (omi / "Journal" / "Session Journal Rollup 2026-W26.md").write_text(
        "# Session Journal Rollup 2026-W26\n", encoding="utf-8"
    )
    broken = [i for i in lint.lint_vault(omi) if i.kind == "broken-link"]
    assert broken == []


def test_wikilink_inside_code_fence_is_not_a_link(tmp_path: Path) -> None:
    """A [[wikilink]] quoted in a fenced code block is documentation, not a link."""
    omi = _omi(tmp_path)
    _write(
        omi,
        "Docs.md",
        "# Docs\n\n## Details\nExample:\n\n```\nUse [[Some Note]] to link.\n```\n\n"
        "## Connections\n- [[Docs]]\n",
    )
    broken = [i for i in lint.lint_vault(omi) if i.kind == "broken-link"]
    assert broken == []
