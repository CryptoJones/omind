# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Aaron K. Clark
"""Command-line entry point for omind.

Subcommands:
  * ``omind setup``  — provision the OMI/Obsidian MCP wiring for an AI agent
    (``--agent`` claude (default), hermes, or openclaw).
  * ``omind serve``  — run the local web UI over an OMI memory folder.
  * ``omind doctor`` — diagnose the wiring.
  * ``omind export`` — write the entire OMI dataset to a json or tar.gz bundle.
  * ``omind import`` — load an OMI dataset bundle back into a folder.
  * ``omind reindex`` — regenerate index.md under the inter-process write lock.
  * ``omind quickstart`` — print the manual-wiring steps `setup` automates.
  * ``omind note`` — safely create/update one OMI note through OmiStore.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from omind import __version__
from omind.agents import AGENT_CHOICES, diagnose_for, run_setup_for
from omind.hooks import HANDLED_EVENTS, run_hook
from omind.provision import (
    ProvisionError,
    SetupConfig,
    default_vault_path,
    run_doctor,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="omind",
        description="OMI/Obsidian memory tooling for AI agents.",
    )
    parser.add_argument("--version", action="version", version=f"omind {__version__}")
    sub = parser.add_subparsers(
        dest="command", metavar="{setup,quickstart,serve,doctor,export,import,reindex,note,hook}"
    )

    setup = sub.add_parser(
        "setup", help="provision the OMI/Obsidian MCP wiring for an AI agent"
    )
    setup.add_argument(
        "--vault",
        type=Path,
        default=default_vault_path(),
        help="path to the Obsidian vault (default: %(default)s)",
    )
    setup.add_argument(
        "--folder", default="OMI", help="memory folder inside the vault (default: OMI)"
    )
    setup.add_argument(
        "--agent",
        choices=AGENT_CHOICES,
        default="claude",
        help="which agent to provision (default: claude — the Claude Code CLI)",
    )
    setup.add_argument(
        "--server-name",
        default="obsidian",
        help="name to register the MCP server under (default: obsidian)",
    )
    setup.add_argument(
        "--dry-run",
        action="store_true",
        help="print the planned actions without changing anything",
    )
    setup.add_argument(
        "--force",
        action="store_true",
        help="overwrite existing seed files and re-register the MCP server",
    )

    quickstart = sub.add_parser(
        "quickstart",
        help="print copy-paste manual-wiring steps (what `setup` would do, as "
        "shell commands and JSON personalized to your paths)",
    )
    quickstart.add_argument(
        "--vault",
        type=Path,
        default=default_vault_path(),
        help="path to the Obsidian vault (default: %(default)s)",
    )
    quickstart.add_argument(
        "--folder", default="OMI", help="memory folder inside the vault (default: OMI)"
    )
    quickstart.add_argument(
        "--agent",
        choices=AGENT_CHOICES,
        default="claude",
        help="which agent the steps target (default: claude — the Claude Code CLI)",
    )
    quickstart.add_argument(
        "--server-name",
        default="obsidian",
        help="name to register the MCP server under (default: obsidian)",
    )

    serve = sub.add_parser("serve", help="run the local web UI over an OMI memory folder")
    serve.add_argument(
        "--vault",
        type=Path,
        default=default_vault_path(),
        help="path to the Obsidian vault (default: %(default)s)",
    )
    serve.add_argument(
        "--folder", default="OMI", help="memory folder inside the vault (default: OMI)"
    )
    serve.add_argument("--host", default="127.0.0.1", help="bind host (default: 127.0.0.1)")
    serve.add_argument("--port", type=int, default=8765, help="bind port (default: 8765)")
    serve.add_argument("--reload", action="store_true", help="auto-reload on code changes (dev)")

    doctor = sub.add_parser("doctor", help="diagnose the OMI/Obsidian MCP wiring")
    doctor.add_argument(
        "--vault",
        type=Path,
        default=default_vault_path(),
        help="path to the Obsidian vault (default: %(default)s)",
    )
    doctor.add_argument(
        "--folder", default="OMI", help="memory folder inside the vault (default: OMI)"
    )
    doctor.add_argument(
        "--agent",
        choices=AGENT_CHOICES,
        default="claude",
        help="which agent's wiring to diagnose (default: claude — the Claude Code CLI)",
    )
    doctor.add_argument(
        "--server-name",
        default="obsidian",
        help="name the MCP server is registered under (default: obsidian)",
    )

    export = sub.add_parser("export", help="write the entire OMI dataset to a bundle")
    export.add_argument(
        "--vault",
        type=Path,
        default=default_vault_path(),
        help="path to the Obsidian vault (default: %(default)s)",
    )
    export.add_argument(
        "--folder", default="OMI", help="memory folder inside the vault (default: OMI)"
    )
    export.add_argument(
        "--format",
        choices=("json", "targz"),
        default="json",
        help="bundle format (default: json)",
    )
    export.add_argument(
        "--out",
        type=Path,
        default=None,
        help="output file (default: omi-export.json / omi-export.tar.gz in CWD)",
    )

    imp = sub.add_parser("import", help="load an OMI dataset bundle into a folder")
    imp.add_argument("file", type=Path, help="bundle to import (.json or .tar.gz)")
    imp.add_argument(
        "--vault",
        type=Path,
        default=default_vault_path(),
        help="path to the Obsidian vault (default: %(default)s)",
    )
    imp.add_argument(
        "--folder", default="OMI", help="memory folder inside the vault (default: OMI)"
    )
    imp.add_argument(
        "--force",
        action="store_true",
        help="overwrite notes whose content differs (default: keep on-disk copy)",
    )

    reindex = sub.add_parser(
        "reindex",
        help="regenerate index.md's Recent Memories list under the write lock "
        "(safe to run from a session that wrote a note file directly)",
    )
    reindex.add_argument(
        "--vault",
        type=Path,
        default=default_vault_path(),
        help="path to the Obsidian vault (default: %(default)s)",
    )
    reindex.add_argument(
        "--folder", default="OMI", help="memory folder inside the vault (default: OMI)"
    )

    note = sub.add_parser(
        "note",
        help="safely create or update one OMI note through OmiStore (the single-writer path)",
    )
    note.add_argument("--title", required=True, help="note title (also derives the filename)")
    note.add_argument("--summary", default="", help="one-line summary")
    note.add_argument(
        "--details",
        default=None,
        help="body text; if omitted, read from stdin (preferred for multi-line content)",
    )
    note.add_argument("--tags", default="", help="comma-separated tags (no '#' needed)")
    note.add_argument("--related-to", default="", help="free-text 'related to' line")
    note.add_argument("--connections", default="", help="comma-separated note titles to [[link]]")
    note.add_argument("--references", default="", help="comma-separated references")
    note.add_argument(
        "--vault",
        type=Path,
        default=default_vault_path(),
        help="path to the Obsidian vault (default: %(default)s)",
    )
    note.add_argument(
        "--folder", default="OMI", help="memory folder inside the vault (default: OMI)"
    )

    hook = sub.add_parser(
        "hook", help="(internal) record one Claude Code action into the OMI journal"
    )
    hook.add_argument(
        "event",
        choices=list(HANDLED_EVENTS),
        help="the Claude Code hook event name",
    )
    hook.add_argument(
        "--vault",
        type=Path,
        default=default_vault_path(),
        help="path to the Obsidian vault (default: %(default)s)",
    )
    hook.add_argument(
        "--folder", default="OMI", help="memory folder inside the vault (default: OMI)"
    )

    return parser


def _run_setup(args: argparse.Namespace) -> int:
    config = SetupConfig(
        vault=args.vault,
        folder=args.folder,
        server_name=args.server_name,
        dry_run=args.dry_run,
        force=args.force,
        agent=args.agent,
    )
    try:
        run_setup_for(config)
    except ProvisionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


def _run_quickstart(args: argparse.Namespace) -> int:
    from omind.quickstart import build_quickstart

    config = SetupConfig(
        vault=args.vault,
        folder=args.folder,
        server_name=args.server_name,
        agent=args.agent,
    )
    print(build_quickstart(config))
    return 0


def _run_doctor(args: argparse.Namespace) -> int:
    config = SetupConfig(
        vault=args.vault,
        folder=args.folder,
        server_name=args.server_name,
        agent=args.agent,
    )
    return run_doctor(config, diagnose_fn=diagnose_for)


def _run_serve(args: argparse.Namespace) -> int:
    import uvicorn

    omi_dir = (args.vault / args.folder).expanduser()
    print(f"omind serve -> {omi_dir}")
    print(f"open http://{args.host}:{args.port}")
    if args.reload:
        os.environ["OMIND_OMI_DIR"] = str(omi_dir)
        uvicorn.run(
            "omind.web.app:get_app",
            factory=True,
            host=args.host,
            port=args.port,
            reload=True,
        )
    else:
        from omind.web.app import create_app

        uvicorn.run(create_app(omi_dir), host=args.host, port=args.port)
    return 0


def _run_export(args: argparse.Namespace) -> int:
    from omind.transfer import TransferError, default_export_name, export_dataset

    omi_dir = (args.vault / args.folder).expanduser()
    out = args.out if args.out is not None else Path(default_export_name(args.format))
    try:
        export_dataset(omi_dir, out, fmt=args.format)
    except TransferError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


def _run_import(args: argparse.Namespace) -> int:
    from omind.transfer import TransferError, import_dataset

    omi_dir = (args.vault / args.folder).expanduser()
    try:
        result = import_dataset(omi_dir, args.file, force=args.force)
    except TransferError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    # Unresolved conflicts (content differs, no --force) are a soft failure so
    # scripts can detect that the import did not fully apply.
    return 1 if (result.conflicts and not args.force) else 0


def _run_reindex(args: argparse.Namespace) -> int:
    from omind.store import OmiStore

    omi_dir = (args.vault / args.folder).expanduser()
    OmiStore(omi_dir).update_index()  # locked + atomic
    print(f"reindexed {omi_dir / 'index.md'}")
    return 0


def _split_csv(value: str) -> list[str]:
    """Split a comma-separated CLI flag into a clean list."""
    return [item.strip() for item in value.split(",") if item.strip()]


def _run_note(args: argparse.Namespace) -> int:
    from omind.notes import upsert_note
    from omind.store import NoteError, NoteFields

    details = args.details
    if details is None:
        details = sys.stdin.read() if not sys.stdin.isatty() else ""
    fields = NoteFields(
        title=args.title.strip(),
        summary=args.summary.strip(),
        details=details.strip(),
        tags=_split_csv(args.tags),
        related_to=args.related_to.strip(),
        connections=_split_csv(args.connections),
        references=_split_csv(args.references),
    )
    omi_dir = (args.vault / args.folder).expanduser()
    try:
        action, filename = upsert_note(omi_dir, fields)
    except NoteError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"{action} {filename}")
    return 0


def _run_hook(args: argparse.Namespace) -> int:
    omi_dir = (args.vault / args.folder).expanduser()
    return run_hook(args.event, omi_dir)  # always 0; must never block the agent


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "setup":
        return _run_setup(args)
    if args.command == "quickstart":
        return _run_quickstart(args)
    if args.command == "serve":
        return _run_serve(args)
    if args.command == "doctor":
        return _run_doctor(args)
    if args.command == "export":
        return _run_export(args)
    if args.command == "import":
        return _run_import(args)
    if args.command == "reindex":
        return _run_reindex(args)
    if args.command == "note":
        return _run_note(args)
    if args.command == "hook":
        return _run_hook(args)
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
