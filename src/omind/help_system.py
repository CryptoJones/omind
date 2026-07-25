# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Aaron K. Clark
"""Authoritative, shared help rendering for the CLI, MCP server, and skills."""

from __future__ import annotations

import argparse
import difflib
import shlex
from typing import Any


def _subcommands(parser: argparse.ArgumentParser) -> dict[str, argparse.ArgumentParser]:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return dict(action.choices)
    return {}


def _tokens(command: str) -> list[str]:
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()
    if tokens and tokens[0].lstrip("/") == "omind":
        tokens.pop(0)
    if tokens and tokens[0] == "help":
        tokens.pop(0)
    return tokens


def render_help(command: str = "") -> dict[str, Any]:
    """Return argparse's live syntax for ``command`` without invoking a shell.

    The parser is the single source of truth, so MCP/skill help cannot drift from
    the installed CLI. ``command`` accepts forms such as ``ai usage``,
    ``omind mesh sync``, and ``/omind help guard``.
    """
    # Imported lazily to keep ``omind.cli -> omind.server`` free of a cycle.
    from omind.cli import build_parser

    parser = build_parser()
    path: list[str] = []
    tokens = _tokens(command)
    for token in tokens:
        choices = _subcommands(parser)
        if token not in choices:
            candidates = sorted(choices)
            close = difflib.get_close_matches(token, candidates, n=3)
            hint = f" Did you mean: {', '.join(close)}?" if close else ""
            return {
                "ok": False,
                "command": "omind " + " ".join(path),
                "error": f"unknown command component {token!r}.{hint}".strip(),
                "available": candidates,
            }
        parser = choices[token]
        path.append(token)
    return {
        "ok": True,
        "command": "omind" + (" " + " ".join(path) if path else ""),
        "help": parser.format_help().rstrip(),
        "subcommands": sorted(_subcommands(parser)),
    }

