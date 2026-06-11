# Manual setup — wiring OMI memory into Claude Code by hand

`omind setup` automates everything on this page. If you'd rather control every
change to your own config files, this is the complete manual path. For the same
steps **personalized to your actual paths** (ready to copy-paste), run:

```bash
omind quickstart --vault "$HOME/Documents/Obsidian Vault"
```

The integration has four independent pieces. Apply the ones you want; each is
safe to redo.

## 1. The memory folder

OMI is just a folder of Markdown notes inside (or standing in for) an Obsidian
vault. A minimal `.obsidian/` config lets the folder open directly as a vault
in the Obsidian app (omind itself doesn't need it):

```bash
OMI="$HOME/Documents/Obsidian Vault/OMI"
mkdir -p "$OMI/.obsidian"

cat > "$OMI/.obsidian/app.json" <<'JSON'
{
  "livePreview": true,
  "newFileLocation": "root",
  "attachmentFolderPath": "./"
}
JSON

echo '{}' > "$OMI/.obsidian/appearance.json"
```

Also seed `core-plugins.json`, `Memory Template.md`, and `index.md` — starter
content for all of them lives in [`src/omind/seeds.py`](../src/omind/seeds.py),
or let `omind setup` write just these (it never clobbers existing files).

## 2. Initialize the mesh node

Makes the folder a git working tree with omind's field-level merge driver,
mints this machine's node identity (`~/.config/omind/node.json`), and locks
the folder to owner-only:

```bash
omind mesh init --vault "$HOME/Documents/Obsidian Vault" --folder OMI
```

Single-machine use is fine too — just skip this step (and pass `--no-mesh` to
`omind setup` if you use the automated path). To replicate with another
machine later: `omind mesh add-peer <name> <ssh-url>` and
`omind mesh install-service` (see [mesh-ops.md](mesh-ops.md)).

## 3. Register the MCP server (user scope)

The server is omind's own node server — no Node.js, npm, or third-party MCP
package involved. Use the absolute path to `omind` (the spawned environment
may lack `~/.local/bin` on `PATH`):

```bash
claude mcp add -s user omi -- "$(command -v omind)" node \
  --vault "$HOME/Documents/Obsidian Vault" --folder OMI
```

If you are migrating from omind 1.x, also remove the retired server:

```bash
claude mcp remove obsidian -s user
```

## 4. Auto-memory hooks

Three hooks in `~/.claude/settings.json` give the agent a deterministic memory
loop: every tool action is journaled to a per-day note (PostToolUse, Stop), and
your memory index is injected as context when a session starts (SessionStart).

**Merge** these entries into your existing `"hooks"` object — don't replace
hooks you've authored yourself. omind recognizes its own entries by the literal
substring `omind hook` in the command, so a later `omind setup` manages only
these three and leaves the rest of the file alone:

```json
{
  "hooks": {
    "PostToolUse": [
      {
        "matcher": "*",
        "hooks": [
          {
            "type": "command",
            "command": "omind hook PostToolUse --vault \"$HOME/Documents/Obsidian Vault\" --folder OMI"
          }
        ]
      }
    ],
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "omind hook Stop --vault \"$HOME/Documents/Obsidian Vault\" --folder OMI"
          }
        ]
      }
    ],
    "SessionStart": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "omind hook SessionStart --vault \"$HOME/Documents/Obsidian Vault\" --folder OMI"
          }
        ]
      }
    ]
  }
}
```

Two practical notes:

- Claude Code does **not** expand `$HOME` or `~` inside hook commands in all
  contexts — prefer the absolute path. `omind quickstart` prints these entries
  with your real paths, and uses the absolute path to the `omind` executable so
  the hooks fire even when the spawned shell lacks `~/.local/bin` on `PATH`.
- The hook handler always exits 0 and swallows its own errors by design; a
  memory glitch must never block the agent.

## Verify

```bash
omind doctor --vault "$HOME/Documents/Obsidian Vault"
```

`doctor` is pure inspection. It checks the tools on `PATH`, the folder layout,
the user-scope MCP registration (and that it's the `omind node` form), the
mesh health, and all three hooks. Restart Claude Code afterward so it loads
the new tools and hooks.

## Undo

```bash
claude mcp remove omi -s user
```

Then delete the three `omind hook` entries from `~/.claude/settings.json`.
Your notes are never touched by setup or teardown — and `mesh init` is
reversible by deleting the folder's `.git/` (your notes stay).
