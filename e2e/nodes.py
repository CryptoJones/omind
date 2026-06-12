# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Aaron K. Clark
"""On-node setup steps shared by every scenario.

Everything a scenario does to a fresh node funnels through here: install the
locally built wheel (never a published release), install the `claude` stub,
and wire two nodes as ssh mesh peers.
"""

from __future__ import annotations

import shlex
from pathlib import Path

from e2e.providers import NodeHandle

VAULT = "/root/vault"
OMI_DIR = f"{VAULT}/OMI"

#: Minimal stand-in for the Claude Code CLI: just enough `mcp add/get/remove/
#: list` for `omind setup`/`omind doctor` to exercise their real wiring on a
#: headless VM. Registrations land in ~/.claude.json under mcpServers — the
#: same file the real CLI maintains and omind's doctor reads.
CLAUDE_STUB = r'''#!/usr/bin/env python3
import json, pathlib, sys

CFG = pathlib.Path.home() / ".claude.json"

def load():
    if CFG.is_file():
        return json.loads(CFG.read_text())
    return {}

def save(data):
    CFG.write_text(json.dumps(data, indent=2) + "\n")

def main(argv):
    if not argv or argv[0] != "mcp":
        return 0  # any non-mcp invocation: succeed quietly
    args = [a for a in argv[1:] if a not in ("-s", "user", "--")]
    cmd, rest = args[0], args[1:]
    data = load()
    servers = data.setdefault("mcpServers", {})
    if cmd == "add":
        name, command, *cmd_args = rest
        servers[name] = {"command": command, "args": cmd_args}
        save(data)
    elif cmd == "remove":
        if rest[0] not in servers:
            print(f"No MCP server found with name: {rest[0]}", file=sys.stderr)
            return 1
        del servers[rest[0]]
        save(data)
    elif cmd == "get":
        if rest[0] not in servers:
            print(f"No MCP server found with name: {rest[0]}", file=sys.stderr)
            return 1
        print(json.dumps(servers[rest[0]]))
    elif cmd == "list":
        for name in servers:
            print(name)
    return 0

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
'''


def install_omind(node: NodeHandle, wheel: Path) -> None:
    """Install the locally built wheel via uv, plus the claude stub and git identity."""
    node.run(
        "command -v curl >/dev/null || "
        "(apt-get update -qq && apt-get install -y -qq curl git python3)",
        timeout=600,
    )
    node.run("command -v uv >/dev/null || curl -LsSf https://astral.sh/uv/install.sh | sh",
             timeout=600)
    node.put(wheel, f"/tmp/{wheel.name}")
    node.run(f"~/.local/bin/uv tool install --force /tmp/{wheel.name}", timeout=600)
    node.run("ln -sf ~/.local/bin/omind /usr/local/bin/omind")
    stub = shlex.quote(CLAUDE_STUB)
    node.run(f"printf %s {stub} > /usr/local/bin/claude && chmod +x /usr/local/bin/claude")
    # mesh_init sets per-repo identity, but a global one keeps stray git calls honest
    node.run('git config --global user.name e2e && git config --global user.email e2e@omind.test')
    version = node.run("omind --version").stdout.strip()
    assert version.startswith("omind "), version


def setup_vault(node: NodeHandle, *, mesh: bool = True) -> None:
    """`omind setup` against the stub claude; doctor must come back clean."""
    flag = "" if mesh else "--no-mesh"
    node.run(f"omind setup --vault {VAULT} {flag}".strip(), timeout=300)
    node.run(f"omind doctor --vault {VAULT}", timeout=300)


def interconnect(nodes: list[NodeHandle]) -> None:
    """Give every node ssh access to every other (the mesh transport).

    Each node receives the run's private key and a Host alias per peer, so
    mesh URLs are plain ``ssh://<peer-name>/...`` regardless of provider port
    mappings.
    """
    for node in nodes:
        node.run("mkdir -p ~/.ssh && chmod 700 ~/.ssh")
        node.put(node.key_path, "/root/.ssh/e2e_key")
        node.run("chmod 600 /root/.ssh/e2e_key")
        config_lines = []
        for peer in nodes:
            if peer.name == node.name:
                continue
            config_lines += [
                f"Host {peer.name}",
                f"  HostName {peer.peer_host}",
                f"  Port {peer.peer_port}",
                "  User root",
                "  IdentityFile /root/.ssh/e2e_key",
                "  StrictHostKeyChecking no",
                "  UserKnownHostsFile /dev/null",
                "  LogLevel ERROR",
            ]
        config = shlex.quote("\n".join(config_lines) + "\n")
        node.run(f"printf %s {config} > /root/.ssh/config")


def add_peers_full_mesh(nodes: list[NodeHandle]) -> None:
    """Register every node as a peer of every other, by Host alias."""
    for node in nodes:
        for peer in nodes:
            if peer.name == node.name:
                continue
            node.run(
                f"omind mesh add-peer {peer.name} ssh://{peer.name}{OMI_DIR} "
                f"--vault {VAULT}"
            )


def write_note(node: NodeHandle, title: str, details: str) -> None:
    node.run(
        f"omind note --title {shlex.quote(title)} --details {shlex.quote(details)} "
        f"--vault {VAULT}"
    )


def sync(node: NodeHandle) -> None:
    node.run(f"omind mesh sync --vault {VAULT}", timeout=600)


def note_digests(node: NodeHandle) -> dict[str, str]:
    """filename -> sha256 of every top-level note, excluding generated files.

    index.md is merge=ours (regenerated locally, allowed to differ); the
    convergence assertion is over the actual memories.
    """
    out = node.run(
        f"cd {OMI_DIR} && for f in *.md; do "
        '[ "$f" = index.md ] && continue; '
        'sha256sum "$f"; done'
    ).stdout
    digests: dict[str, str] = {}
    for line in out.splitlines():
        digest, _, name = line.strip().partition("  ")
        if name:
            digests[name] = digest
    return digests
