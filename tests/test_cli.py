# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Aaron K. Clark
"""Tests for the omind package skeleton."""

from pathlib import Path

import pytest

import omind
from omind.cli import build_parser, main


def test_version_is_set() -> None:
    assert omind.__version__ == "1.1.0"


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
