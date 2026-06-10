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
vault. `obsidian-mcp` validates the folder by reading `.obsidian/app.json` at
startup, so the folder must carry a minimal Obsidian config:

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

## 2. The MCP server install + stdin-EOF guard

Two non-obvious choices here, both learned the hard way (see
[troubleshooting](troubleshooting.md)):

- **Install to a stable npm prefix, not the npx cache.** `npx -y obsidian-mcp`
  runs from `~/.npm/_npx/<hash>/`, which npm garbage-collects out from under a
  registered server.
- **Preload a stdin-EOF guard.** obsidian-mcp's file watcher keeps the Node
  event loop alive, so without the guard the server orphans every time Claude
  Code exits.

```bash
PREFIX="$HOME/.claude/mcp-servers/obsidian"
mkdir -p "$PREFIX"
npm install --prefix "$PREFIX" obsidian-mcp@1.0.6 --no-audit --no-fund

cat > "$HOME/.claude/mcp-servers/obsidian-exit-on-eof.js" <<'JS'
// Managed by omind. Exit obsidian-mcp when its stdin (the MCP client pipe)
// closes; the chokidar file watcher otherwise keeps the Node event loop alive
// and the process orphans when Claude Code exits.
const die = () => process.exit(0);
process.stdin.on("end", die);
process.stdin.on("close", die);
JS
```

## 3. Register the MCP server (user scope)

Register a **direct `node` command** — not `npx` — so Claude Code's
terminating signal reaches Node instead of being swallowed by the npx/npm
wrapper chain:

```bash
claude mcp add -s user obsidian -- node \
  --require "$HOME/.claude/mcp-servers/obsidian-exit-on-eof.js" \
  "$HOME/.claude/mcp-servers/obsidian/node_modules/obsidian-mcp/build/main.js" \
  "$HOME/Documents/Obsidian Vault/OMI"
```

The last argument is the OMI folder the server exposes.

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
the user-scope MCP registration (and that it's the direct-`node` form), the
EOF guard, and all three hooks. Restart Claude Code afterward so it loads the
new tools and hooks.

## Undo

```bash
claude mcp remove obsidian -s user
```

Then delete the three `omind hook` entries from `~/.claude/settings.json` and
remove `~/.claude/mcp-servers/` if nothing else uses it. Your notes are never
touched by setup or teardown.
