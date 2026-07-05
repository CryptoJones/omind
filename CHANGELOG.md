# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [3.8.5] - 2026-07-05

### Fixed
- **Guard: a brand-new `git init` repo with no remote no longer locks the agent out** (#149): the same-turn freshness gate demanded a `git fetch`/`git pull` before any review/edit/test/commit in a repo — but a freshly initialised repo has no remote, so `git fetch` errors (*No remote repository specified*) and `git pull --ff-only` errors (*no tracking information*); the freshness check was unsatisfiable and the agent could not work in its own new repo. A repo with **zero configured remotes has no upstream to be stale against**, so freshness is now waived for it (`repo-work-fresh-base` no longer fires) while the rules-note consult still applies. Detection (`_repo_has_remote`) is subprocess-free — it reads `<repo>/.git/config` for a `[remote "…"]` stanza — and deliberately conservative: a `.git` pointer file (linked worktree/submodule), an unreadable config, or any resolution error is treated as *has a remote*, so freshness is only ever waived when we positively confirm zero remotes. A repo that has a remote is completely unaffected.

### Fixed
- **Verifier: reading the note a guard block demanded is no longer punished** (#148): the deny-log sampling under #147 found 16 cases where the guard blocked repo work demanding `Operational Rules - Git Repos and Secrets` be read, the agent obeyed, and the verifier scored that read off-topic and re-closed the gate. The guard now records the demanded note (per-turn marker, cleared at turn start by both `begin_turn` and the bash reset), and the verifier treats a consult of it as relevant by definition. A ritual read on a turn where nothing demanded it is still judged normally.
- **Verifier: vault writes are no longer scored as consults** (#148): `create-note`/`edit-note`/`delete-note`/`restore-note` used to clear the consult-gate at PreToolUse and then get relevance-scored (and denied — 17 events) at PostToolUse. Writes are acts, not consults: the Claude and Hermes adapters now gate them like any ordinary action, and the verifier never classifies them. The Hermes adapter also gains the navigation-tool exclusion (`list-notes`/`list-tags`/`graph-*`/`backlinks`) the Claude adapter already had.

### Changed
- **Off-topic denials are now auditable** (#148): the `off-topic-consult` violation record carries a `detail` field with the deterministic relevance score and the (truncated) task/activity/pending signals the consult was judged against — without it, the 437-deny log could not distinguish verifier false positives from real gate-dodges. `compliance.log_event` gained an optional `detail` parameter (written only when non-empty; existing readers unaffected). Verifier thresholds remain untouched pending a sampling pass over the enriched log.

## [3.8.3] - 2026-07-02

### Fixed
- **Guard: `git -C <dir>` is now honored for freshness *attribution*, not just recognition** (#147): `git -C <repo> fetch` records freshness for the `-C` target repo instead of the shell's cwd repo, so cross-repo work is satisfiable from a cwd-pinned session. The same parsing tightens the check side — `git -C <repo> commit` now checks the target repo's freshness, not the cwd's. Scoped to commands whose first token is literally `git` (`make -C`/`tar -C` are never misread); relative and repeated `-C` resolve with git's own cumulative semantics; any parse trouble falls back to cwd (fail-open, never a crash).
- **Guard: `git -C <repo> commit/push/...` is now classified as repo work at all** (#147, found while verifying the above end-to-end): the write-verb and side-effect regexes required the verb immediately after `git`, so any `-C`/`-c` form sailed past the rules-note, freshness, and capability-side-effect checks entirely.
- **Guard: the per-turn freshness marker is now a set of repos** (#147): fetching repo B no longer evicts repo A's same-turn freshness, so a turn that legitimately touches two repos doesn't ping-pong between re-fetches. The pre-3.8.3 single-slot marker payload is still read (mid-upgrade sessions).

### Changed
- **Guard: a tight allowlist of provably-inert inspection commands skips the consult-gate** (#147): a bare `pwd`, `whoami`, `id`, `date` (read forms), `uname`, `hostname` (bare), `which`/`command -v`, `git --version`, `true`/`false` no longer requires an OMI consult — no filesystem, repo, network, or side-effect surface, so no consult could inform them. Any shell metacharacter (chain, pipe, redirect, substitution, glob) disqualifies the command, `echo` is excluded by design (arbitrary arguments), and the exemption does not satisfy the gate — the turn's first real action still requires its consult. Deliberately unchanged: bare `git pull` still does not establish freshness (an auto-allowed silent merge commit stays off the table), and the off-topic verifier thresholds are untouched pending a sampling pass over the deny log.

## [3.8.2] - 2026-07-02

### Docs
- Regenerated the README hero graph (`docs/graph.png`) so its nodes are coloured by OKF `type` (with a legend), matching the web graph view. The demo renderer (`docs/graph-demo/render_graph.py`) is now a self-contained `networkx` + `matplotlib` script (no Graphviz dependency); `make_demo_vault.py` assigns each demo note an illustrative `type`.

## [3.8.1] - 2026-07-02

### Fixed
- **Web UI rendered the OKF frontmatter block as body text**: after 3.8.0 gave every note a leading YAML frontmatter block, the note view ran `marked` over the raw note, so the `---` block surfaced as a horizontal rule followed by the frontmatter keys at the top of every note. `renderMarkdown` now strips the leading frontmatter block before rendering — it's already surfaced in the structured header, not body prose.

### Changed
- **The knowledge graph is now coloured by OKF `type`** (web view + `omind graph export`): each distinct `type` present gets a stable colour with a legend built from it; node *size* still encodes link degree. The Graphviz/DOT export fills nodes by type, and the graph JSON (`/api/graph`, `graph export --json`) now carries a `type` per node.

## [3.8.0] - 2026-07-01

### Added
- **Open Knowledge Format (OKF) support**: omind now reads and writes notes as a conformant [OKF v0.1](https://github.com/GoogleCloudPlatform/knowledge-catalog/tree/main/okf) bundle — Google Cloud's vendor-neutral, Apache-2.0 Markdown-plus-YAML-frontmatter spec for sharing knowledge with agents and tools. Every note omind writes now leads with a YAML frontmatter block carrying the one field OKF requires (`type` — derived from the note's kind, e.g. `#feedback` → `Feedback`, when not declared) plus the recommended `title` / `description` / `tags` / `timestamp`; the `index.md` is the OKF directory listing. Producer-defined frontmatter keys are round-tripped (the spec requires preserving unknown keys).
- **`omind convert`**: migrate a pre-OKF vault to OKF in place — idempotent (a note already in OKF form is skipped, no mesh revision bump), with `--check` to validate the three conformance rules and `--dry-run` to preview. Also exposed programmatically as `omind.okf.convert_vault` / `omind.okf.check_conformance`.

### Changed
- Note parsing is now **dual-format and backward-compatible**: metadata is read from either the legacy `## Metadata` section (which still wins for shared fields, so un-upgraded mesh peers keep working) or the OKF frontmatter. Existing vaults keep working with no migration required; the `## Metadata` block is retained in every rendered note alongside the new frontmatter. The mesh merge driver carries the new `type` through three-way merges.

## [3.7.8] - 2026-07-01

### Fixed
- Provision every managed hook script atomically at `0o755`: the writers wrote the file at `mkstemp`'s 0600 default and set the exec bit in a *separate* `chmod` step (and `omi-enforce.py` never got the chmod at all). When a re-provision runs as root and the B2 guard-config hardening then `chown`s the hook to `root`, that 0600 window — or the never-widened enforce hook — is unreadable by the agent user, so `python3 omi-enforce.py` fails with EACCES mid-reprovision (the transient "the guard hook is dead" blip). `_write_managed`/`_write_if_absent` now thread `mode=` into the atomic write, so the mode is set on the temp file *before* the rename and the destination is never briefly at 0600. Adds a regression test asserting every provisioned hook is `0o755` (world-readable, so a `chown root` can't hide it).

## [3.7.7] - 2026-07-01

The follow-up batch to 3.7.6: the deferred findings from the 2026-07-01
adversarial code review (tracked as #125–#131).

### Security
- Sanitize rendered note HTML and add a Host allowlist to the web UI ([#125](https://github.com/CryptoJones/omind/issues/125)): note markdown (authored by agents, synced from mesh peers — untrusted) is now run through DOMPurify before it reaches `innerHTML`, closing a stored-XSS vector where a prompt-injected note could execute JS against the CRUD API when opened. The API also gets a `TrustedHostMiddleware` Host allowlist (localhost by default) as a DNS-rebinding defence, and `omind serve` warns when binding to a non-localhost / all-interfaces address.

### Changed
- CI now tests on macOS and builds + smoke-tests the wheel ([#126](https://github.com/CryptoJones/omind/issues/126)): a `macos-latest` matrix leg (oldest + newest Python) so BSD-userland / case-insensitive-FS / PATH breakage can't ship green, and a `wheel` job that builds the real wheel, installs it non-editable, and asserts the packaged hook scripts and `web/static` assets are present (the editable install never exercised the wheel's file-selection).
- Pin dependency upper bounds and install the fleet by release tag ([#131](https://github.com/CryptoJones/omind/issues/131)): runtime deps are capped below the next major (`fastapi<1.0`, `mcp<2.0`, …) so a breaking upstream major can't land fleet-wide overnight through `uv tool install` (which ignores `uv.lock`), and `scripts/bootstrap.sh` now installs the latest published release tag by default instead of the moving `main` HEAD (override with `--ref`/`$OMIND_REF`).

### Fixed
- Graph view no longer freezes the tab or leaks loops ([#129](https://github.com/CryptoJones/omind/issues/129)): the synchronous pre-settle is bounded by a work budget (a large vault no longer triggers "page unresponsive" before first paint), the O(n²) all-pairs repulsion is skipped above a node threshold so frames stay cheap at scale, and `destroy()` now removes the leaked `window` mouseup listener. The web app also tracks and tears down the graph's render loop when the pane switches away, instead of discarding the handle and leaking a `requestAnimationFrame` loop per open.
- Web UI no longer 500s under its own poll ([#130](https://github.com/CryptoJones/omind/issues/130)): `OmiStore`'s summary cache is guarded by a lock, so concurrent `list_notes()` calls from FastAPI's threadpool can't hit "dictionary changed size during iteration". The MCP server also caches the `[[wikilink]]` graph build (invalidated by a cheap vault signature), so a burst of graph-tool queries costs one full-vault parse instead of one per tool.
- Expire purge tombstones after a TTL ([#127](https://github.com/CryptoJones/omind/issues/127)): a note re-created with a previously-purged filename is no longer silently deleted fleet-wide forever. New tombstones carry a timestamp and stop deleting after `TOMBSTONE_TTL_DAYS` (90); every node converges on the same expiry under the union merge, and expired lines are garbage-collected. Legacy undated tombstones stay permanent (they can't be safely dated under `merge=union`).
- Scope the autonomous-loop guard to one owner session ([#128](https://github.com/CryptoJones/omind/issues/128)): arming a `/loop` no longer refuses stops for every other concurrent session on the machine, and a concurrent session's work no longer resets the owner's no-work backstop counter. The owner is set from `omind loop arm --session` / `$CLAUDE_SESSION_ID`, or claimed by the first session to hit a Stop.

## [3.7.6] - 2026-07-01

Hardening release from an adversarial code review — data-integrity, guard
false-positives, enforcement fail-open holes, and crash/availability fixes. No
API breaks; `NoteFields` gains backward-compatible `frontmatter`/`lead` fields.

### Fixed

- **Note data loss:** the parse/render round-trip no longer drops YAML
  frontmatter (Obsidian Properties) or lead prose before the first `##`; section
  splitting is now fence-aware so a `##` inside a code block is body text, not a
  new section. Applies to the local edit path and the mesh merge driver.
- **Mesh convergence:** equal-rev/different-content now resolves by a symmetric
  content tiebreak, so `merge(A,B)` == `merge(B,A)` and the fleet converges
  instead of ping-ponging; the index-description migration stamps a rev on mesh
  nodes so it no longer creates equal-rev divergence.
- **Mesh replication stalls silently:** `.obsidian/workspace.json` (and friends)
  are now gitignored so their per-machine churn can't abort every peer merge;
  `omind doctor` surfaces recorded per-peer sync errors instead of always
  reporting "ok"; a push timeout records the error and continues to the next
  peer (and still writes sync state) instead of aborting the whole pass; network
  git runs with `GIT_TERMINAL_PROMPT=0` + ssh `BatchMode` so a prompt can't hang.
- **One bad byte no longer downs the vault:** all note/index/log reads decode
  with `errors="replace"` (and strip a BOM), and `search`/`backlinks` skip a note
  deleted mid-scan — a single non-UTF-8 note can't break listing/search/writes.
- **Guard false-positive interruptions:**
  - Freshness now recognizes `git -C <repo> fetch` and compound read forms
    (`git fetch && git status`) — the exact remediation the block message tells
    you to run — instead of only a bare `git fetch`.
  - Forge/destructive seed rules (`gh repo delete`, `gh auth setup-git`, …) are
    command-anchored, so a grep pattern or commit message no longer blocks.
  - A bare `>` (as in `pytest 2>&1`) is no longer treated as a file-writing side
    effect; only real stdout redirects count.
  - The global-config-mutation gate resolves the path against `$HOME`, so a
    project-local `<repo>/.claude/settings.json` is not treated as global config.
  - Global-authorization detection is negation-aware ("don't change" no longer
    authorizes) and covers more imperatives (fix/add/create/…).
- **Guard crash-hardening:** a learned rule whose regex fails to compile or
  matches the empty string is rejected at load and skipped at match time, so one
  bad rule can no longer brick every tool call on the machine. The command-
  position anchor now covers shell keywords/wrappers (`then`, `exec`, `xargs`,
  absolute paths) and `sudoedit`, closing sudo-rule bypasses.
- **Enforcement fail-open holes:** the guard adapter fails **closed** on an
  unparseable event and accepts array-shaped `args`; a contentless
  `list-notes`/`graph-*` call no longer clears the gate or auto-scores relevant;
  the secret-output guard no longer treats `pass show X 2>/dev/null | head` as
  safe (a real leak) and no longer false-blocks `pass` inside another word / a
  grep pattern; the turn-reset clears pending-intent and git-freshness (freshness
  is per-turn again, not per-session).
- **Cron/timer safety:** `checkpoint run` degrades cleanly instead of raising a
  traceback into the timer; `install-timer` writes an absolute `ExecStart` (or
  fails loudly) instead of a silently-broken unit; a malformed journal bullet or
  tz-aware log timestamp no longer crashes a checkpoint, and boundary-minute
  actions are no longer dropped from every window.
- **Self-update:** `OMIND_NO_UPDATE_CHECK` no longer disables an explicit
  `omind self-update`; the install subprocess and the version check have
  timeouts sized for a user-invoked update.
- **`omind lint` false failures:** a dated note series (daily Worklogs) is no
  longer flagged as near-duplicates; links to archived or `Journal/`-subfolder
  notes and `[[wikilinks]]` quoted in code fences are no longer broken-link
  errors — so `lint --strict` passes on a healthy vault.
- **Config/hook write corruption:** every managed settings/config/hook/backup
  write is now atomic (temp file + `os.replace` + directory fsync) so a crash
  mid-write can't brick a harness config or the guard hook; store atomic writes
  fsync the directory too.
- **Filenames:** the reserved-name check is case-insensitive (so a note titled
  "Index" can't destroy `index.md` on a case-insensitive filesystem); dot-prefixed
  and over-long titles raise a clean `NoteError` instead of creating an invisible
  note or an `ENAMETOOLONG` crash; `create_note` closes a concurrent-create race.
- **Enforcement migrate hook:** no longer deletes a memory file on a fuzzy
  filename match or a missing `name:` slug — it migrates (with a timeout and
  permission-safe unlinks) before deleting, and leaves the file if migration
  fails.

## [3.7.5] - 2026-07-01

### Changed
- Treat `send it` as explicit current-turn authorization for guarded global
  config/hook/bootstrap mutations.

### Fixed
- Allow explicit global config/hook/bootstrap authorization to come from the
  current action payload when the captured turn-task file is missing or stale.
- Block side-effect tool calls prompted only by capability questions such as
  `Can you ...?` unless the current turn includes explicit imperative
  authorization.

## [3.7.4] - 2026-07-01

### Fixed
- Allow read-only shell inspection of global agent config and hook paths while
  still requiring explicit current-turn authorization for shell mutations.

## [3.7.3] - 2026-07-01

### Fixed

- **Repo work now requires the git operational OMI note plus a fresh-base check.**
  The guard no longer treats an arbitrary OMI consult as enough before repo review,
  edits, tests, commits, pushes, or releases. In a git repo, repo-sensitive actions
  require reading `Operational Rules - Git Repos and Secrets` during the turn and
  running a same-turn `git fetch --all --prune` or `git pull --ff-only` freshness
  command first.
- **Global config/hook/bootstrap writes now require explicit current-turn user
  authorization.** The guard blocks installed agent config/hook/bootstrap mutation
  when the user asked a question rather than clearly authorizing the change.
- **Codex AGENTS bootstrap now spells out the repo freshness and global-config
  authorization rules.** `omind setup --agent codex` updates the managed block so
  fresh Codex sessions see the rule before acting.

## [3.7.2] - 2026-07-01

### Fixed

- **Codex setup now fully bootstraps OMI memory and trusts omind-owned hooks.**
  `omind setup --agent codex` now installs the `SessionStart` OMI priming hook,
  writes a managed global `~/.codex/AGENTS.md` pointer back to OMI, and persists
  Codex `[hooks.state]` trust hashes for the omind-owned guard and priming hooks.
  The trust hash is computed per machine from the exact hook definition (including
  the local `omind` path), with known-vector tests to catch upstream Codex hash
  changes before a release ships.

## [3.7.1] - 2026-07-01

### Fixed

- **Codex hook installs now use Codex's root `hooks` object.** `omind setup --agent
  codex` writes `~/.codex/hooks.json` as `{"hooks": {...}}`, matching current Codex
  CLI parsing. It also migrates the brief 3.7.0-era Claude-style root event map
  (`PreToolUse`, `PermissionRequest`, etc.) into the new object while preserving
  user-authored hook groups and remaining idempotent.

## [3.7.0] - 2026-07-01

### Added

- **Codex CLI now gets the `omi` MCP server registered, not just the guard (#114).**
  `omind setup --agent codex` merges `[mcp_servers.omi]` into `~/.codex/config.toml`
  (the same table `codex mcp add` writes), so Codex CLI can call the OMI memory
  tools (search-vault, create-note, etc.) directly instead of only having the
  guard's block/deny behavior. `config.toml` is TOML, unlike every other agent
  config omind touches, so the merge uses `tomlkit` for round-trip-safe editing —
  idempotent, preserves unrelated tables/comments/formatting, refuses to overwrite
  unparseable TOML. `omind doctor --agent codex` now reports `codex_mcp_registration`
  alongside the existing `codex_guard` check.

## [3.6.0] - 2026-06-28

### Changed

- **Forge policy reversed — GitHub is now authoritative; Codeberg is a live mirror.** The guard's
  four forge deny-rules — `gh-pr-create-merge`, `gh-api-pr-create`, `github-https-push`, and
  `github-push-discretionary` — are removed from `SEED_RULES`. PRs, merges, and pushes to
  CryptoJones-owned GitHub repos are now allowed (previously hard-blocked, or gated behind
  `OMI_PUSH_GITHUB=1`, under the old Codeberg-authoritative policy). The six destructive /
  privilege-escalation safety rules (`gh-repo-delete`, `gh-api-repo-delete`, `curl-api-repo-delete`,
  `gh-auth-setup-git`, `sudo-use-fleet-sudo`, `privesc-alternatives`) are unchanged. Rationale:
  Codeberg's 100-repo account cap demoted it to a mirror; GitHub natively hosts Dependabot, CodeQL,
  and push secret-scanning, and Dependabot / GitHub-only PRs had no merge path under the old guard.

## [3.5.4] - 2026-06-28

### Fixed

- **`omind setup` no longer wedges the agent on a machine without `jq` (#107).** The OMI-compliance
  guard is a `PreToolUse` `"*"` matcher whose hook parsed events with `jq` and **failed closed** when
  `jq` was absent — blocking every subsequent tool call, including the `Bash` call needed to install
  `jq` (a bootstrap deadlock). The hook now routes the raw event through `omind guard adapter` (the
  pure-Python core) when `jq` is missing, applying the same hard-blocks + gate, so a jq-less host stays
  enforced instead of wedged. `jq` is now a fast-path optimization, not a hard dependency; `omind
  doctor` reports a **warning** (not a failure) when it is absent. Only if `jq` *and* a working `omind`
  core are both missing does the hook fall back to the old fail-open-non-Bash / fail-closed-Bash last
  resort. (Earlier docs claimed setup *checked* for `jq` — it never did; `jq` is intentionally not in
  `REQUIRED_TOOLS` so setup can't refuse on a jq-less box.)
- **Guard no longer false-positives on an escalation keyword that isn't the command being run
  (#98 / #108).** The `sudo-use-fleet-sudo` and `privesc-alternatives` policy rules matched
  `sudo`/`su`/`pkexec`/`doas`/`run0` as a word-bounded token *anywhere* in the command, so benign
  commands were blocked — `grep -rn "sudo" src/`, `cat /var/log/sudo.log`, `git commit -m "fix sudo"`,
  `pass show sudo/akclark`, and even the sanctioned `fleet-sudo --entry akclark/sudo`. Both rules now
  use a new `Rule.match="command"` mode that anchors the token to **command position** (command start
  or after a shell separator `; & | newline ( \``, skipping leading `VAR=val` assignments) via the
  shared `policy._CMD_POSITION` prefix. Real escalation in command position still blocks — `sudo …`,
  `; sudo …`, `a && sudo …`, `a | sudo …`, `$(sudo …)`, `FOO=1 sudo …`, and the `pkexec/doas/run0/su`
  set — and the `OMI_SUDO_OK=1` opt-in is unchanged. Accepted tradeoff: an escalation keyword passed
  as an *argument* to another command (e.g. `ssh host sudo …`) is no longer caught — consistent with
  this guard being a cooperative seatbelt, not a security boundary.

## [3.5.3] - 2026-06-28

### Fixed

- **`LICENSE` was a paraphrased (non-canonical) Apache 2.0 — replaced with the verbatim text.** The
  repo declared `Apache-2.0` (`pyproject.toml` + the OSI classifier) but the `LICENSE` body was a
  reworded rendering missing the entire `1. Definitions` section and all formal term definitions
  (150 lines vs. the canonical ~201). A reworded license is technically not the Apache 2.0 license and
  breaks the SPDX `Apache-2.0` identifier and license scanners (GitHub, FOSSA, etc.). Swapped in the
  verbatim canonical Apache License 2.0, preserving `Copyright 2026 Aaron K. Clark` in the appendix
  notice. (Codeberg #91, GitHub #113)

## [3.5.2] - 2026-06-28

### Fixed

- **Consult-gate dodge: reading the vault index (`index.md`) no longer clears the gate.** The per-turn
  consult gate cleared on *any* Read under the OMI folder, including the auto-generated `index.md`
  ("Recent Memories" table-of-contents), `MEMORY.md`, and the note template. Because the index is
  "relevant to everything", an agent could satisfy the gate every turn by re-reading it without ever
  consulting a task-relevant note — the relevance verifier flagged these as `off-topic-consult` but
  only WARNED, never blocking. A Read of one of those scaffolding files is now allowed through but does
  **not** clear the gate (like `ToolSearch`); a real content note (or an `mcp__omi__*` search/read)
  must be consulted. Fixed in both bash adapters (`omi-guard.sh`, `omi-guard-hermes.sh`) on the
  PreToolUse hot path AND in `verify.consult_target`, since the PostToolUse verifier's
  `record_consult` would otherwise re-create the sentinel and re-clear the gate. The excluded basenames
  live in the new `paths.NON_CONSULT_FILENAMES` (superset of `RESERVED_FILENAMES`).

## [3.5.1] - 2026-06-27

### Fixed

- **Windows CI: `test_mcp_only_setup_errors_when_not_installed` no longer fails on `windows-latest`.**
  The suite-wide home isolation pinned `HOME`/`USERPROFILE` but not `%APPDATA%`/`%LOCALAPPDATA%`,
  where Windows GUI apps store config. Because the `windows-latest` runner ships VS Code, the MCP-only
  provisioners resolved to the real `%APPDATA%\Code\User` and the "errors when not installed" tests saw
  the prereq satisfied and never raised. The isolation fixture now pins both Windows app-data vars at
  the per-test temp home. POSIX is unaffected.
- **CI conformance job degrades gracefully when its token is expired.** The `MCP conformance` job
  installs the private `mcp-conformance` repo via the `MCP_CONFORMANCE_TOKEN` secret; the guard only
  skipped when the token was *unset*, so an expired/invalid token authenticated and failed the clone,
  reddening the build. A failed private-repo install now emits a `::warning::` and skips, honoring the
  job's "graceful no-op until a working token" intent. A valid token still runs the full suite.

## [3.5.0] - 2026-06-26

### Added

- **New `secret-output-guard.sh` PreToolUse(Bash) hook — stop credential VALUES reaching the
  transcript.** `omind setup` now installs and wires a portable bash guard that exits 2 to BLOCK a
  Bash command when a secret value would flow to stdout/the conversation — e.g. `pass show X | head`,
  bare `pass show X`, `pass <user>/<entry>`, `gh auth token`, `echo $(pass show X)`, or a literal
  `ghp_…`/`glpat-…`/`xoxb-…`/`AKIA…`/`BEGIN PRIVATE KEY` token pasted into the command. It ALLOWS the
  safe forms (`TOK=$(pass show X)` captured into a var, `pass show X >/dev/null` or `> file`
  redirected off the transcript, `pass insert`/`pass ls`, and a token piped into a `curl`
  `Authorization` header), with an audited `OMI_SECRET_OK=1` override. The hook is registered FIRST
  in the existing PreToolUse `Bash` matcher (ahead of `git-fresh-base.sh`) and ships verbatim — no
  install-time path substitution. Closes the leak class that `git add`-style secret guards miss (the
  exact mistake that printed a GitHub PAT into a conversation). The guard exempts git one-shot
  credential helpers (`credential.helper='!f(){ echo "password=$(pass X)"; }'`) — that echo feeds
  git's credential protocol on stdin, not the transcript — while still blocking a literal token even
  inside a `credential.helper` command (the literal-token check runs first).

### Changed

- **GitHub-PR hard-block is now owner-aware — third-party OSS PRs are allowed.** The
  `gh-pr-create-merge` and `gh-api-pr-create` guard rules previously blocked *every* GitHub PR (the
  owner's own repos must get PRs on Codeberg, not GitHub). They now BLOCK only when the target repo
  owner is `CryptoJones` (case-insensitive) and ALLOW a PR to a third-party repo the owner doesn't
  control. A third-party PR must name the target explicitly: `gh pr create|merge --repo
  <owner>/<repo>` or `gh api repos/<owner>/<repo>/pulls -f …`. A BARE `gh pr create`/`gh pr merge`
  (no `--repo`) stays BLOCKED — it defaults to the upstream, which may be a CryptoJones repo, so
  blocking is the safe default. All existing red-team protections (the `gh api`/`curl` DELETE rules
  and the GitHub-push rules) are unchanged.

## [3.4.0] - 2026-06-25

### Added

- **Interactive `[[wikilink]]` graph view in the `omind serve` web UI.** A new **Graph**
  button (and a `#graph` deep-link) renders the whole vault as a force-directed graph on a
  canvas — click a node to open that note, hover to highlight its neighbors, drag/zoom/pan,
  and "reset view" to re-fit. Hubs are labelled and nodes are coloured by connectivity
  (orphans stand out). Backed by a new `GET /api/graph` endpoint that reuses
  `graph.to_json`; the renderer is dependency-free (a small canvas force layout, no d3 / no
  graph library) and reads the active theme's CSS variables so it re-skins with the UI.
  Closes #101.

### Fixed

- **Sidebar tag bar no longer pushes the note list off-screen on large vaults.** The
  `#tag-bar` rendered every tag with no height limit, so a vault with hundreds of tags filled
  the entire sidebar and shoved the note list below the fold (notes appeared only at the very
  bottom, cut off by the tags). It's now capped (`max-height: 28vh`) and scrolls. Closes #102.

## [3.3.0] - 2026-06-25

### Added

- **Four more `omind setup --agent` targets: Claude Desktop, Kiro, VS Code, and Amazon Q.**
  `omind setup --agent claude-desktop|kiro|vscode|q` registers omind's own node server (`omind
  node`, the `omi` MCP server) into each tool's config so the OMI memory tools are one command (or
  one copy-paste) away — no hand-editing JSON. Targets and config files: Claude Desktop
  (`claude_desktop_config.json` under the OS application-support dir), Kiro
  (`~/.kiro/settings/mcp.json`), VS Code (user-level `mcp.json`, a `servers` block with `type:
  stdio`), and Amazon Q (`~/.aws/amazonq/mcp.json`). These are MCP-registration only — no guard
  hook and no skill file — and merge idempotently, touching only the `omi` entry they own and
  refusing to overwrite config they cannot parse. `omind quickstart --agent <x>` prints the same
  wiring as copy-paste steps, and `omind doctor --agent <x>` verifies it.

### Added

- **Knowledge graph over the vault's `[[wikilinks]]` (`omind graph`, MCP `graph-*` tools).** Every
  note is a node and every `[[wikilink]]` a directed edge; the store already answered the inbound
  question (`backlinks`) and `lint` already flagged orphans/broken links one note at a time — this
  adds the whole-graph view they leave out. New `omind graph` subcommands: `neighbors <note>
  [--depth N] [--direction out|in|both]` (multi-hop BFS), `path <a> <b>` (shortest link path),
  `orphans` (fully disconnected notes), `dangling` (wikilinks resolving to no note), `stats`, and
  `export [--format json|dot]` (Graphviz-renderable). The same surface is exposed to agents as MCP
  tools `graph-neighbors`, `graph-path`, `graph-orphans`, `graph-dangling`, and `graph-stats`.
  Resolution mirrors `backlinks`/`lint` (a link resolves by note stem or title, case-insensitive);
  self-links and disabled notes are ignored. New module `omind/graph.py` — pure standard library,
  no graph dependency; read-only throughout. Closes #99.

## [3.1.0] - 2026-06-24

### Added

- **Autonomous-loop guard (`omind loop arm|disarm|status`).** While *armed*, the Claude Code `Stop`
  hook refuses to stop — it emits `{"decision": "block", "reason": ...}` so the agent keeps working
  instead of halting at a self-declared "natural stopping point" or pausing to ask. Advisory memory
  notes weren't enough; this is the enforcement. An operator switch, like the guard pause. Bounded
  three ways so a runaway no-stop hook can't trap the agent or burn tokens: real work
  (`PostToolUse`) resets a consecutive-block counter; exceeding `--max-blocks` (default 25)
  consecutive *no-work* stops auto-disarms and allows the stop; and an `--hours` expiry (default 24)
  self-clears a forgotten flag. Like the rest of the hook layer it fails **open** — to *allowing*
  the stop — on any error. New module `omind/loopguard.py`; the `Stop`/`PostToolUse` paths in
  `omind/hooks.py` consult it; `omind loop` CLI added.

## [3.0.2] - 2026-06-22

### Fixed

- **Opt-in env assignment is now recognized on a newline-led line in a multi-line
  command** (regression from the 2.46.0 #2 hardening). The leading-assignment check
  treated only `^` / `;` / `&` / `|` as command boundaries, so a legitimate
  `OMI_PUSH_GITHUB=1 git push … github` (or `OMI_SUDO_OK=1 …`) at the start of a
  *line* inside a multi-line script was not seen as a leading assignment and got
  blocked. A newline **is** a shell command boundary, so it's now in the separator
  class (`[;&|]` → `[;&|\n]`). A plain space still isn't a separator, so the
  forgery guard (`echo "OMI_SUDO_OK=1"`, trailing-comment tokens) is unchanged. +1
  regression test.

## [3.0.1] - 2026-06-22

### Fixed

- **CI: the `omi-guard.sh` fail-closed tests now skip on Windows.** They shell out to
  `bash omi-guard.sh` (a POSIX bash+jq deployment artifact); GitHub's Windows runners
  *have* `jq`, so the old `jq`-only skip let them run under Git Bash, where CRLF/path
  quirks make the script exit 1 and redded the `windows-latest` matrix legs (since
  2.46.0). They're now gated on a real POSIX bash+jq (`sys.platform != "win32"`).
  Test-only; no shipped behavior change.
- **Test suite is now deterministic w.r.t. the `[embed]` extra.** A conftest autouse
  fixture pins the semantic backend OFF by default, so the keyword-path tests (recall,
  relevance, dedup — written before 3.0.0) pass whether or not `[embed]` is installed;
  the semantic tests opt back in. Without this, running `pytest` in an env that has
  `[embed]` failed five keyword-assuming tests.

### Documentation

- Documented the **versioning convention** in `CONTRIBUTING.md` (a new `## Versioning`
  section): SemVer, and a fix/CI/docs change starts at the **patch** bump, never the
  minor — `pyproject.toml` / `__init__.py` / `uv.lock` stay in lockstep.

## [3.0.0] - 2026-06-22

### Added

- **Semantic relevance layer (the 3.0.0 flagship).** A pluggable static-embedding
  backend (`omind.embed`, model2vec — numpy-only, no torch/onnxruntime) measures
  relevance by *meaning* instead of shared tokens, fixing the keyword false-positives
  the 2.45.0 graduated gate had to work *around*. Wired into three places:
  - **Recall** — `store.search` (and so the `search-vault` MCP tool) now *augments*
    substring hits with notes close in meaning, so a natural-language query no longer
    returns `[]`. `retrieve.relevant_titles` (the gate's note suggestions) ranks
    semantically too, with the credential de-prioritization preserved.
  - **Verifier** — `judge` blends semantic cosine into its `max(...)`, so an on-topic
    but keyword-poor consult is no longer flagged off-topic. (`guard verify --explain`
    now reports `semantic_score`.) This reduces friction; it is **not** anti-gaming —
    an echo of the task is similar on both axes.
  - **Dedup** — `omind note` hints (non-blocking) when a new note is close in meaning
    to an existing one, so the same insight updates in place instead of duplicating.
  - A new `omind.vectorindex` embeds note *metadata* (title + summary + tags),
    persists machine-locally, and re-embeds **incrementally** (only changed notes).
  - Optional `[embed]` extra (`pip install 'omind[embed]'`). **Everything fails open**
    to the exact 2.x keyword path when the extra is absent, the model can't load, or
    an encode raises. `OMI_EMBED_DISABLE=1` forces the keyword path; `OMI_EMBED_MODEL`
    overrides the model. `omind guard status` reports whether semantics are on.

### Changed — red-team hardenings folded in (#B1, #B2)

- **Widened the destructive deny-set** to close the regex gaps the red-team found
  (#B1): the `gh api` repo-delete rule is now argument-order-independent; raw
  `curl -X DELETE …api.github.com/repos/…` is blocked; opening a PR via
  `gh api …/pulls` is blocked (reads still pass); and `pkexec` / `doas` / `run0` /
  `su` join `sudo` in the privilege-escalation tier (same `OMI_SUDO_OK=1` opt-in).
  *Framing: this catches honest mistakes — string-matching is never a boundary
  against a determined agent; that requires controls outside the agent.*
- **Self-protection awareness** (#B2): `omind guard status` now flags when the
  guard's own config (hook, learned policy, Claude settings) is agent-writable — the
  kill-shot surface (clear the gate once, then edit the hook to disable it). The real
  mitigation (root-owned + immutable config, server-side branch protection) lives
  outside the agent and is out of scope for code.

## [2.46.0] - 2026-06-22

### Added

- **Operator pause switch for mission-critical speed — `omind guard pause [--for 30m]`
  / `omind guard resume`.** Time-boxes OFF the consult-gate and the PostToolUse verifier
  (the friction + the verifier's `claude -p` tiebreaks — the real latency/token cost; the
  gate itself calls no model), while the **hard destructive blocks stay fully on** — the
  pause is checked *after* the hard-rule layer in `guard.decide`, so a paused window can
  never green-light a repo-delete / discretionary push / raw sudo. Stores an expiry epoch
  so it **auto-resumes** (a fast window can't silently become permanent) and **fails safe**
  (an expired/missing sentinel re-arms the gate). Engagement is logged (`gate-paused`) and
  surfaced by `omind guard status`. Lives in `decide`, so every harness honours it; the
  bash hook adds a zero-subprocess fast-path for non-Bash tools.

### Changed

- **The guard now fails CLOSED on its own errors (#1).** Previously a Bash command ran
  *unchecked* whenever the adapter couldn't evaluate it — missing `jq` (`exit 0`), an
  unset `$HOME` tripping `set -u` (`exit 1`), or an unresolvable/ crashing `omind` core
  (`exit 127`) all read as non-blocking in Claude Code, silently disabling the destructive
  blocks. Now: `$HOME` is defaulted so it can't crash; missing `jq` fails **closed** for
  anything that looks like a Bash command (open, but loud, for non-destructive tools so a
  misconfigured host isn't wedged); and the Bash delegation trusts **only** a clean
  allow(0)/deny(2) from the core — any other exit code BLOCKS. A guard that fails open
  grants false confidence; the destructive path now errs toward blocking.
- **Opt-in tokens must be a real leading env assignment, not a substring (#2).**
  `OMI_PUSH_GITHUB=1` / `OMI_SUDO_OK=1` previously bypassed their hard rule if the token
  appeared *anywhere* in the command — including a comment (`sudo rm -rf / # OMI_SUDO_OK=1`)
  or a quoted string — without ever setting the variable. The opt-in is now honoured only
  when it appears as a genuine leading assignment (command start, after a `;`/`&&`/`|`
  separator, or via `env`), so a forged token can't silently skip a deny.

## [2.45.0] - 2026-06-22

### Changed

- **Graduated consult-gate: relevance is now warn-then-enforce, not block-every-time
  (#98).** The compliance log showed REQUIRE-mode's relevance re-close was near-pure
  friction on a cooperative operator — 112 `off-topic-consult` + 16
  `verify-reclose-floor` events, *every one a legitimate on-topic note* flagged by
  keyword overlap, vs **zero** bypasses caught (all 7 real denies were hard rules). So
  an off-topic consult is now a **warning** until a session's *consecutive* off-topic
  streak crosses `OMI_VERIFY_OFFTOPIC_ESCALATE` (default **5**); only then does REQUIRE
  re-close. A **relevant** consult resets the streak, so honest work — even with the
  odd keyword false-positive — never accrues to enforcement, while a sustained
  off-topic streak (the gaming pattern) still earns it. The first crossing logs
  `verify-offtopic-escalated`, so warning→enforcing is visible, not silent.
  `OMI_VERIFY_OFFTOPIC_ESCALATE=0` keeps the legacy enforce-from-first behavior; the
  per-turn re-close cap is unchanged (never a wedge). Hard-rule denials are untouched —
  that's the real guardrail, and it keeps working. (Semantic relevance — embeddings to
  cut the keyword false-positives at the source — is the planned follow-on.)

## [2.44.2] - 2026-06-22

### Fixed

- **Verifier: path noise no longer keeps a path-heavy blocked command out of a
  clean gate-clear (#97).** Follow-up to #96 (pending-intent). `overlap_score`
  divides by ALL of the pending command's tokens, so the many path components of
  a path-heavy blocked command (`prototype`, `corpus`, `bin`, `elf`, …) diluted
  the meaningful overlap into the model-tiebreaker band instead of a clean
  deterministic clear. The pending intent is now normalized before scoring
  (`retrieve.normalize_intent`): a leading `cd <dir> &&|;` is dropped and each
  path-like token is reduced to its basename, so directory components stop
  padding the denominator and a path-heavy command clears as cleanly as a
  keyword-rich one. Deterministic, no model — the recorded pending stays raw,
  only the scoring view is normalized.

### Fixed

- **Verifier no longer burns re-closes at a work-transition (#96).** #95's
  activity-blend fixed *sustained* delegated work, but the first consult of a NEW
  work-thread still re-closed: the captured task is terse/stale and the recent
  journal activity is still the *previous* thread's, so both signals are cold. The
  verifier now scores the consult against a third signal — the action the
  consult-gate just BLOCKED (the agent's freshest intent, recorded as the turn's
  "pending intent") — and judges relevant on `max(task, activity, pending)`. At a
  transition the blocked action is the new-thread work that tripped the gate, so the
  first consult clears. Zero extra cost: no model call, no extra consult — it reuses
  the blocked-action text the guard already has plus the existing overlap score. It's
  recorded on both block paths (Bash via `decide()`, non-Bash via `guard suggest`),
  reset at turn start, and surfaced as `pending_score` in `guard verify --explain`.
  It cannot weaken the gate — matching the agent's own real pending action *is*
  "consult about what you're doing", and a consult off-topic to all three is still
  irrelevant.

## [2.44.0] - 2026-06-22

### Added

- **Cross-harness: Google Gemini CLI guard (#90).** `omind setup --agent gemini`
  now wires the OMI guard into the Gemini CLI via its `BeforeTool` hook (the
  PreToolUse analog) under the `hooks` key in `~/.gemini/settings.json`, matching
  every tool (`matcher: ".*"`). On a deny the `gemini` adapter emits Gemini's
  `{"decision":"deny","reason":…}` shape on stdout (exit 0), which the CLI
  enforces as a hard tool block. `omind doctor --agent gemini` reports the guard
  wiring; the consult-gate recognizes Gemini's single-underscore MCP tool names
  (`mcp_<server>_<tool>`). Guard-only — MCP-memory registration is a separate
  concern, intentionally not bundled.
- **Cross-harness: OpenClaw guard (#88), detect-only.** `OpenClawProvisioner` now
  installs a gateway hook (`omind guard adapter --harness openclaw`) into
  `openclaw.json`; the adapter renders an `{allow,reason,rule_id}` verdict the
  POST `/hooks/agent` gateway reads. Because OpenClaw's deny-enforcement could not
  be verified against a live gateway, the harness is registered **detect-only**
  (the verdict is advisory, exit 0) until hard-block is proven — then it can be
  promoted to `hard-block` in a follow-up. `omind doctor --agent openclaw` now
  also reports the guard hook.

### Changed

- **Verifier scores relevance against what the agent is *doing*, not only the last
  user prompt (#95).** The verifier captured `turn_task` from the most recent user
  message and scored every consult against it, so when the user delegated
  background/parallel work ("build X *while* I do Y") the agent's genuinely
  on-topic consults scored off-topic against the stale user line and (under
  `OMI_VERIFY_REQUIRE=1`) re-closed the gate. The verifier now also blends in the
  agent's recent **non-OMI** journal activity for the same session and judges a
  consult relevant if it overlaps *either* signal (`max` of the two overlaps, so
  neither dilutes the other). Prior OMI consults are excluded from the activity
  blob so an agent can't bootstrap relevance by reading an arbitrary note. This is
  a distinct root cause from the 2.43.2 stemming fix (topic mismatch, not word-form
  mismatch); the activity window is tunable via `OMI_VERIFY_ACTIVITY` (default 8
  bullets). `omind guard verify --explain` now reports `task_score` and
  `activity_score` separately.

## [2.43.2] - 2026-06-22

### Fixed

- **Verifier relevance scoring no longer mis-scores a real consult as off-topic
  on word-form mismatch.** The deterministic prefilter (`retrieve.overlap_score`)
  tokenized with exact word matching and no stemming, so a consult that *was*
  about the task scored near zero purely because the task said `relevance` /
  `scoring` / `verifier` while the note said `relevant` / `scores` / `verified` —
  landing at/below the `LOW` band, judged irrelevant, and (under
  `OMI_VERIFY_REQUIRE=1`) re-closing the gate. Compounding it, instruction filler
  ("please fix … before we move any further") inflated the task's term count and
  dragged the recall-based score down. The tokenizer now folds morphological
  variants onto a shared stem (`consult`/`consults`/`consulted`,
  `score`/`scored`/`scoring`, `relevance`/`relevant`) and drops generic
  instruction filler from the task. Credential detection runs against the same
  stemmed terms, so the secrets-note de-prioritization is unaffected. The cap +
  escape hatch from 2.43.x already bounded this to *friction*; this removes the
  friction at its source so a relevant consult clears the gate on the first try.

## [2.43.1] - 2026-06-22

### Fixed

- **Consult-gate no longer deadlocks subagents whose OMI tools are deferred.**
  The per-turn gate exempted `mcp__omi__*` consults and OMI-folder Reads but not
  `ToolSearch` — yet where the OMI MCP tools are deferred (e.g. inside a Claude
  Code subagent), `ToolSearch` is the only way to load their schemas so a consult
  can happen at all. Gating it left no tool call able to clear the gate: a true
  deadlock. `ToolSearch` is now allowed through both bash adapters
  (`omi-guard.sh`, `omi-guard-hermes.sh`) and the core `decide()`, WITHOUT
  satisfying the gate (loading a schema is not a consult).

## [2.43.0] - 2026-06-22

### Added

- **The Playbook — always-on operator rules.** A curated priming file
  (`Playbook.md`) injected verbatim into every session's SessionStart context, so
  cross-cutting operating rules (sudo, secrets, forges, pull-before-you-work,
  do-it-yourself) reach a fresh instance without relying on per-turn relevance
  matching. Documented in the README under "The Playbook".
- **`fleet-sudo` wrapper**, installed by `omind setup` to `~/.local/bin`. Runs a
  command under sudo using the fleet sudo password from `pass`, resolving the
  per-host entry itself — so no agent guesses a `pass` entry or hands the user a
  command to run. Works over ssh (`ssh <host> fleet-sudo <cmd>`).
- **Guard rule `sudo-use-fleet-sudo`.** Raw `sudo` in a command is now a hard block
  that points to `fleet-sudo`; a deliberate raw sudo opts in with `OMI_SUDO_OK=1`
  (mirrors the `OMI_PUSH_GITHUB=1` Codeberg-mirror tier).

## [2.42.1] - 2026-06-21

### Fixed

- **The consult gate could permanently wedge under `OMI_VERIFY_REQUIRE=1`.** A
  terse or abstract turn task (e.g. "start picking off backlog items") scores
  near-zero keyword overlap against every note, so the verifier judged *every*
  consult off-topic and re-closed the gate after each one — an unbreakable loop,
  since no note the agent reads can raise the score. The verifier now caps
  re-closes per turn (`OMI_VERIFY_MAX_RECLOSE`, default 2); past the cap it
  degrades to WARN and lets the agent proceed, logging a `verify-reclose-floor`
  event so the blind spot stays visible rather than silent. The lazy
  single-arbitrary-read shortcut is still re-closed and logged — only a
  genuinely-stuck agent reaches the floor. A verifier must never deadlock the
  agent.
- **`omind guard reset` hung when run by hand.** It read the action payload from
  stdin unconditionally, so a bare invocation at a terminal blocked forever on a
  TTY read (Ctrl-D was the only escape). It now treats an interactive stdin as
  empty, so a by-hand recovery run returns immediately.
- **`omind guard reset` with no session id now clears every gate.** Run by hand to
  recover a wedge, it previously cleared only the `nosid` sentinel — never the
  live session's — so manual recovery silently did nothing. It now clears all
  per-turn sentinels and re-close counters. The hook path, which always carries a
  session id, is unchanged.

## [2.42.0] - 2026-06-20

### Added

- **`omind checkpoint` — scheduled recent-work recorder.** You can't reliably
  *force* a running agent to act on a wall clock (agents are turn-driven and idle
  between messages), so the robust way to "record recent work every N minutes" is
  a **scheduled job that mines the trails the hooks already capture** — not asking
  the agent. `omind checkpoint` reads the **journal** (per-action work trail) and
  the **compliance log** (cross-harness guard events), filters them to a recent
  window, and upserts a per-day **`Worklog <date>`** note with a timestamped
  section per run (one note/day — a single recent-memory slot, not a flood).
  - `omind checkpoint --since 15m` runs one checkpoint now (deterministic
    summary: action counts by tool + guard denies/violations). `--llm` adds a
    headless `claude -p` narrative, fail-open to the deterministic summary.
  - `omind checkpoint install-timer --every 15m` wires a **systemd user timer**
    (the same mechanism `omind backup`/`omind mesh` use) so it runs unattended —
    the agent's cooperation is never required, which is what makes it a real
    *force*; `uninstall-timer` removes it. `Type=oneshot`, so a failing checkpoint
    never blocks anything.

## [2.41.3] - 2026-06-20

### Added

- **Cross-harness guard: OpenAI Codex CLI (closes #59).** Codex (>= 0.117)
  adopted the Claude-Code hook schema, so the harness-agnostic guard now
  hard-blocks under Codex too. `omind setup --agent codex` writes a
  `~/.codex/hooks.json` mounting `omind guard adapter --harness codex` on both
  **`PreToolUse`** (blocks at the tool call) and **`PermissionRequest`** (the
  approval-path backstop). On a hard-rule deny the adapter emits Codex's exact
  contract — `{"hookSpecificOutput":{"permissionDecision":"deny",…}}` for
  PreToolUse and `{… "decision":{"behavior":"deny",…}}` for PermissionRequest;
  an allow is empty stdout + exit 0. Verified live against Codex 0.136.
  - A new `codex` `HarnessSpec` (`CAP_HARD_BLOCK`, `FMT_CODEX_HOOK`) + renderer;
    `omind guard selftest` now covers Codex. The adapter reuses the existing
    normalizer (Codex sends Claude-shaped snake_case `tool_name`/`tool_input`).
  - Guard-only wiring — Codex's MCP-memory registration is a separate concern.
    Codex records hooks by hash and skips untrusted ones, so the provisioner
    points the user at `/hooks` to review + trust the omind hook once.
  - `omind doctor --agent codex` reports the hooks.json guard wiring; tests
    isolate `CODEX_HOME` so they never touch a real `~/.codex`.

## [2.41.2] - 2026-06-20

### Added

- **`omind lint` — a vault health check (closes #64).** The store enforces
  structure on the *write* path, but notes also arrive by hand (Obsidian, an
  editor, a botched `--connections` split) and drift in ways no single read
  surfaces. `omind lint` walks the vault once and reports four classes of problem:
  - **broken-link** (`error`) — a `[[wikilink]]` whose target resolves to no note
    by stem or title (the exact breakage the 2.41.0 comma-split fix prevented
    going forward; this finds the ones already on disk). Resolution is
    case-insensitive and understands `[[Note|alias]]` / `[[Note#heading]]` forms.
  - **missing-title** (`warn`) — a note with no `# Title` heading.
  - **isolated** (`info`) — a note with neither inbound nor outbound links
    (orphaned from the graph; a leaf with *any* link is fine).
  - **near-duplicate** (`info`) — two notes whose titles overlap heavily.

  Reserved (`index.md`, `Memory Template.md`) and soft-deleted notes are skipped;
  links *to* reserved notes are not flagged. `--json` emits machine-readable
  issues; `--strict` exits non-zero on any issue (default: only on an `error`).
  It is read-only — it never edits a note.

## [2.41.1] - 2026-06-20

### Added

- **Verifier friction fixes + past-mistakes priming (closes #62).** The Layer-C
  relevance verifier got too strict on terse prompts under `OMI_VERIFY_REQUIRE`
  (a short task string shares few keywords with a relevant note's body, scoring
  low → re-closing the gate). Mitigations:
  - **`omind guard verify --explain`** prints the relevance score, the thresholds,
    which band it lands in, the verdict, and the notes it would suggest — so a
    false negative is debuggable instead of opaque.
  - **Tunable thresholds** — `OMI_VERIFY_HIGH` / `OMI_VERIFY_LOW` override the
    deterministic-relevant / -irrelevant cutoffs (widen the model band or the
    relevant band for short-prompt workflows).
  - **Always-relevant allowlist** — `OMI_VERIFY_ALWAYS_RELEVANT` (comma-separated
    substrings): a consult whose target matches is always relevant (never
    re-closes the gate), e.g. release/project notes you always consult.
  - **Past-mistakes priming** — the `claude -p` relevance prompt now includes this
    agent's recent off-topic consults (from the compliance log) as context.

### Fixed

- **Tests isolate the `OMI_VERIFY_*` env**, so a machine running with
  `OMI_VERIFY_REQUIRE=1` in settings.json can't leak it into the test process.

## [2.41.0] - 2026-06-20

### Added

- **Cross-harness guard — Hermes + OpenCode.** The harness-agnostic guard core now
  enforces under two more agents, not just Claude Code. A declarative
  `HarnessSpec` (`omind.harness`) describes each harness as data — capability
  (`hard-block`/`detect-only`, with graceful degradation) + block-output format —
  and a renderer emits a verdict in each harness's contract. `omind guard selftest`
  validates all three against canned events without a live harness.
  - **Hermes**: a `pre_tool_call` adapter (`omi-guard-hermes.sh`) that blocks with
    Claude-Code-style `{"decision":"block"}`; the per-turn gate resets on the
    existing `pre_llm_call` hook (Hermes' turn boundary).
  - **OpenCode**: a `@opencode-ai/plugin` (`omi-guard.opencode.js`) that throws in
    `tool.execute.before` on a hard-rule deny; installed via a new
    `OpenCodeProvisioner` (`omind setup --agent opencode`). The consult gate is not
    enforced there (its signals are unverified) — only the absolute hard blocks.
- **Guard observability + recovery (QoL).**
  - `omind guard repair` — re-provision a wedged guard hook-set (clobbered/stale
    settings hook path, `OMI_DIR` mismatch).
  - `omind guard log` / `policy` / `status` — view the compliance log, the active
    deny set, and the guardable harnesses.
  - `omind guard explain --command "<cmd>"` — dry-run a command through the policy
    (which rules it hits + the verdict) without touching the gate.
- **`omind search "<query>"`** — search the vault from the terminal.
- **`omind note --connection TITLE`** (repeatable) — comma-safe connection titles
  (the CSV `--connections` wrongly split titles containing commas).
- **`scripts/test.sh`** — run the suite in a sandboxed `HOME`/`CLAUDE_CONFIG_DIR`,
  a harness-level belt to the in-code test-isolation guard.

## [2.40.1] - 2026-06-20

### Added

- **Update nudge surfaces every session.** The "omind X.Y.Z available" notice is
  now injected into the SessionStart priming context (on top of the existing
  `omind node` stderr nudge + `omind doctor` line), so a pending update is visible
  every session instead of only once at MCP-server startup. Reuses the same
  once-a-day cached check — no extra network calls, fully fail-open.

### Fixed

- **Tests can no longer clobber the real `~/.claude`.** A provisioning test that
  didn't isolate `HOME`/`CLAUDE_CONFIG_DIR` could rewrite the developer's live
  `settings.json` to point its guard hook at a pytest temp path, wedging the OMI
  consult gate. The test suite now isolates `HOME` **and** `CLAUDE_CONFIG_DIR`
  (and disables the update-check network call), and the provisioner refuses to
  write a config/hook file outside the temp dir during a `pytest` run — so a
  mis-isolated test fails loudly instead of silently clobbering live config.
- **`omind setup` prunes stale temp-dir `Read(...)` allow-rules** that such test
  runs accumulated in `settings.json` (a real OMI vault never lives under the temp
  dir; this only removes litter).

## [2.40.0] - 2026-06-20

### Added

- **OMI-compliance enforcement: roadmap Phases 2–4.** The guard graduates from a
  blunt per-turn consult gate to a learning, relevance-aware enforcement system.
  - **Policy-as-data (Phase 2).** The deny set is now a data-driven policy
    (`omind.policy`): a `Rule` table with the destructive/forge seed rules kept
    in code (cold-start safe) and *learned* rules persisted to
    `state_dir()/policy.json`. `omind setup` scaffolds `seed-policy.json` for
    inspection. `guard.decide()` enforces the merged policy with identical
    behavior (hard blocks, github-push `OMI_PUSH_GITHUB=1` opt-in).
  - **Compliance log + violation detector (Phase 2 / Layer E).** Every policy
    deny and every post-hoc rule match is recorded to
    `state_dir()/compliance.jsonl`; the PostToolUse hook re-scans the command
    that actually ran and logs hard-rule escapes / soft-rule observations.
  - **Learning loop (Phase 2).** `omind guard learn` compiles a violation into a
    soft learned rule **and** a structured OMI note; `omind guard escalate`
    walks the soft→hard→verifier ladder by recidivism. Seed rules are immutable.
  - **Verifier — Layer C (Phase 3).** `omind.verify` judges whether the note an
    agent consulted was relevant to the turn's task, in the PostToolUse hook
    (off the PreToolUse hot path): a deterministic overlap prefilter decides the
    clear cases and only the ambiguous middle calls headless `claude -p`, failing
    open on any error/timeout/missing binary. WARN by default (logs + a stderr
    nudge naming better notes); `OMI_VERIFY_REQUIRE=1` re-closes the gate when no
    relevant consult exists. The gate sentinel now carries the turn's consults as
    JSON and `omi-gate-reset.sh` captures the prompt as the turn's task.
  - **Just-in-time relevance retrieval (Phase 3).** A gate deny now names the
    notes relevant to the turn's task (`omind guard suggest`,
    `omind.retrieve`) instead of "read any note", de-prioritizing credential/auth
    notes.
  - **Cross-harness groundwork (Phase 4).** `omind.adapters` normalizes any
    harness's pre-action event into the one `omind guard check` schema
    (`omind guard adapter`), and `omind guard export-corpus` emits the compliance
    log as fine-tuning JSONL. Wiring the adapter into the live Hermes/OpenClaw/
    OpenCode hooks, and the fine-tune run itself, remain follow-ups.
  - **Doctor.** `omind doctor` now reports policy rule counts, a compliance-log
    rollup, and whether the verifier's `claude` backend is on PATH (a `warn`, not
    a fail — the verifier fails open to deterministic-only).

## [2.39.0] - 2026-06-19

### Added

- **OMI-guard self-heal + doctor block-path check (closes #86, #87).** A machine
  running a newer omind binary than its installed guard hook-set is no longer
  silently left unprotected:
  - `omind node` self-heals on startup — when the installed OMI-compliance guard
    hook-set has drifted from what the running binary ships, it idempotently
    re-provisions the adapters (preserving the user's own hooks). Fail-open and
    stderr-only (never touches the MCP stdout channel); opt out with
    `OMIND_NO_AUTOHEAL=1`. (#87)
  - A provision manifest (`~/.claude/hooks/.omind-provision.json`) stamps the
    installed hook-set's omind version + shipped hook shas so drift is detectable
    cheaply and offline.
  - `omind doctor` gains an OMI-compliance guard block-path check: it now **fails**
    (instead of a false green) when the `omi-guard.sh` PreToolUse `*` adapter or
    the `omi-gate-reset.sh` UserPromptSubmit gate-reset is missing/unwired, and
    runs a live deny smoke test of the policy engine. (#86)

### Fixed

- **Guard gate sentinel hygiene.** `omind setup` now retires the legacy
  hand-rolled `omi-git-guard.sh` prototype (deregistered from settings.json and
  deleted from disk) so a prototype machine converges onto the shipped
  `omi-guard.sh`. The turn-start gate reset also reaps stale `/tmp/omi-gate-*`
  sentinels left by that prototype — the canonical guard uses the state dir, never
  `/tmp`.

## [2.38.0] - 2026-06-19

### Added

- **Version check + `omind self-update`.** omind now checks the running version
  against the latest on GitHub (newest Release, falling back to the highest git
  *tag* — taking the max, since tag-only releases otherwise look stale), cached
  once a day in `state_dir` and fail-open (`OMIND_NO_UPDATE_CHECK=1` disables it).
  `omind doctor` reports when you're behind, and `omind node` prints a one-line
  **stderr** nudge on start (never blocks, never touches the stdio MCP channel).
  `omind self-update` is the explicit updater: it detects the install method
  (`uv tool` / pip / editable) and reinstalls the latest tag from the public
  GitHub repo (`--check` to only report, `--force` to reinstall regardless).
  Notify-first by design — never silent auto-apply, since omind backs every
  agent's memory. Closes the gap where a pinned `uv tool` install kept serving an
  old version after a release. See `docs/self-update.md`.

## [2.37.0] - 2026-06-19

### Fixed

- **Repeated `edit-note` no longer duplicates a note's body sections.** The note
  format delimits fields with `## H2`, but the only multi-section field the
  MCP/CLI API exposes is `details` — so a structured body goes in there, its
  `## H2`s read back as `extras`, and every subsequent edit rendered *both* the
  re-supplied body and the inherited extras, stacking a duplicate of each section
  on every save (this is what corrupted a long roadmap note into three
  contradictory copies of its sections). The write path (`store.create_note` /
  `update_note`, hence every surface: MCP, web, `omind note`, Hermes upsert) now
  hoists any `## H2` out of `summary`/`details` into `extras` before render, so a
  re-supplied section *replaces* its same-named extra instead of accumulating.
  Round-trip is now stable; genuine unrelated extras are still preserved.

## [2.36.0] - 2026-06-19

### Changed

- **GitHub push relaxed from a hard block to a deliberate opt-in.** `omind.guard`
  no longer hard-denies a GitHub push; a `git push` / HTTPS-remote-set to
  github.com is allowed when the command opts in with `OMI_PUSH_GITHUB=1` — a
  deliberate mirror of Codeberg's exact commit. Impulsive/accidental github-first
  pushes are still blocked, and `gh pr create`/`merge`, `gh auth setup-git`, and
  repo-deletes stay hard. The `omi-guard.sh` adapter delegates Bash commands to
  the core, so it inherits this unchanged. Codeberg remains the source of truth.

## [2.35.0] - 2026-06-19

### Added

- **OMI-compliance enforcement guard (cross-agent core).** A new harness-agnostic
  decision engine, `omind guard check`/`reset` (`omind.guard`), is the single
  place every agent asks "may I run this action?": it hard-blocks the known
  git/forge mistakes (`gh auth setup-git`, HTTPS-GitHub pushes, `gh pr
  create`/`merge`, discretionary `git push …github`, repo deletes) and enforces
  a per-turn "consult OMI before acting" gate — with the policy in one place so a
  rule enforces identically across Claude Code, Hermes, OpenClaw, and OpenCode.
  `omind setup` installs two thin Claude Code adapters — a PreToolUse(`*`)
  `omi-guard.sh` (the per-turn gate runs in bash for speed; Bash commands
  delegate the hard-block policy to the core) and a UserPromptSubmit
  `omi-gate-reset.sh` — preserving existing user hooks, and allow-lists OMI reads
  so the gate's clear-path can never be permission-denied. Fail-open on adapter
  errors; the destructive blocks fail-closed. (First phase of a phased
  enforcement + self-learning subsystem.)

## [2.34.0] - 2026-06-18

### Added

- **`omind` Claude Code skill.** `omind setup` now installs a skill at
  `~/.claude/skills/omind/SKILL.md` (honoring `CLAUDE_CONFIG_DIR`) alongside the
  MCP server registration. The MCP server provides the memory *tools*; the skill
  teaches Claude the *procedure* — search-before-save, the single-writer `omind
  note` write path, and managing the omind CLI (`setup`/`doctor`/`node`/`mesh`).
  It's a managed file (refreshed when omind's guidance drifts, like the hook
  scripts), and `omind doctor` reports whether it's installed. The Hermes/OpenClaw
  `omind-omi-memory` skill is unchanged.

## [2.33.0] - 2026-06-18

### Added

- **Cross-agent OMI session-priming.** `omind setup` now wires session-start OMI
  priming for **Hermes Agent** (a `pre_llm_call` hook + consent-allowlist
  pre-approval) and **OpenClaw** (an omind-owned bootstrap file), not just Claude
  Code. Priming runs once per session (markers in `$XDG_STATE_HOME/omind/session-primed/`)
  and never raises — a broken priming hook must not wedge the agent.
- **Fresh-base git guard hook.** `omind setup` installs a Claude Code
  PreToolUse(Bash) guard (`~/.claude/hooks/git-fresh-base.sh`, shipped as
  package data) and registers it in `settings.json`. Before creating a branch
  off a local `main`/`master`/`develop`, it fetches and blocks the command when
  that local base is behind its `origin/*` counterpart — pushing you to
  `git checkout -b <name> origin/<branch>` instead. Idempotent, fails open on any
  error, and preserves existing user PreToolUse Bash hooks. The fetch is
  `timeout`-portable (uses `timeout`/`gtimeout` when present, else fetches
  directly), so it works on macOS where `timeout` isn't installed.

### Fixed

- **`edit-note` no longer drops non-template `##` sections.** `NoteFields` gains
  an `extras` dict so `parse_note` captures non-template sections and
  `render_fields` re-emits them after the template body, matching the mesh merge
  driver. `update_note`/upsert inherit existing extras; `TEMPLATE_SECTIONS` is
  now the single source of truth in `store`, imported by both `parse_note` and
  `merge`.

## [2.32.0] - 2026-06-14

### Added

- **Enforcement hook — OMI is now the exclusive memory system.** `omind setup`
  now writes `~/.claude/hooks/omi-enforce.py` from package data (`omind._omi_enforce`)
  and adds it to the `PostToolUse` hook entry in `settings.json`, immediately
  after the journal hook. On every tool call, any `.md` file Claude's built-in
  memory system writes to `~/.claude/projects/*/memory/` is intercepted: if a
  matching OMI note already exists (checked by title/filename in the vault), the
  built-in file is deleted; if not, the note is migrated via `omind note` first,
  then deleted. No data loss — `omind doctor` now also verifies the enforcement
  hook is wired and the script file is present. The reference copy lives in
  `extras/omi_enforce.py`.

## [2.31.0] - 2026-06-12

### Added

- **`e2e/` — a real-world testing harness on disposable VMs.** Provisions
  tiny hosts (local podman containers, or RunPod CPU pods via
  `OMIND_E2E_PROVIDER=runpod`), installs a wheel built from the working tree,
  and drives the mesh over *real node-to-node ssh*: fresh-box bootstrap,
  two-node convergence, and concurrent field-level merge. Live-validated on
  RunPod (full suite green in ~8 min, zero leaked pods). The API key is read
  via a configurable variable name (`OMIND_E2E_RUNPOD_KEY_VAR`, default
  `RUNPOD_API_KEY`); every test skips unless a provider is selected, so CI
  and plain `pytest` are untouched. See [e2e/README.md](e2e/README.md).

### Changed

- **Documentation realigned with the code** after the 2.1.0–2.30.0 train:
  mesh.md's lock-scope, list-merge, and peers-as-remotes wording;
  manual-setup.md hook examples (quoted `--folder`); troubleshooting.md's
  obsidian-mcp section rewritten as shipped history; CONTRIBUTING.md's four
  quality gates and the e2e suite in the project layout.

## [2.30.0] - 2026-06-12

### Fixed

- **transfer: bundles never carry `.omi.lock` or `.tmp-*` runtime artifacts,
  and imports skip them in old bundles.** The tar.gz export snapshotted the
  lock file; importing such a bundle while (correctly, since 2.10.0) holding
  the destination's lock made Windows raise `PermissionError` mid-import —
  caught by the Windows CI matrix on the first run of this release train.

## [2.29.0] - 2026-06-12

### Changed

- **store: listings re-parse only changed notes.** `list_notes` (and through
  it `all_tags` and every post-write index regeneration) read and parsed
  every `.md` file in the vault on every call — a 2,000-note vault paid
  2,000 reads per save and per sidebar refresh. A per-store summary cache
  keyed by `(mtime_ns, size)` makes those calls O(changed files) parses +
  O(N) stats, self-invalidating, with deleted notes pruned on each listing.
  Content search still reads file contents (it has to).

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
