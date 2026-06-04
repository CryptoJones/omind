# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Aaron K. Clark
"""Tests for the omind package skeleton."""

import omind
from omind.cli import build_parser


def test_version_is_set() -> None:
    assert omind.__version__ == "1.1.0"


def test_doctor_subcommand_parses() -> None:
    args = build_parser().parse_args(["doctor", "--folder", "OMI", "--server-name", "obsidian"])
    assert args.command == "doctor"
    assert args.folder == "OMI"
    assert args.server_name == "obsidian"
