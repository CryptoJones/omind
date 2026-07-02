# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Aaron K. Clark
"""Command-line entry point for omind.

Subcommands:
  * ``omind setup``  — provision the OMI/Obsidian MCP wiring for an AI agent
    (``--agent`` claude (default), hermes, openclaw, opencode, codex, gemini,
    claude-desktop, kiro, vscode, q).
  * ``omind serve``  — run the local web UI over an OMI memory folder.
  * ``omind doctor`` — diagnose the wiring.
  * ``omind export`` — write the entire OMI dataset to a json or tar.gz bundle.
  * ``omind import`` — load an OMI dataset bundle back into a folder.
  * ``omind reindex`` — regenerate index.md under the inter-process write lock.
  * ``omind quickstart`` — print the manual-wiring steps `setup` automates.
  * ``omind graph`` — query the [[wikilink]] knowledge graph (neighbors, path,
    orphans, dangling links, stats, export).
  * ``omind note`` — safely create/update one OMI note through OmiStore.
  * ``omind rollup`` — compact weeks of daily session journals into summaries.
  * ``omind backup`` — encrypted off-machine backup of the OMI folder (restic).
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from omind import __version__, loopguard
from omind.agents import AGENT_CHOICES, diagnose_for, run_setup_for
from omind.guard import run_guard
from omind.hooks import ALL_HOOK_EVENTS, run_hook
from omind.provision import (
    CheckResult,
    ProvisionError,
    SetupConfig,
    default_vault_path,
    run_doctor,
)


def _add_vault_args(p: argparse.ArgumentParser) -> None:
    """The --vault/--folder pair every vault-touching subcommand shares.

    One definition: a subcommand with silently different defaults or help is
    exactly the drift this prevents.
    """
    p.add_argument(
        "--vault",
        type=Path,
        default=default_vault_path(),
        help="path to the Obsidian vault (default: %(default)s)",
    )
    p.add_argument(
        "--folder", default="OMI", help="memory folder inside the vault (default: OMI)"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="omind",
        description="OMI/Obsidian memory tooling for AI agents.",
    )
    parser.add_argument("--version", action="version", version=f"omind {__version__}")
    sub = parser.add_subparsers(
        dest="command",
        metavar="{setup,quickstart,serve,doctor,self-update,backup,export,import,reindex,note,rollup,hook}",
    )

    setup = sub.add_parser(
        "setup", help="provision the OMI/Obsidian MCP wiring for an AI agent"
    )
    _add_vault_args(setup)
    setup.add_argument(
        "--agent",
        choices=AGENT_CHOICES,
        default="claude",
        help="which agent to provision (default: claude — the Claude Code CLI)",
    )
    setup.add_argument(
        "--server-name",
        default="omi",
        help="name to register the MCP server under (default: omi)",
    )
    setup.add_argument(
        "--no-mesh",
        action="store_true",
        help="skip mesh initialization (single-machine, no replication)",
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
    _add_vault_args(quickstart)
    quickstart.add_argument(
        "--agent",
        choices=AGENT_CHOICES,
        default="claude",
        help="which agent the steps target (default: claude — the Claude Code CLI)",
    )
    quickstart.add_argument(
        "--server-name",
        default="omi",
        help="name to register the MCP server under (default: omi)",
    )

    node = sub.add_parser(
        "node", help="run the local mesh-node MCP server over stdio (docs/mesh.md)"
    )
    _add_vault_args(node)

    mesh = sub.add_parser(
        "mesh", help="peer-to-peer replication of the OMI folder over git (docs/mesh.md)"
    )
    msub = mesh.add_subparsers(
        dest="mesh_command",
        metavar="{init,add-peer,add-seed,remove-peer,sync,daemon,install-service,clone,purge}",
        required=True,
    )
    mesh_init_p = msub.add_parser(
        "init",
        help="make the OMI folder a mesh node: git repo, merge driver, node identity",
    )
    mesh_add_peer = msub.add_parser("add-peer", help="register a peer node (a git remote)")
    mesh_add_peer.add_argument("name", help="peer name (e.g. pluto)")
    mesh_add_peer.add_argument("url", help="git URL (e.g. ssh://pluto/~/path/to/OMI)")
    mesh_add_seed = msub.add_parser(
        "add-seed",
        help="provision a passive bare seed repo (local path or ssh) and register it as a peer",
    )
    mesh_add_seed.add_argument("name", help="peer name for the seed (e.g. seed)")
    mesh_add_seed.add_argument(
        "url", help="where the bare repo lives: ssh://host/path, host:path, or a local path"
    )
    mesh_add_seed.add_argument(
        "--mirror",
        metavar="GIT_URL",
        default=None,
        help="mirror the seed to this git URL on every push "
        "(e.g. a PRIVATE GitHub repo — notes travel in plaintext)",
    )
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
        mesh_add_seed,
        mesh_remove_peer,
        mesh_sync,
        mesh_daemon,
        mesh_install,
        mesh_clone,
        mesh_purge,
    ):
        _add_vault_args(mp)

    # Hidden: invoked by git (merge.omi.driver), never by hand.
    merge_driver = sub.add_parser("merge-driver")
    merge_driver.add_argument("base", type=Path)
    merge_driver.add_argument("ours", type=Path)
    merge_driver.add_argument("theirs", type=Path)
    merge_driver.add_argument("path_label", nargs="?", default="")

    serve = sub.add_parser("serve", help="run the local web UI over an OMI memory folder")
    _add_vault_args(serve)
    serve.add_argument("--host", default="127.0.0.1", help="bind host (default: 127.0.0.1)")
    serve.add_argument("--port", type=int, default=8765, help="bind port (default: 8765)")
    serve.add_argument("--reload", action="store_true", help="auto-reload on code changes (dev)")

    doctor = sub.add_parser("doctor", help="diagnose the OMI/Obsidian MCP wiring")
    _add_vault_args(doctor)
    doctor.add_argument(
        "--agent",
        choices=AGENT_CHOICES,
        default="claude",
        help="which agent's wiring to diagnose (default: claude — the Claude Code CLI)",
    )
    doctor.add_argument(
        "--server-name",
        default="omi",
        help="name the MCP server is registered under (default: omi)",
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
        _add_vault_args(backup_sub)

    export = sub.add_parser("export", help="write the entire OMI dataset to a bundle")
    _add_vault_args(export)
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
    _add_vault_args(imp)
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
    _add_vault_args(reindex)

    convert = sub.add_parser(
        "convert",
        help="migrate an OMI vault to the Open Knowledge Format (OKF): give every "
        "note YAML frontmatter with a 'type' (idempotent, in place)",
    )
    convert.add_argument(
        "--dry-run",
        action="store_true",
        help="report what would change without writing any note",
    )
    convert.add_argument(
        "--check",
        action="store_true",
        help="only check OKF conformance and report; make no changes",
    )
    _add_vault_args(convert)

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
    note.add_argument(
        "--connection",
        action="append",
        default=[],
        metavar="TITLE",
        help="a single connection title (repeatable) — use for titles that "
        "contain commas, which --connections would wrongly split",
    )
    note.add_argument("--references", default="", help="comma-separated references")
    _add_vault_args(note)

    search = sub.add_parser("search", help="search OMI notes from the terminal")
    search.add_argument(
        "query", help="case-insensitive substring over title/summary/details/tags"
    )
    search.add_argument("--tag", default=None, help="also require this tag")
    _add_vault_args(search)

    lint = sub.add_parser(
        "lint",
        help="check the vault for broken wikilinks, isolated/orphaned notes, "
        "missing titles, and near-duplicate notes",
    )
    lint.add_argument(
        "--json", action="store_true", help="emit issues as JSON instead of a report"
    )
    lint.add_argument(
        "--strict",
        action="store_true",
        help="exit non-zero on any issue (default: only on an error-severity issue)",
    )
    _add_vault_args(lint)

    graph_p = sub.add_parser(
        "graph",
        help="query the [[wikilink]] knowledge graph: neighbors, path, orphans, "
        "dangling links, stats, export",
    )
    gsub = graph_p.add_subparsers(
        dest="graph_command",
        metavar="{neighbors,path,orphans,dangling,stats,export}",
        required=True,
    )
    g_neighbors = gsub.add_parser("neighbors", help="notes within N hops of a note")
    g_neighbors.add_argument("note", help="note filename, stem, or title")
    g_neighbors.add_argument(
        "--depth", type=int, default=1, help="hops to traverse (default: 1)"
    )
    g_neighbors.add_argument(
        "--direction",
        choices=("out", "in", "both"),
        default="both",
        help="follow links it makes (out), links to it (in), or both (default: both)",
    )
    g_path = gsub.add_parser("path", help="shortest link path between two notes")
    g_path.add_argument("source", help="start note (filename, stem, or title)")
    g_path.add_argument("target", help="end note (filename, stem, or title)")
    g_orphans = gsub.add_parser("orphans", help="notes with no inbound or outbound links")
    g_dangling = gsub.add_parser("dangling", help="wikilinks pointing at no existing note")
    g_stats = gsub.add_parser("stats", help="counts: notes, links, orphans, dangling")
    g_export = gsub.add_parser("export", help="dump the whole graph for visualization")
    g_export.add_argument(
        "--format",
        choices=("json", "dot"),
        default="json",
        help="output format (default: json)",
    )
    for gp in (g_neighbors, g_path, g_orphans, g_dangling, g_stats, g_export):
        _add_vault_args(gp)

    checkpoint = sub.add_parser(
        "checkpoint",
        help="summarize recent activity (journal + compliance log) into a daily "
        "worklog note; install-timer runs it unattended every N minutes",
    )
    checkpoint.add_argument(
        "action",
        nargs="?",
        default="run",
        choices=("run", "install-timer", "uninstall-timer"),
        help="run a checkpoint now (default), or install/remove the systemd user timer",
    )
    checkpoint.add_argument(
        "--since",
        default="15m",
        help="window to summarize for `run` (e.g. 15m, 1h, 90; default: 15m)",
    )
    checkpoint.add_argument(
        "--every",
        default="15m",
        help="timer interval for install-timer (e.g. 15m, 1h; default: 15m)",
    )
    checkpoint.add_argument(
        "--llm",
        action="store_true",
        help="add a headless `claude -p` narrative (fail-open to the deterministic summary)",
    )
    _add_vault_args(checkpoint)

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
    _add_vault_args(rollup)

    hook = sub.add_parser(
        "hook",
        help="(internal) record an agent action into the OMI journal, or emit "
        "session priming",
    )
    hook.add_argument(
        "event",
        choices=list(ALL_HOOK_EVENTS),
        help="the hook event name (Claude Code: PostToolUse/Stop/SessionStart; "
        "Hermes Agent: pre_llm_call)",
    )
    _add_vault_args(hook)

    loop = sub.add_parser(
        "loop",
        help="autonomous-loop guard: while ARMED, the Stop hook refuses to stop so "
        "the agent keeps working (arm/disarm/status)",
    )
    loop.add_argument("action", choices=("arm", "disarm", "status"))
    loop.add_argument(
        "--reason", default=None, help="why the loop is armed (surfaced in the directive)"
    )
    loop.add_argument(
        "--max-blocks",
        type=int,
        default=loopguard.DEFAULT_MAX_BLOCKS,
        help="auto-disarm after this many consecutive stops with NO work between (backstop)",
    )
    loop.add_argument(
        "--hours",
        type=float,
        default=loopguard.DEFAULT_HOURS,
        help="auto-expire the armed flag after this many hours (0 = never)",
    )
    loop.add_argument(
        "--session",
        default=None,
        help="the loop's owner session id (only this session is refused; "
        "defaults to $CLAUDE_SESSION_ID). Prevents trapping concurrent sessions.",
    )

    guard = sub.add_parser(
        "guard",
        help="(internal) OMI-compliance enforcement decision for an agent "
        "action; called by the per-harness guard adapters",
    )
    guard.add_argument(
        "action",
        choices=(
            "check",
            "reset",
            "learn",
            "escalate",
            "verify",
            "suggest",
            "adapter",
            "selftest",
            "export-corpus",
            "log",
            "policy",
            "explain",
            "status",
            "pause",
            "resume",
            "repair",
        ),
        help="check/reset the gate; learn/escalate rules; verify/suggest; adapter "
        "normalizes another harness's event; selftest replays canned events; "
        "export-corpus emits fine-tuning JSONL; log/policy/status inspect the "
        "compliance log, active rules, and guardable harnesses; explain dry-runs a "
        "command (--command); pause/resume time-box off the consult-gate + verifier "
        "for mission-critical speed (--for; hard blocks stay on); repair "
        "re-provisions a wedged guard hook-set",
    )
    guard.add_argument(
        "--omi-dir",
        type=Path,
        default=None,
        help="OMI folder for actions that read/write notes (learn/verify/suggest); "
        "defaults to the standard vault's OMI folder",
    )
    guard.add_argument(
        "--harness",
        default="claude",
        help="harness whose event shape + block-output format the adapter targets "
        "(claude, hermes, opencode); default: claude",
    )
    guard.add_argument(
        "--command",
        dest="guard_command",
        default="",
        help="the command to dry-run (for `guard explain`)",
    )
    guard.add_argument(
        "--limit", type=int, default=20, help="max compliance-log rows (for `guard log`)"
    )
    guard.add_argument(
        "--for",
        dest="pause_for",
        default="",
        help="pause duration for `guard pause` (e.g. 30m, 2h, 90s, or a bare number "
        "= minutes; default 30m)",
    )
    guard.add_argument(
        "--explain",
        action="store_true",
        help="for `guard verify`: print the relevance score/threshold/band "
        "diagnostic (debug a REQUIRE-mode false negative) instead of judging",
    )

    selfupdate = sub.add_parser(
        "self-update", help="check GitHub for a newer omind and reinstall it"
    )
    selfupdate.add_argument(
        "--check", action="store_true", help="only report whether an update is available"
    )
    selfupdate.add_argument(
        "--force", action="store_true", help="reinstall the latest even if not newer"
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
        no_mesh=args.no_mesh,
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
    """The agent's wiring checks plus the backup and mesh checks."""
    from omind.backup import diagnose_backup
    from omind.mesh import diagnose_mesh

    return diagnose_for(config) + diagnose_mesh(config) + diagnose_backup(config)


def _run_doctor(args: argparse.Namespace) -> int:
    config = SetupConfig(
        vault=args.vault,
        folder=args.folder,
        server_name=args.server_name,
        agent=args.agent,
    )
    rc = run_doctor(config, diagnose_fn=_diagnose_with_backup)
    from omind.update import update_nudge

    nudge = update_nudge()
    if nudge:
        print(f"\n{nudge}")
    return rc


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
            from omind.provision import _doctor_symbols

            results = verify_backup((args.vault / args.folder).expanduser())
            symbols = _doctor_symbols()  # ASCII degrade on cp1252 consoles
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
    from omind.mesh import MeshError, load_node_config
    from omind.server import run_node

    omi_dir = (args.vault / args.folder).expanduser()
    # With a mesh identity, every MCP write stamps the next Lamport rev.
    # A corrupt node.json must not take the memory tools away from every
    # Claude session — degrade to unstamped writes and say so on stderr.
    try:
        cfg = load_node_config(omi_dir)
    except MeshError as exc:
        print(f"warning: {exc}; serving without a mesh identity", file=sys.stderr)
        cfg = None
    # #87: self-heal a stale/missing guard hook-set on a newer binary (fail-open,
    # stderr only — stdout is the MCP channel). Opt out with OMIND_NO_AUTOHEAL=1.
    from omind.provision import autoheal_on_startup

    autoheal_on_startup(args.vault, args.folder)
    from omind.update import update_nudge

    nudge = update_nudge()  # cached, fail-open; stderr only — stdout is the MCP channel
    if nudge:
        print(nudge, file=sys.stderr)
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
        elif args.mesh_command == "add-seed":
            mesh.add_seed(omi_dir, args.name, args.url, mirror=args.mirror)
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


def _serve_allowed_hosts(host: str) -> list[str]:
    """The Host-header allowlist for a bind host, warning on non-localhost.

    A deliberate all-interfaces bind (0.0.0.0 / ::) disables the Host check
    (``["*"]``) since the operator opted into remote access; a specific remote
    host is added to the localhost allowlist so it works while other hostnames
    (a DNS-rebinding attacker's) stay blocked.
    """
    from omind.web.app import DEFAULT_ALLOWED_HOSTS

    localhost = {"127.0.0.1", "localhost", "::1", "[::1]"}
    if host in {"0.0.0.0", "::", ""}:
        print(
            "  WARNING: binding to all interfaces — the web API is UNAUTHENTICATED and "
            "Host-header protection is disabled. Prefer --host 127.0.0.1.",
            file=sys.stderr,
        )
        return ["*"]
    if host in localhost:
        return list(DEFAULT_ALLOWED_HOSTS)
    print(
        f"  WARNING: binding to {host} — the web API is unauthenticated; only expose it "
        "on a trusted network.",
        file=sys.stderr,
    )
    return [*DEFAULT_ALLOWED_HOSTS, host]


def _run_serve(args: argparse.Namespace) -> int:
    import uvicorn

    omi_dir = (args.vault / args.folder).expanduser()
    allowed = _serve_allowed_hosts(args.host)
    print(f"omind serve -> {omi_dir}")
    print(f"open http://{args.host}:{args.port}")
    if args.reload:
        os.environ["OMIND_OMI_DIR"] = str(omi_dir)
        os.environ["OMIND_ALLOWED_HOSTS"] = ",".join(allowed)
        uvicorn.run(
            "omind.web.app:get_app",
            factory=True,
            host=args.host,
            port=args.port,
            reload=True,
        )
    else:
        from omind.web.app import create_app

        uvicorn.run(create_app(omi_dir, allowed_hosts=allowed), host=args.host, port=args.port)
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


def _run_convert(args: argparse.Namespace) -> int:
    from omind import okf

    omi_dir = (args.vault / args.folder).expanduser()
    if args.check:
        report = okf.check_conformance(omi_dir)
    else:
        result = okf.convert_vault(omi_dir, dry_run=args.dry_run)
        verb = "would convert" if args.dry_run else "converted"
        print(f"{verb} {result.converted} note(s); {result.unchanged} already in OKF form")
        report = result.report
    for problem in report.problems:
        print(f"  [x] {problem.filename}: {problem.problem}")
    tail = "all conformant" if report.ok else f"{len(report.problems)} non-conformant"
    print(f"OKF v0.1 conformance: {report.conformant}/{report.concepts} concept notes — {tail}")
    return 0 if report.ok else 1


def _run_search(args: argparse.Namespace) -> int:
    from omind.store import OmiStore

    omi_dir = (args.vault / args.folder).expanduser()
    results = OmiStore(omi_dir).search(args.query, tag=args.tag)
    if not results:
        print("no matches")
        return 0
    for note in results:
        summary = f" — {note.summary}" if note.summary else ""
        print(f"{note.title}{summary}")
    return 0


def _run_lint(args: argparse.Namespace) -> int:
    import json

    from omind import lint

    omi_dir = (args.vault / args.folder).expanduser()
    issues = lint.lint_vault(omi_dir)
    if args.json:
        print(json.dumps([i.__dict__ for i in issues], indent=2))
    else:
        print(lint.format_report(issues, omi_dir=omi_dir))
    if args.strict and issues:
        return 1
    return 1 if any(i.severity == "error" for i in issues) else 0


def _run_graph(args: argparse.Namespace) -> int:
    import json

    from omind import graph as graphmod

    omi_dir = (args.vault / args.folder).expanduser()
    g = graphmod.build_graph(omi_dir)
    cmd = args.graph_command
    try:
        if cmd == "neighbors":
            hits = graphmod.neighbors(
                g, args.note, depth=args.depth, direction=args.direction
            )
            if not hits:
                print("no neighbors")
                return 0
            for filename, distance in hits:
                print(f"{distance}\t{filename}")
            return 0
        if cmd == "path":
            path = graphmod.shortest_path(g, args.source, args.target)
            if path is None:
                print("no path")
                return 1
            print(" -> ".join(path))
            return 0
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    if cmd == "orphans":
        found = graphmod.orphans(g)
        print("\n".join(found) if found else "no orphan notes")
        return 0
    if cmd == "dangling":
        links = graphmod.dangling_links(g)
        if not links:
            print("no dangling links")
            return 0
        for src, target in links:
            print(f"{src}\t[[{target}]]")
        return 0
    if cmd == "stats":
        print(json.dumps(graphmod.stats(g), indent=2))
        return 0
    # cmd == "export"
    if args.format == "dot":
        print(graphmod.to_dot(g))
    else:
        print(json.dumps(graphmod.to_json(g), indent=2))
    return 0


def _run_checkpoint(args: argparse.Namespace) -> int:
    from omind import checkpoint

    omi_dir = (args.vault / args.folder).expanduser()
    if args.action == "install-timer":
        try:
            checkpoint.install_timer(args.every, args.vault, args.folder)
        except (ValueError, FileNotFoundError, OSError) as exc:
            print(f"error: could not install timer: {exc}", file=sys.stderr)
            return 1
        return 0
    if args.action == "uninstall-timer":
        try:
            checkpoint.uninstall_timer()
        except OSError as exc:
            print(f"error: could not uninstall timer: {exc}", file=sys.stderr)
            return 1
        return 0
    # `checkpoint run` fires from a systemd timer. Its contract is "never raises
    # into a timer" — a vault/store error here must be a clean non-zero exit with
    # a message, not an unhandled traceback every interval.
    try:
        action, filename = checkpoint.write_checkpoint(omi_dir, since=args.since, llm=args.llm)
    except Exception as exc:  # noqa: BLE001 — a timer must degrade, never crash-loop
        print(f"error: checkpoint failed: {exc}", file=sys.stderr)
        return 1
    print(f"{action} {filename}")
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
        # CSV titles plus any repeatable --connection (exact titles, comma-safe).
        connections=(
            _split_csv(args.connections) + [c.strip() for c in args.connection if c.strip()]
        ),
        references=_split_csv(args.references),
    )
    omi_dir = (args.vault / args.folder).expanduser()
    try:
        action, filename = upsert_note(omi_dir, fields)
    except NoteError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"{action} {filename}")
    if action == "created":
        _dedup_hint(omi_dir, fields, filename)
    return 0


def _dedup_hint(omi_dir: Path, fields: object, filename: str) -> None:
    """After CREATING a note, hint (to stderr, non-blocking) when a semantically
    very similar note already exists — so the next insight updates it instead of
    duplicating (3.0.0). Silent unless the embed backend is installed and a close
    match is found; never raises, never blocks the write."""
    try:
        from omind import vectorindex

        title = getattr(fields, "title", "")
        summary = getattr(fields, "summary", "")
        tags = getattr(fields, "tags", []) or []
        text = "\n".join([title, summary, " ".join(tags)]).strip()
        near = vectorindex.VectorIndex(omi_dir).nearest(text, exclude=filename, limit=1)
        if not near:
            return
        name, score = near[0]
        try:
            threshold = float(os.environ.get("OMI_DEDUP_THRESHOLD") or 0.6)
        except ValueError:
            threshold = 0.6
        if score >= threshold:
            other = name[:-3] if name.endswith(".md") else name
            print(
                f"note: this looks similar to [[{other}]] (cosine {score:.2f}) — if it is "
                "the same insight, re-run with that title to update it in place.",
                file=sys.stderr,
            )
    except Exception:
        return


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


def _run_loop(args: argparse.Namespace) -> int:
    """Operator switch for the autonomous-loop guard (arm/disarm/status)."""
    if args.action == "arm":
        session = args.session or os.environ.get("CLAUDE_SESSION_ID")
        st = loopguard.arm(
            reason=args.reason, max_blocks=args.max_blocks, hours=args.hours, session=session
        )
        exp = st["expires_at"] or "never"
        owner = st.get("owner") or "first session to stop (unclaimed)"
        print(
            f"loop guard ARMED — the Stop hook will refuse to stop until `omind loop disarm`.\n"
            f"  owner: {owner}\n"
            f"  backstop: auto-disarm after {st['max_blocks']} consecutive stops with no work; "
            f"expires {exp}."
        )
        return 0
    if args.action == "disarm":
        loopguard.disarm()
        print("loop guard DISARMED — stops are allowed again.")
        return 0
    st = loopguard.status()
    state = "ARMED" if st["armed"] else "disarmed"
    print(
        f"loop guard: {state}"
        + (f" (reason: {st['reason']})" if st.get("reason") else "")
        + f"\n  blocks={st['blocks']}/{st['max_blocks']}  armed_at={st['armed_at']}"
        + f"  expires={st['expires_at']}"
    )
    return 0


def _run_self_update(args: argparse.Namespace) -> int:
    from omind.update import self_update

    return self_update(check_only=args.check, force=args.force)


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
    if args.command == "search":
        return _run_search(args)
    if args.command == "lint":
        return _run_lint(args)
    if args.command == "graph":
        return _run_graph(args)
    if args.command == "checkpoint":
        return _run_checkpoint(args)
    if args.command == "reindex":
        return _run_reindex(args)
    if args.command == "convert":
        return _run_convert(args)
    if args.command == "note":
        return _run_note(args)
    if args.command == "rollup":
        return _run_rollup(args)
    if args.command == "hook":
        return _run_hook(args)
    if args.command == "loop":
        return _run_loop(args)
    if args.command == "guard":
        omi_dir = args.omi_dir if args.omi_dir is not None else (default_vault_path() / "OMI")
        return run_guard(
            args.action,
            omi_dir=omi_dir,
            harness=args.harness,
            limit=args.limit,
            command=args.guard_command,
            explain=args.explain,
            duration=args.pause_for,
        )
    if args.command == "self-update":
        return _run_self_update(args)
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
