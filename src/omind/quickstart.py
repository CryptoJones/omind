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

from omind import seeds
from omind.provision import (
    OBSIDIAN_MCP_VERSION,
    Provisioner,
    SetupConfig,
    claude_settings_path,
    eof_guard_path,
    server_install_dir,
)


def _sh(lines: list[str]) -> str:
    return "```bash\n" + "\n".join(lines) + "\n```"


def _heredoc(path: str, tag: str, content: str) -> list[str]:
    return [f"cat > {path} <<'{tag}'", content.rstrip("\n"), tag]


def build_quickstart(config: SetupConfig) -> str:
    """The full quickstart text for one vault/folder/server-name combination."""
    prov = Provisioner(config=config, log=lambda _msg: None)
    omi = config.omi_dir
    omi_q = f'"{omi}"'
    obsidian_dir_q = f'"{omi / ".obsidian"}"'
    guard = eof_guard_path()
    install_dir = server_install_dir()
    settings = claude_settings_path()

    scaffold_lines = [f"mkdir -p {obsidian_dir_q}"]
    for filename, content in seeds.OBSIDIAN_CONFIG_FILES.items():
        scaffold_lines += _heredoc(f'"{omi / ".obsidian" / filename}"', "JSON", content)

    guard_lines = [
        f'mkdir -p "{install_dir}"',
        f'npm install --prefix "{install_dir}" obsidian-mcp@{OBSIDIAN_MCP_VERSION}'
        " --no-audit --no-fund",
    ] + _heredoc(f'"{guard}"', "JS", seeds.EOF_GUARD_JS)

    register_cmd = " ".join(
        ["claude", "mcp", "add", "-s", "user", config.server_name, "--"]
        + [part if " " not in part else f'"{part}"' for part in prov._server_command(str(omi))]
    )

    hooks_json = json.dumps({"hooks": prov._omind_hook_entries()}, indent=2)

    return f"""\
omind quickstart — manual wiring for {omi}

Everything below is exactly what `omind setup` would do for you. Apply the
steps you want by hand; each is independent and safe to re-run. Prefer the
automated path? Just run:

    omind setup --vault "{config.vault}" --folder {config.folder}

[1/4] Scaffold the memory folder
obsidian-mcp refuses to start without <folder>/.obsidian/app.json, so create
the folder and its minimal Obsidian config (skip any file you already have):

{_sh(scaffold_lines)}

Optionally seed `Memory Template.md` and `index.md` in {omi_q} —
`omind setup` writes starter versions, or copy them from the repo's
`src/omind/seeds.py`.

[2/4] Install the MCP server and the stdin-EOF guard
The server is installed to a stable npm prefix (NOT the npx cache, which npm
garbage-collects out from under registered servers). The tiny `--require`
preload makes the server exit when Claude Code closes its stdin pipe —
without it the file watcher keeps Node alive and the process orphans:

{_sh(guard_lines)}

[3/4] Register the MCP server with Claude Code (user scope)

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

Undo: `claude mcp remove {config.server_name} -s user`, delete the three
"omind hook" entries from {settings}, and remove
"{install_dir.parent}" if nothing else uses it. Your notes in
{omi_q} are never touched by any of this.
"""
