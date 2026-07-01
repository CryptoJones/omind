# omind — Adversarial Code Review

**Scope:** every tracked source file under `src/omind/`, plus packaging, CI, e2e harness, shell hooks, and docs scripts.
**Posture assumed:** production infrastructure requiring 24/7 uptime with **zero false-positive interruptions**. A crash in a hook interrupts every agent turn on the machine; a false-positive guard block halts legitimate work; a fail-open bug defeats the enforcement entirely; a torn config write bricks a harness; a silent mesh stall diverges the fleet's memory.
**Method:** eight parallel reviewers, one per subsystem cluster, each instructed to read every line and report from CRITICAL down to pedantic LOW with `file:line` anchors and concrete failure scenarios. Reviewed against `main` @ `d2644ff` (fetched, up to date with `github/main`). Version lockstep verified: `pyproject.toml` = `__init__.py` = `3.7.5`.

**Tally:** 8 CRITICAL · ~38 HIGH · ~58 MEDIUM · ~135 LOW.

> Note on severity: several findings marked HIGH here (torn `omi-guard.sh`, the enforcement-migrate deletions, the backup-alert self-disable) are arguably CRITICAL for *this* threat model. They are ranked by blast radius and how silently they fail.

---

## Cross-cutting themes (read these first)

These ten patterns generate the majority of the individual findings. Fixing them at the root retires whole classes of the flaws listed below.

1. **Non-atomic writes are the default across the entire codebase.** Nearly every persistence site uses `path.write_text()` — open-truncate-write in place, no tmp-file + `os.replace`, no `fsync`, no backup. A crash / OOM-kill / `ENOSPC` mid-write corrupts the target. This bricks harness configs (`agents.py`, 15 sites; `provision.py` settings.json ×3), the guard hook itself (`provision.py` `_write_managed` → a truncated `omi-guard.sh` denies *every* tool call), the backup config (`backup.py:141`), the compliance log, and the checkpoint timer unit. `store._atomic_write` does tmp+replace correctly but skips the directory `fsync`, so even it isn't crash-durable. **This is the single highest-leverage fix in the repo.**

2. **Strict UTF-8 decoding means one bad byte takes down the whole vault.** `store.py`, `server.py`, `web/app.py`, `cli.py search`, and `compliance.py` all call `read_text(encoding="utf-8")` with no `errors=`. A single note with a stray latin-1 byte (arriving via mesh sync or an external editor) raises `UnicodeDecodeError` — not `OSError`, so existing guards miss it — and takes down all listing, search, and *writes* vault-wide until the file is found by hand. `graph.py` and `lint.py` deliberately use `errors="replace"` for the same reads, proving the hazard was understood and then applied inconsistently.

3. **False-positive guard blocks — the most-hated failure mode — are reachable through a dozen independent regex paths.** The regex-over-raw-command-text guard is riddled with anywhere-matches: `guard.py:498`'s side-effect rule ends in a bare `|>|>>`, so **any** command containing `>` (`pytest 2>&1`, `grep '->' src`, `2>/dev/null`) is a "side effect"; `guard.py:453`'s global-config rule matches **project-local** `<repo>/.claude/settings.json`, not just `~/.claude/`; `policy.py:101`'s destructive/forge seed rules (`gh repo delete`, `gh auth setup-git`) fire on the substring anywhere, so `grep -rn "gh repo delete"` or a commit message mentioning it is hard-blocked — routine when working on omind itself. `guard.py:463`'s auth list matches "make"/"change" anywhere (so "don't change anything" *authorizes* a mutation) while missing "fix"/"add"/"create" (so real imperatives are blocked). `guard.py:476` reads the polite imperative "Could you please commit this?" as a *question* and refuses to act. `retrieve.py`'s stemmer and `len(w) > 2` filter drive dead-on consults to **0 relevance**; `lint.py` flags every dated `Worklog` pair as "100% similar" so `lint --strict` exits 1 forever; `doctor`'s block-path self-test **fails during a legitimate `omind guard pause`**. Most damning: **`guard.py:449`'s freshness check only registers a bare, exact `git fetch` — `git -C <repo> fetch` and `git fetch --all --prune && git status` (the *exact* command the block message tells you to run) never clear it**, producing the re-block loop observed live throughout this very review session.

4. **The enforcement layer has fail-open holes in exactly the spots that must fail closed.** `guard.py:668`: a single learned/hand-edited rule with an invalid regex (or one matching the empty string, e.g. a trailing `|`) makes **every** `omind guard check` crash → omi-guard.sh fails closed → every tool call blocked with an unsatisfiable "consult OMI" message (the consult path can't fix a crashing regex loop). `adapters.py:92`: malformed/truncated event JSON → `guard._load` returns `{}` → empty action → **no hard rule can match** → a destructive command is waved through with no log. `secret-output-guard.sh:75`: the "was it redirected?" test matches a bare `2>/dev/null`, so `pass show X 2>/dev/null | head` leaks the secret to the transcript while the guard says OK. `verify.py:192`: a contentless `mcp__omi__list-notes`/`graph-*` call both clears the gate and always scores "relevant" — a verifier-proof gate-dodge. `policy.py:55`'s command-position anchor misses `then`/`exec`/`xargs`/absolute paths, so `if true; then sudo rm -rf /; fi` sails past the hard sudo rule. `provision.py`'s `hookset_drift()` compares a manifest to package data, never to disk, so a truncated guard script reads as "no drift".

5. **The mesh can stop replicating silently — the worst failure because nobody notices.** Tombstones never expire, so re-creating a note with a purged filename gets deleted fleet-wide on the next sync (`mesh.py:525`). The gitignore omits Obsidian's `workspace.json`, whose recurring text-merge conflict makes `_merge_ref` abort the *entire* peer merge every cycle (`mesh.py:62`). `diagnose_mesh` reports every fetched peer "ok" no matter how divergent and never reads the recorded per-peer errors (`mesh.py:945`). Push timeouts abort the remaining peers' pushes and skip the sync-state write; inbox merge failures are logged and dropped.

6. **Read-then-write outside the lock defeats concurrency safety throughout.** `server.py` edit-note, `web/app.py`, `notes.py` upsert, `checkpoint.py`, and `create_note`'s TOCTOU all read, transform, then write without holding the lock across the whole operation and without passing `expected_version`. The `(content, version)` pair is read non-atomically, so the optimistic-concurrency token can vouch for content the caller never saw — silently reverting a concurrent session's edit in exactly the race it exists to prevent. POSIX `flock` has no timeout, so one wedged holder freezes every session on the machine.

7. **The enforcement-migrate path deletes memory files.** The shipped `_omi_enforce.py` (installed as a PostToolUse hook) deletes a memory file when ≥2 of its first 3 slug words appear in *some* other OMI filename — no content comparison, no migration — and deletes any file lacking a `name:` frontmatter key outright. It also hardcodes the default vault path, so a user who ran `--vault /data/notes` gets built-in memories migrated into the wrong, unreplicated vault and the source deleted. The comment claims this path "guarantee[s] no data loss."

8. **Cron/timer paths fail silently forever.** `checkpoint.py`'s generated `ExecStart` falls back to a non-absolute `omind`, which systemd rejects — the timer fires and fails every interval while the install reports success. `write_checkpoint` is called with no exception handling despite the module contract "Never raises into a timer." `self-update` runs its subprocess with no timeout and no post-install verification, and `OMIND_NO_UPDATE_CHECK=1` (a privacy knob) silently disables *explicit* `self-update` too.

9. **Secrets leak into replicated, world-readable files.** `hooks.py:183` journals the first 80 chars of every Bash command — including inline `Authorization: Bearer …` / `PGPASSWORD=…` — into a `0644` Markdown journal the git mesh then replicates to every peer. `fleet-sudo.sh:37` leaves the sudo password on stdin when credentials are cached, so `fleet-sudo tee /etc/x` writes the password into the file. The compliance log is `0644` and stores command lines. `transfer.py` bundles the entire `.git/` (every hard-purged note's history) into "migration" tarballs.

10. **Packaging/CI blind spots let a bad release reach the whole fleet.** No macOS in the test matrix despite a macOS fleet. The wheel is never built or smoke-tested, so a change that drops the `.sh`/`.js` hook scripts or fonts from the package ships green and crashes on installed nodes. Runtime deps are unpinned with no upper bound and the fleet self-updates from untagged `main` HEAD, so a breaking upstream release lands everywhere overnight.

---

## CRITICAL

### store.py:222 — Parse/render round-trip silently destroys YAML frontmatter and all pre-`##` content
`split_sections` drops every line before the first `## ` heading (other than the `# Title` line), and `render_fields` never re-emits them. An Obsidian note with YAML frontmatter (`---`/`tags:`/`---`, which Obsidian's Properties UI adds routinely) or any prose between the H1 and the first `##` loses that content permanently on the next `update_note`, `upsert_note`, or mesh merge (`merge_note_texts` renders the same way). Silent, unrecoverable loss of hand-curated memory on the primary edit path.

### agents.py:438 — Every harness-config write is a non-atomic in-place truncate
All fifteen write sites use `path.write_text(...)` with no tmp-file + `os.replace`, no `fsync`, no backup (438, 479, 551, 600, 661, 714, 753, 841, 932, 1073, 1102, 1296, 1340, 1403, 1474). A kill/OOM/`ENOSPC` between truncate and flush leaves the user's `config.yaml`/`settings.json`/`config.toml`/`mcp.json`/`AGENTS.md` empty or torn, bricking that harness. One setup run does this up to three times back-to-back per harness.

### provision.py:721 — settings.json rewritten with a plain truncate-and-write; runs unattended from autoheal
`ensure_hooks_installed` (721), `ensure_guard_hook_installed` (774), and `ensure_omi_guard_installed` (915) all end in `path.write_text(json.dumps(...))`. A crash mid-write leaves a truncated `~/.claude/settings.json`; Claude Code then discards the whole file — every omind guard hook *and* every user permission/hook silently vanishes for all sessions until repaired. The same code runs from `autoheal_on_startup` at every `omind node` start, so several sessions launching together race each other and Claude Code's own permission writes — last stale writer wins, user config lost.

### mesh.py:525 — Tombstones are forever; re-creating a purged note is silently deleted fleet-wide
`_apply_tombstones` runs every sync and unlinks any top-level `.md` named in `.omi-tombstones`, but tombstone lines are never expired (the file is `merge=union`, accumulate-only) and `create_note`/`upsert_note` never consult it. Purge "Pluto Setup.md", then months later save a new note titled "Pluto Setup": sync commits it, `_apply_tombstones` unlinks it, the deletion propagates to every node — no error, no log, no report entry. The content survives only in git history, which nothing surfaces.

### mesh.py:62 — Gitignore omits Obsidian's volatile files; `workspace.json` merge-conflicts abort peer merges forever
`GITIGNORE = ".omi.lock\n.tmp-*\n"` is the entire list, `_commit_locked` runs `git add -A`, and seeds.py deliberately makes the folder an openable Obsidian vault. Obsidian constantly rewrites `.obsidian/workspace.json` differently per machine; it gets no merge driver, so two nodes with Obsidian open produce a real text conflict, `_merge_ref` runs `git merge --abort`, and the **entire** peer merge — notes included — fails every cycle. Replication between those nodes stops indefinitely while doctor still shows the peer "ok".

### transfer.py:304 — targz import writes into `.git/`, corrupting repo state — even without `--force`
`_import_targz` filters only top-level `index.md` and runtime artifacts by basename; every `.git/*` member passes the traversal guard (it's inside the OMI dir) and reaches `_classify_and_write`. Missing files are *added* without `--force`: a bundle's `.git/refs/heads/main` silently repoints `main` at foreign history, an added `.git/packed-refs` injects `refs/omind/<source>` that the next mesh sync merges and pushes fleet-wide, and into a fresh folder the entire foreign `.git` (config, remotes, hooks) lands wholesale and is later adopted by `mesh_init`.

### guard.py:668 — One bad learned rule in policy.json bricks every tool call on the machine
`decide()` runs `rule.compiled().search(command)` over every loaded rule with no `try/except`, and neither `learn.learn_violation` nor `policy._rule_from_dict` validates that a pattern compiles. A learned/hand-edited rule with an invalid regex makes every `omind guard check` crash (exit 1), which omi-guard.sh treats as "policy not evaluated" → fail-closed for Bash **and** (via the suggest fallback) for every non-Bash tool. The block message says "consult OMI", but the OMI-consult path can never unblock a crashing regex loop. A pattern that matches the empty string (a trailing `|`) is equally fatal: it hard-blocks every non-Bash action (`command:""`). `compliance.record_post_tool` catches `re.error`; the hot path that actually blocks does not.

### secret-output-guard.sh:75 — Any `>` token counts as "redirected", so `pass show X 2>/dev/null | head` leaks the secret
The rule-3 escape test `'>[[:space:]]*/dev/null|>>?[[:space:]]*[^|&[:space:]]'` matches the stderr redirect `2>/dev/null` (and any `>file` anywhere in a compound command) and allows the whole command — while stdout still pipes straight into the transcript. The exact burned-PAT scenario the header cites (`pass show … | head`) passes the guard the moment the agent appends the habitual `2>/dev/null`. Fail-open of the secret-output control under an extremely common idiom.

---

## HIGH

### Enforcement core (guard.py / policy.py / verify.py / shell hooks)

- **guard.py:449 — Freshness only registers for a bare, exact-form `git fetch`, causing a re-block loop.** `_GIT_FRESH_ONLY_RE` requires the *entire* command to be `git fetch…`, so `git -C <repo> fetch --all --prune` (the `-C` breaks `git\s+fetch`) and `git fetch --all --prune && git status -sb` — literally what the block message instructs — never record freshness, and the next Read re-blocks with the same instruction. Observed live throughout this review: only the bare command cleared the check.
- **guard.py:498 — `_SHELL_SIDE_EFFECT_RE`'s bare `|>|>>` makes any `>` a "side effect".** `2>/dev/null`, `grep '->' src`, `pytest -q 2>&1`, a `=>` inside an echoed string — all classified as side-effect actions. Same defect in `_GLOBAL_MUTATING_BASH_RE` (line 488): read-only `jq . ~/.claude/settings.json 2>/dev/null` is classified as a global-config *mutation*.
- **guard.py:463 — `_GLOBAL_AUTH_RE` word-anywhere match both false-authorizes and false-blocks.** Authorization is any of `make|modify|edit|write|install|update|change|patch|apply|do it|go ahead|proceed|send it` anywhere in the prompt: "don't change anything yet" silently authorizes a global mutation, while common true imperatives — "fix", "add", "create", "remove", "set up" — are absent and get blocked.
- **guard.py:476 — `_CAPABILITY_QUESTION_RE` treats polite imperatives as questions and misses addressed questions.** "Could you please commit and push this?" is classified as a capability question and blocked until the user types "do it"; conversely "Dixie, can you update the hook?" is *not* matched (anchored at text start), falls through to `_GLOBAL_AUTH_RE`, matches "update", and the mutation proceeds on a question.
- **guard.py:453 — `_GLOBAL_CONFIG_RE` matches PROJECT-local `.claude/settings.json`.** The prefix class accepts any preceding slash, so `/home/u/myproject/.claude/settings.json` — an ordinary version-controlled project-settings file — triggers the "global config mutation" rule and blocks the Edit absent turn authorization. The regex cannot tell `~/.claude/` from `<repo>/.claude/`.
- **guard.py:445 — The repo-sensitive net is so wide that reading any file in any repo needs a note-read plus a network fetch every turn.** `_READ_REVIEW_TOOLS` makes every Read/Grep/Glob/LS repo-sensitive when a `.git` ancestor exists, and `_REPO_TEST_RE` sweeps in `python`/`make`/`go` at command position. A `$HOME` dotfiles repo makes *every* home-dir Read repo-sensitive; the git-meshed vault makes every non-OMI vault Read demand a vault `git fetch`. A per-turn network fetch before read-only work is a standing availability tax that fails ugly offline.
- **policy.py:101 — Destructive/forge seed rules match ANYWHERE (default `match="search"`), false-blocking greps/commit messages/docs.** Unlike the sudo tier (command-position anchored), `gh-repo-delete`, `gh-auth-setup-git`, `gh-api-repo-delete`, `curl-api-repo-delete` fire on the substring anywhere: `grep -rn "gh repo delete" src/`, `git commit -m "docs: forbid gh auth setup-git"`, or a heredoc writing this README hard-block — routine when working on omind itself. The fix was applied to only the sudo tier.
- **verify.py:192 — `list-notes`/`graph-*`/`list-tags` both clear the gate and always score "relevant".** Any non-search `mcp__omi__*` tool becomes `("read", "")`; `_consult_text` returns `""`; `judge_with_activity` fails open on empty text → recorded `relevant=True`, off-topic streak reset. A contentless listing is a permanent, verifier-proof gate-dodge — the loophole the NON_CONSULT_FILENAMES machinery calls "load-bearing", reopened through the MCP path.
- **loopguard.py:56 — The loop guard is machine-global: arming one session traps every concurrent session.** `loop_guard.json` has no session scoping, and `run_hook` calls `register_block()` on every session's Stop; with one session armed for a `/loop`, an unrelated Claude session on the same machine has its stops refused and is fed "DO NOT STOP… execute the next task NOW" for up to 24 h.
- **omi-guard.sh:143 — Non-Bash core failure is fail-closed with a misleading "consult OMI" message.** Any non-0/2 exit from `omind guard check` (the regex crash above, OOM, broken venv) sends every Read/Edit/Grep to the suggest fallback and exits 2 with "consult OMI before acting this turn" — a remediation that cannot succeed. The operator sees an infinite consult loop instead of "guard core is broken".
- **omi-gate-reset.sh:17 — Turn reset omits `pending-$sid` and `git-fresh-$sid`, so freshness is per-session, not per-turn.** The Claude turn-start hook clears only `gate-` and `reclose-`, while the Python `begin_turn` also clears pending intent and git freshness. Under Claude Code the "same-turn freshness check" is actually once-per-session — one fetch at 9 am satisfies "freshness" for commits at 6 pm (fail-open vs the stated control).
- **secret-output-guard.sh:42 — `pass` read pattern has no word boundary; matches inside words and strings.** `pass[[:space:]]+[A-Za-z0-9_.@-]+/` matches "…bypass proxy/8080…", `git commit -m "pass tests/unit"`, `grep "pass shadow/" file` — any occurrence of "pass" followed by a slashed token, anywhere. Rule 3 then blocks these as un-redirected secret reads.

### Enforcement / provision / migrate

- **provision.py:378 — Torn `omi-guard.sh` hard-blocks every tool call, and no check can detect it.** `_write_managed` writes the live PreToolUse `*` hook non-atomically. A crash mid-rewrite leaves a truncated bash script; bash exits 2 ("unexpected end of file"), and PreToolUse exit 2 = *deny* — every tool call in every session is blocked. `hookset_drift()` compares the manifest to package SHAs, never to disk, so autoheal reports "no drift" and doctor (which checks only `is_file()`) reports healthy while the machine is bricked.
- **provision.py:378 — PermissionError on hardened files is an unhandled traceback, not a graceful error.** The documented hardened deployment (immutable `~/.claude/hooks/*`, root-owned settings) makes `write_text` raise `PermissionError`; nothing converts `OSError` → `ProvisionError`, and `cli.py:540` catches only `ProvisionError`, so `omind setup` on a hardened box dies with a raw traceback half-provisioned.
- **provision.py:565 / :573 — Enforcement hook migrates into the wrong vault and deletes memory files.** The PostToolUse entry passes no `--vault`/`--folder`, and the installed `_omi_enforce.py` hardcodes `VAULT = HOME/"Documents/Obsidian Vault"`, `--folder OMI`, `OMIND = HOME/.local/bin/omind`. A user who set a custom vault gets built-in memories migrated to the wrong, unreplicated vault and the source deleted. Separately, `omi_exists` deletes a memory file on a fuzzy ≥2-of-3-slug-word filename match with no content comparison, and deletes any file lacking a `name:` key outright — despite the comment claiming "no data loss."
- **agents.py:1393 — Codex path deletes a user's own `[mcp_servers.obsidian]` table with no ownership check.** `del servers[LEGACY_SERVER_NAME]` runs unconditionally, unlike `_drop_legacy_entry` which at least requires an `obsidian-mcp` substring. A user who ran `codex mcp add obsidian …` themselves loses it silently.
- **agents.py:1455 — JSONC configs (VS Code, Kiro, OpenCode, Gemini) fail strict `json.loads` with misleading advice.** These harnesses accept comments/trailing commas; omind's `_read_settings` uses bare `json.loads` and hard-fails setup with "Fix or remove it" — advice that, if followed, destroys the user's comments or config.
- **agents.py:390 — Unreadable configs raise raw uncaught `OSError`.** `_read_config`/`_read_toml_config` catch only parser errors; no write site handles `OSError`. A root-owned/chmod-444 config (common post-hardening) aborts setup half-done with a traceback. `install_bootstrap` shows the correct `except OSError → ProvisionError` pattern that everything else omits.

### Storage / concurrency

- **store.py:227 — Section splitting is code-fence-blind, scrambling notes that contain fenced `##` examples.** A `##` line inside a triple-backtick block is treated as a section boundary; on round-trip the fence is split and the note is structurally corrupted. `_split_field_headings`, `_metadata_line_edit`, and the merge driver share the blindness — highly realistic for a programmer's vault.
- **store.py:547 — Case-sensitive reserved-filename check destroys `index.md` on macOS/Windows.** `"Index.md"` passes `_reject_reserved`; on a case-insensitive FS `upsert_note("Index", …)` matches the existing `index.md`, and `_atomic_write` replaces the vault's table-of-contents with note content — the exact catastrophe the docstring warns about. Same hole for "Memory Template".
- **store.py:575 — Dot-prefixed titles create notes invisible to every listing, search, and index.** Title ".NET migration notes" → ".NET migration notes.md", which `_note_paths` skips forever (`startswith(".")`), while `create_note` reports success — a saved memory that can never be recalled.
- **store.py:594 — One non-UTF-8 note breaks every listing, search, and write in the vault.** `UnicodeDecodeError` isn't caught by `_cached_summary`'s `OSError` guard, and every write calls `_write_index` → `list_notes` inside the lock, so writes throw *after* the note file was replaced — the store half-completes (note written, index stale) while all reads are down.
- **store.py:846 — `create_note`'s duplicate check is outside the write lock (TOCTOU).** Two concurrent sessions creating the same title both pass `if path.exists()`, and `write_note` overwrites unconditionally — the second silently destroys the first's note.
- **merge.py:226 — Equal revs with different content make `merge(A,B) ≠ merge(B,A)`; the mesh never converges.** `ours_wins = o_rev.sort_key() > t_rev.sort_key()` is False on both sides when revs are equal, so each node keeps the other's value and successive syncs swap forever — violating the module's own symmetry contract. Reachable via any unstamped mutation (`_migrate_index_descriptions`, a node whose config load failed).
- **merge.py:336 — The merge driver eats frontmatter and pre-section content from all three inputs.** `merge_note_texts` rebuilds output purely from `render_fields` + `##`-keyed extras, so YAML frontmatter and pre-`##` prose vanish even when base/ours/theirs all carry it identically — contradicting "the driver must never eat hand-curated content."
- **filelock.py:39 — POSIX lock blocks forever while Windows raises after ~10s.** `fcntl.flock(LOCK_EX)` has no timeout, and the same `.omi.lock` is held across an entire mesh fetch/merge/regenerate cycle (network git, 600s proc timeout). One `SIGSTOP`ped process or hung mount blocks every MCP write on the machine with no error and no escape.

### Server / web / semantic (false-positive & availability)

- **server.py:160 / web/app.py:70 — One undecodable note 500s search/list vault-wide and blanks the UI.** `store.search`/`list_notes` read with strict UTF-8 and lack the `OSError` guard `_cached_summary` has; a note deleted mid-scan also 500s the whole search.
- **app.js:284 — Stored XSS: note HTML is rendered unsanitized through marked v15 into `innerHTML`.** The vendored marked 15.0.12 has zero sanitization and the output lands in `contentEl.innerHTML`. Notes are agent-written and mesh-synced, so a prompt-injected `<img src=x onerror="fetch('/api/notes')…">` executes with same-origin access to the full CRUD API — it can exfiltrate or rewrite the entire vault the moment the user opens the note. Titles/tags are carefully `escapeHtml`'d, then the whole body is passed through raw.
- **graph.js:242 — Synchronous O(n²) settle loop freezes the tab before first paint.** `while (alpha > 0.02 && guard++ < 600) step()` runs an all-pairs repulsion loop on the main thread; at 10k notes that's ~259 × 10⁸ pair updates synchronously — a hard "page unresponsive" freeze on every Graph click. Multi-second freeze even at 2k notes.
- **retrieve.py:97 — Single-pass stemmer produces divergent stems, driving legit consults to 0 relevance.** `embed`→`emb`, `embedding`→`embedd`, `embeds`→`embed`; `verified`→`verif` vs `verification`→`verific`. Since `overlap_score` is the verifier's only score without the `[embed]` extra, the right note can score 0 from word-form mismatch alone — the false-positive block the module claims to prevent.
- **retrieve.py:121 — `len(w) > 2` discards every 2-letter tech term.** "fix the CI" tokenizes to `{fix}`; "CI pipeline setup" shares zero overlap → `overlap_score` = 0 → a dead-on consult judged irrelevant, unrecoverable without an embed backend.

### CLI / cron / update

- **cli.py:847 — `omind checkpoint run` has no exception handling on the cron-facing path.** `write_checkpoint` → `upsert_note` raises `NoteError`/`OSError` on an unwritable vault, reserved-name collision, etc. From the systemd timer this is an unhandled traceback every 15 minutes — contradicting "Never raises into a timer." `_run_note` correctly catches it.
- **checkpoint.py:107 — Minute-truncation vs second-precision cutoff permanently drops boundary actions.** Journal bullets carry only `HH:MM` (floored to `:00`) while `cutoff` keeps real seconds, so an action at 12:00:45 is rejected by both the window that should contain it and the next — excluded from *every* checkpoint forever.
- **checkpoint.py:252 — `ExecStart` falls back to a non-absolute `omind`, creating a timer that fails forever.** systemd requires an absolute `ExecStart`; when omind isn't on PATH (`python -m omind` installs), the unit is invalid, fires-and-fails every interval, and the install reports success (systemctl output swallowed).
- **update.py:150 — `OMIND_NO_UPDATE_CHECK=1` disables explicit `self-update` with a lying error.** The privacy env var (meant to disable the passive nudge) short-circuits `check_for_update(force=True)`, so `self-update` prints "could not reach GitHub (offline…)" and exits 1 even when online; `--force` doesn't help.
- **lint.py:144 — Near-duplicate check flags every pair of dated periodic notes.** `Worklog 2026-06-29` and `Worklog 2026-06-30` both tokenize to `{worklog, 2026}` → Jaccard 1.0 → "titles 100% similar". `omind checkpoint` creates exactly one such note per day, so after a month lint emits ~435 bogus issues (O(N²)) and `lint --strict` exits 1 forever.

### Packaging / CI

- **pyproject.toml:33 — Unpinned runtime deps with no upper bounds on a fleet that self-updates from git.** `uv tool install git+…` resolves fresh from PyPI and ignores `uv.lock`, so a breaking `mcp`/`fastapi` release lands on every machine at the next self-update overnight with no cap.
- **bootstrap.sh:97 — `omind` can land off-PATH; the script dies mid-provision.** On macOS (default: `~/.local/bin` not on PATH) with a pre-existing uv, `omind setup` exits 127 under `set -e`, leaving a half-bootstrapped node (installed, never provisioned).
- **test.yml:22 — No macOS in the test matrix for a Linux+macOS fleet.** macOS-specific breakage (BSD userland in hooks, PATH behavior, case-insensitive-FS vault paths) ships to production with zero CI signal.

### Hooks / mesh / backup (availability & data)

- **mesh.py:650 — A push timeout aborts the remaining pushes and skips the sync-state write.** `run_command` raises on `TimeoutExpired` regardless of `check=False`; one hung peer makes the `MeshError` escape `sync()`, skipping all later peers and leaving the state file stale. Fetch is defended against this; push is not.
- **mesh.py:945 — Doctor marks every fetched peer "ok" and never reads recorded sync errors.** A peer that merge-conflicts every cycle keeps "syncing recently", so `mesh_sync` is "ok" while the fleet diverges — the nobody-notices failure mode.
- **mesh.py:47 — 120s cap on fetch/clone/push with no resume permanently wedges catch-up.** A node offline for days (stated as normal) or a first clone over WAN may need >120s; git doesn't resume, so every retry restarts from zero and times out again — that peer never syncs again.
- **backup.py:306 — The BACKUP FAILING alert is permanently disarmed after its first recovery on a mesh node.** `delete_note` soft-disables (leaves `Disabled: true`); the next `upsert_note` inherits the flag, so the re-written alert stays hidden from listings/search/priming forever. The one mechanism meant to surface a dying backup silently stops after the first fail→recover cycle.
- **backup.py:288 — rsync fallback has no retention and leaves partial snapshots indistinguishable from complete ones.** Dated dirs accumulate until the disk fills; an interrupted rsync leaves a *partial* newest-timestamp dir, so the operator restores the latest and gets a truncated vault.
- **fleet-sudo.sh:37 — Cached sudo credentials leave the password on the command's stdin.** `sudo -S` reads stdin only when it prompts; with cached creds the child inherits the pipe holding `<password>\n`, so `fleet-sudo tee /etc/x` writes the fleet sudo password into the file, and stdin-reading commands ingest it.
- **transfer.py:153 — targz export bundles the entire `.git/`.** Full history (including hard-purged notes), node identity, and remote URLs leak out of any "migration" bundle; this is also what makes the CRITICAL import finding reachable.

---

## MEDIUM

### guard / adapters / harness / verify
- **adapters.py:92 — Malformed event JSON silently degrades to an empty action, bypassing all hard rules.** `guard._load` returns `{}` on any parse failure → `command=""` → no hard rule matches → a destructive command is waved through with no log, in the component whose entire job is to block.
- **adapters.py:59 — Array-shaped `args` are silently dropped.** `_first_str` accepts only a non-empty string, so `"args": ["repo","delete","a/b"]` yields `command=""` and hard rules never see the payload — despite the docstring claiming tolerance of `args` variations.
- **harness.py:71 — `spec_for` silently falls back to the Claude exit-2 contract for unknown harness names.** With `--harness` having no `choices=`, a typo like `gemeni` renders denies as exit-2 — but Gemini/Codex ignore exit codes and parse stdout JSON, so the denies become no-ops with zero diagnostic.
- **guard.py:210 — Gate sentinel written non-atomically and read-modify-written without a lock.** Parallel PreToolUse/PostToolUse hooks can read a truncated file (→ `{}`, consults lost) or clobber each other's consult records — losing the `relevant:true` record the REQUIRE-mode verifier and `_has_consulted_git_rules` depend on, re-blocking legitimate work.
- **guard.py:172 — Git-freshness state is single-slot per session; multi-repo work thrashes.** `git-fresh-<sid>.json` holds one repo; fetching repo B overwrites repo A, so a session alternating between the code repo and the meshed vault re-blocks and re-fetches on every switch.
- **guard.py:700 — Operator pause silently disables controls that print as "(hard)".** `gate_paused()` returns allow before the global-config-auth and repo-work checks, so those "omi-guard (hard)" rules are off during a pause — while `_run_pause` tells the operator "HARD destructive blocks stay ON". The capability-question check runs *before* the pause, an undocumented inconsistent ordering.
- **guard.py:419 — Per-session state files accumulate forever; `clear_all_gates` misses `turn-*`.** Up to six files per session id are never reaped, and the recovery sweep omits `turn-*` — so `turn-<sid>.txt` files containing full raw user prompts pile up unboundedly with no deletion path.
- **guard.py:517 — `\benv` in `_opt_in_satisfied` lets the sudo opt-in be forged inside a string.** `echo "use env OMI_SUDO_OK=1" && sudo rm -rf …` satisfies the opt-in and skips the sudo hard block — contradicting the docstring's guarantee that a token forged in a string must not skip the deny.
- **policy.py:55 — `_CMD_POSITION` misses shell keywords/wrappers, bypassing the sudo/privesc hard rules.** `then`, `do`, `exec`, `nohup`, `command`, `xargs`, `time`, and absolute paths aren't treated as command position, so `if true; then sudo rm -rf /; fi`, `xargs sudo`, and `/usr/bin/sudo x` sail past the hard sudo rules — fail-open via ordinary shell forms.
- **policy.py:203 — Loader accepts uncompilable patterns and wrong-typed fields.** `_rule_from_dict` never verifies the pattern compiles (feeding the guard.py CRITICAL) and passes a wrong-typed `severity` straight through, after which `rule.severity != SEVERITY_HARD` silently demotes an intended hard rule to non-blocking.
- **verify.py:434 — REQUIRE-mode re-close destroys the turn's consult evidence, including the git-rules read**, so every repo-sensitive action re-demands a fresh note read even though it was read this turn.
- **verify.py:16 — Doc lie: an unreadable note does NOT fail open.** The docstring promises "unreadable note fails open (treated relevant)", but `_consult_text` falls back to the note *name*, which is scored normally; a short name lands IRRELEVANT → nudge/streak/possible re-close.
- **verify.py:248 — Nested headless `claude -p` (15 s default) inside the PostToolUse hook.** Every ambiguous-band consult synchronously spawns a full nested Claude session (which fires its own SessionStart hooks/journal writes) while the harness waits on PostToolUse.
- **verify.py:145 — Activity signal reads only today's LOCAL-date journal; vanishes at midnight.** A session spanning midnight loses all pre-midnight bullets, so on-topic consults start scoring off-topic against a cold task — exactly during long overnight loops.
- **omi-guard.sh:80 — `*"$OMI_DIR"*` substring match lets a prefix-sibling directory clear the gate.** A Read of `…/OMI-Archive/x.md` or `…/OMI backup/x.md` counts as an OMI consult — fail-open, inconsistent with the Python side's `parents` containment.
- **omi-guard.sh:33 — In the no-jq fallback, the block message's own advice cannot clear the gate.** `guard adapter` marks consults only by `mcp__omi__` tool prefixes, so a Read under the OMI folder is never a consult in this mode — yet the deny text says "or Read any file under the OMI folder."
- **omi-guard-hermes.sh:18 — `set -u` with no HOME default: unset HOME kills the whole guard, fail-open.** The Claude adapter fixed exactly this ("Default it so the guard can't be silently disabled"); the Hermes copy never received the fix.
- **omi-guard-hermes.sh:22 — Missing jq disables ALL Hermes enforcement (hard blocks included) with no warning**, unlike omi-guard.sh's #107 handling — the same policy has opposite failure postures per harness (Claude fail-closed, Hermes fail-open).
- **omi-gate-reset.sh:13 — Missing jq skips the reset entirely: the per-turn gate silently becomes per-session**, and `turn-$sid.txt` is never captured so the verifier judges every consult task-less.
- **omi-guard.opencode.js:25 — Non-gate, non-clearable rules pass the throw filter; safety rests on tool-name casing luck.** The plugin throws on any deny with `rule_id !== "omi-gate"` — including `repo-work-*` and `capability-question-*`, none satisfiable under OpenCode. They can't fire only because OpenCode reports lowercase `bash`/`read`, which fails guard.py's case-sensitive checks; if casing ever normalizes, OpenCode sessions wedge permanently.
- **secret-output-guard.sh:64 — `credential.helper` substring anywhere bypasses the entire guard.** `pass show gh-YOLO | head  # credential.helper` is allowed; the combined `git config credential.helper '…' && pass show x` prints the secret — plausible precisely when configuring git auth.
- **secret-output-guard.sh:35 — Missing jq silently disables the guard; the reassuring comment is wrong.** No omind policy rule covers secret-output, so no-jq means zero protection — but the comment claims "omi-guard fails-closed for Bash separately."
- **secret-output-guard.sh:73 — sed substitution-stripping is line-based; a multi-line captured read false-blocks.** `TOK=$(\n  pass show x\n)` leaves `pass show x` as a "bare" read and gets blocked despite being captured.
- **extras/omi_enforce.py:1 — Full duplicate of `src/omind/_omi_enforce.py` with no sync mechanism.** Byte-identical except type annotations; a fix in one copy silently misses the other, and all `_omi_enforce.py` defects (fuzzy-match deletion, slug-less deletion, timeout-less subprocess, unwrapped `unlink`) apply here at the same lines.

### agents.py
- **:319 — `_drop_legacy_entry` deletes a user's own hand-installed obsidian-mcp server on substring match.** String matching can't distinguish "omind installed this" from "user installed this".
- **:1211 — Codex trust hash uses `ensure_ascii=True`, diverging from serde_json for non-ASCII paths.** A home dir with non-ASCII makes the computed `trusted_hash` mismatch Codex's, so the guard hooks are silently treated as untrusted and skipped — enforcement disappears with no error.
- **:1328 — `install_bootstrap` destroys user text in AGENTS.md when a marker is orphaned.** A deleted END line makes the next run append a second block; the run after replaces everything from the orphan START through the new END, deleting all user text between.
- **:973 — Docstring says hook trust "can't be scripted," then `install_hook_trust` scripts exactly that**, bypassing the harness's consent control.
- **:1030 — Legacy hooks.json migration drops non-list shapes, then `install_guard` deletes the originals.** User root-level hook data that was never merged is permanently deleted — contradicting "without losing user-authored hook groups."
- **:468 — `HOOK_MARKER` literal matching misses Windows `omind.EXE`, duplicating priming hooks on every re-run.** provision.py fixed this with `_HOOK_COMMAND_RE`; agents.py never adopted it.
- **:526 — Hermes guard hook is registered even when the guard script was never written.** `_write_guard_script` swallows the failure and returns; `install_guard` registers the path anyway → every Hermes tool call invokes a nonexistent script.
- **:661 — agents.py writes skip the `_guard_test_isolation` protection provision.py writes have** — the exact recorded "twice rewrote this machine's live omi-guard.sh" failure mode, unguarded in the newer code.
- **:336 — `shutil.which("omind") or "omind"` wires a bare command the harness may not resolve, yet `verify()` still reports success** (it compares the config against the same computed value).
- **:125 — `opencode_config_path` ignores the documented `opencode.jsonc` variant**, creating a second competing config.
- **:82 — OpenClaw preference order probes the *oldest* legacy name (`.clawdbot`) before the newer `.moltbot`**, contradicting the stated rename chronology.
- **:311 — Agent memory skills are write-if-absent while Claude's is `_write_managed`**, so Hermes/OpenClaw/OpenCode installs never receive skill fixes (re-creating the issue-#49 failure mode).
- **:1351 — A UTF-8 BOM makes every parser fail with a misleading "not valid" error** on a semantically fine config (no reader uses `utf-8-sig`).
- **:438 — No backup-before-modify anywhere** despite wholesale rewrites of nine harness configs.

### provision.py
- **:1009 — autoheal read-modify-writes settings.json concurrently with live sessions**; lost updates drop user config (see CRITICAL companion).
- **:954 — Settings entries installed even when the hook-script write failed**, wiring hooks to nonexistent files → hook errors on every tool call.
- **:121 — Manifest write failures silently suppressed → perpetual autoheal thrash on hardened installs**, all failures swallowed by a blanket `except Exception: return`.
- **:135 — `hookset_drift` never inspects what's on disk** (manifest vs package data), so a truncated/deleted guard script reads as "no drift."
- **:144 — Version check is bare string inequality**; a downgrade or two coexisting installs stomp/ping-pong the hook-set and manifest on every node start.
- **:886 — Temp-dir permission prune silently deletes the user's own `Read(/tmp/…)` allow rules** on every setup and autoheal.
- **:219 — Stale legacy `~/.claude/.claude.json` fallback makes setup run `claude mcp remove` for a server the CLI doesn't have** → `check=True` non-zero → `ProvisionError` → setup aborts before any hooks install.
- **:1296 — Doctor's block-path smoke test spuriously FAILS during a legitimate `omind guard pause`**, telling the operator the guard is broken when it's working as configured.
- **:320 — `SetupConfig` never expands `~`/env vars**; setup scaffolds under a literal `./~/Vault` while hooks journal into the expanded path — a split-brain vault the mesh never replicates.
- **:542 — `shutil.which("omind")` at setup bakes a possibly ephemeral interpreter path into every hook and the MCP registration** (breaks after a `uvx`/project-venv prune).
- **:946 — No uninstall path exists**; `pip uninstall omind` leaves a PreToolUse `*` hook and three `omind hook` commands pointing at a missing binary — every tool call then fires failing hooks.

### hooks.py
- **:183 — First 80 chars of every Bash command (including inline credentials) are journaled into a mesh-replicated `0644` file.** No redaction pass.
- **:533 — PostToolUse hook can synchronously spawn a `claude -p` model call**, stalling the agent up to 15s per consult — contradicting the "hot-path cheap" design.
- **:456 — An unwritable state dir makes `_already_primed` return False forever**, re-injecting the full (48k+ char) priming payload on every Hermes turn.

### store / notes / journal
- **store.py:424 — `_metadata_line_edit` edits the first matching line anywhere in the file** while parsing is Metadata-scoped; a Details bullet `- Disabled: true` makes `disable_note` "succeed" while the note stays listed, and `restore_note` deletes that body line.
- **store.py:993 — `_migrate_index_descriptions` mutates content without bumping the Lamport rev** → same-rev/different-content divergence (feeds merge.py:226).
- **store.py:464 — `_configured_node_id` swallows every exception**, silently degrading a mesh vault to unstamped writes and misordered LWW.
- **store.py:67 — `_atomic_write` never fsyncs the directory** (rename not durable on power loss) and strands `.tmp-*.md` litter on SIGKILL.
- **store.py:740 / server.py:71 / web/app.py:84 — No atomic `(content, version)` read**; the token can vouch for content the caller never saw, silently reverting a concurrent edit.
- **store.py:223 — A UTF-8 BOM defeats title detection**; the H1 is silently dropped and round-trips as a bare `# `.
- **store.py:574 — No case/Unicode normalization or length limit on filenames** → cross-platform mesh collisions, cross-note clobbering ("a/b" and "a:b" → same file), and raw `ENAMETOOLONG` on long titles.
- **store.py:389 — `_hoist_field_headings` doesn't exclude template sections**, producing duplicate `## Summary` headings (the known duplicate-H2 family).
- **store.py:653 — Every search and every cold-store write re-reads and re-parses the entire vault**; `upsert_note` builds a fresh cold-cache `OmiStore` per call → ~10k reads per write at 10k notes, holding the vault-wide lock.
- **notes.py:28 — upsert read-merge happens outside the lock with no `expected_version`** (lost update); upserting a soft-deleted note reports "updated" but the memory stays invisible; the create-or-update decision is TOCTOU.
- **journal.py:111 — migrate's exists-check-then-rename can silently destroy a journal a hook just created** (hook path takes only a per-file flock, not `.omi.lock`).
- **journal.py:275 — Re-rolling a week overwrites a previously archived same-day daily** from a late-synced offline peer.

### mesh / transfer / backup
- **mesh.py:606 — Inbox-ref merge failures are logged and dropped** — excluded from the report, from `report.ok`, and from sync state.
- **mesh.py:296 — Seed post-receive stalls every push behind a synchronous `git push --mirror`** that force-deletes refs absent on the seed and repoints an adopted repo's `main`.
- **mesh.py:502 — `--allow-unrelated-histories` on every merge** lets one wrong remote URL splice a foreign repository into the vault and replicate it fleet-wide.
- **mesh.py:602 — Sync blocks forever on the store lock** (POSIX flock, no watchdog); systemd sees a live process so `Restart=on-failure` never fires.
- **transfer.py:121 — Export reads the vault without the store lock** — a TOCTOU snapshot (note A post-merge, note B pre-merge), not point-in-time.
- **transfer.py:97 — JSON export silently drops `Journal/` and every subfolder** despite claiming to export "the entire OMI dataset" — journal history lost on migration with no warning.
- **backup.py:255 — A timeout-killed restic leaves a stale repo lock** that fails every subsequent backup until manual `restic unlock`.
- **backup.py:363 — `restic restore latest` is not scoped by host/path**, so verify can check another machine's snapshot in a shared fleet repo (spurious warning, or silently verifies the wrong machine).
- **backup.py:207 — init partial failure wedges** (passfile created before `restic init`; a failed init makes re-running refuse with a false "would overwrite snapshots" message).
- **backup.py:141 — `save_config` is a non-atomic `write_text`**, and a torn `backup.json` disables both backups and the failure counter — the escalation machinery needs the file that broke.
- **backup.py:255 — restic runs with no excludes and no store-lock coordination** — captures `.omi.lock`/`.tmp-*` and mid-merge states.

### server / semantic
- **server.py:157 — Sync tool handlers run full-vault blocking I/O on the MCP event loop**; each graph tool rebuilds the whole graph from disk per call (5 separate full-vault parses, no caching) → seconds-long loop stalls at 10k notes.
- **server.py:130 / :71 — edit-note reads fields outside the write lock**, reopening the lost-update race `_mutate_note` exists to close; read-note's `(raw, version)` pair is non-atomic.
- **web/app.py:67 — No auth, no Host validation, no CSRF defence on a fully destructive JSON API.** localhost binding doesn't stop DNS rebinding; `--host 0.0.0.0` is accepted with no warning. A `TrustedHostMiddleware` allowlist is one line.
- **web/app.py:71 — Shared `OmiStore` mutated concurrently from FastAPI's threadpool.** The SPA fires `/api/notes` and `/api/tags` together; both mutate `_summary_cache` while the other iterates it → `RuntimeError: dictionary changed size during iteration` → sporadic 500s under the 5s poll.
- **graph.js:217 / :24 — `destroy()` doesn't remove the `window` mouseup listener** (leaked per render), and graph fetch has no `res.ok` check → unhandled rejection with the pane stale.
- **app.js:703 / :746 — `render`'s destroy handle is discarded** (every Graph click leaks a permanent rAF loop + observers), and the 5s poll `JSON.stringify`s the entire note list twice per tick.
- **graph.py:90 — Title/stem collisions silently resolve every link to the last-parsed note**, so graph tools and `backlinks` disagree about the same vault.
- **retrieve.py:78 — `"pass"` in `_CREDENTIAL_TERMS` both disarms and misfires the credential safeguard on ordinary English** ("make the tests pass").
- **retrieve.py:164 — Tags compared raw against stemmed task tokens**; hyphenated/inflected tags (`consult-gate`, `embeddings`) never match, so the double-weighted tag channel contributes nothing.
- **retrieve.py:196 — `_semantic_titles` returns `[]` (not `None`) on an empty index**, suppressing the keyword fallback so the gate names no notes even when keyword ranking would.
- **vectorindex.py:37 — `_safe` hashes the *unresolved* path** (docstring says "resolved"), so symlink vs real path get two indexes and two different vaults opened as `"."` share one index → `rank()` returns filenames from the wrong vault.
- **vectorindex.py:91 / :139 / :94 — `_save` swallows `OSError` then `_ranked` re-reads from the unwritten file** (silent empty rankings + full re-embed per query); `refresh()` re-parses the whole vault with a cold store on every query; a fixed temp filename races concurrent refreshers into a truncated JSON publish.
- **embed.py:96 — First uncached use triggers an unbounded-network model download inside a gate decision** (guard hooks are short-lived, so the module cache resets every invocation → every guarded action re-pays a hub attempt on a flaky network).

### cli / checkpoint / compliance / learn / corpus
- **cli.py:759 — `omind search` crashes on one non-UTF-8 note.** `cli.py:1030 — guard hardcodes `"OMI"` and has no `--vault/--folder`**, so on a non-default folder `guard learn/verify/suggest` reads the wrong directory. **cli.py:379 — `checkpoint --llm`/`--since` are silently dropped by `install-timer`.**
- **checkpoint.py:108 — A malformed journal bullet (`27:30`) crashes every subsequent checkpoint** (`day.replace` `ValueError`). **:123 — Windows longer than a day silently truncate to two journals.** **:127 — The guard-event filter has no upper bound and `TypeError`s on tz-aware timestamps.** **:147 — `_llm_narrative` misses `UnicodeDecodeError` and ignores the exit code** (adopts an error message as the day's narrative). **:184 — read-compose-write outside the lock loses sections**; a transient read failure rebuilds the note from one section. **:159 — Empty windows still write**, churning an idle machine forever. **:236 — `_systemctl` has no timeout** (hangs on a wedged user D-Bus). **:259 — `Persistent=true` catch-up still summarizes only the last window**, leaving downtime gaps.
- **compliance.py:100 — One bad byte permanently crashes every consumer** (`UnicodeDecodeError` isn't `OSError`), despite "Skips bad lines; never raises." **:96 — Unbounded append-only log, fully re-read by every consumer** every 15 minutes.
- **learn.py:147 — Escalation counts all-time recidivism with no window or decay**; three soft observations *ever* permanently convert a rule to a hard block, and the log is never pruned so the count only grows.
- **corpus.py:56 — The training corpus is 100% DENY and mislabels allowed actions** (even soft-rule `observed` matches the guard allowed at runtime) → teaches a degenerate always-deny judge.

### quickstart
- **quickstart.py:206 — `--agent codex|gemini|opencode` prints Claude Code wiring** under a banner claiming "exactly what `omind setup` would do" — wrong instructions for 3 of the 10 advertised agents.

### update
- **update.py:82 — Tag-prefix asymmetry**: accepts bare `3.8.0` tags but always installs `@v3.8.0`, so a `v`-less tag makes every fleet self-update fail. **:239 — The update subprocess has no timeout** (a hung clone wedges the whole update pass). **:206 — No post-install verification; the pip path can leave a partial/absent install** (`--force-reinstall` uninstalls before installing). **:214 — Explicit self-update uses the 2s nudge timeout**, failing on a slow-but-working link.

### packaging / CI
- **bootstrap.sh:42 — Fleet installs track un-tagged `main` HEAD**, not a SemVer release. **:70 — Unpinned `curl | sh` uv installer with truncation risk.**
- **test.yml:39 — CI only ever installs editable; the wheel the fleet installs is never built or smoke-tested** (a dropped hook script ships green). **test.sh:17 — `exec` defeats the EXIT trap**, leaking the sandbox dir every run.
- **conftest.py:93 — `HERMES_HOME` is not isolated** (bare `pytest` on a machine with it set points Hermes tests at the real state dir).
- **dependabot.yml:4 — `pip` ecosystem never regenerates `uv.lock`**, breaking the lockstep rule. **gitleaks.toml:12 — Wholesale directory allowlists blind the scanner** to a real key pasted into `e2e/providers.py`. **nodes.py:72 — Node install keyed on `curl` alone**; git/python3 can stay missing.

---

## LOW (pedantic nits, dead code, doc lies, style hazards)

### guard / adapters / harness / verify
- adapters.py:83 — `omi_dir` parameter is dead (accepted, documented, never used); the `Path` import exists only to type it.
- adapters.py:65 — single-underscore consult prefix false-positives on `omi_*`-named servers (`mcp_omi_backup_fetch`).
- adapters.py:78 — `consult_kind` heuristic mislabels `create-note`/`edit-note`/`list-notes` as `"search"`, and anything containing "read" (`spread`) as `"read"`.
- adapters.py:98 — output streams hardwired to `sys.stdout`/`sys.stderr` despite the injectable input stream.
- adapters.py:92 — cross-module use of the private `guard._load`.
- harness.py:99 — unknown Codex events rendered with a hardcoded `hookEventName: "PreToolUse"`.
- harness.py:158 — "side-effect free" selftest claim holds only while every canned command stays a hard rule.
- harness.py:89 — `import json` repeated function-locally in five branches.
- guard.py:548 — a SEARCH whose query contains the note title satisfies the "read the note" requirement (substring check over consult targets).
- guard.py:1086 — self-protection surface omits `secret-output-guard.sh` and `omi-gate-reset.sh` (both agent-writable, both able to disable a control).
- guard.py:172 — freshness write/check asymmetry: a sessionless action can pass via a real "nosid" session's fetch but can never record its own.
- guard.py:258 — `/tmp/omi-gate-*` legacy reap runs on every turn-reset/re-close forever; on multi-user hosts other users' files are re-attempted each turn.
- policy.py:83 — `compiled()` recompiles every pattern on every guard invocation; nothing caches the compiled regex (hot-path latency).
- policy.py:154 — neither sudo rule covers `sudoedit` (no boundary before "edit"); `privesc-alternatives` omits it too.
- verify.py:241 — `_parse_verdict` reads "Note:" / "Not sure, but RELEVANT" as IRRELEVANT (`low.startswith("no")`).
- verify.py:169 — `_past_mistakes_context` full-scans the unbounded compliance log per tiebreak.
- verify.py:185 — `_under` fallback is a substring test (`…/OMI-backup/…` counts); `Path(target)` can raise `ValueError` on an embedded NUL.
- loopguard.py:131 — cross-session `reset()` zeroes the shared block counter, so the no-work auto-disarm backstop is inert under concurrency.
- loopguard.py:110 — `_expired` catches only `ValueError`; a corrupt `expires_at` parses as "never expires", keeping the guard armed on corrupt state.
- loopguard.py:149 — `int(state.get("blocks", 0))` `TypeError`s on JSON `null`.
- _omi_enforce.py:17 — hardcoded `~/.local/bin/omind`; where omind resolves elsewhere, `migrate` always fails but MEMORY.md and slug-less files are STILL deleted (pure deletion).
- _omi_enforce.py:93 — vault-wide glob per memory file on every PostToolUse (O(vault size) work appended to every tool call).
- _omi_enforce.py:44 — tab-indented YAML parsed as top-level keys (can clobber `name`/`description`).
- omi-guard.sh:143 — header claims "Fail-open on adapter errors"; the script is fail-closed almost everywhere (doc lie for debuggers).
- omi-guard.sh:58 — dead `SENT=` variable and a comment describing a removed pure-bash sentinel fast path.
- omi-guard.sh:47 — Bash detection greps the raw event, so in the doubly-degraded mode a Write whose *content* contains `"tool_name": "Bash"` is fail-closed blocked.
- omi-guard.sh:130 — pause fast-path skips the capability-side-effect check the core applies before its own pause.
- omi-guard.sh:18 — `HOME=/tmp` fallback diverges from Python's pwd-based home resolution (state-dir disagreement).
- omi-guard-hermes.sh:72 — broken/missing OMIND lets destructive commands run unguarded under Hermes (unconditional `exit 0`).
- omi-guard-hermes.sh:28 — dead `SENT=` assignment · :59 `read_file` consults recorded with `tool:"Read"` (mislabeled).
- omi-gate-reset.sh:16 — `$HOME` under `set -u` with no default crashes the hook when HOME is unset (no reset runs + banner every prompt).
- omi-gate-reset.sh:24 — `turn-$sid.txt` written by non-atomic truncate; a concurrent reader sees an empty/partial prompt at turn boundaries.
- omi-guard.opencode.js:39 — all non-block errors swallowed with zero logging (a wrong `__OMIND_BIN__` silently disables the guard, undiagnosable — even a `console.error` is absent).
- secret-output-guard.sh:40 — `OMI_SECRET_OK=1` override is an anywhere-substring match (forgeable in a comment/string), the exact hazard guard.py's opt-in was rewritten to prevent.
- secret-output-guard.sh:56 — dead `|ghu` alternative (the bracket class already contains `u`).

### agents.py
- :3 stale module docstring (claims only Hermes+OpenClaw; actually 10 targets) · :392 "not valid YAML" message wrong for valid-but-tagged YAML · :511 chmod failure on the guard script silently suppressed · :704 OpenClaw bootstrap path list accumulates stale entries after a root rename · :707 setup re-enables entries the user deliberately disabled · :828 non-list hook containers silently replaced across four adapters · :430 user customizations inside the omind-owned entry (`env`) clobbered · :319 `json.dumps(legacy)` can `TypeError` on YAML-typed dates · :1047 same `desired` dict aliased into two event lists · :1065 `data.get("hooks") != hooks_cfg` is dead when hooks already exists · :1217 Codex trust keys positional; stale entries never pruned · :1252 `_hook_trust_installed` vacuously true when hooks.json is unreadable · :1285 trust-entry rewrite drops foreign fields Codex stored · :1374 string-valued `args` explodes into characters · :171 `CODEX_HOOK_EVENTS` duplicates `CODEX_HOOK_EVENT_STATE_KEYS` · :193 `GEMINI_HOME` honoring is an unverified assumption · :338 hook command interpolation shell-unsafe for exotic paths · :354 Hermes/OpenClaw `verify()` checks only MCP registration, not hooks · :923 OpenCode `register_mcp` skips `_drop_legacy_entry`.

### provision.py
- :548/:565 hook command quoting incomplete (`$`/backtick/`"`/space); `python3` hardcoded · :246 ownership markers are substring matches that can claim user entries · :703 user hooks appended inside omind's entry dropped wholesale · :704 merge re-appends omind's entries at the end, reordering on every drift · :701 malformed non-list hooks value silently discarded · :721 settings.json created 0644 and ASCII-escaped · :893 `desired_read` literal duplicated · :818 manifest stamped "current" even when a resource failed to install · :45 hook destinations ignore `CLAUDE_CONFIG_DIR` · :644 redundant mkdir · :925 verify greps `"Connected"` out of CLI output · :1206 `_diagnose_hooks` reports only the first problem · :284 20s network guard budgeted onto every Bash tool call.

### hooks.py
- :352 "whole payload capped" is false (static sections uncapped, ~64k possible) · :339 SessionStart priming does a synchronous network version check · :453 primed-marker check races; the marker dir grows unbounded · :417 "Never raises" but the build call sits outside the try · :507 bullet timestamp and journal filename from two separate `now()` calls (midnight straddle) · :268 `finally` unlocks even when the lock was never acquired · :262 POSIX journal lock has no timeout · :264 `os.write` return values unchecked · :58 shipped priming defaults include an owner-specific note name.

### store / notes / filelock / merge / journal / clock / paths / proc
- store.py:781/:815 dead reserved-filename checks · :230 duplicate `## Heading` silently coalesced · :643 sort key reverses title tiebreak, trusts free-text dates · :74 tag regex truncates at dots (`#v1.2`→`v1`) · :187 `today()` uses local date across a multi-tz mesh · :959 titles with `[[`/`]]` produce broken index wikilinks · :698 `_semantic_recall` swallows every exception · :513 lock file 0644 breaks shared-group vaults · :569 NUL bytes escape as `ValueError` not `NoteError` · :587 `glob("*.md")` misses `.MD`/`.Md` on case-sensitive FS.
- notes.py:27 — sanitization collisions let one title hijack another note.
- filelock.py:11 "held for milliseconds" is a doc lie · :3 no NFS caveat · :31 Windows `unlock_fd` raises when the region isn't held.
- merge.py:6/:240 "converges without losing data" overstates LWW; the log message is wrong for equal revs · :312/:351 extras re-emitted in sorted() order (reorders authored layout) · :290 `_strip_blank` duplicates `store._strip_blank_edges` · :175 conflict-marker blocks containing `##` are dismembered on the next parse.
- journal.py:114 bullets appended without the hooks' flock (mid-line interleave) · :234 retention cutoff uses naive local `now()` · :236 vault-wide lock held across the whole rollup tally · :31/:108 cross-module use of store privates · :243 an invalid `week` argument is a silent no-op.
- clock.py:22 — out-of-charset node ids silently demote stamped revs to "unversioned"; `Rev.parse("007@n")` re-renders as `7@n`.
- paths.py:20 `RESERVED_FILENAMES` is exact-case · :41 `JOURNAL_GLOB` also matches rollup files · :46 a relative `XDG_STATE_HOME` is honored, making the sync-signal path CWD-dependent.
- proc.py:53 timeout kills only the direct child, not its process group · :53 stdin inherited when `input_text` is None · :47 Windows `which()` resolves against the parent's PATH · :65 error messages join argv without quoting.

### mesh / seeds / transfer / backup / fleet-sudo / git-fresh-base
- mesh.py:70 no `GIT_TERMINAL_PROMPT=0`/BatchMode (interactive prompt burns the timeout) · :612 a typo'd `--only` peer syncs nothing and reports ok · :635 `ps.merged = True` set even when no merge was attempted · :525 `_apply_tombstones` takes an unused `store` param · :548 `conflict_scan` false-positives on notes quoting markers; never scans subfolders · :952 doctor's ahead/behind uses `check=True` and can raise uncaught · :800 systemd ExecStart: unquoted `omind_exe`, unescaped `%` · :768 daemon catches only `MeshError` → 30s crash loop on anything else · :299 seed `main` chosen by committer date (clock skew → stale clone) · :277 peer URL never validated (`ext::` transport executes commands).
- seeds.py:248 CODEX bootstrap template hardcodes one person's identity and private note titles · :16 seeding a live Obsidian vault guarantees volatile files the mesh neither ignores nor merge-routes · :59 `{{summary}}`/`{{details}}` aren't Obsidian placeholders and survive literally.
- transfer.py:285 malformed JSON entries skipped silently (partial import reads as success) · :283 `store` param shadowed by a dead reassignment · :206 `result.conflicts and not force` is a dead condition · :198 import never touches the sync signal (waits out the full mesh interval) · :255 `_atomic_write_bytes` fsyncs the file but not the directory.
- backup.py:132 `int(...)` raises uncaught `ValueError` on a non-numeric `consecutive_failures` · :366 restored-sentinel path reconstruction wrong on Windows · :312 an `OSError` from `save_config` inside `_record_failure` replaces the real error · :412 timer ExecStart: unquoted `omind_exe`, unescaped `%`.
- fleet-sudo.sh:22 entries never validated; a failing `pass show` pipes an empty password to sudo (pam_faillock lockout risk) · :28 hardcoded fleet-specific pass entries · :25 `tr -d '[:space:]'` mangles entries with internal spaces · :29 probe loop can trigger up to four pinentry prompts · :9 "works remotely too" overstates it.
- git-fresh-base.sh:58 macOS fetch has no timeout wrapper; the `GIT_TERMINAL_PROMPT=0` claim is wrong for network stalls · :32 regexes parse the raw command string (false blocks on commit messages) · :47 a failed `cd "$cwd"` is ignored (evaluates the wrong repo) · :36 long-form/flagged branch creations bypass the guard · :58 hardcoded `origin` + unconditional network fetch on every guarded command.

### server / web / semantic
- server.py:205 `direction` never validated (typo silently means "both"); negative depth returns `[]` · :8 docstring says the server touches the sync-signal file; it doesn't anymore · :74 read-note resolves `safe_name` and reads the file three times per call.
- web/app.py:119 `update_note_raw` exists-check-then-write TOCTOU · :88 `get_note` echoes the caller's name instead of the canonical filename · :112 update endpoints don't return the new version token · :91 `get_backlinks` re-reads every note per note open.
- app.js:361 `openNote` has no stale-response guard (rapid clicks render the wrong note) · :273 wikilink substitution runs before markdown parsing (corrupts code blocks) · :699 `#graph` hash set but never cleared · :583 dead branch in new-note cancel · :241 conflict-overwrite retry can clobber a third write · :903 `state.mesh` races the first delete (wrong confirm text).
- graph.js:86 `fit()` on an empty vault poisons the transform with NaN · :222 wheel zoom is unclamped · :75 `dpr` captured once and never refreshed.
- graph.py:62 `resolve("")` matches the first untitled note · :113 `_adjacent` treats any unrecognized `direction` as "both" · :82 unreadable notes vanish from the graph with no trace · :204 `_dot_quote` doesn't escape newlines.
- retrieve.py:157 `_tokens(task)` recomputed per note in the ranking loop · :149 trailing-slash paths un-reduced · :85 `_CD_PREFIX_RE` strips only one leading `cd`.
- embed.py:109 `OMI_EMBED_DISABLE=0` disables semantics (truthy-string check) · :110 disabled-path overwrites `_last_error` on every call.
- vectorindex.py:106 index only contains non-archived notes (archived-inclusive semantic search returns none) · :148 corrupt vector elements raise `TypeError` out of `_ranked` · :111 `assert isinstance` disappears under `python -O` · :143 empty query string is embedded rather than short-circuited.

### cli / checkpoint / compliance / learn / corpus / update
- cli.py:67 top-level metavar omits 8 subcommands · :30 eager import of `omind.agents` (yaml+tomlkit) on every invocation · :863 `omind note` can block forever reading stdin · :1042 bare `omind` exits 0 after printing help · :986 no `KeyboardInterrupt` handling · :806 inconsistent exit-code semantics for empty results · :311 `lint --json` vs `graph export --format json` · :600 imports the private `_doctor_symbols`.
- checkpoint.py:69 `parse_since` silently maps garbage to 15 min · :72 huge `--since` raises `OverflowError` · :96 window doc says `(cutoff, now]` but code is `[cutoff, now]` (double-count) · :259 raw strings interpolated into the systemd unit · :249 `--every 0` produces `OnUnitActiveSec=0s` · :280 success logged unconditionally · :290 `uninstall_timer` unlink races and raises · :196 section cap miscounts on `### ` lines · :210 naive local datetimes.
- compliance.py:117 `recidivism()` is dead code and inconsistent with `recidivism_counts()` · :85 log created world-readable (0644) · :88 `os.write` return unchecked · :89 fd leaked if `unlock_fd` raises.
- learn.py:154 verifier branch can report a `hard → hard` "escalation" · :52 `_slug` discards all non-ASCII · :39 `note_action=None` conflates "skipped" with "failed".
- corpus.py:62 deleted rules silently get a generic training target.
- update.py:223 `--check` exit code carries no signal; `--check --force` lies · :190 install detection breaks under `UV_TOOL_DIR`/pipx · :126 a backwards clock jump makes the cache fresh forever · :57 `_parse` matches version prefixes (`3.8.0rc1`→`(3,8,0)`).

### packaging / CI / e2e / docs / extras
- pyproject.toml:42 `e2e` extra can't run the e2e suite (no pytest) · :52 `dev` is an extra not a dependency-group.
- bootstrap.sh:36 `--help` prints past the usage block and breaks when piped · :70 `curl` never checked as a prerequisite.
- test.yml:33 no dependency caching · :55 gitleaks pinned by mutable tag, nothing updates it · :42 `pip-audit` gates every PR on the unpinned tree · :86 conformance job degrades to a permanent silent no-op.
- gitleaks.toml:13 allowlist regexes unanchored.
- nodes.py:154 `note_digests` breaks on an empty vault (unexpanded glob) · conftest.py:38 wheel-build failures swallow diagnostics · :30 deprecated `item.fspath` · providers.py:167 `podman port` parsing depends on last-line luck · :253 `_wait_for_ssh_endpoint` never checks pod failure state · sweep.py:23 sweep kills pods of concurrent runs.
- conformance.toml:9 hardcoded POSIX `.venv/bin/omind` relative to CWD.
- woodpecker.yml:14 Codeberg CI runs `pytest -v` with no coverage (fail_under never enforced) · :7 single Python + mutable image tag.
- codeql-config.yml:8 `paths-ignore: tests` is dead config · :6 SAST scope excludes shipped `extras/`/`e2e/`.
- extras/omi_write.py:38 source-tree fallback can't rescue a partially broken install · :83 non-TTY stdin with no writer hangs forever.
- _omi_enforce.py:1 shipped hook runs under system python3 (3.9 floor via builtin-generic annotations, no `from __future__`).
- docs/graph-demo/make_demo_vault.py:15 `"Then"` duplicated in STOP set · :10 bare `sys.argv` indexing · :35 `random.choice` crashes on no mid-length lines · render_graph.py:23 titles interpolated into DOT without escaping · :8 unchecked `sys.argv`.

---

## Recommended remediation order

1. **One atomic-write helper, used everywhere** (tmp + `os.replace` + directory `fsync` + optional `.bak`). Route all of `agents.py`, `provision.py`, `backup.py`, `compliance.py`, and the checkpoint timer through it. Retires the 4 write-corruption CRITICAL/HIGH findings and dozens of downstream ones.
2. **Decode with `errors="replace"` on every vault read** (`store.py`, `server.py`, `web/app.py`, `cli.py`, `compliance.py`). Retires the "one bad byte downs the vault" class.
3. **Preserve frontmatter and pre-`##` content, and make section splitting fence-aware** in `store.split_sections`/`render_fields`/`merge_note_texts`. Retires the CRITICAL data-loss round-trip and the merge-driver eat.
4. **Make the mesh observably fail** — surface per-peer errors in `diagnose_mesh`, gitignore `.obsidian/workspace.json` (or add a merge driver), expire tombstones, and stop aborting the whole merge on one file. Retires the two mesh CRITICALs and the "nobody notices" HIGHs.
5. **Fix the false-positive gate paths — this is the "zero false-positive interruptions" requirement directly.** Anchor the anywhere-matching guard regexes at command position (`guard.py:498` bare `>`, `guard.py:453` project-vs-global `.claude/`, `policy.py:101` forge/destructive seed rules); make the freshness check accept the `-C` and compound forms it tells the agent to run (`guard.py:449`); rework the auth/capability-question heuristics (`guard.py:463`/`:476`); fix stemmer divergence and the `len(w) > 2` cutoff in `retrieve.py`; the dated-note duplicate flag and archived/journal broken-link errors in `lint.py`; and the doctor block-path test firing during a pause.
6. **Harden the guard against its own crash and close the fail-open holes.** Wrap `rule.compiled().search()` in `try/except re.error` and validate patterns at load time so one bad learned rule can't brick every tool call (`guard.py:668`/`policy.py:203`); fix the secret-output `>`-means-redirected and no-word-boundary `pass` matches (`secret-output-guard.sh:75`/`:42`); make `adapters.py` fail closed (not open) on parse failure and handle array-`args`; make `verify.py:192` not treat contentless listings as relevant consults; extend `policy.py:55`'s command-position anchor to shell keywords/wrappers; and make `hookset_drift` inspect disk rather than package data.
7. **Harden the migrate/delete paths** — `_omi_enforce.py` must compare content and migrate before unlinking, honor the configured vault, and never delete on a fuzzy filename match.
8. **CI: build+smoke-test the wheel, add macOS to the matrix, pin an upper bound on runtime deps, and tag releases the fleet installs by tag.**
