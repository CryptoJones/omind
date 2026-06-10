# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- External commands (`npm`, `claude`, `restic`, `rsync`, `systemctl`, …) now
  run with a timeout (10 minutes by default; 1 hour for the snapshot-producing
  backup calls), so a stalled npm install or a restic hung on a dead SFTP link
  fails loudly instead of wedging `omind setup` or the unattended backup timer
  forever. The subprocess plumbing previously duplicated between provisioning
  and backup (Windows `.cmd`-shim resolution, output capture, error mapping)
  now lives in one shared module, `omind.proc`. With tests.

- Windows part 3, courtesy of the new windows-latest CI legs:
  `omind setup` re-runs no longer duplicate the auto-memory hooks on Windows —
  `shutil.which` resolves the hook command to `omind.EXE`, which the literal
  `"omind hook"` marker match didn't recognize as omind's own entry (doctor
  reported the hooks missing for the same reason). Re-importing a bundle over
  a vault written through Windows text mode no longer flags every note as a
  conflict (newline-insensitive comparison). The journal hot path and the
  backup password file now open with `O_BINARY`/`newline="\n"` so CRT text
  mode can't rewrite their bytes. With tests; the suite now runs on
  windows-latest (Python 3.10 and 3.14) in CI.

- Hook errors are no longer invisible: the hook handlers still never block or
  fail the agent, but every swallowed error now leaves a one-line breadcrumb
  in `~/.local/state/omind/hook-failures.log` (size-capped, best-effort), and
  `omind doctor` warns when that log has entries from the last 7 days.
  Previously a full disk or a permissions change meant the session journal
  just silently stopped existing. With tests.

## [1.2.0] - 2026-06-10

### Fixed

- `omind doctor` no longer crashes on consoles that can't encode `✓`/`✗`
  (Windows cp1252): the check markers degrade to ASCII (`+`/`!`/`x`) when
  stdout's encoding can't represent them.

- Windows part 2: subprocess calls (`npm`, `claude`, `restic`, …) now resolve
  the executable via `shutil.which` on Windows before spawning, so `.cmd`
  shims like `npm.cmd` run — `CreateProcess` does not resolve them from a bare
  name, which broke `omind setup` at the obsidian-mcp install step on the
  win11-openclaw box. POSIX path untouched.

- omind now runs on Windows: the POSIX-only `fcntl.flock` imports in the store
  and the journal hot path crashed every command at import time
  (`ModuleNotFoundError: No module named 'fcntl'`). New `omind.filelock` shim
  locks via `fcntl.flock` on POSIX and `msvcrt.locking` on Windows, preserving
  the single-writer guarantees on both. Found live while provisioning OpenClaw
  on a Windows 11 VM. With tests.

- `index.md` regeneration no longer wipes descriptions: each Recent Memories
  line now renders as `- [[note]] — {summary}` from the note's own `## Summary`
  (collapsed, ≤100 chars), with a one-time lock-protected migration that copies
  existing hand-written index descriptions into notes whose Summary was empty.
  The list is capped at the 25 newest notes (with an `*(N notes total)*`
  footer) so the SessionStart priming payload stops growing unbounded, and
  top-level `Session Journal *.md` strays are excluded. With tests.

- Daily auto-journal notes now live in a `Journal/` subfolder instead of the
  vault root, so they no longer pollute note listings, the regenerated index,
  or SessionStart priming. `omind setup` and `omind reindex` migrate existing
  `Session Journal *.md` from the root (and the legacy `logs/` location) under
  the write lock, idempotently. New `omind rollup [--week]` compacts a week of
  dailies into one summary note and archives or deletes the raw files
  (default retention 30 days). With tests.

- `omind hook` journaling no longer marks a tool action as `(error)` just because
  its response carries a `stderr` field — git, curl, npm and friends write
  progress there on success. Only explicit failure signals count now:
  `is_error`, `success: false`, a non-empty `error` field, or a nonzero
  `exit_code`/`returncode`. With tests.

### Added

- `omind backup` — encrypted, unattended off-machine backup of the OMI folder,
  wrapping restic: `init` (generates `~/.config/omind/backup.pass`, 0600,
  refuses overwrite), `run` (snapshot + 7d/4w/6m retention; 3 consecutive
  failures upsert a `BACKUP FAILING` note through the single-writer path so it
  surfaces in priming, success clears it), `verify` (restic check + restore the
  latest index.md to a temp dir and diff), and `install-timer` (daily systemd
  user timer). Degrades to rsync `--link-dest` dated snapshots when restic is
  absent. `omind doctor` reports backup health for every agent (unconfigured /
  last-success age / failing). New module `src/omind/backup.py`; the password
  never reaches a command line or log. With tests (all subprocess calls
  mocked).

- SessionStart priming now injects the newest `Session State YYYY-MM-DD` handoff
  note and the last 20 action bullets of the newest auto-journal (labeled
  "recent actions (auto-journal)"), after the static priming files. A 48k-char
  total payload cap keeps the static files whole and truncates the dynamic
  sections first, so a restarted session picks up "where we left off" without
  reading anything by hand. With tests.

- `omind setup --agent hermes|openclaw` — provision **Hermes Agent** and
  **OpenClaw** against the same OMI folder and the same obsidian-mcp install as
  Claude Code. Registers the stdio MCP server in the agent's own config
  (`mcp_servers` in `~/.hermes/config.yaml`, `mcp.servers` in
  `~/.openclaw/openclaw.json` — legacy `~/.clawdbot`/`~/.moltbot` roots and
  config names detected), merging only omind's entry and refusing to overwrite
  a config it cannot parse, and installs an `omind-omi-memory` skill that
  routes the agent's memory writes through the single-writer `omind note`
  path. `omind doctor --agent ...` and `omind quickstart --agent ...` gain the
  matching diagnosis and manual steps. New module `src/omind/agents.py`; new
  runtime dependency PyYAML. With tests.
- `omind note` — create or update a single OMI note from the command line through
  the safe write path (the `.omi.lock` flock + atomic `os.replace` + `note_version`
  re-check), rendering the canonical note format. Upserts by title (creates, or
  updates in place); body is read from stdin so multi-line content pipes cleanly.
  New module `src/omind/notes.py` (`upsert_note`) is the single write entry point
  reused by external writers — e.g. Hermes' `hermes-omi-memory-sync` skill — so no
  one writes OMI raw. See `docs/mesh.md` → "Node types & the single-writer rule".
  With tests.
- `extras/omi_write.py` — a tracked, standalone reference helper that writes one
  OMI note through the safe path (`omind.notes.upsert_note`), with env-based vault
  resolution (`OMIND_OMI_DIR` / `OBSIDIAN_VAULT_PATH`) and a source-tree import
  fallback. Equivalent to `omind note`, but as a single file embedders (e.g.
  Hermes' `hermes-omi-memory-sync` skill) can drop in. Excluded from the wheel.

- Inter-process write safety so concurrent Claude Code sessions (and the web UI
  and cron) can read and write the same OMI folder at once without corrupting
  it. `OmiStore` now serializes every write under an advisory `flock` on a
  shared `.omi.lock`, and all note/index writes go through an atomic same-dir
  temp-file + `os.replace`, so a reader never sees a half-written file and two
  saves can't interleave a note write with another save's `index.md`
  regeneration. The optimistic-concurrency check (`note_version`) is now
  re-validated inside the lock. Reads stay lock-free (atomic renames keep them
  consistent). The lock and temp files are dotfiles, excluded from listings,
  exports, and imports. Verified with a 24-process concurrency test.
- `omind reindex` — regenerate `index.md`'s Recent Memories list under the same
  write lock. Lets a session that wrote a note file directly (the reliable path
  when the Obsidian MCP stalls on permission prompts) refresh the index safely
  instead of hand-editing the shared `index.md` and racing other sessions.
- SessionStart hook now injects the OMI priming notes' *content* (`index.md`,
  `Memory Workflow.md`, `CLAUDE CODE PERSONALITY.md`) directly into context
  instead of only emitting a "go read OMI" reminder — so the vault is present
  at session start whether or not the agent issues reads. Per-file 16K cap
  guards context; falls back to the read-the-vault reminder if no note is
  readable.
- Auto-memory hooks: `omind setup` now idempotently installs Claude Code hooks
  (PostToolUse, Stop, SessionStart) into `~/.claude/settings.json` so every
  agent action is recorded into a per-day OMI journal note
  (`Session Journal YYYY-MM-DD.md`, tagged `#session-journal`) — complementing
  hand-authored curated notes. The hook handler is a new internal subcommand
  `omind hook <event>` (new module `src/omind/hooks.py`): it reads the hook's
  stdin JSON and appends one bullet under an `O_APPEND`+`flock` write (never
  blocks or fails the agent), while SessionStart injects a "read OMI" reminder.
  The merge preserves existing settings keys and user-authored hooks, replaces
  only omind's own entries (matched by an `omind hook` marker), and updates on
  vault-path drift. `omind doctor` verifies the hooks are installed. With tests.
- `omind export` / `omind import` to store and load the entire OMI dataset on
  request. Two formats via `--format`: `json` (a human-readable, diffable
  bundle of every note's raw Markdown + parsed fields; the derived `index.md`
  is omitted and regenerated on import) and `targz` (a byte-for-byte snapshot
  of the whole OMI folder, including `.obsidian/`, for full-fidelity
  migration). `import` auto-detects the format by extension. Import identity is
  the filename and is content-aware: new notes are added, byte-identical ones
  are no-ops, and notes whose content differs are skipped (on-disk copy kept)
  unless `--force` is given. Imports never delete; archive members are
  path-traversal guarded. New module `src/omind/transfer.py` with tests.
- `docs/mesh.md` — design for the 2.0.0 **git-backed memory mesh**: full
  peer-to-peer replication of the OMI folder over git (no central server, full
  offline operation), building on the existing per-node write safety with
  cross-node Lamport versioning, a field-level conflict merge over `NoteFields`,
  and **soft-delete** (disable / restore) instead of tombstoned hard deletes.
  Design only — not yet implemented. Linked from the README roadmap.

### Fixed

- `claude_config_path()` pointed at `~/.claude/.claude.json`, which never
  exists — Claude Code stores `mcpServers` in `~/.claude.json`. As a result
  `registered_server()` always returned `None`, so `omind doctor` reported a
  false `[✗] MCP server 'obsidian' not registered at user scope` even when
  `claude mcp get obsidian` showed it Connected, and `omind setup` re-runs hit
  `claude mcp add` → `already exists` (exit 1) instead of being idempotent. Now
  reads `~/.claude.json`, falling back to the legacy path only if the canonical
  file is absent. Added regression tests in `tests/test_provision.py`.

### Changed

- CI now runs `mypy src`. The project was already `strict = true` in
  `pyproject.toml`, but neither the GitHub Actions nor the Woodpecker
  pipeline actually invoked the type checker.
- CI now runs `pip-audit`. Both pipelines scan the resolved dependency
  tree for known CVEs after the `mypy src` step; `pip-audit>=2.10.0` is
  in the `dev` extra so the scan reproduces locally.
- Internal: `OmindProvisioner.check_prereqs()` is now `-> None`. It only
  ever raises or logs — nothing consumed the missing-tools list it used to
  return.

### Removed

- Dead `.prose-omi li.task` CSS rule. The bundled `marked` renders task
  list items as plain `<li>`, so the selector never matched anything.
- Stale `store.SECTIONS` reference in the `seeds.py` template comment —
  no such symbol exists; the actual parse contract is `store.parse_note`.

## [1.1.0] - 2026-06-04

Fixes a process leak in the provisioned MCP server: `obsidian-mcp` instances
piled up as orphans, one per Claude Code session, and tool calls could appear to
hang. See [docs/troubleshooting.md](docs/troubleshooting.md) for the full
diagnosis.

### Fixed

- `obsidian-mcp` no longer orphans when Claude Code exits. The root cause was
  two-fold: the server never exits on stdin EOF (its file watcher keeps Node
  alive), and the `npx -y obsidian-mcp` wrapper chain swallowed the termination
  signal before it reached Node.

### Changed

- `omind setup` now registers the server as a direct
  `node --require <eof-guard> <obsidian-mcp> <vault>/OMI` command instead of
  `npx -y obsidian-mcp`. `obsidian-mcp` is installed to a stable prefix
  (`~/.claude/mcp-servers/obsidian`) rather than relying on the
  garbage-collectable npx cache, and a small stdin-EOF guard preload makes the
  server exit cleanly on disconnect. Existing `npx`-form registrations are
  migrated automatically on the next `omind setup`.
- Prerequisite check now requires `npm` (used to install the pinned server)
  rather than `npx`.

### Added

- `omind doctor` flags a registration still using the leak-prone `npx` form and
  a missing stdin-EOF guard, and points to `omind setup` to repair them.

## [1.0.0] - 2026-06-03

First stable release. The web UI now runs fully offline and tolerates the OMI
folder being written by Claude Code's MCP and Hermes' cron at the same time.

### Added

- Offline asset vendoring: the SPA no longer loads Tailwind, fonts, or the
  Markdown renderer from a CDN. Tailwind is compiled to a committed stylesheet,
  fonts are served as local `woff2`, and `marked` is bundled. Build inputs live
  under `src/omind/web/tailwind/` and are excluded from the wheel.
- External-change guard: each note carries an opaque version token (mtime +
  size). Saves send the token they last read; if the file changed underneath
  them the API answers `409 Conflict` and the UI offers to overwrite.
- Live list refresh: the sidebar polls for changes every few seconds so notes
  written by other tools appear without a manual reload. Polling pauses while an
  editor is open or the tab is hidden.
- Keyboard shortcuts: `/` focuses search, `n` opens a new note, `Esc` cancels an
  edit, `Ctrl`/`Cmd`+`S` saves, and `j`/`k` move through the list.
- Backlinks panel: the note view lists other notes that `[[wikilink]]` to it.
- `omind doctor`: diagnoses the setup — Node/npx availability, MCP registration
  at user scope, and OMI folder/`.obsidian` config readability.

## [0.3.0] - 2026-06-03

### Added

- Switchable UI in six languages — English, Spanish, French, Arabic, Russian,
  and Chinese — with right-to-left layout for Arabic. The choice persists and
  auto-detects from the browser on first visit.

## [0.2.0] - 2026-06-03

### Changed

- Redesigned the web UI as a themeable, modern interface with five colour
  themes (midnight, carbon, dusk, paper, mint).

### Added

- README screenshot of the web UI.

## [0.1.0] - 2026-06-03

### Added

- `omind setup`: idempotent provisioning of the `obsidian-mcp` server for the
  Claude Code CLI at user scope, over an OMI folder in an Obsidian vault.
- `omind serve`: a localhost FastAPI + Tailwind web app to view, edit, and add
  OMI memory notes, with structured-form and raw-Markdown editing.
- End-user install methods and a `CONTRIBUTING` guide.

[1.1.0]: https://github.com/CryptoJones/omind/releases/tag/v1.1.0
[1.0.0]: https://github.com/CryptoJones/omind/releases/tag/v1.0.0
[0.3.0]: https://github.com/CryptoJones/omind/releases/tag/v0.3.0
[0.2.0]: https://github.com/CryptoJones/omind/releases/tag/v0.2.0
[0.1.0]: https://github.com/CryptoJones/omind/releases/tag/v0.1.0
