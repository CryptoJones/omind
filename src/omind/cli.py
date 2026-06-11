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
  * ``omind rollup`` — compact weeks of daily session journals into summaries.
  * ``omind backup`` — encrypted off-machine backup of the OMI folder (restic).
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
    CheckResult,
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
        dest="command",
        metavar="{setup,quickstart,serve,doctor,backup,export,import,reindex,note,rollup,hook}",
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

    node = sub.add_parser(
        "node", help="run the local mesh-node MCP server over stdio (docs/mesh.md)"
    )
    node.add_argument(
        "--vault",
        type=Path,
        default=default_vault_path(),
        help="path to the Obsidian vault (default: %(default)s)",
    )
    node.add_argument(
        "--folder", default="OMI", help="memory folder inside the vault (default: OMI)"
    )

    mesh = sub.add_parser(
        "mesh", help="peer-to-peer replication of the OMI folder over git (docs/mesh.md)"
    )
    msub = mesh.add_subparsers(
        dest="mesh_command",
        metavar="{init,add-peer,remove-peer,sync,daemon,install-service,clone,purge}",
        required=True,
    )
    mesh_init_p = msub.add_parser(
        "init",
        help="make the OMI folder a mesh node: git repo, merge driver, node identity",
    )
    mesh_add_peer = msub.add_parser("add-peer", help="register a peer node (a git remote)")
    mesh_add_peer.add_argument("name", help="peer name (e.g. pluto)")
    mesh_add_peer.add_argument("url", help="git URL (e.g. ssh://pluto/~/path/to/OMI)")
    mesh_remove_peer = msub.add_parser("remove-peer", help="forget a peer node")
    mesh_remove_peer.add_argument("name")
    mesh_sync = msub.add_parser(
        "sync", help="one-shot commit + fetch/merge/push against every reachable peer"
    )
    mesh_sync.add_argument(
        "--peer", action="append", default=None, help="sync only this peer (repeatable)"
    )
    mesh_daemon = msub.add_parser(
        "daemon", help="run the replication loop (interval sync + on-write debounce)"
    )
    mesh_install = msub.add_parser(
        "install-service",
        help="install the replication daemon as a user service (systemd/launchd)",
    )
    mesh_clone = msub.add_parser("clone", help="seed a fresh node from a peer")
    mesh_clone.add_argument("url", help="git URL of an existing node")
    mesh_purge = msub.add_parser(
        "purge",
        help="hard-delete a note from EVERY node (tombstoned); the normal "
        "delete only archives — this is the rare exception",
    )
    mesh_purge.add_argument("note", help="note filename (e.g. 'Old Note.md')")
    for mp in (
        mesh_init_p,
        mesh_add_peer,
        mesh_remove_peer,
        mesh_sync,
        mesh_daemon,
        mesh_install,
        mesh_clone,
        mesh_purge,
    ):
        mp.add_argument(
            "--vault",
            type=Path,
            default=default_vault_path(),
            help="path to the Obsidian vault (default: %(default)s)",
        )
        mp.add_argument(
            "--folder", default="OMI", help="memory folder inside the vault (default: OMI)"
        )

    # Hidden: invoked by git (merge.omi.driver), never by hand.
    merge_driver = sub.add_parser("merge-driver")
    merge_driver.add_argument("base", type=Path)
    merge_driver.add_argument("ours", type=Path)
    merge_driver.add_argument("theirs", type=Path)
    merge_driver.add_argument("path_label", nargs="?", default="")

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

    backup = sub.add_parser(
        "backup", help="encrypted off-machine backup of the OMI folder (restic)"
    )
    bsub = backup.add_subparsers(
        dest="backup_command",
        metavar="{init,run,verify,install-timer}",
        required=True,
    )
    backup_init = bsub.add_parser(
        "init",
        help="generate the password file (0600) and initialize the encrypted restic repository",
    )
    backup_init.add_argument(
        "--repo",
        required=True,
        help="restic repository spec (e.g. sftp:host:/path or a local path)",
    )
    for name, helptext in (
        ("run", "back up the OMI folder, then apply 7d/4w/6m retention"),
        ("verify", "restic check + diff the latest snapshot's index.md against the live file"),
        ("install-timer", "install a daily systemd user timer running `omind backup run`"),
    ):
        backup_sub = bsub.add_parser(name, help=helptext)
        backup_sub.add_argument(
            "--vault",
            type=Path,
            default=default_vault_path(),
            help="path to the Obsidian vault (default: %(default)s)",
        )
        backup_sub.add_argument(
            "--folder", default="OMI", help="memory folder inside the vault (default: OMI)"
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

    rollup = sub.add_parser(
        "rollup",
        help="compact weeks of daily session journals into one summary note each, "
        "then archive (default) or delete the dailies",
    )
    rollup.add_argument(
        "--week",
        default=None,
        metavar="YYYY-Www",
        help="roll up exactly this ISO week now (default: every week older than "
        "the retention window)",
    )
    rollup.add_argument(
        "--retain-days",
        type=int,
        default=30,
        help="keep raw dailies this many days before rolling them up (default: 30)",
    )
    rollup.add_argument(
        "--delete",
        action="store_true",
        help="delete rolled-up dailies instead of archiving them to Journal/Archive/",
    )
    rollup.add_argument(
        "--vault",
        type=Path,
        default=default_vault_path(),
        help="path to the Obsidian vault (default: %(default)s)",
    )
    rollup.add_argument(
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


def _diagnose_with_backup(config: SetupConfig) -> list[CheckResult]:
    """The agent's wiring checks plus the agent-independent backup check."""
    from omind.backup import diagnose_backup

    return diagnose_for(config) + diagnose_backup(config)


def _run_doctor(args: argparse.Namespace) -> int:
    config = SetupConfig(
        vault=args.vault,
        folder=args.folder,
        server_name=args.server_name,
        agent=args.agent,
    )
    return run_doctor(config, diagnose_fn=_diagnose_with_backup)


def _run_backup(args: argparse.Namespace) -> int:
    from omind.backup import (
        BackupError,
        init_backup,
        install_timer,
        run_backup,
        verify_backup,
    )

    try:
        if args.backup_command == "init":
            init_backup(args.repo)
            return 0
        if args.backup_command == "run":
            run_backup((args.vault / args.folder).expanduser())
            return 0
        if args.backup_command == "verify":
            results = verify_backup((args.vault / args.folder).expanduser())
            symbols = {"ok": "✓", "warn": "!", "fail": "✗"}
            for result in results:
                print(f"  [{symbols[result.level]}] {result.message}")
            return 1 if any(r.level == "fail" for r in results) else 0
        # install-timer
        install_timer(SetupConfig(vault=args.vault, folder=args.folder))
        return 0
    except BackupError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def _run_node(args: argparse.Namespace) -> int:
    # Imported lazily: the mcp SDK is only needed when actually serving.
    from omind.mesh import load_node_config
    from omind.server import run_node

    omi_dir = (args.vault / args.folder).expanduser()
    # With a mesh identity, every MCP write stamps the next Lamport rev.
    cfg = load_node_config(omi_dir)
    return run_node(omi_dir, node_id=cfg.node_id if cfg else None)


def _run_mesh(args: argparse.Namespace) -> int:
    from omind import mesh
    from omind.store import NoteError

    omi_dir = (args.vault / args.folder).expanduser()

    def require_node_id() -> str:
        cfg = mesh.load_node_config(omi_dir)
        if cfg is None:
            raise mesh.MeshError(f"not a mesh node yet — run `omind mesh init` first ({omi_dir})")
        return cfg.node_id

    try:
        if args.mesh_command == "init":
            mesh.mesh_init(omi_dir)
        elif args.mesh_command == "add-peer":
            mesh.add_peer(omi_dir, args.name, args.url)
            print(f"peer added: {args.name} -> {args.url}")
        elif args.mesh_command == "remove-peer":
            mesh.remove_peer(omi_dir, args.name)
            print(f"peer removed: {args.name}")
        elif args.mesh_command == "sync":
            report = mesh.sync(omi_dir, require_node_id(), only=args.peer)
            for ps in report.peers:
                state = ps.error or ("synced" if ps.pushed else "merged, push pending")
                print(f"{ps.name}: {state}")
            if not report.peers:
                print("no peers configured (committed local changes only)")
            return 0 if report.ok else 1
        elif args.mesh_command == "daemon":
            cfg = mesh.load_node_config(omi_dir)
            if cfg is None:
                raise mesh.MeshError(
                    f"not a mesh node yet — run `omind mesh init` first ({omi_dir})"
                )
            return mesh.run_daemon(omi_dir, cfg)
        elif args.mesh_command == "install-service":
            mesh.install_service(args.vault, args.folder)
        elif args.mesh_command == "clone":
            mesh.clone(args.url, omi_dir)
            print(f"node ready at {omi_dir}; next: omind setup")
        elif args.mesh_command == "purge":
            mesh.purge(omi_dir, args.note, require_node_id())
    except (mesh.MeshError, NoteError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


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
    from omind.journal import migrate_journals
    from omind.store import OmiStore

    omi_dir = (args.vault / args.folder).expanduser()
    moved = migrate_journals(omi_dir)  # locked; idempotent no-op on a clean vault
    if moved:
        print(f"moved {len(moved)} session journal(s) into Journal/")
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


def _run_rollup(args: argparse.Namespace) -> int:
    import re

    from omind.journal import migrate_journals, rollup_journals

    if args.week is not None and not re.fullmatch(r"\d{4}-W\d{2}", args.week):
        print(f"error: --week must look like 2026-W24, got {args.week!r}", file=sys.stderr)
        return 1
    omi_dir = (args.vault / args.folder).expanduser()
    moved = migrate_journals(omi_dir)  # sweep strays first so they roll up too
    if moved:
        print(f"moved {len(moved)} session journal(s) into Journal/")
    results = rollup_journals(
        omi_dir, week=args.week, retain_days=args.retain_days, delete=args.delete
    )
    if not results:
        print("nothing to roll up")
        return 0
    for result in results:
        if args.delete:
            fate = f"deleted {len(result.deleted)}"
        else:
            fate = f"archived {len(result.archived)}"
        print(
            f"{result.week}: {len(result.days)} day(s) -> "
            f"{result.rollup_filename} ({fate} dailies)"
        )
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
    if args.command == "node":
        return _run_node(args)
    if args.command == "mesh":
        return _run_mesh(args)
    if args.command == "merge-driver":
        from omind.merge import run_merge_driver

        return run_merge_driver(args.base, args.ours, args.theirs, args.path_label)
    if args.command == "serve":
        return _run_serve(args)
    if args.command == "doctor":
        return _run_doctor(args)
    if args.command == "backup":
        return _run_backup(args)
    if args.command == "export":
        return _run_export(args)
    if args.command == "import":
        return _run_import(args)
    if args.command == "reindex":
        return _run_reindex(args)
    if args.command == "note":
        return _run_note(args)
    if args.command == "rollup":
        return _run_rollup(args)
    if args.command == "hook":
        return _run_hook(args)
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
