# Troubleshooting

## Where omind logs

omind has no central log file; each piece logs where it runs. When something
misbehaves, look in this order:

1. **`omind doctor`** — run it first. It checks the whole wiring (tools, MCP
   registration, hooks, backup health) and points at the relevant log when a
   check warns.
2. **Foreground commands** (`setup`, `backup run`, `export`, …) print
   everything to stdout/stderr — there is no hidden file copy.
3. **The unattended backup timer** logs to the systemd user journal:
   `journalctl --user -u omind-backup.service`. Three consecutive failures
   also write a `BACKUP FAILING` note into the vault itself, so the problem
   surfaces in session priming.
4. **The auto-memory hooks** never block or fail the agent, so their errors
   are swallowed by design — but every swallowed error leaves a one-line
   breadcrumb in **`~/.local/state/omind/hook-failures.log`**
   (`$XDG_STATE_HOME/omind/hook-failures.log`). If the session journal
   stops growing, read that file; `omind doctor` warns when it has entries
   from the last 7 days. The log restarts past 256 KiB, so it never grows
   unbounded. Delete it to clear the doctor warning once the cause is fixed.
5. **The web UI** (`omind serve`) logs uvicorn request/error output to the
   terminal it runs in.

## The MCP server "times out", and `obsidian-mcp` processes pile up

> **Historical (1.x).** omind 2.0 retired `obsidian-mcp` entirely — the MCP
> server is now omind's own `omind node`, which exits cleanly on stdin EOF by
> construction (covered by a regression test). This section is kept for
> archaeology and for anyone still on 1.x.

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

This is what `omind setup` provisioned in 1.x, and the fix shipped in 2.0:

- `provision.py::Provisioner.register_mcp` now registers omind's own
  `omind node --vault … --folder …` server directly — no npx, no Node.js, no
  third-party MCP. `setup` also removes the retired `obsidian` registration.
- `omind doctor` warns when the registered command is not the expected
  `omind node` form, so a leak-prone leftover wrapper is surfaced.
- Optional: document that read-only OMI tools (`list-available-vaults`,
  `read-note`, `search-vault`) can be added to Claude Code's permission
  allowlist to avoid per-call prompts; leave write/delete tools behind prompts.

## `omind setup` on a machine without `jq` locks the agent out of every tool

### Symptoms

Immediately after `omind setup` on a fresh machine, the *next* tool call the
agent makes fails — and so does every one after it:

```
PreToolUse:Bash hook error: [~/.claude/hooks/omi-guard.sh]:
  omi-guard: jq not found — cannot evaluate this action. Install jq (omind setup/doctor check for it).
  omi-guard: BLOCKING this Bash command (fail-closed: hard-rules could not be checked).
```

The guard is registered as a `PreToolUse` `"*"` matcher, so once it trips it
blocks **all** tools — `Bash`, `Read`, everything — not just Bash.

> **Fixed (#107).** The guard no longer fails closed on a missing `jq`. Update
> omind (`omind self-update` / reinstall) and re-run `omind setup` to deploy the
> fixed hook; on current versions a jq-less host is no longer wedged. The rest of
> this section explains the old behavior and how to recover a machine still
> running the pre-fix hook.

### Root cause (pre-fix)

The OMI-compliance guard (`omi-guard.sh`) parses the hook event with `jq`. The
pre-fix hook **failed closed** when `jq` was absent: if it could not read the
command it could not enforce the hard-rules, so it refused the action. The trap
was that `omind setup` did **not** install `jq` (and, contrary to an earlier
version of this note, did not actually *check* for it either — `jq` was never in
`REQUIRED_TOOLS`), and on a clean box `jq` is usually missing. The result was a
bootstrap deadlock: the only way to satisfy the guard was to install `jq`, but
installing `jq` requires a `Bash` call, which the guard blocked.

> Note this is the opposite of the *Bash-only* guards `git-fresh-base.sh` and
> `omi-guard-hermes.sh`, which `exit 0` (fail **open**) when `jq` is missing.
> Only the `"*"` compliance guard failed closed, which is why it could wedge the
> whole session.

### The fix (current behavior)

When `jq` is absent the hook now routes the raw event through `omind guard
adapter` (the pure-Python core, which parses the event itself and applies the
**same** hard-blocks + per-turn gate). Enforcement is preserved — the host is no
longer wedged — and `jq` becomes a *performance* optimization for the fast path,
not a hard dependency. `omind doctor` now reports a **warning** (not a failure)
when `jq` is missing. Only if `jq` **and** a working `omind` core are both absent
does the hook fall back to the conservative last resort: fail open for non-Bash
tools, fail closed for Bash.

### Recovery (a machine still running the pre-fix hook)

The guard is read live from `~/.claude/settings.json` on every tool call (it is
active the moment `setup` writes it — no restart needed), so editing that file
takes effect immediately. Use an editor/tool that is **not** routed through the
blocked Bash hook to break the loop:

1. In `~/.claude/settings.json`, temporarily remove the `PreToolUse` block whose
   matcher is `"*"` and whose command is `…/omi-guard.sh` (leave the `Bash`
   matcher block alone). This lifts the fail-closed gate.
2. Either install `jq` (`sudo apt-get install -y jq`; `brew install jq` on macOS;
   `dnf install jq` on Fedora) **or** simply update omind and re-run `omind
   setup` to deploy the fixed hook (which no longer needs `jq`).
3. Restore the `"*"` / `omi-guard.sh` block you removed in step 1 (or let
   `omind setup` rewrite it).
4. Run `omind doctor` to confirm the guard is wired and the policy loads.

Because the fixed hook self-heals on a jq-less host, after step 2's `omind setup`
no manual `settings.json` edit is needed on subsequent fresh machines.
