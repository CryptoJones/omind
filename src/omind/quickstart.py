# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Aaron K. Clark
"""Render the manual-wiring quickstart that `omind setup` automates.

`omind quickstart` prints copy-paste shell commands and JSON, personalized to
the caller's vault/folder/server-name, for people who want to apply every
change to their own config files by hand. The snippets are built from the same
:mod:`omind.provision` helpers `setup` executes, so the manual path can never
drift from the automated one.

Pure rendering — this module never touches the filesystem or runs commands.
"""

from __future__ import annotations

import json

import yaml

from omind import paths, seeds
from omind.agents import (
    AGY_HOOK_NAME,
    AgentProvisioner,
    AgyProvisioner,
    AmazonQProvisioner,
    ClaudeDesktopProvisioner,
    GooseProvisioner,
    HermesProvisioner,
    KiroProvisioner,
    McpOnlyProvisioner,
    OpenClawProvisioner,
    VsCodeProvisioner,
    agy_agents_path,
    agy_hooks_path,
    agy_mcp_config_path,
    agy_skill_dir,
    goose_config_path,
    goose_hints_path,
    hermes_config_path,
    openclaw_config_path,
)
from omind.provision import (
    Provisioner,
    SetupConfig,
    claude_settings_path,
)


def _sh(lines: list[str]) -> str:
    return "```bash\n" + "\n".join(lines) + "\n```"


def _heredoc(path: str, tag: str, content: str) -> list[str]:
    return [f"cat > {path} <<'{tag}'", content.rstrip("\n"), tag]


def _scaffold_and_mesh_blocks(config: SetupConfig) -> tuple[str, str]:
    """The agent-independent steps: OMI scaffold + mesh initialization."""
    omi = config.omi_dir
    obsidian_dir_q = f'"{omi / ".obsidian"}"'

    scaffold_lines = [f"mkdir -p {obsidian_dir_q}"]
    for filename, content in seeds.OBSIDIAN_CONFIG_FILES.items():
        scaffold_lines += _heredoc(f'"{omi / ".obsidian" / filename}"', "JSON", content)

    mesh_lines = [
        f'omind mesh init --vault "{config.vault}" --folder {config.folder}',
        "# then, to replicate with another machine:",
        f'#   omind mesh add-peer <name> <ssh-url> --vault "{config.vault}" '
        f"--folder {config.folder}",
        f'#   omind mesh install-service --vault "{config.vault}" --folder {config.folder}',
    ]

    return _sh(scaffold_lines), _sh(mesh_lines)


def _build_agent_quickstart(config: SetupConfig) -> str:
    """Manual steps for the non-Claude agents (Hermes Agent, OpenClaw)."""
    prov: AgentProvisioner
    if config.agent == "hermes":
        prov = HermesProvisioner(config=config, log=lambda _msg: None)
        agent_config = hermes_config_path()
        snippet = yaml.safe_dump(
            {"mcp_servers": {config.server_name: prov.desired_server_entry()}},
            sort_keys=False,
        )
        snippet_block = f"```yaml\n{snippet.rstrip()}\n```"
        merge_hint = f"MERGE into the existing YAML in {agent_config} (top-level key)"
    else:
        prov = OpenClawProvisioner(config=config, log=lambda _msg: None)
        agent_config = openclaw_config_path()
        snippet = json.dumps(
            {"mcp": {"servers": {config.server_name: prov.desired_server_entry()}}},
            indent=2,
        )
        snippet_block = f"```json\n{snippet}\n```"
        merge_hint = f"MERGE into the existing JSON in {agent_config} (don't replace other keys)"

    omi = config.omi_dir
    scaffold_block, mesh_block = _scaffold_and_mesh_blocks(config)
    skill_path = prov.skill_dir() / paths.AGENT_SKILL_FILENAME
    skill_content = seeds.AGENT_SKILL_TEMPLATE.format(
        vault=config.vault, folder=config.folder, omi_dir=omi
    )
    skill_block = _sh(
        [f'mkdir -p "{prov.skill_dir()}"'] + _heredoc(f'"{skill_path}"', "SKILL", skill_content)
    )

    return f"""\
omind quickstart — manual {prov.AGENT_LABEL} wiring for {omi}

Everything below is exactly what `omind setup --agent {config.agent}` would do
for you. Apply the steps you want by hand; each is independent and safe to
re-run. Prefer the automated path? Just run:

    omind setup --agent {config.agent} --vault "{config.vault}" --folder {config.folder}

[1/4] Scaffold the memory folder
Create the folder and a minimal Obsidian config so it opens directly as a
vault (skip any file you already have):

{scaffold_block}

[2/4] Initialize the mesh node
Makes the folder a git working tree with omind's field-level merge driver,
mints this machine's node identity, and locks the folder to owner-only:

{mesh_block}

[3/4] Register the MCP server with {prov.AGENT_LABEL}
The server is omind's own node server (`omind node`) — no Node.js, npm, or
third-party MCP package involved. {merge_hint}:

{snippet_block}

[4/4] Install the memory skill
Teaches {prov.AGENT_LABEL} to read memory through the MCP tools and to write
it through `omind note` — the single-writer path that keeps concurrent agents
from corrupting the folder:

{skill_block}

Verify the wiring (pure inspection, changes nothing):

    omind doctor --agent {config.agent} --vault "{config.vault}" --folder {config.folder}

Then restart {prov.AGENT_LABEL} to load the tools and skill.

Undo: delete the '{config.server_name}' entry from {agent_config} and remove
"{skill_path.parent}". Your notes in "{omi}" are never touched by any of this.
"""


#: MCP-registration-only targets, keyed by their ``--agent`` value.
MCP_ONLY_PROVISIONERS: dict[str, type[McpOnlyProvisioner]] = {
    "claude-desktop": ClaudeDesktopProvisioner,
    "kiro": KiroProvisioner,
    "vscode": VsCodeProvisioner,
    "q": AmazonQProvisioner,
}


def _build_mcp_only_quickstart(config: SetupConfig) -> str:
    """Manual steps for an MCP-registration-only target (Claude Desktop, Kiro,
    VS Code, Amazon Q): scaffold + mesh + a single config-block to merge."""
    prov = MCP_ONLY_PROVISIONERS[config.agent](config=config, log=lambda _msg: None)
    agent_config = prov.config_path()
    snippet = json.dumps(
        {prov.BLOCK_KEY: {config.server_name: prov.desired_server_entry()}},
        indent=2,
    )
    snippet_block = f"```json\n{snippet}\n```"
    omi = config.omi_dir
    scaffold_block, mesh_block = _scaffold_and_mesh_blocks(config)

    return f"""\
omind quickstart — manual {prov.AGENT_LABEL} wiring for {omi}

Everything below is exactly what `omind setup --agent {config.agent}` would do
for you. Apply the steps you want by hand; each is independent and safe to
re-run. Prefer the automated path? Just run:

    omind setup --agent {config.agent} --vault "{config.vault}" --folder {config.folder}

[1/3] Scaffold the memory folder
Create the folder and a minimal Obsidian config so it opens directly as a
vault (skip any file you already have):

{scaffold_block}

[2/3] Initialize the mesh node
Makes the folder a git working tree with omind's field-level merge driver,
mints this machine's node identity, and locks the folder to owner-only:

{mesh_block}

[3/3] Register the MCP server with {prov.AGENT_LABEL}
The server is omind's own node server (`omind node`) — no Node.js, npm, or
third-party MCP package involved. MERGE into the existing JSON in
{agent_config} (create the file if absent; don't replace other keys):

{snippet_block}

Verify the wiring (pure inspection, changes nothing):

    omind doctor --agent {config.agent} --vault "{config.vault}" --folder {config.folder}

Then restart {prov.AGENT_LABEL} to load the tools.

Undo: delete the '{config.server_name}' entry from {agent_config}. Your notes
in "{omi}" are never touched by any of this.
"""


def _build_goose_quickstart(config: SetupConfig) -> str:
    """Manual steps for Block's goose: scaffold + mesh + the `omi` stdio
    extension to merge into config.yaml + the OMI block for `.goosehints`."""
    prov = GooseProvisioner(config=config, log=lambda _msg: None)
    omi = config.omi_dir
    scaffold_block, mesh_block = _scaffold_and_mesh_blocks(config)

    ext_yaml = yaml.safe_dump(
        {"extensions": {config.server_name: prov.desired_server_entry()}},
        sort_keys=False,
    )
    ext_block = f"```yaml\n{ext_yaml.rstrip()}\n```"
    hints_block = f"```markdown\n{prov.bootstrap_content().rstrip()}\n```"

    return f"""\
omind quickstart — manual {prov.AGENT_LABEL} wiring for {omi}

Everything below is exactly what `omind setup --agent {config.agent}` would do
for you. goose has no blocking hooks, so this is memory + priming (no guard).
Apply the steps you want by hand; each is independent and safe to re-run.
Prefer the automated path? Just run:

    omind setup --agent {config.agent} --vault "{config.vault}" --folder {config.folder}

[1/4] Scaffold the memory folder
Create the folder and a minimal Obsidian config so it opens directly as a
vault (skip any file you already have):

{scaffold_block}

[2/4] Initialize the mesh node
Makes the folder a git working tree with omind's field-level merge driver,
mints this machine's node identity, and locks the folder to owner-only:

{mesh_block}

[3/4] Register the omi MCP server as a goose extension
goose reads MCP servers from the `extensions` map in {goose_config_path()}
(its own `cmd`/`args` stdio shape). MERGE this into that file under
`extensions` (create the file if absent; don't replace other extensions):

{ext_block}

[4/4] Prime goose to read OMI first
goose injects its global hints file into every session's system prompt. Add
this block to {goose_hints_path()} (append it; keep any hints you already
wrote). The markers let a later `omind setup` manage only this block:

{hints_block}

Verify the wiring (pure inspection, changes nothing):

    omind doctor --agent {config.agent} --vault "{config.vault}" --folder {config.folder}

Then start a new goose session to load the tools and priming.

Undo: delete the '{config.server_name}' entry from {goose_config_path()} and
remove the marked block from {goose_hints_path()}. Your notes in "{omi}" are
never touched by any of this.
"""


def _build_agy_quickstart(config: SetupConfig) -> str:
    """Manual steps for Antigravity CLI (agy): scaffold + mesh + MCP config + hooks +
    skill + bootstrap."""
    prov = AgyProvisioner(config=config, log=lambda _msg: None)
    omi = config.omi_dir
    scaffold_block, mesh_block = _scaffold_and_mesh_blocks(config)
    mcp_config = agy_mcp_config_path()
    hooks_config = agy_hooks_path()
    agents_path = agy_agents_path()
    skill_dir = agy_skill_dir()

    mcp_json = json.dumps(
        {"mcpServers": {config.server_name: prov.desired_server_entry()}},
        indent=2,
    )
    mcp_block = f"```json\n{mcp_json}\n```"

    hooks_json = json.dumps(
        {AGY_HOOK_NAME: prov.desired_hook_block()},
        indent=2,
    )
    hooks_block = f"```json\n{hooks_json}\n```"

    bootstrap_block = f"```markdown\n{prov.bootstrap_content().rstrip()}\n```"

    return f"""\
omind quickstart — manual {prov.AGENT_LABEL} wiring for {omi}

Everything below is exactly what `omind setup --agent {config.agent}` would do
for you. Apply the steps you want by hand; each is independent and safe to
re-run. Prefer the automated path? Just run:

    omind setup --agent {config.agent} --vault "{config.vault}" --folder {config.folder}

[1/5] Scaffold the memory folder
Create the folder and a minimal Obsidian config so it opens directly as a
vault (skip any file you already have):

{scaffold_block}

[2/5] Initialize the mesh node
Makes the folder a git working tree with omind's field-level merge driver,
mints this machine's node identity, and locks the folder to owner-only:

{mesh_block}

[3/5] Register the MCP server with {prov.AGENT_LABEL}
The server is omind's own node server (`omind node`) — no Node.js, npm, or
third-party MCP package involved. MERGE into {mcp_config} under `mcpServers`
(create the file if absent; don't replace other servers):

{mcp_block}

[4/5] Install OMI lifecycle hooks
Antigravity supports hard blocking and priming through lifecycle hooks. MERGE
this into {hooks_config} under `{AGY_HOOK_NAME}` (create the file if absent):

{hooks_block}

[5/5] Prime Antigravity to read OMI first
Add this block to {agents_path} (create the file or append to it). The markers
let a later `omind setup` manage only this block:

{bootstrap_block}

Install the packaged skill:
Run `omind setup --agent {config.agent}` or copy the packaged skill into:
    {skill_dir}

Verify the wiring (pure inspection, changes nothing):

    omind doctor --agent {config.agent} --vault "{config.vault}" --folder {config.folder}

Then restart {prov.AGENT_LABEL} to load the tools, hooks, and skill.

Undo: delete the '{config.server_name}' entry from {mcp_config}, remove
`{AGY_HOOK_NAME}` from {hooks_config}, and remove the marked block from
{agents_path}. Your notes in "{omi}" are never touched by any of this.
"""


def build_quickstart(config: SetupConfig) -> str:
    """The full quickstart text for one vault/folder/server-name/agent combination."""
    if config.agent in ("hermes", "openclaw"):
        return _build_agent_quickstart(config)
    if config.agent == "goose":
        return _build_goose_quickstart(config)
    if config.agent in ("agy", "antigravity"):
        return _build_agy_quickstart(config)
    if config.agent in MCP_ONLY_PROVISIONERS:
        return _build_mcp_only_quickstart(config)
    prov = Provisioner(config=config, log=lambda _msg: None)
    omi = config.omi_dir
    omi_q = f'"{omi}"'
    settings = claude_settings_path()
    scaffold_block, mesh_block = _scaffold_and_mesh_blocks(config)

    register_cmd = " ".join(
        ["claude", "mcp", "add", "-s", "user", config.server_name, "--"]
        + [part if " " not in part else f'"{part}"' for part in prov._server_command()]
    )

    hooks_json = json.dumps({"hooks": prov._omind_hook_entries()}, indent=2)

    return f"""\
omind quickstart — manual wiring for {omi}

Everything below is exactly what `omind setup` would do for you. Apply the
steps you want by hand; each is independent and safe to re-run. Prefer the
automated path? Just run:

    omind setup --vault "{config.vault}" --folder {config.folder}

[1/4] Scaffold the memory folder
Create the folder and a minimal Obsidian config so it opens directly as a
vault (skip any file you already have):

{scaffold_block}

Optionally seed `Memory Template.md` and `index.md` in {omi_q} —
`omind setup` writes starter versions, or copy them from the repo's
`src/omind/seeds.py`.

[2/4] Initialize the mesh node
Makes the folder a git working tree with omind's field-level merge driver,
mints this machine's node identity, and locks the folder to owner-only:

{mesh_block}

[3/4] Register the MCP server with Claude Code (user scope)
The server is omind's own node server (`omind node`) — no Node.js, npm, or
third-party MCP package involved:

{_sh([register_cmd])}

[4/4] Auto-memory hooks — merge into {settings}
These journal every Claude Code action into a per-day note in your OMI folder
and inject your memory index as context at session start. MERGE the entries
into your existing "hooks" object — don't replace user-authored hooks. omind
identifies its own entries by the literal substring "omind hook", so a later
`omind setup` will manage only these and leave yours alone:

```json
{hooks_json}
```

Verify the wiring (pure inspection, changes nothing):

    omind doctor --vault "{config.vault}" --folder {config.folder}

Then restart Claude Code to load the tools and hooks.

Undo: `claude mcp remove {config.server_name} -s user` and delete the three
"omind hook" entries from {settings}. Your notes in
{omi_q} are never touched by any of this.
"""
