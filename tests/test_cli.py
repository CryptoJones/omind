# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Aaron K. Clark
"""Tests for the omind package skeleton."""

import io
import re
from pathlib import Path

import pytest

import omind
from omind.cli import build_parser, main


def test_version_is_set() -> None:
    # Compare against pyproject's version (the single source of truth) so a
    # release bump can't leave __version__ behind — that happened in 2.0.1.
    # (regex, not tomllib: CI's floor is 3.10)
    pyproject = (Path(__file__).parent.parent / "pyproject.toml").read_text(encoding="utf-8")
    declared = re.search(r'^version = "([^"]+)"', pyproject, re.MULTILINE)
    assert declared is not None
    assert omind.__version__ == declared.group(1)


def test_help_command_uses_live_nested_parser(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["help", "ai", "usage"]) == 0
    output = capsys.readouterr().out
    assert "usage: omind ai usage" in output
    assert "--since" in output and "--json" in output


def test_help_command_reports_unknown_component(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["help", "ai", "usgae"]) == 2
    error = capsys.readouterr().err
    assert "unknown command component" in error
    assert "usage" in error


def test_doctor_subcommand_parses() -> None:
    args = build_parser().parse_args(["doctor", "--folder", "OMI", "--server-name", "obsidian"])
    assert args.command == "doctor"
    assert args.folder == "OMI"
    assert args.server_name == "obsidian"


def test_hook_subcommand_parses() -> None:
    args = build_parser().parse_args(["hook", "PostToolUse", "--folder", "OMI"])
    assert args.command == "hook"
    assert args.event == "PostToolUse"
    assert args.folder == "OMI"


def test_hook_subcommand_rejects_unknown_event() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["hook", "NotAnEvent"])


def test_reindex_subcommand_parses() -> None:
    args = build_parser().parse_args(["reindex", "--folder", "OMI"])
    assert args.command == "reindex"
    assert args.folder == "OMI"


def test_reindex_regenerates_index_for_directly_written_note(tmp_path: Path) -> None:
    # Simulate a session that wrote a note file directly (bypassing the store),
    # then ran `omind reindex` to refresh index.md safely.
    omi = tmp_path / "OMI"
    omi.mkdir()
    (omi / "Hand Written.md").write_text("# Hand Written\n\n## Summary\nhi\n", encoding="utf-8")
    rc = main(["reindex", "--vault", str(tmp_path), "--folder", "OMI"])
    assert rc == 0
    index = (omi / "index.md").read_text(encoding="utf-8")
    assert "[[Hand Written]]" in index


def test_reindex_migrates_stray_journals_into_journal_subfolder(tmp_path: Path) -> None:
    from omind import hooks

    omi = tmp_path / "OMI"
    omi.mkdir()
    name = "Session Journal 2026-06-01.md"
    (omi / name).write_text(hooks.journal_header(name), encoding="utf-8")
    rc = main(["reindex", "--vault", str(tmp_path), "--folder", "OMI"])
    assert rc == 0
    assert (omi / "Journal" / name).is_file()
    assert not (omi / name).exists()
    assert "Session Journal" not in (omi / "index.md").read_text(encoding="utf-8")


def test_rollup_subcommand_parses() -> None:
    args = build_parser().parse_args(["rollup", "--week", "2026-W24", "--delete"])
    assert args.command == "rollup"
    assert args.week == "2026-W24"
    assert args.retain_days == 30
    assert args.delete is True


def test_rollup_rejects_bad_week(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["rollup", "--week", "next-week"])
    assert rc == 1
    assert "--week" in capsys.readouterr().err


def test_rollup_compacts_a_week_of_journals(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from omind import hooks

    omi = tmp_path / "OMI"
    jdir = omi / "Journal"
    jdir.mkdir(parents=True)
    name = "Session Journal 2026-06-08.md"
    (jdir / name).write_text(
        hooks.journal_header(name)
        + "- 09:00 [session aaaa1111] PostToolUse Bash -> `make` (ok)\n",
        encoding="utf-8",
    )
    rc = main(["rollup", "--week", "2026-W24", "--vault", str(tmp_path), "--folder", "OMI"])
    assert rc == 0
    assert "Session Journal Rollup 2026-W24.md" in capsys.readouterr().out
    assert (jdir / "Session Journal Rollup 2026-W24.md").is_file()
    assert (jdir / "Archive" / name).is_file()


def test_rollup_nothing_to_do(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["rollup", "--vault", str(tmp_path), "--folder", "OMI"])
    assert rc == 0
    assert "nothing to roll up" in capsys.readouterr().out


def test_note_subcommand_parses() -> None:
    args = build_parser().parse_args(
        ["note", "--title", "An Insight", "--tags", "a,b", "--folder", "OMI"]
    )
    assert args.command == "note"
    assert args.title == "An Insight"
    assert args.tags == "a,b"


def test_note_requires_title() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["note"])


def test_note_creates_then_updates(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    common = ["--vault", str(tmp_path), "--folder", "OMI"]
    rc = main(["note", "--title", "Attention Insight", "--summary", "gist",
               "--tags", "thesis,attention", "--details", "first body", *common])
    assert rc == 0
    assert capsys.readouterr().out.strip() == "created Attention Insight.md"

    note = (tmp_path / "OMI" / "Attention Insight.md").read_text(encoding="utf-8")
    assert note.startswith("---\n") and "\ntype:" in note  # OKF frontmatter w/ required type
    assert "# Attention Insight" in note
    assert "## Summary\ngist" in note
    assert "#thesis" in note and "first body" in note
    assert "[[Attention Insight]]" in (tmp_path / "OMI" / "index.md").read_text(encoding="utf-8")

    # Re-writing the same title updates in place (upsert), not a duplicate/error.
    rc = main(["note", "--title", "Attention Insight", "--summary", "revised",
               "--details", "second body", *common])
    assert rc == 0
    assert capsys.readouterr().out.strip() == "updated Attention Insight.md"
    note = (tmp_path / "OMI" / "Attention Insight.md").read_text(encoding="utf-8")
    assert "revised" in note and "second body" in note


def test_note_reads_details_from_stdin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO("piped body text"))
    rc = main(["note", "--title", "Piped", "--vault", str(tmp_path), "--folder", "OMI"])
    assert rc == 0
    assert "piped body text" in (tmp_path / "OMI" / "Piped.md").read_text(encoding="utf-8")


# -- 2.41.0: note --connection (comma-safe) + omind search -------------------


def test_note_connection_flag_preserves_comma_titles(tmp_path: Path) -> None:
    rc = main([
        "note", "--title", "Test Note",
        "--summary", "s", "--details", "body",
        "--connection", "A Note, with a comma",
        "--connection", "Plain Other",
        "--connections", "CsvOne,CsvTwo",
        "--vault", str(tmp_path), "--folder", "OMI",
    ])
    assert rc == 0
    note = (tmp_path / "OMI" / "Test Note.md").read_text(encoding="utf-8")
    assert "[[A Note, with a comma]]" in note  # comma title kept whole
    assert "[[Plain Other]]" in note
    assert "[[CsvOne]]" in note and "[[CsvTwo]]" in note  # CSV still splits on commas


def test_search_finds_notes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from omind.store import NoteFields, OmiStore

    OmiStore(tmp_path / "OMI").create_note(
        NoteFields(title="Codeberg Note", summary="about codeberg releases")
    )
    assert main(["search", "codeberg", "--vault", str(tmp_path), "--folder", "OMI"]) == 0
    assert "Codeberg Note" in capsys.readouterr().out
    # a miss prints "no matches"
    assert main(["search", "zzznotthere", "--vault", str(tmp_path), "--folder", "OMI"]) == 0
    assert "no matches" in capsys.readouterr().out


def test_dedup_hint_warns_about_a_similar_existing_note(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """3.0.0: creating a note close in meaning to an existing one hints (to stderr,
    non-blocking) to update the existing note instead of duplicating."""
    from omind import embed
    from omind.cli import _dedup_hint
    from omind.store import NoteFields, OmiStore

    vocab = ["release", "push", "forge", "smoothie"]

    def _enc(texts: list[str]) -> list[list[float]]:
        rows = []
        for text in texts:
            low = text.lower()
            vec = [float(low.count(w)) for w in vocab]
            norm = sum(x * x for x in vec) ** 0.5 or 1.0
            rows.append([x / norm for x in vec])
        return rows

    monkeypatch.setattr(embed, "available", lambda: True)
    monkeypatch.setattr(embed, "encode", _enc)
    monkeypatch.setenv("OMI_DEDUP_THRESHOLD", "0.3")
    omi = tmp_path / "OMI"
    OmiStore(omi).create_note(
        NoteFields(title="Release Guide", summary="how to release and push to the forge")
    )
    fields = NoteFields(title="Shipping Steps", summary="release and push to the forge")
    _dedup_hint(omi, fields, "Shipping Steps.md")
    err = capsys.readouterr().err
    assert "Release Guide" in err and "similar" in err
    embed.reset()
