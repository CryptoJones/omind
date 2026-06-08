# Lessons Learned

Operational lessons from real installs and debugging sessions. For the deep
dive on the MCP server timeout/orphan-process problem, see
[docs/troubleshooting.md](docs/troubleshooting.md); this file captures the
shorter, broader lessons.

---

## 1. The README install assumes a tool that may not be present

### Symptom
On a fresh machine, none of the three install paths in the README worked
out of the box:

- `uv` — not installed
- `pipx` — not installed
- `pip` — not installed (`python3 -m pip` → `No module named pip`)

…and the system Python was **3.9**, while `omind` requires **>=3.10**
(`requires-python = ">=3.10"` in `pyproject.toml`).

### Fix
Install `uv` first — it is self-contained **and manages its own Python**, so it
satisfies the `>=3.10` requirement without touching system Python:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"          # uv installs here
uv tool install git+https://github.com/CryptoJones/omind.git
```

`uv tool install` builds `omind` in its own isolated virtualenv and puts the
`omind` executable on `PATH` (`~/.local/bin/omind`).

### Lesson
Verify `~/.local/bin` is on `PATH` in new shells. The README could note that
`uv` is the most robust path precisely because it bootstraps a compatible
Python when the system one is too old.

---

## 2. `omind setup` is not idempotent against an already-registered server

### Symptom
Re-running setup when the `obsidian` MCP server already exists exits **1**:

```
error: command failed: claude mcp add -s user obsidian -- node ...
MCP server obsidian already exists in user config
```

The folder/seed/EOF-guard steps are idempotent ("exists, leaving as-is"), but
the final `claude mcp add` is not — `claude mcp add` refuses to overwrite an
existing entry, so setup fails even though nothing is actually wrong.

### Fix / workaround
The registration is harmless to redo. To force a clean re-register:

```bash
claude mcp remove obsidian -s user
omind setup --vault "$HOME/Documents/Obsidian Vault"
```

### Lesson
`provision.py::register_mcp` should check for an existing `obsidian` entry and
**skip or update** it (idempotent) instead of letting `claude mcp add` error.

### Root cause & fix (resolved 2026-06-08)
This was the **same bug as #3**: `claude_config_path()` pointed at the
nonexistent `~/.claude/.claude.json`, so `registered_server()` always returned
`None` and `register_mcp()` always re-ran `claude mcp add` into the "already
exists" error. Fixed by pointing at `~/.claude.json`. `omind setup` is now
genuinely idempotent (exit 0 on re-run: "MCP server 'obsidian' already points
at ...").

---

## 3. `omind doctor` reports a false negative for the registered server

### Symptom
`omind doctor` reports:

```
[✗] MCP server 'obsidian' not registered at user scope (run `omind setup`)
```

…while the server is, in fact, registered at user scope and connected. Ground
truth from the Claude CLI:

```
$ claude mcp get obsidian
obsidian:
  Scope: User config (available in all your projects)
  Status: ✔ Connected
  Command: node
  Args: --require .../obsidian-exit-on-eof.js .../obsidian-mcp/build/main.js '.../OMI'
```

So `doctor` and `claude mcp add` disagree: `add` says "already exists",
`doctor` says "not registered". `claude mcp get/list` is the authoritative
source — the server is correctly wired in the leak-free direct-`node` form.

### Root cause & fix (resolved 2026-06-08)
It *was* a config-path mismatch. `claude_config_path()` returned
`~/.claude/.claude.json`, but Claude Code stores `mcpServers` in `~/.claude.json`
(directly in `$HOME`). So `registered_server()` read a file that does not exist
and always returned `None`. Fixed by reading `~/.claude.json` (with a legacy
fallback). `omind doctor` now reports `[✓] ... All checks passed`. Regression
tests added in `tests/test_provision.py`
(`test_claude_config_path_*`, `test_doctor_finds_server_via_canonical_config_path`).

### Lesson
The existing tests monkeypatched `claude_config_path` to a temp file, so they
**could never catch a wrong default**. When a function's whole job is to locate
a real, well-known path, add at least one test that asserts the *actual* default
(not just behavior under a patched path).

---

## 4. "Hanging" MCP tool calls are usually the permission prompt + cold start

### Symptom
`mcp__obsidian__*` tool calls from Claude Code appear to hang.

### What is *not* wrong
The `obsidian-mcp` server is healthy. Driven by hand it answers `initialize`,
`tools/list`, and tool calls in well under a second:

```bash
printf '%s\n' \
  '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"probe","version":"0.0.1"}}}' \
  '{"jsonrpc":"2.0","method":"notifications/initialized"}' \
  '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}' \
  | node ~/.claude/mcp-servers/obsidian/node_modules/obsidian-mcp/build/main.js \
        "$HOME/Documents/Obsidian Vault/OMI"
```

(All the `Registering tool: ...` chatter goes to **stderr**, not the JSON-RPC
stream, so it does not corrupt responses.)

### Cause & fix
The `mcp__obsidian__*` tools are not on Claude Code's allowlist, so **every call
triggers a permission prompt**; combined with first-call cold start this reads
as a hang. Add the read-only tools to the allowlist to stop the prompting:

```
mcp__obsidian__read-note
mcp__obsidian__search-vault
mcp__obsidian__list-available-vaults
```

Leave write/delete tools (`create-note`, `edit-note`, `delete-note`,
`move-note`, tag ops) behind prompts. See
[docs/troubleshooting.md](docs/troubleshooting.md) for the related
orphaned-process leak and its root cause.

### Lesson
When reading OMI memory is time-sensitive, **reading the vault files directly
from disk** is a reliable fallback that bypasses the MCP layer entirely — the
notes are plain Markdown under `<vault>/OMI/`.

---

## 5. The MCP `vault` parameter is the lowercase slug, not the folder name

### Symptom
MCP tools that take a `vault` argument need the **registered vault name**, which
is derived from the folder and **lowercased**. For the `OMI` folder the server
logs:

```
Vault "OMI" registered as "omi"
```

### Lesson
Pass `vault: "omi"` (lowercase), not `"OMI"`, to `read-note` / `search-vault` /
etc. Use `list-available-vaults` to confirm the exact slug if unsure.
