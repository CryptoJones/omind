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
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from omind.backup import config_dir
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

        _write_if_changed(omi_dir / ".gitattributes", GITATTRIBUTES, log)
        _write_if_changed(omi_dir / ".gitignore", GITIGNORE, log)

        status = git(omi_dir, "status", "--porcelain").stdout
        if status.strip():
            git(omi_dir, "add", "-A")
            git(omi_dir, "commit", "-m", f"omind mesh: snapshot from {cfg.node_id}")
            log("committed working tree")
        else:
            log("working tree clean")
    return cfg
