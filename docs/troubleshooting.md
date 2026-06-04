# Troubleshooting

## The MCP server "times out", and `obsidian-mcp` processes pile up

### Symptoms

- Obsidian/OMI MCP tool calls from Claude Code appear to **time out** or hang
  for many minutes, ending in `MCP error -32001: ... user-cancel`.
- Over time, **orphaned `obsidian-mcp` processes accumulate** — one (or more)
  per Claude Code session that ever touched the server. `ps` shows them
  reparented to init (PPID 1):

  ```
  node .../obsidian-mcp /path/to/Obsidian Vault/OMI   # PPID=1, never exits
  ```

### What is *not* wrong

The `obsidian-mcp` server itself is healthy. Spawned fresh it boots in ~0.6s,
answers `initialize`, `tools/list`, and tool calls instantly, and keeps a clean
JSON-RPC stream (all its logging goes to stderr). A long-lived connection that
sits idle and then receives file-watcher events still responds correctly. So
the package, the vault, and the `.obsidian/app.json` config are not the problem.

### Root cause of the orphaned processes

Two issues compound:

1. **`obsidian-mcp` never exits when its stdin closes.** Its chokidar file
   watcher keeps the Node event loop alive, so closing the client pipe is not
   enough to stop it — it waits for a signal. (It *does* die cleanly on
   `SIGTERM`.)

2. **The `npx -y obsidian-mcp` wrapper chain swallows the signal.** `omind
   setup` registers the server as:

   ```
   npx -y obsidian-mcp <vault>/OMI
   ```

   which at runtime is a tree of `npx → npm exec → sh -c → node`. When Claude
   Code exits it terminates the process it spawned (the top `npx`/`npm`
   process), but `npm` does not forward the signal to the `node` grandchild.
   The `node` process is orphaned and — per (1) — never exits on its own.

### Perceived "timeouts"

Separately from the leak, the long pending tool calls ending in
`-32001 ... user-cancel` after 900–1100s are tool calls that sat **waiting to
be dispatched** until they were cancelled — most consistent with an unanswered
permission prompt (the `mcp__obsidian__*` tools are not on Claude Code's
allowlist, so each call prompts) and/or a stale connection to one of the leaked
processes above. The server is not the bottleneck.

### Fix

Register the server as a **direct `node` invocation** (no `npx` wrapper, so
Claude Code's signal lands on `node` itself) **plus a tiny preload that exits
the process on stdin EOF** (so it stops even if the client only closes the pipe
instead of signalling). Also install `obsidian-mcp` to a **stable path** — the
`npx` cache directory (`~/.npm/_npx/<hash>/`) can be garbage-collected.

1. Install to a stable location (free, local, no cost):

   ```bash
   mkdir -p ~/.claude/mcp-servers/obsidian
   cd ~/.claude/mcp-servers/obsidian
   npm install obsidian-mcp@1.0.6 --no-audit --no-fund
   ```

2. Create the EOF-guard preload at
   `~/.claude/mcp-servers/obsidian-exit-on-eof.js`:

   ```js
   // Guarantee the server exits when its stdin (the MCP client pipe) closes.
   // obsidian-mcp's chokidar file watcher otherwise keeps the event loop alive,
   // so the server orphans when Claude Code exits.
   const die = () => process.exit(0);
   process.stdin.on("end", die);
   process.stdin.on("close", die);
   ```

3. Re-register the server with the direct command:

   ```bash
   claude mcp remove obsidian -s user
   claude mcp add -s user obsidian -- \
     node --require ~/.claude/mcp-servers/obsidian-exit-on-eof.js \
     ~/.claude/mcp-servers/obsidian/node_modules/obsidian-mcp/build/main.js \
     "$HOME/Documents/Obsidian Vault/OMI"
   ```

4. Clean up any already-orphaned processes (one-time):

   ```bash
   # inspect first, then terminate by PID
   ps -eo pid,ppid,cmd | grep '[o]bsidian-mcp'
   kill -TERM <pids>
   ```

### Verification

After the change, a fresh spawn:

- responds to `initialize` and `list-available-vaults` in ~0.1s, and
- **exits with code 0 the moment stdin closes** — confirmed by spawning the
  exact registered command, closing stdin, and observing the process terminate
  on its own. No orphan is left behind.

### Implications for `omind`

This is what `omind setup` provisions, so the fix belongs in the tool itself:

- `provision.py::Provisioner.register_mcp` currently emits
  `npx -y obsidian-mcp <target>` (around lines 164–173). It should instead
  provision a stable install + the direct `node --require <preload> <entry>`
  command described above.
- `provision.py::diagnose` / `omind doctor` could add a check that the
  registered command is the direct-`node` form (not `npx`) and warn if it finds
  the leak-prone wrapper.
- Optional: document that read-only OMI tools (`list-available-vaults`,
  `read-note`, `search-vault`) can be added to Claude Code's permission
  allowlist to avoid per-call prompts; leave write/delete tools behind prompts.
