# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Aaron K. Clark
"""Mesh replication over git (docs/mesh.md).

The OMI folder becomes a git working tree; every machine is a full node and
nodes replicate peer-to-peer over git remotes. This module owns the node's
identity and repo plumbing: ``mesh init``, the per-node config, and the
``git`` wrapper every later piece (sync, daemon, clone) builds on.

Git is driven via subprocess (never a native binding) to keep the pip-audit
dependency surface clean, and every git invocation that can touch the working
tree runs under the same ``.omi.lock`` that serializes note writes — a sync
must never interleave with a half-written note.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shlex
import shutil
import socket
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape as xml_escape

from omind.backup import config_dir
from omind.paths import sync_state_path
from omind.proc import run_command
from omind.store import OmiStore, _atomic_write

Logger = Callable[[str], None]


class MeshError(Exception):
    """Raised when a mesh operation (git, config, init) fails."""


GIT_TIMEOUT = 120.0
"""Seconds per git invocation; LAN fetches and local plumbing are fast."""

#: Merge-driver routing for the OMI repo. Notes get the field-level omi
#: driver; generated files are never merged (ours + regenerate); journals and
#: tombstones are append-only line sets, so git's union driver is exact.
GITATTRIBUTES = """\
* -text
*.md merge=omi
index.md merge=ours
Journal/*.md merge=union
.omi-tombstones merge=union
"""

#: Never replicate the lock or torn temp files.
GITIGNORE = """\
.omi.lock
.tmp-*
"""

_NODE_ID_RE = re.compile(r"[^A-Za-z0-9._-]+")


def git(
    omi_dir: Path,
    *args: str,
    check: bool = True,
    timeout: float = GIT_TIMEOUT,
) -> subprocess.CompletedProcess[str]:
    """Run git against the OMI repo, output captured, failures as MeshError."""
    return run_command(
        ["git", "-C", str(omi_dir), *args], error=MeshError, check=check, timeout=timeout
    )


# -- node config ----------------------------------------------------------------


@dataclass
class NodeConfig:
    """This machine's identity and sync cadence for one OMI folder.

    Lives in ``~/.config/omind/node.json`` — deliberately *outside* the OMI
    folder, which replicates: a synced node identity would make two machines
    claim the same Lamport node-id.
    """

    node_id: str
    interval_seconds: int = 300
    debounce_seconds: int = 10


def node_config_path() -> Path:
    return config_dir() / "node.json"


def _config_key(omi_dir: Path) -> str:
    return str(Path(omi_dir).expanduser().resolve())


def _read_config_file() -> dict[str, Any]:
    path = node_config_path()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise MeshError(f"unreadable node config {path}: {exc}") from exc
    return data if isinstance(data, dict) else {}


def load_node_config(omi_dir: Path) -> NodeConfig | None:
    """The node config for this OMI folder, or None when never initialized."""
    nodes = _read_config_file().get("nodes")
    if not isinstance(nodes, dict):
        return None
    entry = nodes.get(_config_key(omi_dir))
    if not isinstance(entry, dict) or not entry.get("node_id"):
        return None
    return NodeConfig(
        node_id=str(entry["node_id"]),
        interval_seconds=int(entry.get("interval_seconds", 300)),
        debounce_seconds=int(entry.get("debounce_seconds", 10)),
    )


def save_node_config(omi_dir: Path, cfg: NodeConfig) -> None:
    path = node_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = _read_config_file()
    nodes = data.setdefault("nodes", {})
    nodes[_config_key(omi_dir)] = {
        "node_id": cfg.node_id,
        "interval_seconds": cfg.interval_seconds,
        "debounce_seconds": cfg.debounce_seconds,
    }
    # Atomic: a torn node.json breaks every later mesh command — or worse,
    # loses this folder's entry and mints a new node_id on the next setup.
    _atomic_write(path, json.dumps(data, indent=2, sort_keys=True) + "\n")


def new_node_id() -> str:
    """``<hostname>-<6 hex>`` — generated once per folder, never regenerated."""
    host = _NODE_ID_RE.sub("-", socket.gethostname().split(".")[0]).strip("-") or "node"
    return f"{host}-{secrets.token_hex(3)}"


# -- mesh init --------------------------------------------------------------------


def _merge_driver_command() -> str:
    """The git config line invoking omind's field-level note merge driver.

    Absolute interpreter path: git spawns the driver without our PATH or
    venv activation, especially under systemd or on Windows. Forward slashes
    keep the quoting sane in .git/config on every platform.
    """
    exe = sys.executable.replace(os.sep, "/")
    return f'"{exe}" -m omind merge-driver %O %A %B %P'


def _write_if_changed(path: Path, content: str, log: Logger) -> None:
    if path.is_file() and path.read_text(encoding="utf-8") == content:
        return
    path.write_text(content, encoding="utf-8")
    log(f"wrote {path.name}")


def harden_permissions(omi_dir: Path, log: Logger) -> None:
    """Owner-only access to the memory store (POSIX).

    Meshes never interact unless explicitly peered, but on a multi-user host a
    traversable OMI folder would let another local user `git fetch` the whole
    memory history via a file:// remote — read access alone leaks everything.
    0700 on the folder closes that; Windows ACLs are out of scope (documented).
    """
    if os.name == "nt":
        return
    for target in (omi_dir, omi_dir / ".git"):
        try:
            if target.is_dir() and (target.stat().st_mode & 0o077):
                target.chmod(0o700)
                log(f"tightened {target.name or target} to owner-only (0700)")
        except OSError as exc:
            log(f"could not tighten permissions on {target}: {exc}")


def mesh_init(omi_dir: Path, log: Logger = print) -> NodeConfig:
    """Turn the OMI folder into a mesh node: git repo, merge driver, identity.

    Idempotent — safe to re-run after upgrades (it refreshes the driver
    command and attributes without minting a new node-id or new commits when
    nothing changed).
    """
    omi_dir = Path(omi_dir).expanduser()
    if not omi_dir.is_dir():
        raise MeshError(f"OMI folder not found: {omi_dir}")
    store = OmiStore(omi_dir)

    cfg = load_node_config(omi_dir)
    if cfg is None:
        cfg = NodeConfig(node_id=new_node_id())
        save_node_config(omi_dir, cfg)
        log(f"node id: {cfg.node_id} (saved to {node_config_path()})")
    else:
        log(f"node id: {cfg.node_id} (existing)")

    with store.write_lock():
        if not (omi_dir / ".git").exists():
            git(omi_dir, "init")
            log("initialized git repository")
        if git(omi_dir, "rev-parse", "--verify", "HEAD", check=False).returncode != 0:
            # Unborn repo: deterministic branch name across nodes (old-git
            # compatible). Never moves an existing HEAD.
            git(omi_dir, "symbolic-ref", "HEAD", "refs/heads/main")

        git(omi_dir, "config", "user.name", cfg.node_id)
        git(omi_dir, "config", "user.email", f"omind@{cfg.node_id}")
        # The merge engine is newline-sensitive; never let git rewrite bytes.
        git(omi_dir, "config", "core.autocrlf", "false")
        git(omi_dir, "config", "merge.omi.name", "omind field-level note merge")
        git(omi_dir, "config", "merge.omi.driver", _merge_driver_command())
        # "ours" is NOT a built-in attributes driver (only text/binary/union
        # are); without this line `index.md merge=ours` falls back to a text
        # merge and the generated index conflicts on every cross edit.
        git(omi_dir, "config", "merge.ours.driver", "true")

        _write_if_changed(omi_dir / ".gitattributes", GITATTRIBUTES, log)
        _write_if_changed(omi_dir / ".gitignore", GITIGNORE, log)

        status = git(omi_dir, "status", "--porcelain").stdout
        if status.strip():
            git(omi_dir, "add", "-A")
            git(omi_dir, "commit", "-m", f"omind mesh: snapshot from {cfg.node_id}")
            log("committed working tree")
        else:
            log("working tree clean")
    harden_permissions(omi_dir, log)
    return cfg


# -- peers ----------------------------------------------------------------------

#: File listing hard-purged note filenames; merge=union so purges from every
#: node accumulate, and sync applies them after each merge.
TOMBSTONES_FILENAME = ".omi-tombstones"


def peers(omi_dir: Path) -> dict[str, str]:
    """The configured peer remotes: name -> fetch URL.

    One ``git config --get-regexp`` call instead of ``git remote`` plus one
    ``get-url`` per remote (the daemon calls this every sync tick). Keys are
    unambiguous even for URLs containing spaces — `remote -v`'s
    whitespace-delimited output is not (the default vault path,
    "Obsidian Vault", contains one).
    """
    res = git(omi_dir, "config", "--get-regexp", r"^remote\..*\.url$", check=False)
    result: dict[str, str] = {}
    for line in res.stdout.splitlines():
        key, _, url = line.partition(" ")
        if key.startswith("remote.") and key.endswith(".url") and url:
            result[key[len("remote.") : -len(".url")]] = url.strip()
    return result


def add_peer(omi_dir: Path, name: str, url: str) -> None:
    """Register a peer (an ordinary git remote — the only membership state)."""
    if not re.fullmatch(r"[A-Za-z0-9._-]+", name):
        raise MeshError(f"invalid peer name: {name!r}")
    git(omi_dir, "remote", "add", name, url)


def remove_peer(omi_dir: Path, name: str) -> None:
    git(omi_dir, "remote", "remove", name)


# -- seed (passive bare peer) -----------------------------------------------------

#: Remote name a seed mirrors to (when configured). Generic on purpose — the
#: mirror host (GitHub, Codeberg, another box) is the operator's choice.
MIRROR_REMOTE = "mirror"

#: hooks/post-receive installed into every seed. Nodes push only to their
#: refs/omind/<id> outbox, so a bare seed would never grow a branch; without
#: one, fetching from a seed yields nothing mergeable and doctor's peer check
#: reads "never fetched" forever. The hook points main at the freshest outbox
#: ref after every push, then mirror-pushes everything if a mirror remote is
#: configured.
SEED_POST_RECEIVE = """\
#!/bin/sh
# Installed by `omind mesh add-seed`; re-running it overwrites local edits.
newest=$(git for-each-ref --sort=-committerdate --count=1 \
    --format='%(objectname)' 'refs/omind/*')
[ -n "$newest" ] && git update-ref refs/heads/main "$newest"
if git config remote.mirror.url >/dev/null 2>&1; then
    exec git push --quiet --mirror mirror
fi
exit 0
"""


@dataclass
class SeedTarget:
    """Where a seed repo lives: the ssh command prefix to reach its host
    (empty for a local path) and the repo directory on that host."""

    ssh: list[str]
    path: str

    @property
    def remote(self) -> bool:
        return bool(self.ssh)


_SCP_LIKE_RE = re.compile(r"^(?P<host>[^/:]+):(?P<path>.+)$")
_DRIVE_RE = re.compile(r"^[A-Za-z]:[\\/]")


def _parse_seed_url(url: str) -> SeedTarget:
    """Split a git URL into how-to-reach-the-host + on-host path.

    Understands ssh:// URLs, scp-like host:path, and local paths — the forms
    we can provision a repo at. http(s)/git URLs are fetch-only transports.
    """
    if url.startswith("ssh://"):
        from urllib.parse import urlsplit

        parts = urlsplit(url)
        if not parts.hostname or not parts.path:
            raise MeshError(f"unusable ssh URL (need host and path): {url}")
        host = f"{parts.username}@{parts.hostname}" if parts.username else parts.hostname
        ssh = ["ssh", *(["-p", str(parts.port)] if parts.port else []), host]
        # git treats ssh://host/~/x as home-relative; hand the shell ~/x too.
        path = parts.path[1:] if parts.path.startswith("/~") else parts.path
        return SeedTarget(ssh=ssh, path=path)
    if "://" in url:
        raise MeshError(f"cannot provision a seed over {url.split('://', 1)[0]}://")
    scp = _SCP_LIKE_RE.match(url)
    if scp and not _DRIVE_RE.match(url):
        return SeedTarget(ssh=["ssh", scp.group("host")], path=scp.group("path"))
    return SeedTarget(ssh=[], path=str(Path(url).expanduser()))


def _seed_run(
    target: SeedTarget, args: list[str], check: bool = False
) -> subprocess.CompletedProcess[str]:
    """Run one argv on the seed's host (over ssh when remote, else directly)."""
    if target.remote:
        # The remote shell re-parses the command line; quote every word.
        return run_command(
            [*target.ssh, " ".join(shlex.quote(a) for a in args)],
            error=MeshError,
            check=check,
            timeout=GIT_TIMEOUT,
        )
    return run_command(args, error=MeshError, check=check, timeout=GIT_TIMEOUT)


def _seed_git(
    target: SeedTarget, *args: str, check: bool = False
) -> subprocess.CompletedProcess[str]:
    return _seed_run(target, ["git", "-C", target.path, *args], check=check)


def _install_seed_hook(target: SeedTarget) -> None:
    if not target.remote:
        hook = Path(target.path) / "hooks" / "post-receive"
        hook.write_text(SEED_POST_RECEIVE, encoding="utf-8")
        hook.chmod(0o755)
        return
    qhook = shlex.quote(f"{target.path}/hooks/post-receive")
    run_command(
        [*target.ssh, f"cat > {qhook} && chmod +x {qhook}"],
        error=MeshError,
        check=True,
        timeout=GIT_TIMEOUT,
        input_text=SEED_POST_RECEIVE,
    )


def add_seed(
    omi_dir: Path,
    name: str,
    url: str,
    mirror: str | None = None,
    log: Logger = print,
) -> None:
    """Provision a bare seed repo at *url* and register it as a peer.

    A seed is a passive rendezvous, not a hub (docs/mesh.md): nodes keep
    syncing peer-to-peer without it, but a seed on a usually-up box gives
    off-LAN machines a meeting point and `mesh clone` a bootstrap source.
    Creates the bare repo (locally or over ssh), installs the post-receive
    hook, configures the optional mirror remote, and adds the peer here.
    Every step converges on re-run instead of failing on existing state.
    """
    omi_dir = Path(omi_dir).expanduser()
    if not re.fullmatch(r"[A-Za-z0-9._-]+", name):
        raise MeshError(f"invalid peer name: {name!r}")
    target = _parse_seed_url(url)

    probe = _seed_git(target, "rev-parse", "--is-bare-repository")
    if probe.returncode == 0:
        if probe.stdout.strip() != "true":
            raise MeshError(f"{target.path}: exists and is not a bare repository")
        log(f"seed repo already present: {target.path}")
    else:
        init = _seed_run(target, ["git", "init", "--bare", "--initial-branch=main", target.path])
        if init.returncode != 0:
            raise MeshError(f"git init: {_first_line(init.stderr or init.stdout)}")
        log(f"created bare seed repo: {target.path}")

    if mirror:
        current = _seed_git(target, "remote", "get-url", MIRROR_REMOTE)
        if current.returncode != 0:
            _seed_git(target, "remote", "add", MIRROR_REMOTE, mirror, check=True)
        elif current.stdout.strip() != mirror:
            _seed_git(target, "remote", "set-url", MIRROR_REMOTE, mirror, check=True)
        log(f"mirror remote -> {mirror}")
    _install_seed_hook(target)
    log("post-receive hook installed (main pointer + mirror push)")

    existing = peers(omi_dir).get(name)
    if existing is None:
        add_peer(omi_dir, name, url)
        log(f"peer added: {name} -> {url}")
    elif existing == url:
        log(f"peer already registered: {name}")
    else:
        raise MeshError(f"peer {name} already points at {existing}")
    log(f"next: `omind mesh sync`, and `omind mesh add-peer {name} <url>` on the other nodes")


# -- sync -----------------------------------------------------------------------


@dataclass
class PeerSync:
    name: str
    fetched: bool = False
    merged: bool = False
    pushed: bool = False
    error: str = ""


@dataclass
class SyncReport:
    peers: list[PeerSync] = field(default_factory=list)
    committed: bool = False
    conflicts: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(not p.error for p in self.peers)


def _commit_locked(omi_dir: Path, message: str) -> bool:
    """Stage + commit everything. Caller MUST hold the store write lock."""
    # Never complete a merge an earlier crashed/timed-out sync abandoned:
    # `git add -A && git commit` on a tree with MERGE_HEAD would commit the
    # half-merged state (conflict markers included) and push it to peers.
    if git(omi_dir, "rev-parse", "-q", "--verify", "MERGE_HEAD", check=False).returncode == 0:
        git(omi_dir, "merge", "--abort", check=False)
    if not git(omi_dir, "status", "--porcelain").stdout.strip():
        return False
    git(omi_dir, "add", "-A")
    git(omi_dir, "commit", "-m", message)
    return True


def commit_local(omi_dir: Path, node_id: str) -> bool:
    """Commit local changes. The lock spans staging, so a concurrent store
    write can never be half-staged."""
    store = OmiStore(omi_dir)
    with store.write_lock():
        return _commit_locked(omi_dir, f"omind: local changes on {node_id}")


def _first_line(text: str) -> str:
    """The most diagnostic line of a git message (CONFLICT/error/fatal first)."""
    lines = [ln for ln in text.strip().splitlines() if ln.strip()]
    for ln in lines:
        if ln.startswith(("CONFLICT", "error:", "fatal:")):
            return ln
    return lines[0] if lines else ""


def _merge_ref(omi_dir: Path, ref: str) -> str:
    """Merge one ref; '' on success, else a one-line error (merge aborted)."""
    try:
        res = git(
            omi_dir,
            "merge",
            "--no-edit",
            "--allow-unrelated-histories",
            "-m",
            f"omind sync: merge {ref}",
            ref,
            check=False,
        )
    except MeshError as exc:
        # A timed-out merge (run_command raises before the returncode check)
        # still leaves MERGE_HEAD and a half-merged tree behind — abort it.
        git(omi_dir, "merge", "--abort", check=False)
        return f"merge {ref}: {_first_line(str(exc))}"
    if res.returncode == 0:
        return ""
    git(omi_dir, "merge", "--abort", check=False)
    return f"merge {ref}: {_first_line(res.stderr or res.stdout)}"


def _inbox_refs(omi_dir: Path, node_id: str) -> list[str]:
    """refs/omind/* other nodes pushed to us (never our own outbox ref)."""
    out = git(omi_dir, "for-each-ref", "--format=%(refname)", "refs/omind").stdout
    return [r for r in out.splitlines() if r.strip() and r != f"refs/omind/{node_id}"]


def _apply_tombstones(omi_dir: Path, store: OmiStore) -> None:
    """Unlink any note a purge tombstone names. Caller holds the write lock."""
    tomb = omi_dir / TOMBSTONES_FILENAME
    if not tomb.is_file():
        return
    for line in tomb.read_text(encoding="utf-8").splitlines():
        name = line.strip()
        if not name:
            continue
        target = omi_dir / name
        # Tombstones name plain note files only; never follow odd paths.
        if target.parent == omi_dir and target.is_file() and target.suffix == ".md":
            target.unlink()


def conflict_scan(omi_dir: Path) -> list[str]:
    """Filenames of notes carrying conflict markers (for the report + doctor)."""
    hits: list[str] = []
    for path in sorted(omi_dir.glob("*.md")):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue
        if any(line.startswith("<<<<<<<") for line in text.splitlines()):
            hits.append(path.name)
    return hits


def _write_sync_state(omi_dir: Path, report: SyncReport) -> None:
    path = sync_state_path(omi_dir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "last_sync": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "peers": [asdict(p) for p in report.peers],
            "conflicts": report.conflicts,
            "ok": report.ok,
        }
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    except OSError:
        pass  # advisory state for doctor; never fail a sync over it


def read_sync_state(omi_dir: Path) -> dict[str, Any] | None:
    path = sync_state_path(omi_dir)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def sync(
    omi_dir: Path,
    node_id: str,
    only: list[str] | None = None,
    log: Logger = print,
) -> SyncReport:
    """One full replication pass: commit, fetch+merge each reachable peer,
    regenerate generated files, apply tombstones, push our outbox ref.

    Partitions are normal: an unreachable peer is recorded and skipped, never
    an exception. Pushes go to ``refs/omind/<node_id>`` — never to the peer's
    checked-out branch (pushing into a non-bare repo's current branch is
    refused by git); the peer merges its inbox on its own next cycle.

    The write lock covers only the steps that touch the working tree
    (commit, merge, tombstones, index). ``git fetch``/``git push`` only move
    refs and objects and run unlocked — holding the lock across network calls
    blocked every note writer for up to GIT_TIMEOUT per unreachable peer
    (POSIX flock has no timeout).
    """
    omi_dir = Path(omi_dir).expanduser()
    store = OmiStore(omi_dir)
    report = SyncReport()
    with store.write_lock():
        report.committed = _commit_locked(omi_dir, f"omind: local changes on {node_id}")

        # Merge anything peers pushed into our inbox since last time.
        for ref in _inbox_refs(omi_dir, node_id):
            error = _merge_ref(omi_dir, ref)
            if error:
                log(f"inbox {ref}: {error}")

    for name in sorted(peers(omi_dir)):
        if only is not None and name not in only:
            continue
        ps = PeerSync(name=name)
        report.peers.append(ps)
        try:
            git(omi_dir, "fetch", name)
            ps.fetched = True
        except MeshError as exc:
            ps.error = _first_line(str(exc)) or "unreachable"
            log(f"peer {name}: unreachable, skipped")
            continue
        ref = f"refs/remotes/{name}/main"
        with store.write_lock():
            # A writer may have saved between locks; commit so the merge
            # never sees (or clobbers) uncommitted local changes.
            _commit_locked(omi_dir, f"omind: local changes on {node_id}")
            if git(omi_dir, "rev-parse", "--verify", ref, check=False).returncode == 0:
                error = _merge_ref(omi_dir, ref)
                if error:
                    ps.error = error
                    log(f"peer {name}: {error}")
                    continue
            ps.merged = True

            # Regenerate what merges may have touched, then publish.
            _apply_tombstones(omi_dir, store)
            store.update_index_locked()
            _commit_locked(omi_dir, f"omind: post-merge regeneration on {node_id}")
        push = git(omi_dir, "push", name, f"HEAD:refs/omind/{node_id}", check=False)
        ps.pushed = push.returncode == 0
        if not ps.pushed:
            ps.error = f"push: {_first_line(push.stderr or push.stdout)}"
            log(f"peer {name}: {ps.error}")

    with store.write_lock():
        # Even with no peers reachable, leave generated files consistent.
        _apply_tombstones(omi_dir, store)
        store.update_index_locked()
        _commit_locked(omi_dir, f"omind: post-merge regeneration on {node_id}")

        report.conflicts = conflict_scan(omi_dir)
    _write_sync_state(omi_dir, report)
    for note_name in report.conflicts:
        log(f"conflict markers in {note_name} (tagged; resolve and save)")
    return report


# -- clone / purge -----------------------------------------------------------------


def clone(url: str, omi_dir: Path, log: Logger = print) -> NodeConfig:
    """Seed a fresh node from a peer: git clone, then mesh init.

    The driver config lives in .git/config, which never travels — every clone
    re-runs init to wire it (and to mint this machine's own node identity).
    The seed remote stays configured as the first peer, named ``origin``.
    """
    omi_dir = Path(omi_dir).expanduser()
    if omi_dir.exists() and any(omi_dir.iterdir()):
        raise MeshError(f"refusing to clone into non-empty {omi_dir}")
    run_command(
        ["git", "clone", url, str(omi_dir)], error=MeshError, check=True, timeout=GIT_TIMEOUT
    )
    log(f"cloned {url}")
    return mesh_init(omi_dir, log=log)


def purge(omi_dir: Path, name: str, node_id: str, log: Logger = print) -> None:
    """Hard-delete a note everywhere: tombstone + unlink + commit.

    The tombstone line (merge=union) reaches every node; each applies it on
    its next sync. The default delete is disable — this is the rare exception.
    """
    omi_dir = Path(omi_dir).expanduser()
    store = OmiStore(omi_dir)
    target = store.safe_name(name)  # validates: no traversal, .md appended
    with store.write_lock():
        tomb = omi_dir / TOMBSTONES_FILENAME
        existing = tomb.read_text(encoding="utf-8").splitlines() if tomb.is_file() else []
        if target.name not in existing:
            # Atomic: a torn tombstone file would un-purge every prior purge
            # mesh-wide once the truncation merged out to the peers.
            _atomic_write(tomb, "\n".join([*existing, target.name]) + "\n")
        if target.is_file():
            target.unlink()
        store.update_index_locked()
        _commit_locked(omi_dir, f"omind: purge {target.name} from {node_id}")
    log(f"purged {target.name} (tombstoned for every node)")


# -- daemon ---------------------------------------------------------------------


def _should_sync(
    now: float,
    last_sync: float,
    signal_mtime: float | None,
    cfg: NodeConfig,
) -> bool:
    """The daemon's trigger decision (pure, unit-tested).

    Sync when the interval elapsed, or when a write signal is newer than the
    last sync AND has sat for the debounce window (so a burst of writes from
    one agent turn batches into a single commit+sync).
    """
    if now - last_sync >= cfg.interval_seconds:
        return True
    if signal_mtime is None or signal_mtime <= last_sync:
        return False
    return now - signal_mtime >= cfg.debounce_seconds


def run_daemon(
    omi_dir: Path,
    cfg: NodeConfig,
    log: Logger = print,
    *,
    _max_tick_seconds: float | None = None,
) -> int:
    """Replicate continuously: interval sync + on-write debounced sync.

    Runs until SIGTERM/SIGINT (clean exit 0). ``_max_tick_seconds`` bounds the
    loop for tests only.
    """
    from omind.paths import sync_signal_path

    omi_dir = Path(omi_dir).expanduser()
    signal_file = sync_signal_path(omi_dir)
    stop = {"flag": False}

    def _terminate(_signum: int, _frame: object) -> None:
        stop["flag"] = True

    # SIGTERM only exists as a signal on POSIX; Windows gets Ctrl+C/SIGINT.
    import signal as signal_mod

    if hasattr(signal_mod, "SIGTERM"):
        signal_mod.signal(signal_mod.SIGTERM, _terminate)

    log(f"omind mesh daemon: {omi_dir} as {cfg.node_id} (interval {cfg.interval_seconds}s)")
    last_sync = 0.0  # epoch start -> first tick syncs immediately
    started = time.time()
    try:
        while not stop["flag"]:
            now = time.time()
            if _max_tick_seconds is not None and now - started >= _max_tick_seconds:
                break
            signal_mtime: float | None
            try:
                signal_mtime = signal_file.stat().st_mtime
            except OSError:
                signal_mtime = None
            if _should_sync(now, last_sync, signal_mtime, cfg):
                try:
                    report = sync(omi_dir, cfg.node_id, log=log)
                    if not report.ok:
                        log("sync finished with peer errors (see above)")
                except MeshError as exc:
                    log(f"sync failed: {_first_line(str(exc))}")
                last_sync = time.time()
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    log("omind mesh daemon: stopped")
    return 0


# -- service install -----------------------------------------------------------------

MESH_SERVICE_UNIT = "omind-mesh.service"
MESH_LAUNCHD_LABEL = "net.thenetwerk.omind-mesh"


def install_service(vault: Path, folder: str, log: Logger = print) -> None:
    """Install + start the replication daemon as a user-level service.

    systemd user unit on Linux (Restart=on-failure — a crashed daemon comes
    back; a SIGTERM'd one stays stopped), launchd agent on macOS. Windows:
    prints the schtasks one-liner instead (no auto-install in 2.0).
    """
    omi_dir = (vault / folder).expanduser()
    if load_node_config(omi_dir) is None:
        raise MeshError(f"not a mesh node yet — run `omind mesh init` first ({omi_dir})")
    omind_exe = shutil.which("omind") or "omind"
    # Quoted like the hook command: systemd ExecStart and schtasks both
    # word-split an unquoted folder name containing a space.
    daemon_cmd = f'{omind_exe} mesh daemon --vault "{vault}" --folder "{folder}"'

    if sys.platform == "linux":
        from omind.backup import systemd_user_dir

        unit_dir = systemd_user_dir()
        unit_dir.mkdir(parents=True, exist_ok=True)
        unit = (
            "[Unit]\n"
            "Description=omind mesh replication daemon\n"
            "\n"
            "[Service]\n"
            "Type=simple\n"
            f"ExecStart={daemon_cmd}\n"
            "Restart=on-failure\n"
            "RestartSec=30\n"
            "\n"
            "[Install]\n"
            "WantedBy=default.target\n"
        )
        (unit_dir / MESH_SERVICE_UNIT).write_text(unit, encoding="utf-8")
        log(f"wrote {unit_dir / MESH_SERVICE_UNIT}")
        run_command(["systemctl", "--user", "daemon-reload"], error=MeshError)
        run_command(["systemctl", "--user", "enable", "--now", MESH_SERVICE_UNIT], error=MeshError)
        log(f"enabled {MESH_SERVICE_UNIT}")
        return

    if sys.platform == "darwin":
        agents = Path.home() / "Library" / "LaunchAgents"
        agents.mkdir(parents=True, exist_ok=True)
        plist_path = agents / f"{MESH_LAUNCHD_LABEL}.plist"
        args = [omind_exe, "mesh", "daemon", "--vault", str(vault), "--folder", folder]
        # XML-escape: a vault path containing & or < would yield an invalid plist.
        args_xml = "\n".join(f"      <string>{xml_escape(a)}</string>" for a in args)
        plist = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
            '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
            '<plist version="1.0">\n'
            "  <dict>\n"
            "    <key>Label</key>\n"
            f"    <string>{MESH_LAUNCHD_LABEL}</string>\n"
            "    <key>ProgramArguments</key>\n"
            "    <array>\n"
            f"{args_xml}\n"
            "    </array>\n"
            "    <key>KeepAlive</key>\n"
            "    <true/>\n"
            "    <key>RunAtLoad</key>\n"
            "    <true/>\n"
            "  </dict>\n"
            "</plist>\n"
        )
        plist_path.write_text(plist, encoding="utf-8")
        log(f"wrote {plist_path}")
        uid = os.getuid()
        run_command(
            ["launchctl", "bootstrap", f"gui/{uid}", str(plist_path)], error=MeshError, check=False
        )
        log(f"loaded {MESH_LAUNCHD_LABEL}")
        return

    log("Windows: auto-install is not supported in 2.0; run the daemon at logon with:")
    log(f'  schtasks /Create /SC ONLOGON /TN omind-mesh /TR "{daemon_cmd}"')


# -- doctor ---------------------------------------------------------------------


def diagnose_mesh(config: Any) -> list[Any]:
    """The mesh doctor checks (pure read except `git config --get` calls).

    ``config`` is a :class:`omind.provision.SetupConfig`; imported lazily to
    keep provision -> mesh a one-way dependency at module level.
    """
    from omind.provision import CheckResult

    results: list[CheckResult] = []
    omi = Path(config.omi_dir).expanduser()
    if not (omi / ".git").is_dir():
        return [
            CheckResult(
                "mesh",
                "warn",
                f"{omi} is not a mesh node — replication is off "
                "(run `omind mesh init`, or ignore if single-machine is intended)",
            )
        ]

    cfg = load_node_config(omi)
    if cfg is None:
        results.append(
            CheckResult(
                "mesh_identity",
                "fail",
                "git repo present but no node identity — run `omind mesh init`",
            )
        )
    else:
        results.append(CheckResult("mesh_identity", "ok", f"mesh node {cfg.node_id}"))

    try:
        driver = git(omi, "config", "--get", "merge.omi.driver", check=False)
        ours = git(omi, "config", "--get", "merge.ours.driver", check=False)
    except MeshError as exc:
        return [*results, CheckResult("mesh_driver", "fail", f"git unavailable: {exc}")]
    if driver.returncode != 0 or ours.returncode != 0:
        results.append(
            CheckResult(
                "mesh_driver",
                "fail",
                "merge driver not configured — run `omind mesh init` "
                "(merges would conflict instead of field-merging)",
            )
        )
    else:
        results.append(CheckResult("mesh_driver", "ok", "omi + ours merge drivers configured"))

    attrs = omi / ".gitattributes"
    if attrs.is_file() and "merge=omi" in attrs.read_text(encoding="utf-8"):
        results.append(CheckResult("mesh_attributes", "ok", ".gitattributes routes notes to omi"))
    else:
        results.append(
            CheckResult(
                "mesh_attributes", "warn", ".gitattributes missing — run `omind mesh init`"
            )
        )

    if os.name != "nt" and (omi.stat().st_mode & 0o077):
        results.append(
            CheckResult(
                "mesh_permissions",
                "warn",
                f"{omi} is group/world accessible — another local user could read "
                "the memory history; run `omind mesh init` to tighten to 0700",
            )
        )

    peer_map = peers(omi)
    if not peer_map:
        results.append(
            CheckResult(
                "mesh_peers", "warn", "no peers configured — `omind mesh add-peer` to replicate"
            )
        )
    for name in sorted(peer_map):
        ref = f"refs/remotes/{name}/main"
        if git(omi, "rev-parse", "--verify", ref, check=False).returncode != 0:
            results.append(
                CheckResult(f"mesh_peer:{name}", "warn", f"peer {name}: never fetched")
            )
            continue
        counts = git(omi, "rev-list", "--left-right", "--count", f"HEAD...{ref}").stdout.split()
        ahead, behind = (counts + ["0", "0"])[:2]
        results.append(
            CheckResult(
                f"mesh_peer:{name}",
                "ok",
                f"peer {name}: {ahead} ahead / {behind} behind (as of last fetch)",
            )
        )

    state = read_sync_state(omi)
    interval = cfg.interval_seconds if cfg else 300
    if state is None:
        results.append(
            CheckResult("mesh_sync", "warn", "never synced — `omind mesh sync` or install-service")
        )
    else:
        age: float | None
        try:
            then = datetime.fromisoformat(str(state.get("last_sync", "")))
            age = (datetime.now(timezone.utc) - then).total_seconds()
        except ValueError:
            age = None
        if age is not None and age <= 2 * interval:
            results.append(CheckResult("mesh_sync", "ok", f"last sync {int(age)}s ago"))
        elif age is not None:
            results.append(
                CheckResult(
                    "mesh_sync",
                    "warn",
                    f"last sync {int(age // 60)}m ago (> 2x the {interval}s interval) — "
                    "is the daemon running?",
                )
            )
        else:
            results.append(CheckResult("mesh_sync", "warn", "sync state unreadable"))

    conflicted = conflict_scan(omi)
    if conflicted:
        results.append(
            CheckResult(
                "mesh_conflicts",
                "warn",
                f"conflict markers in: {', '.join(conflicted)} — resolve and save",
            )
        )
    else:
        results.append(CheckResult("mesh_conflicts", "ok", "no unresolved conflict markers"))

    disabled = len(
        [s for s in OmiStore(omi).list_notes(include_disabled=True) if s.disabled]
    )
    if disabled:
        results.append(
            CheckResult("mesh_archived", "ok", f"{disabled} archived note(s) (restorable)")
        )
    return results
