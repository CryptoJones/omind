# omind install verification matrix

Agent-executable acceptance tests for a **new omind install** (or a periodic
drift audit of an existing one). Every row is a command an agent can run
unattended plus a machine-checkable pass criterion.

`omind doctor` is the fast smoke test — it covers roughly group A–C by
inspection. This matrix exists because doctor checks *presence*, not
*behavior*: a hook can be wired and still not block, an MCP server can be
registered and still not answer. The behavioral groups (D, E, F) are the ones
that catch real drift.

## Safety rules for the executing agent

1. **Never run destructive commands to test the guard.** Use `omind guard
   explain --command '<cmd>'` (pure dry-run, nothing executes) and `omind guard
   selftest` (canned events). No row in this matrix may execute `sudo`, `gh repo
   delete`, or a raw API DELETE.
2. **Write tests go to a throwaway vault**, never the real OMI folder:
   ```sh
   export TESTVAULT="$(mktemp -d)/vault"; mkdir -p "$TESTVAULT/OMI"
   ```
   Pass `--vault "$TESTVAULT" --folder OMI` on every write/index/mesh row.
   Rows marked **RO** are read-only and may run against the live vault.
3. **Do not `omind guard pause`** as part of testing, and do not leave the guard
   paused. If `guard status` reports PAUSED, that is a finding (see D0), not a
   precondition to satisfy.
4. Record every row as PASS / FAIL / SKIP with the observed output. A SKIP needs
   a reason (optional dependency absent, no mesh peers configured, etc.).

---

## A. Binary and environment

| ID | Command | Pass criterion |
|----|---------|----------------|
| A1 | `command -v omind` | Resolves; path is on `PATH` for non-interactive shells too (`zsh -lc 'command -v omind'`) |
| A2 | `omind --version` | Prints a version; matches the intended release. A SessionStart banner advertising a newer version than `--version` reports is drift |
| A3 | `omind help` | Exits 0 and lists all subcommands: setup, quickstart, node, mesh, serve, doctor, backup, export, import, reindex, convert, note, consolidate, search, bench, lint, graph, recover, checkpoint, ai, rollup, hook, loop, guard |
| A4 | `command -v claude git jq` | All three present — `claude` backs the verifier model, `jq` is the guard fast path, `git` backs mesh/merge |
| A5 | `omind help <sub>` for each subcommand in A3 | Every subcommand has authoritative help and exits 0 (no `NotImplementedError`, no empty body) |

## B. Wiring — what `omind setup` is supposed to have installed

| ID | Command | Pass criterion |
|----|---------|----------------|
| B1 | `omind doctor` | Exit 0, **zero problems**. Warnings triaged individually against this table |
| B2 | `omind doctor` line "MCP server 'omi'" | `[✓]`, not "differs from the expected `omind node` command". This is the single most common drift after a manual edit of `~/.claude.json` |
| B3 | `jq '.mcpServers.omi' ~/.claude.json` | Command array is `[<omind>, node, --vault, <vault>, --folder, OMI]` — an absolute interpreter path pointing into a venv that no longer exists is the classic post-reinstall break |
| B4 | `jq '.hooks \| keys' ~/.claude/settings.json` | Contains `PreToolUse`, `PostToolUse`, `Stop`, `SessionStart`, `UserPromptSubmit` |
| B5 | `jq -r '.hooks[][]\|.hooks[]?.command' ~/.claude/settings.json` | Contains `omind hook PostToolUse`, `omind hook Stop`, `omind hook SessionStart`, plus the four managed scripts: `omi-guard.sh`, `omi-gate-reset.sh`, `omi-enforce.py`, `secret-output-guard.sh` (and `git-fresh-base.sh` where repo rules apply) |
| B6 | `ls -l ~/.claude/hooks/` | All hook scripts present and executable (`0755`). Managed guard scripts are expected to be root-owned/write-protected — a user-writable `omi-guard.sh` defeats self-protection |
| B7 | `cat ~/.claude/hooks/.omind-provision.json` | Manifest present; its recorded SHAs match the shipped hook resources. Mismatch = **hookset drift** (someone hand-edited a managed script); repair with `omind setup` |
| B7a | `omind doctor` line "auto-memory hooks run a non-canonical omind" | Absent. Present = hooks are pinned to a different install than `~/.local/bin/omind`, so `self-update` will never reach them — the failure that let one box run 8.1.1 hooks under an 8.2.0 binary indefinitely |
| B7b | Confirm `omind setup` can actually write `~/.claude/settings.json` | Writable by the user. If it is root-owned **and** immutable (`chattr +i`), setup dies with `PermissionError` and every "run `omind setup`" repair instruction in this matrix is impossible until an operator clears it — verify before trusting any B/C row's remediation |
| B8 | `test -f ~/.claude/skills/omind/SKILL.md` | Present — this is how agents discover authoritative command syntax |
| B9 | `omind doctor` line "seed files present" | `[✓]` — `index.md` + note template exist in the OMI folder |
| B10 | `test -f "$OMI/.obsidian/app.json"` | Obsidian config seeded |
| B11 | `git -C "$OMI" config --get-regexp 'merge\.(omi\|ours)'` + `cat "$OMI/.gitattributes"` | Both merge drivers configured and `.gitattributes` routes `*.md` to the `omi` driver — without this, mesh sync silently produces conflict markers in notes |

## C. Hook behavior (not just presence)

Drive each hook the way the harness does: JSON on stdin.

| ID | Command | Pass criterion |
|----|---------|----------------|
| C1 | `echo '{"hook_event_name":"SessionStart","session_id":"verify"}' \| omind hook SessionStart --vault "$TESTVAULT" --folder OMI` | Exit 0; emits the priming capsule (index/playbook/memory-workflow sections). Empty output on a seeded vault = broken priming |
| C2 | `echo '{"hook_event_name":"PostToolUse","session_id":"verify","tool_name":"Bash","tool_input":{"command":"ls"}}' \| omind hook PostToolUse --vault "$TESTVAULT" --folder OMI` | Exit 0; a journal entry appears for session `verify` |
| C3 | `echo '{"hook_event_name":"Stop","session_id":"verify"}' \| omind hook Stop --vault "$TESTVAULT" --folder OMI` | Exit 0; session journal is finalized |
| C4 | `~/.claude/hooks/omi-gate-reset.sh < /dev/null` | Exit 0; the per-turn consult gate is reset (verify with `omind guard status` before/after) |
| C5 | `omind doctor` line "no recorded hook failures" | `[✓]`. A recorded failure means a hook crashed in a real session — read it, don't clear it |
| C6 | Latency: `time` each of C1–C3 | Each well under its configured timeout (10–20s in settings). A hook at 80%+ of timeout will flake under load |
| C7 | `omind loop status` | Reports DISARMED on a fresh install. ARMED on a new install means the Stop hook will refuse to stop — a hang, not a feature |

## D. Guard policy engine — the block path

**This is the group that matters.** A guard that is wired but not blocking is
worse than no guard, because it reads as protection.

| ID | Command | Pass criterion |
|----|---------|----------------|
| D0 | `omind guard status` | **NOT PAUSED.** `guard pause` time-boxes off the consult-gate + verifier; a long pause left armed disables the soft path indefinitely. Also confirm "self-protection: guard config is write-protected" |
| D1 | `omind guard selftest` | **Every** harness row `[ok]`, and exit 0. Rows: claude/exit2, hermes/claude_json, opencode/json_signal, codex/codex_hook, gemini/gemini, openclaw/openclaw. Beware measuring `$?` through a pipe (`selftest \| head` reports *head's* status) — redirect instead |
| D2 | `omind guard policy` | Lists the 6 seed rules + any learned rules. Seed count < 6 = a broken policy load |
| D3 | `omind guard explain --command 'sudo ls'` | `DENY`, rule `sudo-use-fleet-sudo`, tier `sudo` |
| D4 | `omind guard explain --command 'pkexec id'` / `'doas id'` / `'run0 id'` / `'su -c id root'` | `DENY` on all four, rule `privesc-alternatives` |
| D5 | `omind guard explain --command 'gh repo delete a/b'` | `DENY`, rule `gh-repo-delete` |
| D6 | `omind guard explain --command 'gh api repos/o/r -X DELETE'` **and** `'gh api -X DELETE repos/o/r'` | `DENY` both orders, rule `gh-api-repo-delete` (order-independence is a regression-tested property) |
| D7 | `omind guard explain --command 'curl -X DELETE https://api.github.com/repos/o/r'` | `DENY`, rule `curl-api-repo-delete` |
| D8 | `omind guard explain --command 'gh auth setup-git'` | `DENY`, rule `gh-auth-setup-git` |
| D9 | **False-positive anchors.** `omind guard explain` on each of: `grep -rn sudo .`, `cat /var/log/sudo.log`, `git commit -m "fix sudo"`, `pass show sudo/akclark`, `fleet-sudo whoami`, `man su`, `tmux new -s run0`, `gh pr view` | `ALLOW` on **all**. These are command-position-anchoring regressions that make the guard unusable if they break |
| D10 | `OMI_SUDO_OK=1 omind guard explain --command 'sudo ls'` | Allowed — the documented opt-in escape hatch still works |
| D11 | `omind guard log --limit 20` | Recent decisions are being written; `deny` and `violation` events both appear over the install's history |
| D12 | `omind doctor` line "policy engine allowed an unconsulted action" | Absent. If present, the block path is broken — cross-check with D0/D1 before concluding it's a code fault |

## E. MCP surface

Start the server the way Claude Code does and exercise it. Use a throwaway
vault so write tools are safe.

| ID | Check | Pass criterion |
|----|-------|----------------|
| E1 | `omind node --vault "$TESTVAULT" --folder OMI` over stdio; issue `tools/list` | Server handshakes and lists the full tool set: search-vault, recall-note, read-note, list-notes, list-tags, create-note, edit-note, delete-note, restore-note, backlinks, graph, graph-neighbors, help |
| E2 | From a live agent session: `mcp__omi__list-notes` | Returns notes — proves the *registered* server (B3) actually runs, which E1 alone does not |
| E3 | `mcp__omi__search-vault` for a known-present phrase **RO** | Returns the expected note |
| E4 | `mcp__omi__recall-note` **RO** | Returns a capsule, not an error |
| E5 | If `mcp-conformance` is available: run it against `conformance.toml` | All probes pass; output under `max_output_chars` (200000) |
| E6 | `jq '.permissions.allow' ~/.claude/settings.json` | Read-only omi tools pre-allowed (read-note, search-vault, list-notes, recall-note, help) so routine recall doesn't prompt. Write tools must **not** be blanket-allowed |

## F. Write path, locking, and recovery

All against `$TESTVAULT`.

| ID | Command | Pass criterion |
|----|---------|----------------|
| F1 | `omind note --vault "$TESTVAULT" --folder OMI ...` create a note | Note written with valid YAML frontmatter incl. `type`; appears in `index.md` Recent Memories |
| F2 | Re-run F1 with an edit + a stale `expected_version` | Rejected as a concurrent-write conflict. Silent overwrite = data-loss bug |
| F3 | `test -f "$TESTVAULT/OMI/.omi.lock"` and run two `omind note` writes concurrently | Single-writer lock holds; both writes land, neither corrupts |
| F4 | `omind recover --vault "$TESTVAULT" --folder OMI` with no pending txn | Reports nothing to recover, exits 0 |
| F5 | `mcp__omi__delete-note` then `restore-note` on a scratch note | Delete is a soft archive (file still on disk); restore brings it back |
| F6 | `omind convert --vault "$TESTVAULT" --folder OMI` run twice | Idempotent — second run reports no changes |
| F7 | `omind lint --vault "$TESTVAULT" --folder OMI` | Exits cleanly; on the real vault **RO**, review broken wikilinks / orphans / dupes as findings rather than failures |

## G. Retrieval

| ID | Command | Pass criterion |
|----|---------|----------------|
| G1 | `omind doctor` line "search index: FTS5 available" | `[✓]`. FTS5 missing = the keyword path is degraded to a scan |
| G2 | `omind doctor` line "semantic search" | `[✓]` if `omind[embed]` was intended. "off (keyword path) — model2vec not importable" is a legitimate SKIP only if the install deliberately omitted the extra; it costs ~20pp recall@1 |
| G3 | `omind doctor` line "search index: … stale note(s)" | Zero stale notes and index age consistent with the write timer. Non-zero stale = run `omind reindex --rebuild` and re-check |
| G4 | `omind search '<known phrase>'` **RO** | Returns the expected note in the top hits |
| G5 | `omind reindex --vault "$TESTVAULT" --folder OMI` | Exits 0; index rebuilt under the write lock |
| G6 | `omind bench --vault "$TESTVAULT" --folder OMI` | Reports index-build, search latency, capsule build, recall token cost. Record as the **install's baseline** — this row's value is the number, compared over time |
| G7 | `omind graph stats` / `graph orphans` / `graph dangling` **RO** | All exit 0 and return structured output |

## H. Mesh, backup, transfer

Skip with a reason if the install is standalone (no peers).

| ID | Command | Pass criterion |
|----|---------|----------------|
| H1 | `omind mesh status` | Node identity present; each peer reports ahead/behind counts with a recent fetch |
| H2 | `omind doctor` line "last sync" | Recent relative to the sync interval. Hours-old sync on an active mesh = a broken timer |
| H3 | `omind doctor` line "no unresolved conflict markers" | `[✓]`. Any `<<<<<<<` in a note means the omi merge driver (B11) isn't doing its job |
| H4 | `omind mesh sync` (dry-run first if supported) | Completes without conflict |
| H5 | `omind backup` status / `omind doctor` "last backup succeeded" | Succeeded recently, to the intended destination. A backup that has never run on a new install is expected — verify the *schedule* exists |
| H6 | `omind export --vault "$TESTVAULT" --folder OMI` → `omind import` into a second temp vault | Round-trips: note count and content match the source |

## I. Scheduled / ancillary features

| ID | Command | Pass criterion |
|----|---------|----------------|
| I1 | `omind checkpoint --vault "$TESTVAULT" --folder OMI` | Produces/updates a daily worklog note |
| I2 | `systemctl --user list-timers \| grep -i omind` (or the platform equivalent) | The checkpoint timer is installed and scheduled if `install-timer` was intended |
| I3 | `omind rollup --vault "$TESTVAULT" --folder OMI` | Compacts dailies; default archives rather than deletes |
| I4 | `omind consolidate --vault "$TESTVAULT" --folder OMI` | Proposes merges **without mutating** the vault (verify with `git -C status`/checksums before and after) |
| I5 | `omind ai` | Reports token usage and the active model-expense profile |
| I6 | `omind serve` on the throwaway vault | Binds **localhost only**, unauthenticated by design. A non-loopback bind is a security finding, not a config preference |

---

## Reporting format

```
omind install verification — <host> — <version> — <date>
A: 5/5  B: 11/11  C: 6/7 (C7 FAIL)  D: 11/13 (D0,D1 FAIL)  E: 6/6
F: 7/7  G: 6/7 (G2 SKIP)  H: 6/6  I: 5/6 (I2 SKIP — no timer)

FAIL D0 — guard PAUSED for 185h; consult-gate + verifier disabled
FAIL D1 — selftest [FAIL] claude/exit2, gemini/gemini (downstream of D0)
SKIP G2 — omind[embed] not installed; keyword path only
```

Group D failures are release-blocking. Groups B/C failures are usually repaired
by re-running `omind setup`; re-run the failed rows afterward rather than
assuming the repair took.
