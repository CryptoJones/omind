#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Aaron K. Clark
"""Reference helper: write one OMI note safely from outside omind.

``omind note`` is the supported CLI for this. This standalone script exists for
embedders — e.g. Hermes' ``hermes-omi-memory-sync`` skill — that want a single
file they can drop next to a skill and call, with environment-based vault
resolution and a source-tree import fallback. Both go through
:func:`omind.notes.upsert_note`, so every write takes the ``.omi.lock`` flock +
atomic ``os.replace`` + ``note_version`` re-check. Writing OMI files raw bypasses
all of that and races other writers — and the memory mesh's replication daemon.
See ``docs/mesh.md`` → "Node types & the single-writer rule".

Usage (body comes from stdin so multi-line content pipes cleanly):

    echo "full body text..." | python extras/omi_write.py \
        --title "Transformer Attention Insight" \
        --summary "One-line gist" \
        --tags thesis,insight,attention \
        --connections "Attention Mechanisms,Thesis Chapter 3"

Upsert semantics: creates the note, or updates it in place if it already exists
(matched by the title-derived filename). Prints the resulting filename.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# Work whether or not omind is installed on the path: fall back to importing
# straight from the sibling source checkout (extras/ -> ../src).
try:
    from omind.notes import upsert_note
    from omind.store import NoteError, NoteFields
except ModuleNotFoundError:  # pragma: no cover - run from a source checkout
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
    from omind.notes import upsert_note
    from omind.store import NoteError, NoteFields


def _csv(value: str | None) -> list[str]:
    """Split a comma-separated flag into a clean list."""
    return [item.strip() for item in (value or "").split(",") if item.strip()]


def _default_omi_dir() -> str:
    """Resolve the OMI folder: explicit env wins, then the vault env, then
    omind's default vault."""
    if env := os.environ.get("OMIND_OMI_DIR"):
        return env
    if vault := os.environ.get("OBSIDIAN_VAULT_PATH"):
        return str(Path(vault) / "OMI")
    from omind.provision import default_vault_path

    return str(default_vault_path() / "OMI")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Safely write an OMI note via OmiStore.")
    parser.add_argument("--title", required=True, help="Note title (also derives the filename).")
    parser.add_argument("--summary", default="", help="One-line summary.")
    parser.add_argument(
        "--details",
        default=None,
        help="Body text. If omitted, read from stdin (preferred for multi-line content).",
    )
    parser.add_argument("--tags", default="", help="Comma-separated tags (no '#' needed).")
    parser.add_argument("--related-to", default="", help="Free-text 'related to' line.")
    parser.add_argument("--connections", default="", help="Comma-separated titles to [[link]].")
    parser.add_argument("--references", default="", help="Comma-separated references.")
    parser.add_argument(
        "--omi",
        default=None,
        help="OMI folder (default: resolved from env, else omind's vault).",
    )
    args = parser.parse_args(argv)

    details = args.details
    if details is None:
        details = sys.stdin.read() if not sys.stdin.isatty() else ""

    fields = NoteFields(
        title=args.title.strip(),
        summary=args.summary.strip(),
        details=details.strip(),
        tags=_csv(args.tags),
        related_to=args.related_to.strip(),
        connections=_csv(args.connections),
        references=_csv(args.references),
    )

    try:
        action, filename = upsert_note(args.omi or _default_omi_dir(), fields)
    except NoteError as exc:
        print(f"omi_write: {exc}", file=sys.stderr)
        return 1

    print(f"{action} {filename}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
