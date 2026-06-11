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
import socket
import subprocess
import sys
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from omind.backup import config_dir
from omind.paths import sync_state_path
from omind.proc import run_command
from omind.store import OmiStore

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
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


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
    """The configured peer remotes: name -> fetch URL."""
    out = git(omi_dir, "remote", "-v").stdout
    found: dict[str, str] = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[2] == "(fetch)":
            found[parts[0]] = parts[1]
    return found


def add_peer(omi_dir: Path, name: str, url: str) -> None:
    """Register a peer (an ordinary git remote — the only membership state)."""
    if not re.fullmatch(r"[A-Za-z0-9._-]+", name):
        raise MeshError(f"invalid peer name: {name!r}")
    git(omi_dir, "remote", "add", name, url)


def remove_peer(omi_dir: Path, name: str) -> None:
    git(omi_dir, "remote", "remove", name)


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


def _commit_locked(omi_dir: Path, node_id: str, message: str) -> bool:
    """Stage + commit everything. Caller MUST hold the store write lock."""
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
        return _commit_locked(omi_dir, node_id, f"omind: local changes on {node_id}")


def _first_line(text: str) -> str:
    """The most diagnostic line of a git message (CONFLICT/error/fatal first)."""
    lines = [ln for ln in text.strip().splitlines() if ln.strip()]
    for ln in lines:
        if ln.startswith(("CONFLICT", "error:", "fatal:")):
            return ln
    return lines[0] if lines else ""


def _merge_ref(omi_dir: Path, ref: str) -> str:
    """Merge one ref; '' on success, else a one-line error (merge aborted)."""
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
    """
    omi_dir = Path(omi_dir).expanduser()
    store = OmiStore(omi_dir)
    report = SyncReport()
    with store.write_lock():
        report.committed = _commit_locked(omi_dir, node_id, f"omind: local changes on {node_id}")

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
            _commit_locked(omi_dir, node_id, f"omind: post-merge regeneration on {node_id}")
            push = git(omi_dir, "push", name, f"HEAD:refs/omind/{node_id}", check=False)
            ps.pushed = push.returncode == 0
            if not ps.pushed:
                ps.error = f"push: {_first_line(push.stderr or push.stdout)}"
                log(f"peer {name}: {ps.error}")

        # Even with no peers reachable, leave generated files consistent.
        _apply_tombstones(omi_dir, store)
        store.update_index_locked()
        _commit_locked(omi_dir, node_id, f"omind: post-merge regeneration on {node_id}")

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
            tomb.write_text("\n".join([*existing, target.name]) + "\n", encoding="utf-8")
        if target.is_file():
            target.unlink()
        store.update_index_locked()
        _commit_locked(omi_dir, node_id, f"omind: purge {target.name} from {node_id}")
    log(f"purged {target.name} (tombstoned for every node)")
