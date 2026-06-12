# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [2.28.0] - 2026-06-12

### Changed

- **mesh: `sync()` regenerates and commits once after all peer merges**
  instead of re-applying tombstones, re-parsing the whole vault for the
  index, and running a `git status/add/commit` round per peer (all under the
  write lock). Pushes now also always carry tombstone-applied state.

## [2.27.0] - 2026-06-12

### Fixed

- **store: every write surface now nudges the mesh daemon's debounced sync.**
  The write-signal touch lived only in the MCP server's tool wrappers, so
  edits made through `omind serve`, `omind note`, or `omind import` sat
  uncommitted and unreplicated for up to the full sync interval (default
  300s) instead of debounce-syncing in ~10s — invisible until a machine dies
  holding five minutes of unsynced memories. The touch now happens in
  `OmiStore`'s write paths (mesh folders only), so new write surfaces get it
  for free.

## [2.26.0] - 2026-06-12

### Changed

- **cli: the `--vault`/`--folder` pair is defined once** (`_add_vault_args`)
  and applied to all 14 vault-touching subcommands, instead of being
  hand-copied onto each — changing the default vault path or help text was a
  13-place edit where missing one gave a subcommand silently different
  defaults.

## [2.25.0] - 2026-06-12

### Changed

- **journal: weekly rollups render through `store.render_fields`** instead of
  a third hand-built copy of the note template — when the template grows a
  field, rollups now grow it automatically instead of drifting out of the
  shape `parse_note` and the merge driver expect. (Daily journals keep their
  bespoke header on purpose: the trailing `## Actions` section is the
  O_APPEND hot path and deliberately bypasses the store.)

## [2.24.0] - 2026-06-12

### Changed

- **store/merge: one section splitter.** The merge driver's extra-section
  pass re-implemented `parse_note`'s `## heading` splitter with its own regex
  and its own top-level-`#` handling; if the two ever disagreed on what
  counts as a heading, template-owned content would be classified as "extra"
  and emitted twice in every merged note mesh-wide. Both now use a shared
  `store.split_sections` — which also stops the merge driver from silently
  dropping extra-section content that followed a stray top-level `#` line.

## [2.23.0] - 2026-06-12

### Fixed

- **hooks: one `action_bullets()` extractor for both SessionStart priming and
  rollups — and the two copies had already drifted.** `hooks._journal_tail`
  never reset at the next `## ` heading, so bullets in any section *after*
  `## Actions` were wrongly primed into SessionStart context; `journal.py`'s
  copy reset correctly. The shared helper (owned by hooks, next to the writer
  that defines the format) uses the correct reset semantics.

## [2.22.0] - 2026-06-12

### Changed

- **paths: the session-journal filename convention is defined once.**
  `JOURNAL_PREFIX`/`JOURNAL_GLOB` in `paths.py` now feed the writer
  (`hooks.journal_name`), the rollup/migration globs and regex in
  `journal.py`, and the index-exclusion regex in `store.py` — previously the
  pattern was hand-encoded in five places, so renaming it would have left
  rollups never matching new dailies and journals flooding the index.

## [2.21.0] - 2026-06-12

### Changed

- **store/web/server: single read + single parse on the hot single-note
  paths.** The web `GET /api/notes/{name}` and MCP `read-note` read the same
  file twice (`read_note` then `read_fields`); `search()` parsed every
  matching note twice (filter pass, then `_summarize` re-parse); and
  `_summarize` hand-rolled the whitespace-collapse + truncation that
  `_collapse` already implements. One read, one parse, one snippet rule.

## [2.20.0] - 2026-06-12

### Fixed

- **cli: `omind backup verify` uses the shared doctor symbol map** (with its
  ASCII degrade) instead of a hardcoded `✓/!/✗` dict — on the cp1252 Windows
  consoles the degrade exists for, `backup verify` crashed with
  `UnicodeEncodeError` while printing its checklist.

### Changed

- **hooks: `failure_log_path` derives from `paths.state_dir()`** instead of
  re-implementing the XDG_STATE_HOME resolution — doctor reads this log to
  surface swallowed hook errors, and a drift between writer and reader would
  make those failures invisible again.

## [2.19.0] - 2026-06-12

### Changed

- **provision: one shared `_read_mcp_servers()` reader** replaces the
  copy-pasted read-config/parse-JSON/get-`mcpServers` blocks in
  `registered_server` and `_legacy_server` — error-handling fixes were bound
  to land in one copy and not the other, making doctor and the legacy
  retirement path disagree about what is registered.
- **provision: removed the dead `run_setup()` wrapper** — nothing referenced
  it (the CLI goes through `agents.run_setup_for`, which constructs the
  `Provisioner` itself, including agent dispatch the wrapper bypassed).

## [2.18.0] - 2026-06-12

### Changed

- **mesh: `_commit_locked` no longer takes an unused `node_id` parameter** —
  it implied the commit identity depended on it (it actually comes from the
  `user.name` git config set in `mesh_init`) and forced every call site to
  thread a value that did nothing.

## [2.17.0] - 2026-06-12

### Changed

- **mesh: removed a duplicated `merge.ours.driver` config block in
  `mesh_init`** — the same git-config line (and its 3-line comment) appeared
  twice back-to-back; a future edit would likely have touched only one copy.

## [2.16.0] - 2026-06-12

### Changed

- **mesh: `peers()` reads all remotes in one `git config --get-regexp` call**
  instead of `git remote` plus one `get-url` subprocess per remote — the
  daemon runs this at the top of every sync tick, so with N peers that was
  N+1 forked processes per cycle, forever.

## [2.15.0] - 2026-06-12

### Fixed

- **store: the optimistic-concurrency token is now content-based.** It was
  `mtime_ns + size`, which collides when two same-size writes land within one
  filesystem timestamp tick (1–2s granularity on FAT/exFAT and some network
  mounts — places Obsidian vaults actually live), letting a stale save pass
  the conflict check and destroy the concurrent edit. The token is now
  `size + BLAKE2 content digest`: two tokens can only match when the bytes
  are identical, in which case there is nothing to lose.

## [2.14.0] - 2026-06-12

### Fixed

- **store: `backlinks` now matches aliased and heading wikilinks.** The raw
  `[[...]]` capture was compared whole against the target's title/stem, so
  `[[Note A|the project]]` and `[[Note A#Details]]` — both backlinks in
  Obsidian — were silently missed. Only the part before `|` or `#` names the
  target note, and that's what gets compared now.

## [2.13.0] - 2026-06-12

### Fixed

- **provision/mesh/backup: `--folder` is quoted everywhere a command string is
  serialized, and launchd plist arguments are XML-escaped.** The hook command
  in `settings.json`, the systemd `ExecStart` lines (mesh daemon + backup
  timer), and the printed `schtasks` one-liner all quoted `--vault` but left
  `--folder` bare — `omind setup --folder "My Memory"` produced hooks and
  services that word-split into a stray positional and silently never worked.
  The macOS plist interpolated arguments into XML unescaped, so a vault path
  containing `&` or `<` yielded an invalid plist.

## [2.12.0] - 2026-06-12

### Fixed

- **bootstrap: check the dependencies omind actually has.** The script
  hard-required node/npm — which omind doesn't use (its own header says so) —
  so a machine with `claude` installed via the native installer aborted the
  documented one-line install for no reason. And it never checked `git`, the
  one tool `omind setup` and the mesh genuinely require (and that `uv tool
  install` of a git URL needs). It now checks git + claude, treats npm purely
  as install guidance for claude, and fails fast with a clear message when
  git is absent.

## [2.11.0] - 2026-06-12

### Fixed

- **cli: a corrupt `node.json` no longer crashes `omind node` at startup.**
  `_run_node` called `load_node_config` unguarded, so invalid JSON (partial
  write, manual edit) made every Claude session's MCP server die with a
  traceback — all OMI memory tools gone behind an opaque "server failed to
  start". It now warns on stderr and serves without a mesh identity
  (unstamped writes), matching how `_run_mesh` already degrades.

## [2.10.0] - 2026-06-12

### Fixed

- **transfer: `omind import` honors the single-writer contract.** The import
  write phase now runs under the store's `.omi.lock` (so the mesh daemon's
  `git add -A` can never stage a half-applied import), every file lands via
  atomic same-dir temp + `os.replace` instead of in-place `write_bytes`, and
  on a mesh node imported top-level notes get a Lamport rev stamp — an
  imported note carrying a stale rev would otherwise lose the next merge.

## [2.9.0] - 2026-06-12

### Fixed

- **journal: re-rolling a week no longer destroys the earlier aggregate.**
  `rollup_journals` recomputed a week's stats only from dailies still in
  `Journal/`, then overwrote the existing rollup note — so a late daily for an
  already-archived week (e.g. union-merged in from an offline peer) replaced a
  five-day aggregate with a one-day one. The recompute now includes that
  week's dailies in `Journal/Archive/`, so rewriting the rollup is always a
  superset of what it replaces.

## [2.8.0] - 2026-06-12

### Fixed

- **store: a stale save can no longer resurrect a purged note.** The
  optimistic-concurrency check was skipped when the target file was missing
  (`expected_version is not None and path.is_file()`), so a client holding a
  pre-purge version token silently recreated the note — which then replicated
  back out across the mesh until each peer's next tombstone pass. A missing
  file now counts as a token mismatch (`note_version` returns `""`) and
  raises `NoteConflictError`.

## [2.7.0] - 2026-06-12

### Fixed

- **mesh: `sync()` no longer holds the vault's exclusive write lock across
  network I/O.** `git fetch`/`git push` (up to 120s each per peer) ran inside
  the lock, and POSIX flock has no timeout — with unreachable peers, every
  note writer (MCP `edit-note`, the web UI) blocked for minutes per sync
  tick. Fetch/push only move refs and objects, so they now run unlocked; the
  lock covers exactly the working-tree steps (commit, merge, tombstones,
  index regeneration), re-committing any local write that lands between the
  locked sections so merges never see a dirty tree.

## [2.6.0] - 2026-06-12

### Fixed

- **store: the write paths now reject reserved filenames.** Only
  `disable_note`/`purge_note` guarded against them, so a note titled `index`
  (via `omind note`, the MCP `create-note`/`edit-note` tools, or the web UI)
  mapped to `index.md`, overwrote the vault index, and the next index
  regeneration adopted the rendered note body as the hand-written intro —
  permanently. `write_note`/`create_note`/`update_note` raise `NoteError`
  for `index.md` and `Memory Template.md` instead.

## [2.5.0] - 2026-06-12

### Fixed

- **mesh: `.omi-tombstones` and `node.json` are written atomically** (same-dir
  temp file + `os.replace`, the store's own `_atomic_write`) instead of
  in-place `write_text`. A crash mid-write previously truncated the tombstone
  list — and the truncation merged out to every peer as clean line deletions,
  resurrecting previously hard-purged notes mesh-wide. A torn `node.json`
  either broke every subsequent mesh command or silently minted a fresh
  `node_id`, breaking the never-regenerated Lamport identity invariant.

## [2.4.0] - 2026-06-12

### Fixed

- **store: `disable_note`, `restore_note`, and `update_note` are now atomic
  read-modify-writes.** They previously read the note *before* `write_note`
  took the inter-process lock and wrote the transformed snapshot back with no
  version check — any edit landing in that window (another Claude session, the
  web UI) was silently reverted. The whole cycle now runs under one
  `write_lock()` via a shared `_mutate_note` helper (the flock is not
  reentrant, so nesting through `write_note` was never an option).

## [2.3.0] - 2026-06-12

### Fixed

- **store/notes: updating a note no longer resets its `Created:` date or wipes
  fields the caller didn't pass.** `update_note` back-fills an empty `created`
  from the existing note (an empty value was silently rewritten to today by the
  renderer), and `upsert_note` — the path behind `omind note`, Hermes, and the
  backup failure note — now keeps the existing summary/details/tags/
  connections/action-items/references when the incoming fields leave them
  empty, instead of erasing whatever the CLI flags couldn't express.

## [2.2.0] - 2026-06-12

### Fixed

- **store: Lamport rev-stamping no longer depends on each caller passing
  `node_id`.** `OmiStore` now derives the node identity from the mesh node
  config on first use when the caller doesn't supply one. Previously only the
  MCP server (`omind node`) passed it, so on a mesh node, edits made through
  the web UI (`omind serve`), `omind note`, or `omind import` were written
  unstamped — and the field-level merge driver's last-writer-wins rule handed
  those fields to an *older* stamped peer edit on the next sync, silently
  discarding the newer local change. A corrupt node config degrades to
  unstamped writes instead of breaking note CRUD.

## [2.1.0] - 2026-06-12

### Fixed

- **mesh: a timed-out `git merge` is now aborted** instead of leaving
  `MERGE_HEAD` and a half-merged tree behind. Previously `_merge_ref` only ran
  `git merge --abort` on a non-zero exit; a merge that hit the 120s git
  timeout raised before that check, and the next sync's `git add -A && git
  commit` completed the abandoned merge — conflict markers included — and
  pushed it to every peer. `_commit_locked` now also aborts any leftover
  in-progress merge before staging, so no crashed sync can ever be committed
  as a merge commit.

## [2.0.1] - 2026-06-11

### Added

- **`omind mesh add-seed <name> <url> [--mirror <git-url>]`** — provision a
  passive bare "seed" repo (at a local path or over ssh) and register it as
  a peer, in one repeatable command. It creates the bare repo, installs a
  post-receive hook that points `main` at the freshest node outbox ref (a
  bare seed never grows a branch on its own, which left `doctor`'s peer
  check reading "never fetched" forever and the seed unfetchable as a
  relay), and — with `--mirror` — mirror-pushes the whole seed to a hosted
  git repo (e.g. a private GitHub repository) after every received push.
  Every step converges on re-run. Docs: a new "Add a seed" runbook section
  in [docs/mesh-ops.md](docs/mesh-ops.md).

## [2.0.0] - 2026-06-11

**The memory mesh.** omind goes from a single-machine memory tool to a
peer-to-peer mesh: every machine runs a full local node and nodes replicate
over git+ssh — no central server, full offline operation. Design:
[docs/mesh.md](docs/mesh.md); operation: [docs/mesh-ops.md](docs/mesh-ops.md).

### Added

- **`omind node`** — omind's own MCP server over stdio (official `mcp` SDK),
  exposing the store as nine tools (`read-note`, `create-note`, `edit-note`
  with optimistic concurrency, `search-vault`, `list-notes`, `delete-note`,
  `restore-note`, `backlinks`, `list-tags`). Exits cleanly on stdin EOF by
  construction — the entire obsidian-mcp hang class (#49) is structurally
  gone, held by a subprocess regression test.
- **`omind mesh`** — `init` (git repo + field-level merge driver + node
  identity), `add-peer`/`remove-peer` (peers are plain git remotes), `sync`
  (commit, fetch/merge each reachable peer, push to a per-node
  `refs/omind/<id>` outbox — never a peer's checked-out branch), `daemon`
  (interval + on-write debounce), `install-service` (systemd user unit /
  launchd agent), `clone` (seed a new machine), `purge` (the rare
  hard-delete-everywhere, via replicated tombstone).
- **Per-note Lamport revisions** (`- Rev: <n>@<node-id>` in `## Metadata`) —
  the cross-node ordering truth; wall clocks are never trusted.
- **Field-level 3-way merge driver** (`merge=omi`): set-union lists,
  rev-LWW scalars, line-merged details where disjoint edits both apply and
  same-point additions concatenate; a truly diverging region keeps both
  sides under conflict markers plus a `#merge-conflict` tag. Every rule is
  side-symmetric, so two nodes merging each other's work converge
  byte-identically — even on conflict. Unknown `## Sections` are preserved.
- **Archive instead of delete**: deleting a note on a mesh node sets
  `Disabled: true` — hidden from listings/search/index but on disk and
  restorable (web UI "archived" toggle + Restore button; `restore-note`
  tool). Hard removal exists only as `omind mesh purge`.
- **Doctor mesh checks**: node identity, merge-driver health, `.gitattributes`
  routing, folder permissions, per-peer ahead/behind, last-sync age,
  unresolved conflict markers, archived-note count.
- **Privacy hardening**: `mesh init`/`clone` lock the OMI folder to owner-only
  (0700) on POSIX — meshes never interact unless explicitly peered over
  authenticated ssh (no discovery, no listener), and a traversable folder on
  a shared host would leak the memory history to local users via `file://`.
- Web UI: `GET /api/meta` (delete semantics), `include_disabled` listing,
  `POST /api/notes/{name}/restore`, archived badges, six-language strings.

### Changed (breaking)

- **The MCP server is omind itself.** `omind setup` registers `omi` →
  `omind node ...` and removes the retired `obsidian` (obsidian-mcp)
  registration from Claude Code, Hermes, and OpenClaw configs. The default
  `--server-name` is now `omi` — workflow notes referencing
  `mcp__obsidian__*` tools need the new prefix.
- **Deleting archives** (mesh nodes): `OmiStore.delete_note`, the web DELETE,
  and the MCP `delete-note` soft-delete on a folder that replicates; plain
  folders keep 1.x unlink behavior (`omind setup --no-mesh`).
- **Dependencies**: Node.js and npm are no longer required at all; `git` is.
  New Python dependency: the official `mcp` SDK.
- `omind setup` initializes the mesh by default (`--no-mesh` opts out).

### Removed

- obsidian-mcp install machinery, the npx/direct-node registration forms, and
  the entire stdin-EOF-guard apparatus (preload, managed-file refresh, doctor
  checks, real-node tests). The 1.x troubleshooting saga is preserved in
  [docs/troubleshooting.md](docs/troubleshooting.md) as history.

### Fixed

- **obsidian-mcp going silently deaf after idle** (#49) — fixed twice over:
  the 1.x eof-guard gained a transport watchdog (shipped unreleased), and
  2.0 then deleted the failure mode outright by replacing the server.

### Migration (1.x → 2.0)

```bash
uv tool upgrade omind        # or: pipx upgrade omind
omind setup                  # re-registers omi, removes obsidian, mesh init
omind doctor                 # should be green
# optional, per extra machine:
omind mesh add-peer <name> <ssh-url>
omind mesh install-service
```

Notes are untouched: legacy notes carry no Rev line and round-trip
byte-identical until their first mesh-mode edit.

## [1.3.0] - 2026-06-10

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

### Changed

- The canonical OMI filenames (`INDEX_FILENAME`, `MEMORY_TEMPLATE_FILENAME`,
  `RESERVED_FILENAMES`, `EOF_GUARD_FILENAME`, `AGENT_SKILL_FILENAME`) moved
  from `omind.seeds` to the new `omind.paths` module; `omind.seeds` no longer
  exports them. Embedders importing those names must update their imports —
  the CLI is unaffected.
- CI now runs the full suite on Windows (Python 3.10 and 3.14) alongside
  Linux 3.10–3.14, and the CLI subcommand wiring (serve/export/import/doctor/
  backup/setup) gained end-to-end integration tests.

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
  the write lock, idempotently. With tests.

- `omind hook` journaling no longer marks a tool action as `(error)` just because
  its response carries a `stderr` field — git, curl, npm and friends write
  progress there on success. Only explicit failure signals count now:
  `is_error`, `success: false`, a non-empty `error` field, or a nonzero
  `exit_code`/`returncode`. With tests.

### Added

- `omind rollup [--week]` — compact a week of daily session journals into one
  summary note each, then archive (default, to `Journal/Archive/`) or delete
  the raw dailies; default retention 30 days. With tests.

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

Proudly Made in Nebraska. Go Big Red! 🌽 https://xkcd.com/2347/
