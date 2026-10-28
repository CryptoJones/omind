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

import contextlib
import functools
import json
import os
import re
import secrets
import shlex
import socket
import stat
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape as xml_escape

from omind import filelock
from omind.backup import config_dir
from omind.clock import Rev
from omind.paths import sync_state_path
from omind.proc import run_command
from omind.store import OmiStore, _atomic_write, parse_note

Logger = Callable[[str], None]


class MeshError(Exception):
    """Raised when a mesh operation (git, config, init) fails."""


GIT_TIMEOUT = 120.0
"""Seconds per git invocation; LAN fetches and local plumbing are fast."""

#: Merge-driver routing for the OMI repo. Notes get the field-level omi
#: driver; generated files are never merged (ours + regenerate); journals and
#: tombstones are append-only line sets, so git's union driver is exact.
#:
#: ``* -text`` is load-bearing, not tidiness. Git for Windows ships
#: ``core.autocrlf=true`` at the *system* level, so without this line a Windows
#: peer would get CRLF working-tree checkouts of every note. omind writes LF on
#: every platform (``store._atomic_write``), so the vault would then disagree
#: with itself: ``note_version`` digests — raw-byte hashes — would differ across
#: the fleet for identical notes, and every index would rebuild on that peer.
#: ``-text`` disables git's translation repo-wide and overrides ``autocrlf``.
#: Deleting it breaks nothing on Linux or macOS, which is what makes it easy to
#: delete.
GITATTRIBUTES = """\
* -text
*.md merge=omi
index.md merge=ours
Journal/*.md merge=union
.omi-tombstones merge=union
"""

#: Never replicate the lock, torn temp files, or Obsidian's volatile per-machine
#: app state. ``.obsidian/workspace*.json`` (and the caches) are rewritten
#: constantly and DIFFERENTLY on every machine; with ``git add -A`` committing
#: them and no merge driver, two nodes with Obsidian open produce a genuine text
#: conflict that aborts the WHOLE peer merge every cycle — silently stopping
#: replication. Ignoring them is what keeps the mesh converging.
GITIGNORE = """\
.omi.lock
.tmp-*
*.scratch.md
.obsidian/workspace.json
.obsidian/workspace-mobile.json
.obsidian/workspace
.obsidian/cache
.obsidian/.DS_Store
.trash/
"""

_NODE_ID_RE = re.compile(r"[^A-Za-z0-9._-]+")


def git(
    omi_dir: Path,
    *args: str,
    check: bool = True,
    timeout: float = GIT_TIMEOUT,
) -> subprocess.CompletedProcess[str]:
    """Run git against the OMI repo, output captured, failures as MeshError.

    Network calls run with ``GIT_TERMINAL_PROMPT=0`` and ssh ``BatchMode`` so an
    unknown host key / credential prompt fails fast instead of hanging until the
    timeout on every sync tick (the churning-seed-IP scenario).
    """
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    env.setdefault("GIT_SSH_COMMAND", "ssh -oBatchMode=yes")
    return run_command(
        ["git", "-C", str(omi_dir), *args],
        error=MeshError,
        check=check,
        timeout=timeout,
        env=env,
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
    """Save this folder's node config under the node-config lock.

    Two concurrent `omind setup` runs (two agents, fleet automation) otherwise
    read-modify-write the shared file and one folder's entry — its node_id —
    is lost, minting a duplicate Lamport identity on the next setup
    (2026-08-27 review). The flock lives on a sibling ``.lock`` file because
    the data file is replaced atomically."""
    path = node_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with filelock.exclusive(path.with_suffix(".json.lock")):
        _save_node_config_unlocked(omi_dir, cfg)


def _save_node_config_unlocked(omi_dir: Path, cfg: NodeConfig) -> None:
    path = node_config_path()
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

    lock_path = node_config_path().with_suffix(".json.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    # The load→mint→save cycle is serialized: two concurrent setups minting
    # node ids would give two folders the same Lamport identity (merge order
    # corrupts) (2026-08-27 review).
    with filelock.exclusive(lock_path):
        cfg = load_node_config(omi_dir)
        if cfg is None:
            cfg = NodeConfig(node_id=new_node_id())
            _save_node_config_unlocked(omi_dir, cfg)
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


#: How long a purge tombstone keeps deleting a re-created note before it expires.
#: Generous so a node offline for a long stretch still applies the deletion, but
#: finite so re-creating a note with a previously-purged filename (#127) is not
#: silently deleted forever. Expiry is by the per-line timestamp, so every node
#: converges on the same decision under the union merge.
TOMBSTONE_TTL_DAYS = 90


def _parse_tombstone(line: str) -> tuple[datetime | None, str, str]:
    """``(timestamp|None, filename, rev)`` for a tombstone line.

    New lines are ``<iso-8601>\\t<filename>[\\t<rev>]``; a legacy bare
    ``<filename>`` (no timestamp) has no expiry — it can't be safely dated under
    the ``merge=union`` driver (a peer would re-add the old line), so it stays
    permanent as before. The optional third field is the Lamport Rev the purge
    observed: when a live note's rev DOMINATES it, the note was edited after the
    purge decision, and the edit wins over the delete instead of being destroyed
    silently (2026-08-27 review).
    """
    line = line.strip()
    if not line:
        return None, "", ""
    parts = line.split("\t")
    if len(parts) == 1:
        return None, line, ""
    ts: datetime | None
    try:
        ts = datetime.fromisoformat(parts[0].strip())
    except ValueError:
        ts = None
    name = parts[1].strip()
    rev = parts[2].strip() if len(parts) > 2 else ""
    return ts, name, rev


def _as_utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _live_rev_newer(target: Path, purged_rev: str) -> bool:
    """True when the live note's Rev dominates the rev captured at purge time —
    the note was edited after the purge decision, so the edit wins."""
    try:
        live = parse_note(target.read_text(encoding="utf-8")).rev
        a, b = Rev.parse(live), Rev.parse(purged_rev)
    except Exception:
        return False
    if a is None or b is None:
        return False
    return a.sort_key() > b.sort_key()


def _apply_tombstones(omi_dir: Path, store: OmiStore, now: datetime | None = None) -> list[str]:
    """Unlink any note a live purge tombstone names, and GC expired tombstones.

    Returns the names it REFUSED to delete: when a tombstone captured the
    purge-time Rev and the live note's Rev is newer, an edit raced the purge
    and the edit survives (reported as a purge-edit conflict) instead of being
    destroyed vault-wide with no trace (2026-08-27 review).
    Caller holds the write lock."""
    tomb = omi_dir / TOMBSTONES_FILENAME
    conflicts: list[str] = []
    if not tomb.is_file():
        return conflicts
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=TOMBSTONE_TTL_DAYS)
    kept: list[str] = []
    changed = False
    for raw in tomb.read_text(encoding="utf-8").splitlines():
        ts, name, rev = _parse_tombstone(raw)
        if not name:
            changed = changed or bool(raw.strip())
            continue
        # An expired dated tombstone no longer deletes (so a re-created note
        # survives) and is dropped to bound the file. Under union-merge a lagging
        # peer may re-add the line, but it stays expired, so this converges.
        if ts is not None and _as_utc(ts) < cutoff:
            changed = True
            continue
        kept.append(raw.rstrip("\n"))
        target = omi_dir / name
        # Tombstones name plain note files only; never follow odd paths.
        if target.parent == omi_dir and target.is_file() and target.suffix == ".md":
            # An edit newer than the purge beats the delete: keep the note and
            # report it, instead of silently destroying the other agent's work.
            if rev and _live_rev_newer(target, rev):
                conflicts.append(name)
                continue
            target.unlink()
    if changed:
        _atomic_write(tomb, ("\n".join(kept) + "\n") if kept else "")
    return conflicts


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


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _update_sync_state(omi_dir: Path, mutate: Callable[[dict[str, Any]], None]) -> None:
    """Read-modify-write the machine-local sync state under its sibling lock.

    The state lives in the STATE dir, never the vault (invariant 1). The daemon,
    a manual ``omind mesh sync`` and ``omind maintain --sync`` can all record at
    once, so the read-modify-write holds ``filelock.exclusive`` on a SIBLING
    ``.lock`` path (the data file is replaced atomically; a flock on it would
    protect a dead inode). Advisory and fail-open: never fail a sync over it.
    """
    path = sync_state_path(omi_dir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with filelock.exclusive(path.with_name(path.name + ".lock")):
            state = read_sync_state(omi_dir) or {}
            mutate(state)
            _atomic_write(path, json.dumps(state, indent=2) + "\n")
    except Exception:  # advisory state, fail-open: a sync never fails over it
        return


def _failure_count(state: dict[str, Any]) -> int:
    try:
        return max(0, int(state.get("consecutive_failures") or 0))
    except (TypeError, ValueError):
        return 0


def _sync_succeeded(report: SyncReport) -> bool:
    """Whether a completed pass counts as a SUCCESSFUL sync for doctor.

    Replication happened when the pass committed locally and reached at least
    one peer (or there is no peer to reach). One laptop being offline must not
    mark a node unhealthy; every peer failing must.
    """
    return not report.peers or any(p.pushed for p in report.peers)


def _write_sync_state(omi_dir: Path, report: SyncReport) -> None:
    now = _now_iso()
    succeeded = _sync_succeeded(report)

    def mutate(state: dict[str, Any]) -> None:
        state.update(
            last_sync=now,
            last_attempt=now,
            peers=[asdict(p) for p in report.peers],
            conflicts=report.conflicts,
            ok=report.ok,
        )
        if succeeded:
            state["last_success"] = now
            state["consecutive_failures"] = 0
            state.pop("last_error", None)
        else:
            state["consecutive_failures"] = _failure_count(state) + 1
            errors = [f"{p.name}: {p.error}" for p in report.peers if p.error]
            state["last_error"] = errors[0] if errors else "no peer reached"

    _update_sync_state(omi_dir, mutate)


def _record_sync_failure(omi_dir: Path, message: str) -> None:
    """A pass that raised before finishing (the 2026-09 telesto failure mode:
    every tick died on a stale ``HEAD.lock`` and nothing durable said so)."""
    now = _now_iso()

    def mutate(state: dict[str, Any]) -> None:
        state["last_attempt"] = now
        state["ok"] = False
        state["consecutive_failures"] = _failure_count(state) + 1
        state["last_error"] = message or "sync failed"

    _update_sync_state(omi_dir, mutate)


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
    """One full replication pass (see :func:`_sync_pass`), recorded for doctor.

    A pass that raises is recorded as a failure in the state dir before the
    error propagates, so doctor can see repeated failures (#498).
    """
    omi_dir = Path(omi_dir).expanduser()
    try:
        return _sync_pass(omi_dir, node_id, only=only, log=log)
    except Exception as exc:
        # Any exception, not only MeshError: a corrupt tombstone file or an
        # index write error repeats every tick just the same (review, #501).
        _record_sync_failure(omi_dir, _first_line(str(exc)) or type(exc).__name__)
        raise


def _sync_pass(
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

    merged_peers: list[PeerSync] = []
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
        merged_peers.append(ps)

    # Regenerate + commit ONCE after all merges (also with no peers reachable,
    # to leave generated files consistent) — per-peer regeneration was a full
    # vault re-parse and a git status/add/commit round for every peer, under
    # the lock. Every push below then carries the regenerated state.
    with store.write_lock():
        purge_conflicts = _apply_tombstones(omi_dir, store)
        store.update_index_locked()
        _commit_locked(omi_dir, f"omind: post-merge regeneration on {node_id}")

        report.conflicts = conflict_scan(omi_dir)
        for name in purge_conflicts:
            if name not in report.conflicts:
                report.conflicts.append(name)
        if purge_conflicts:
            log(
                "purge-edit conflict: kept newer edits to "
                + ", ".join(sorted(purge_conflicts))
                + " (the tombstone stays armed for genuinely-stale copies)"
            )

    for ps in merged_peers:
        # A push can TIME OUT (a hung/blackholed peer); run_command raises on
        # TimeoutExpired even with check=False. Catch it per-peer so one slow peer
        # records its error and the loop still pushes the rest AND writes sync
        # state — previously the MeshError escaped, skipping every later peer and
        # leaving the state file stale.
        try:
            push = git(omi_dir, "push", ps.name, f"HEAD:refs/omind/{node_id}", check=False)
            ps.pushed = push.returncode == 0
            if not ps.pushed:
                ps.error = f"push: {_first_line(push.stderr or push.stdout)}"
        except MeshError as exc:
            ps.pushed = False
            ps.error = f"push: {_first_line(str(exc)) or 'timed out'}"
        if not ps.pushed:
            log(f"peer {ps.name}: {ps.error}")
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
        already = any(_parse_tombstone(line)[1] == target.name for line in existing)
        if not already:
            # Dated so the tombstone expires (TOMBSTONE_TTL_DAYS) and a note
            # re-created with this filename later is not silently deleted (#127).
            # The third field captures the Rev the purge observed, so a peer
            # applying the tombstone later can tell "the purged note" from "a
            # newer edit that raced the purge" — the edit wins (2026-08-27).
            rev = ""
            if target.is_file():
                with contextlib.suppress(Exception):
                    rev = store.read_fields(target.name).rev
            stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
            new_line = f"{stamp}\t{target.name}" + (f"\t{rev}" if rev else "")
            # Atomic: a torn tombstone file would un-purge every prior purge
            # mesh-wide once the truncation merged out to the peers.
            _atomic_write(tomb, "\n".join([*existing, new_line]) + "\n")
        if target.is_file():
            target.unlink()
        store.update_index_locked()
        _commit_locked(omi_dir, f"omind: purge {target.name} from {node_id}")
    log(f"purged {target.name} (tombstoned for every node)")


# -- stale git locks (#498) -------------------------------------------------------

#: A git ``*.lock`` younger than this belongs to an in-flight operation and is
#: never reported or touched. Git holds a ref or index lock for one command; even
#: a large merge finishes well inside GIT_TIMEOUT (2 min), so 10 min is generous.
STALE_LOCK_SECONDS = 600.0

#: Doctor warns when the last SUCCESSFUL sync is older than this many daemon
#: intervals on a node with peers (a seed is a peer) — the same 2x the attempt
#: check has always used.
STALE_SYNC_INTERVALS = 2

#: How many self-heal events the sync state keeps for doctor.
_MAX_LOCK_EVENTS = 5

#: Top-level lock files git leaves under ``.git/`` when it dies mid-command, by
#: kind. ``refs/**/*.lock`` is walked separately and is always a ref lock.
_TOP_LEVEL_LOCKS = {
    "index.lock": "index",
    "HEAD.lock": "ref",
    "packed-refs.lock": "ref",
    "config.lock": "config",
}

#: A git failure caused by an existing lock file (``Unable to create
#: '.../HEAD.lock': File exists``, ``cannot lock ref 'HEAD'``).
_LOCK_ERROR_RE = re.compile(r"\.lock\b|cannot lock ref|unable to create", re.IGNORECASE)


@dataclass(frozen=True)
class GitLock:
    """One git lock file under the vault's ``.git/``.

    ``git_running`` is tri-state: True (a live git process is using this repo),
    False (provably none), None (could not tell). Only False can make a lock
    stale: when in doubt, the lock is treated as in use.
    """

    path: Path
    age_seconds: float
    kind: str  # "ref" | "index" | "config"
    git_running: bool | None
    #: (st_dev, st_ino, st_mtime_ns) when judged — removal proves it is the same file.
    identity: tuple[int, int, int] = (0, 0, 0)

    @property
    def stale(self) -> bool:
        return self.age_seconds >= STALE_LOCK_SECONDS and self.git_running is False

    @property
    def healable(self) -> bool:
        """Safe to remove: a stale REF lock. ``index.lock`` can sit on staged
        content and ``config.lock`` on a half-written config, so neither is ever
        removed automatically."""
        return self.stale and self.kind == "ref"


def _is_git_process(name: str) -> bool:
    """``git`` itself or one of its helpers (``git-remote-https``, ``git.exe``)."""
    base = name.strip().replace("\\", "/").rsplit("/", 1)[-1].lower()
    base = base.removesuffix(".exe")
    return base == "git" or base.startswith("git-")


def _within(path: Path, repo: Path) -> bool:
    return path == repo or repo in path.parents


def _argv_targets_repo(argv: list[str], cwd: Path, repo: Path) -> bool:
    """Whether any argument (``-C OMI``, ``--git-dir=/v/OMI/.git``, an absolute
    path) resolves into the repo when read relative to the process's cwd."""
    for arg in argv[1:]:
        for candidate in (arg, arg.partition("=")[2]):
            if not candidate or len(candidate) > 4096:
                continue
            try:
                if _within((cwd / candidate).resolve(), repo):
                    return True
            except (OSError, ValueError, RuntimeError):
                continue
    return False


def _linux_git_running(proc_root: Path, repo: Path) -> bool | None:
    """Scan ``/proc`` for a git process whose cwd or argv points into *repo*.

    A git process we cannot inspect (another user's, gone mid-read in an odd
    way) makes the answer None — unknown — never False.
    """
    unknown = False
    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            comm = (entry / "comm").read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue  # exited between listdir and read
        if not _is_git_process(comm):
            continue
        try:
            cwd = Path(os.readlink(entry / "cwd"))
            raw = (entry / "cmdline").read_bytes()
        except OSError:
            unknown = True
            continue
        argv = [os.fsdecode(a) for a in raw.split(b"\0") if a]
        if _within(cwd, repo) or _argv_targets_repo(argv, cwd, repo):
            return True
    return None if unknown else False


def _process_names(cmd: list[str], parse: Callable[[str], str]) -> list[str] | None:
    """Every process name a listing command prints, or None when it failed."""
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=15, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if res.returncode != 0:
        return None
    names = [parse(line) for line in res.stdout.splitlines() if line.strip()]
    return names or None  # an empty process table is not a believable answer


def _windows_process_name(line: str) -> str:
    """Image name from one ``tasklist /FO CSV /NH`` row (``"git.exe","123",...``)."""
    return line.split(",", 1)[0].strip().strip('"')


def _git_process_running(omi_dir: Path) -> bool | None:
    """Whether a live git process is using the vault repo (None: can't tell).

    Linux reads ``/proc`` and attributes each git process to a repo by its cwd
    and argv. macOS/BSD (``ps``) and Windows (``tasklist``) cannot cheaply see
    another process's cwd, so there the answer is only ever False when NO git
    process is running at all; any git process anywhere answers None. That is
    the conservative choice: a lock is never called stale on a guess.
    """
    try:
        repo = Path(omi_dir).expanduser().resolve()
        proc = Path("/proc")
        if sys.platform.startswith("linux") and (proc / "self").exists():
            return _linux_git_running(proc, repo)
        if os.name == "nt":
            names = _process_names(["tasklist", "/FO", "CSV", "/NH"], _windows_process_name)
        else:
            names = _process_names(["ps", "-A", "-o", "comm="], str.strip)
        if names is None:
            return None
        return None if any(_is_git_process(n) for n in names) else False
    except Exception:
        return None


def find_git_locks(omi_dir: Path, *, now: float | None = None) -> list[GitLock]:
    """Every git lock file under the vault's ``.git/``, with its age.

    Covers ``index.lock``, ``HEAD.lock``, ``packed-refs.lock``, ``config.lock``
    and ``refs/**/*.lock``. The live-process check runs only when some lock is
    already old enough to be stale (it costs a process-table scan).
    """
    git_dir = Path(omi_dir).expanduser() / ".git"
    if not git_dir.is_dir():
        return []
    candidates: list[tuple[Path, str]] = [
        (git_dir / name, kind) for name, kind in _TOP_LEVEL_LOCKS.items()
    ]
    refs = git_dir / "refs"
    if refs.is_dir():
        for root, _dirs, files in os.walk(refs):  # followlinks=False
            candidates.extend((Path(root) / f, "ref") for f in files if f.endswith(".lock"))
    clock = time.time() if now is None else now
    found: list[tuple[Path, str, float, tuple[int, int, int]]] = []
    for path, kind in candidates:
        try:
            st = path.lstat()
        except OSError:
            continue  # absent, or released between the scan and the stat
        if not stat.S_ISREG(st.st_mode):
            continue  # never act on a symlink or directory named *.lock
        ident = (st.st_dev, st.st_ino, st.st_mtime_ns)
        found.append((path, kind, max(0.0, clock - st.st_mtime), ident))
    running: bool | None = None
    if any(age >= STALE_LOCK_SECONDS for _, _, age, _ in found):
        running = _git_process_running(git_dir.parent)
    return [
        GitLock(path, age, kind, running, ident) for path, kind, age, ident in sorted(found)
    ]


def _fmt_age(seconds: float) -> str:
    s = int(seconds)
    if s < 120:
        return f"{s}s"
    if s < 2 * 3600:
        return f"{s // 60}m"
    if s < 2 * 86400:
        return f"{s // 3600}h"
    return f"{s // 86400}d"


def _lock_label(omi_dir: Path, lock: GitLock) -> str:
    try:
        return lock.path.relative_to(Path(omi_dir).expanduser()).as_posix()
    except ValueError:
        return lock.path.as_posix()


def _record_lock_event(omi_dir: Path, event: dict[str, Any]) -> None:
    def mutate(state: dict[str, Any]) -> None:
        events = state.get("lock_events")
        kept = [e for e in events if isinstance(e, dict)] if isinstance(events, list) else []
        state["lock_events"] = [*kept, event][-_MAX_LOCK_EVENTS:]

    _update_sync_state(omi_dir, mutate)


def _remove_if_same(lock: GitLock) -> bool:
    """Remove *lock* only if the file at its path is still the one judged stale.

    A plain ``unlink(path)`` would race: git could release the old lock and
    create a fresh one at the same path between the re-check and the unlink.
    Instead the lock is atomically renamed aside (into ``.git/`` itself, where
    the name cannot read as a ref), and its identity — device, inode, mtime —
    compared with what was judged. A different file is put back with a
    no-clobber hard link; if even that is impossible it is left beside the
    path, never deleted. Returns whether the judged lock was removed.
    """
    git_dir = next(p for p in lock.path.parents if p.name == ".git")
    aside = git_dir / f"omind-stale-lock-{secrets.token_hex(6)}"
    os.replace(lock.path, aside)
    st = aside.lstat()
    if (st.st_dev, st.st_ino, st.st_mtime_ns) == lock.identity:
        aside.unlink()
        return True
    with contextlib.suppress(OSError):
        os.link(aside, lock.path)  # fails rather than clobber a newer lock
        aside.unlink()
    return False


def heal_stale_ref_locks(omi_dir: Path, log: Logger = print) -> list[GitLock]:
    """Remove provably stale REF locks, then check the repo with ``git fsck``.

    Runs under the store write lock (so no omind git operation can start
    meanwhile) and RE-CHECKS every lock under it: removal needs the lock to be
    older than STALE_LOCK_SECONDS, no live git process on the repo, and a ref
    lock — never ``index.lock`` or ``config.lock``. Records a ``mesh-stale-lock``
    event (stderr + the state dir, which doctor reads). Returns what it removed.
    """
    omi_dir = Path(omi_dir).expanduser()
    removed: list[GitLock] = []
    with OmiStore(omi_dir).write_lock():
        for lock in find_git_locks(omi_dir):
            if not lock.healable:
                continue
            try:
                if not _remove_if_same(lock):
                    log(
                        f"mesh-stale-lock: {_lock_label(omi_dir, lock)} changed while being "
                        "removed; left in place"
                    )
                    continue
            except FileNotFoundError:
                continue  # someone else cleared it
            except OSError as exc:
                log(f"mesh-stale-lock: could not remove {_lock_label(omi_dir, lock)}: {exc}")
                continue
            removed.append(lock)
        if not removed:
            return []
        try:
            fsck = git(omi_dir, "fsck", "--connectivity-only", check=False)
            fsck_result = (
                "clean"
                if fsck.returncode == 0
                else _first_line(fsck.stderr or fsck.stdout) or f"exit {fsck.returncode}"
            )
        except MeshError as exc:
            fsck_result = f"did not finish: {_first_line(str(exc))}"
    labels = [f"{_lock_label(omi_dir, lk)} ({_fmt_age(lk.age_seconds)} old)" for lk in removed]
    log(
        f"mesh-stale-lock: removed stale {', '.join(labels)}; "
        f"git fsck --connectivity-only: {fsck_result}"
    )
    _record_lock_event(
        omi_dir,
        {
            "event": "mesh-stale-lock",
            "at": _now_iso(),
            "removed": [_lock_label(omi_dir, lk) for lk in removed],
            "fsck": fsck_result,
        },
    )
    return removed


def _stale_lock_line(omi_dir: Path, stale: list[GitLock], message: str) -> str:
    """The ONE line a failed tick logs when a stale lock is the cause."""
    labels = ", ".join(
        f"{_lock_label(omi_dir, lk)} ({_fmt_age(lk.age_seconds)} old)" for lk in stale
    )
    noun = "lock" if len(stale) == 1 else "locks"
    tail = ""
    if any(not lk.healable for lk in stale):
        tail = " — index.lock/config.lock are never removed automatically; delete by hand"
    return f"sync failed: stale git {noun} {labels}, no git process running: {message}{tail}"


def _stale_locks(omi_dir: Path) -> list[GitLock]:
    """Fail-open detector for the daemon: any error answers 'none found'."""
    try:
        return [lk for lk in find_git_locks(omi_dir) if lk.stale]
    except Exception:
        return []


def _handle_lock_failure(omi_dir: Path, message: str, log: Logger) -> bool | None:
    """Diagnose a git failure that may be a stale lock; self-heal ref locks.

    Returns None when no stale lock explains the failure (the caller logs its
    usual line), else whether a stale ref lock was removed. Logs ONE line naming
    the stale lock(s), plus the ``mesh-stale-lock`` event when it heals.
    """
    stale = _stale_locks(omi_dir) if _LOCK_ERROR_RE.search(message) else []
    if not stale:
        return None
    log(_stale_lock_line(omi_dir, stale, message))
    if not any(lk.healable for lk in stale):
        return False
    try:
        return bool(heal_stale_ref_locks(omi_dir, log=log))
    except Exception as exc:
        log(f"mesh-stale-lock: self-heal skipped: {exc}")
        return False


def _sync_tick(omi_dir: Path, node_id: str, log: Logger) -> None:
    """One daemon sync, with stale-lock diagnosis and ref-lock self-heal (#498).

    A pass that dies on a stale lock is retried once, straight after the heal.
    A peer whose fetch hit a stale remote-tracking ref lock is healed for the
    next tick. Everything added here is fail-open: a detection or heal error
    falls back to the plain ``sync failed`` line and never stops the daemon.
    """
    try:
        report = sync(omi_dir, node_id, log=log)
    except MeshError as exc:
        message = _first_line(str(exc))
        healed = _handle_lock_failure(omi_dir, message, log)
        if healed is None:
            log(f"sync failed: {message}")
        if not healed:
            return
        try:
            report = sync(omi_dir, node_id, log=log)
        except MeshError as retry_exc:
            log(f"sync failed after removing the stale lock: {_first_line(str(retry_exc))}")
            return
    if not report.ok:
        log("sync finished with peer errors (see above)")
        lock_errors = [p.error for p in report.peers if _LOCK_ERROR_RE.search(p.error)]
        if lock_errors:
            _handle_lock_failure(omi_dir, lock_errors[0], log)


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

    # Under systemd/launchd stdout is a pipe, so Python block-buffers it and
    # nothing reaches the journal until the process exits: three days of
    # `sync failed` lines once surfaced with a single timestamp, at restart.
    # Flush every line so a failing sync is visible while it is failing.
    if log is print:
        log = functools.partial(print, flush=True)

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
                _sync_tick(omi_dir, cfg.node_id, log)
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
    # Imported lazily: provision imports mesh, so a module-level import cycles.
    from omind.provision import canonical_omind_argv, canonical_omind_cmd, double_quote

    # `python -m omind` on Windows, where Smart App Control blocks omind.exe (#380).
    omind_argv = canonical_omind_argv()
    # Quoted like the hook command: systemd ExecStart and schtasks both
    # word-split an unquoted path or folder name containing a space, and the
    # Windows interpreter path holds the username (`C:\Users\Jane Doe\...`).
    daemon_cmd = (
        f'{canonical_omind_cmd(double_quote)} mesh daemon --vault "{vault}" --folder "{folder}"'
    )

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
            # Belt and braces with run_daemon's line flush: the journal must
            # see each line as it is written, not in one burst at exit.
            "Environment=PYTHONUNBUFFERED=1\n"
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
        args = [*omind_argv, "mesh", "daemon", "--vault", str(vault), "--folder", folder]
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
            "    <key>EnvironmentVariables</key>\n"
            "    <dict>\n"
            "      <key>PYTHONUNBUFFERED</key>\n"
            "      <string>1</string>\n"
            "    </dict>\n"
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
    # Inside /TR "...", schtasks takes an embedded double quote as \".
    tr = daemon_cmd.replace('"', '\\"')
    log(f'  schtasks /Create /SC ONLOGON /TN omind-mesh /TR "{tr}"')


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

    results.extend(_diagnose_git_locks(omi))

    state = read_sync_state(omi)
    # Per-peer errors recorded by the last sync (fetch/merge/push failures). Doctor
    # previously reported every fetched peer "ok" no matter how divergent and never
    # read these, so a peer that conflict-aborts or is rejected every cycle looked
    # healthy while the fleet silently diverged.
    peer_errors: dict[str, str] = {}
    if state is not None:
        for entry in state.get("peers", []) or []:
            if isinstance(entry, dict) and entry.get("error"):
                peer_errors[str(entry.get("name") or "")] = str(entry.get("error"))

    peer_map = peers(omi)
    if not peer_map:
        results.append(
            CheckResult(
                "mesh_peers", "warn", "no peers configured — `omind mesh add-peer` to replicate"
            )
        )
    for name in sorted(peer_map):
        if name in peer_errors:
            results.append(
                CheckResult(
                    f"mesh_peer:{name}",
                    "fail",
                    f"peer {name}: last sync FAILED — {peer_errors[name]}",
                )
            )
            continue
        ref = f"refs/remotes/{name}/main"
        if git(omi, "rev-parse", "--verify", ref, check=False).returncode != 0:
            results.append(
                CheckResult(f"mesh_peer:{name}", "warn", f"peer {name}: never fetched")
            )
            continue
        counts = git(
            omi, "rev-list", "--left-right", "--count", f"HEAD...{ref}", check=False
        ).stdout.split()
        ahead, behind = (counts + ["0", "0"])[:2]
        results.append(
            CheckResult(
                f"mesh_peer:{name}",
                "ok",
                f"peer {name}: {ahead} ahead / {behind} behind (as of last fetch)",
            )
        )

    interval = cfg.interval_seconds if cfg else 300
    if state is None:
        results.append(
            CheckResult("mesh_sync", "warn", "never synced — `omind mesh sync` or install-service")
        )
    else:
        results.append(_diagnose_sync_age(state, interval, peered=bool(peer_map)))
        results.extend(_diagnose_lock_events(state))

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


def _parse_ts(value: object) -> datetime | None:
    try:
        return _as_utc(datetime.fromisoformat(str(value)))
    except (TypeError, ValueError):
        return None


def _diagnose_git_locks(omi: Path) -> list[Any]:
    """Doctor lines for git lock files under ``.git/`` (#498). Fail-open.

    A fresh lock is an in-flight git operation: no line. A stale one fails,
    with its path and age, because every sync fails until it is gone. An old
    lock that a live (or possibly live) git process may own only warns.
    """
    from omind.provision import CheckResult

    try:
        locks = find_git_locks(omi)
    except Exception:
        return []
    results: list[CheckResult] = []
    for lock in locks:
        if lock.age_seconds < STALE_LOCK_SECONDS:
            continue
        label = _lock_label(omi, lock)
        age = _fmt_age(lock.age_seconds)
        if lock.stale:
            fix = (
                "the mesh daemon removes it on its next failed sync, or delete it by hand"
                if lock.healable
                else "never removed automatically — check nothing is using the repo, then delete it"
            )
            results.append(
                CheckResult(
                    f"mesh_git_lock:{label}",
                    "fail",
                    f"stale git lock {label} ({age} old, no git process running) — "
                    f"every sync fails while it exists; {fix}",
                )
            )
        else:
            why = (
                "a git process is still running against the repo"
                if lock.git_running
                else "could not confirm that no git process is using it"
            )
            results.append(
                CheckResult(
                    f"mesh_git_lock:{label}", "warn", f"git lock {label} is {age} old; {why}"
                )
            )
    if not results:
        results.append(CheckResult("mesh_git_lock", "ok", "no stale git locks"))
    return results


def _diagnose_sync_age(state: dict[str, Any], interval: int, *, peered: bool) -> Any:
    """The sync-freshness line. With peers it judges the last SUCCESSFUL sync
    and names the consecutive failures (#498: five weeks of failed ticks once
    went unnoticed); without peers, the last completed pass as before."""
    from omind.provision import CheckResult

    limit = STALE_SYNC_INTERVALS * interval
    now = datetime.now(timezone.utc)
    if not peered:
        then = _parse_ts(state.get("last_sync"))
        if then is None:
            return CheckResult("mesh_sync", "warn", "sync state unreadable")
        age = (now - then).total_seconds()
        if age <= limit:
            return CheckResult("mesh_sync", "ok", f"last sync {int(age)}s ago")
        return CheckResult(
            "mesh_sync",
            "warn",
            f"last sync {int(age // 60)}m ago (> {STALE_SYNC_INTERVALS}x the {interval}s "
            "interval) — is the daemon running?",
        )

    if "last_success" in state:
        success = _parse_ts(state.get("last_success"))
    else:  # written before 10.3.7: a completed, ok pass was the only record
        success = _parse_ts(state.get("last_sync")) if state.get("ok") else None
    failures = _failure_count(state)
    detail = ""
    if failures:
        detail = f"; {failures} failed sync(s) in a row, last: {state.get('last_error') or '?'}"
    if success is None:
        return CheckResult("mesh_sync", "warn", f"no successful sync recorded yet{detail}")
    age = (now - success).total_seconds()
    if age <= limit:
        return CheckResult("mesh_sync", "ok", f"last successful sync {int(age)}s ago")
    return CheckResult(
        "mesh_sync",
        "warn",
        f"last successful sync {_fmt_age(age)} ago (> {STALE_SYNC_INTERVALS}x the "
        f"{interval}s interval)" + (detail or " — is the daemon running?"),
    )


def _diagnose_lock_events(state: dict[str, Any]) -> list[Any]:
    """The most recent ``mesh-stale-lock`` self-heal, if it happened in the last
    week: ok when fsck came back clean, warn when it did not."""
    from omind.provision import CheckResult

    events = state.get("lock_events")
    if not isinstance(events, list) or not events or not isinstance(events[-1], dict):
        return []
    last = events[-1]
    when = _parse_ts(last.get("at"))
    if when is None or datetime.now(timezone.utc) - when > timedelta(days=7):
        return []
    removed = ", ".join(str(r) for r in last.get("removed") or []) or "?"
    fsck = str(last.get("fsck") or "?")
    level = "ok" if fsck == "clean" else "warn"
    return [
        CheckResult(
            "mesh_stale_lock",
            level,
            f"mesh-stale-lock: self-healed {removed} at {when.isoformat(timespec='seconds')}; "
            f"git fsck --connectivity-only: {fsck}",
        )
    ]
