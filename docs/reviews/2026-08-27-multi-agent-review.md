# omind — Code & Feature Review (multi-agent lens), 2026-08-27

Reviewed at `main` = `9ce5fbf` (v8.8.0), clean tree. Method: nine time-boxed deep
reviews across three parallel rounds (memory core, MCP surface, mesh replication,
enforcement, retrieval, fleet/ops, web/CLI, memory lifecycle) plus a survey of
2025–26 agent-memory systems (Mem0/OpenMemory, Letta, Zep/Graphiti, Cognee,
LangMem, Memori, A-MEM, Basic Memory, MemOS). All Critical/High findings were
independently re-verified line-by-line before inclusion. Read-only — nothing
modified, nothing run beyond reads.

Everything in BACKLOG.md's prior review rounds (2026-08-01 code review,
claude-obsidian comparison, ten-model hive, reads-OMI-then-ignores-it) was
treated as known and is not re-reported.

**Verdict.** The foundations are genuinely strong and in several respects ahead
of every surveyed system: git-native P2P replication with Lamport revisions and
a symmetric, byte-symmetry-tested merge driver; enforcement at the harness
boundary; temporal validity; txn journal + recovery; a derived hybrid index
that fails open everywhere it should. No architecture-level problems found.
What stands between omind and "the multi-agent memory layer it can be" is one
batch of fixable concurrency/enforcement defects, and then a feature story for
*fleets*: identity, conflict visibility, feedback, background maintenance, and
scoping. The findings below are ranked; the roadmap at the end turns the gaps
into an order of work.

---

## Part 1 — Code findings

### A. Fix first (verified correctness/safety)

1. **HIGH | rules.py:300–310 — `git push origin +main` bypasses the public-main
   deny.** `_pushed_branches()` strips `-`-prefixed options but never strips the
   leading `+` of a refspec, so the destination becomes `"+main"` and never
   matches the seed rule `no-direct-push-public-main`. The force-push variant —
   the riskiest one — is the one that walks past the one deterministic note rule.
   Fix: `dest = dest.lstrip("+")` (and judge `--force` explicitly).
2. **HIGH | guard.py:1375–1386 — the agent can clear its own gate in-band.**
   `omind guard reset` reads a session id from stdin and unconditionally
   `clear_gate(session)`; the agent knows its session id (hooks echo it), and an
   empty session clears *every* gate on the machine. No auth, no compliance log
   entry on this path. Multi-agent: also clears any *other* agent's gate.
   Fix: log every reset to the compliance log; only honor harness-shaped stdin
   events; rate-limit per turn.
3. **HIGH | guard.py:1150–1152 — freshness is granted before the fetch runs.**
   `_record_git_freshness()` fires in the PreToolUse path and returns allow; a
   fetch that exits 1 (unreachable mirror) has still marked the repo fresh, and
   the subsequent commit passes the freshness gate. The block message asserts
   "the fetch must succeed (exit 0)" but nothing enforces it — no PostToolUse
   handler clears the sentinel on failure. Fix: record freshness in PostToolUse
   on `exit_code == 0`, or have the commit path verify the recorded outcome.
4. **HIGH | txn.py:395–425 — `omind recover` writes to the vault with no lock.**
   `_rollback_journal` restores bytes and unlinks via `_atomic_write_bytes` /
   `path.unlink()`; `recover()` never takes `OmiStore.write_lock()`. `omind
   recover` running while an agent is mid-write interleaves two atomic replaces
   on the same path — last one wins, one side silently lost; two concurrent
   recovers can both restore and both rmdir the same journal. Fix: hold the
   store write lock per journal rollback; re-check `pending()` inside it.
5. **HIGH | txn.py:253–270 — the prepared journal directory is not durable.**
   `prepare()` mkdirs the transaction dir and fsyncs the dir *itself* and the
   journal's parent — but never fsyncs `transaction_dir` (the new directory's
   parent), so the directory entry itself can vanish on power loss after the
   fsynced note writes landed: partial multi-note state with no journal — the
   exact scenario txn exists to prevent. Fix: `_fsync_dir(directory.parent)`
   after the mkdir.
6. **HIGH | update.py:238–246, 313–341 — self-update executes mutable-tag
   network code, no pin, no rollback.** `git+https://github.com/…@v{version}` +
   `pip install --force-reinstall` (or `uv tool install --force`). HTTPS only
   defeats passive MITM; tags are mutable, there is no commit-SHA pin, no
   release signature/checksum, and no rollback path — while `_post_update_heal`
   auto-rewrites hook scripts, so one bad push lands on every fleet machine's
   memory infrastructure. Fix: resolve tag→SHA and pin `@<sha>`; verify a signed
   digest; persist previous version and add `self-update --rollback`.
7. **HIGH (quality) | searchindex.py:560–563, 683–684 + embed.py:93 — vector
   spaces can silently mix, and vector-less notes never backfill.** The index
   meta records the model *name* only (`StaticModel.from_pretrained(model_name)`
   takes no revision), so two machines with the same model name but different
   cached snapshots produce same-width, different-space vectors that pass the
   `size != width` check and coexist in one matrix. Worse: a machine that pulled
   notes while its `[embed]` backend was missing ingests FTS-only rows, and the
   mtime/size skip means installing the backend later never backfills them —
   the semantic leg is permanently degraded on that box with no agent-visible
   signal (only `doctor`). Fix: store model+revision (or a hash of the encoder)
   in meta and wipe-on-change like the #210 schema lesson; backfill vector-less
   chunks when the backend appears; surface counts in `search --explain`.
8. **MED | mesh.py:587–613 — purge silently clobbers an edit that raced it.**
   `_apply_tombstones` unlinks any file a live tombstone names, no content or
   Rev check. An agent editing a note that was purged elsewhere during the TTL
   window gets its edit destroyed vault-wide on the next sync, with nothing in
   SyncReport. Fix: compare the live note's Rev against the tombstone timestamp;
   keep/quarantine and report `purge-edit-conflict` instead of unlinking.
9. **MED | mesh.py:237–243 (+ provision.py config merges, backup.save_config) —
   setup-time identity writes are unlocked read-modify-write.** `mesh_init`
   mints a node id outside any lock on `~/.config/omind/node.json`; two
   concurrent `omind setup` runs can mint duplicate Lamport node ids (the code's
   own comments explain why that corrupts merge order) or clobber a peer entry.
   Agent-config JSON merges (`~/.claude.json` etc.) are last-writer-wins. Fix:
   flock these files — `filelock` already exists for exactly this.
10. **MED | guard.py:356–373, 442, 476 + loopguard.py:81–89 — gate state files
    are unlocked read-modify-write.** Gate sentinels, re-close and off-topic
    counters, and loopguard's `loop_guard.json` (with a *shared fixed* `.tmp`)
    lose increments/consult records when hooks from parallel tool calls or
    multiple sessions interleave: spurious REQUIRE re-closes, late anti-wedge
    trips. Fix: route through `filelock` like `policy.json` does.
11. **MED | consolidate.py:126–144 — consolidation bypasses temporal validity.**
    The #169 mechanism is the thing cross-agent coherence most depends on, and
    `--apply` never sets `Supersedes:` on the merged note nor `Superseded by:`
    on the archived sources — so recall can keep serving the archived originals
    as current. Fix: set both inside `create_and_disable_sources`.

### B. Concurrency & coordination

12. **MED | checkpoint.py:260,267 — worklog append is a lost-update race**
    (read outside the lock, `update_note` with no `expected_version`).
13. **MED | okf.py:130–139 — `convert_vault` read→`write_note` with no
    expected_version silently reverts a concurrent agent's edit.**
14. **MED | server.py:312–354 — silent last-write-wins when either agent omits
    `expected_version`.** No error, no conflict report, nothing in the response
    says a competing writer existed. Fix: return
    `"concurrency": "unverified"`; consider refusing omitted tokens.
15. **MED | merge.py:235–248 — scalar LWW losses are invisible in the vault.**
    Concurrent summary/title/frontmatter edits resolve by rev with only a
    daemon-stderr line; `conflict_scan` greps for markers only. Fix: append a
    `merge-lww` tag when `scalar` discards a changed value, so doctor sees it.
16. **LOW | consolidate.py:179–185 — propose-side TOCTOU** (fields read before
    version capture; apply's revalidation then passes against newer content).
17. **LOW | journal.py:285–291 — rollup can undercount / strand an entry** when
    a hook append lands between tally and rename (append path deliberately
    bypasses the store lock). Count drift only, no corruption.
18. **LOW | store.py:893 — `.omi.lock` opened without `O_NOFOLLOW`** (unlike the
    hardened append path, #187) and nothing prevents the lock file's inode being
    swapped by git operations → two processes can flock different inodes.

### C. Agent surface & retrieval

19. **MED | server.py:242–245 — `read-note` is the one unbounded tool result**
    (full raw body, no cap/truncation marker; recall-note clamps at 8000).
20. **MED | verify.py:329–337, 305–315 — the verifier is prompt-injectable by
    note content** (raw note text interpolated into the verifier prompt; any
    reply containing the substring "relevant" parses as relevant). Bounded
    impact (decides only the ambiguous band) but it's a boundary — require a
    bare one-word verdict and fence the untrusted material.
21. **LOW | retrieve.py/searchindex — the indexed path's credential penalty
    inspects title+tags only** (fallback also scores summary), so a secret under
    an innocuous title is not de-ranked. Invariant 7 otherwise holds on both
    paths (verified).
22. **LOW | searchindex.py:880 — `search()` is the only public query path not
    under the index lock**; concurrent in-process searches can sporadically and
    silently degrade to the substring fallback. Also `refresh()`'s exceptions
    escape `search()`'s try (saved only by store's blanket catch), and a
    post-pull embed batch under the query lock head-of-line-blocks searchers.
23. **LOW | tests/test_server.py:189–199 — the bounded-output test omits
    `search-vault` and `graph op=path`**; add both so the invariant stays pinned.
24. **LOW | server.py:358–391 — archived-note scoping and supersession are
    undiscoverable** from tool descriptions (`include_archived` undocumented;
    `edit-note` never mentions `supersedes` — the natural update path bypasses
    the temporal-validity chain).

### D. Enforcement hardening

25. **MED | rules.py:225–238 + model_http.py:41 — harness-PATH dependence.**
    `gh`/`git`/verifier-CLI resolution via the *harness's* PATH: up to ~20s
    serial subprocess latency in the PreToolUse hot path, and a PATH shim can
    spoof `visibility: private` (disabling the public-repo deny) or neuter the
    verifier. Resolve from config-known absolute paths or validate.
26. **LOW | compliance.py:324 — Layer-E opt-in uses a loose substring regex**
    (the exact forge `_opt_in_satisfied` was written to kill). Records, not
    enforcement, but it blinds the recidivism loop. Reuse the strict matcher.
27. **LOW | verify.py:133–134 — `"truncated": true` is content-matchable**, so
    a note body can re-arm the gate (self-healing via the re-read escape).
28. **LOW | wall-clock in gate state** (pause TTL, visibility cache, tombstone
    timestamps) — NTP steps and cross-machine skew cause transient divergence;
    clock.py already distrusts wall clock everywhere else.

### E. Ops, web, CLI

29. **MED | web/app.py:93–95 — Host allowlist doesn't stop plain cross-site
    POSTs** (no Origin check; a page in the user's browser sends
    `Host: 127.0.0.1`, and a no-Content-Type Blob POST parses as JSON →
    create/restore reachable cross-site). Flagged, not runtime-verified — needs
    a live probe. Fix: reject foreign `Origin` like the Host middleware.
30. **MED | cli.py:975–976 — `mesh purge` is the one ungated hard delete**
    (no `--yes`/confirm/preview, while every other destructive surface is
    gated). One typo tombstones the note on every peer.
31. **MED | transfer.py:313–335 — import accepts `.obsidian/` plugin config**
    (executable-ish in-vault content that then mesh-replicates). Strip or
    refuse `plugins/` + `community-plugins.json` on import.
32. **LOW | cli.py:949 — `mesh add-peer` echoes remote URLs verbatim**
    (credential-embedded remotes land in captured agent logs). Redact userinfo.
33. **LOW | transfer.py:153 — export JSON leaks the local absolute vault path.**
34. **LOW | ai_usage.py — the per-vault `ai-usage-*.jsonl` ledger has no
    rotation** (compliance.jsonl rotates at 8 MiB; this sibling grows forever).
35. **LOW | backup.py:296–306 — degraded unencrypted rsync is not escalated**
    (logged, but never raises the failure-note machinery). Count it.

### F. Verified intact (the part that held)

- Merge semantics: symmetric, byte-symmetry-tested; "unchanged side never wins
  by rev"; equal-rev/different-content tie-broken deterministically; node ids
  minted outside the replicated tree; archive-vs-edit keeps the edit; set-field
  removals don't resurrect; `MERGE_HEAD` hygiene; no half-merged vaults.
- txn recovery honors newer-edit-wins (conflict keeps the journal).
- Every store write path is locked + atomic + Rev-stamped; optimistic
  concurrency re-checked *inside* the lock; `note_version` is content-based.
- Every list-shaped MCP tool pages (`_page`, MAX_PAGE=100) — including the
  search path; `read-note` is the only unbounded result (finding 19).
- Web: bind/Host defaults match docs/serve.md exactly; all six mutating routes
  documented; static-path hardening and API-404 fallback hold; list endpoints
  return summaries only; web conflict flow (409 + explicit confirm) is the
  model the MCP surface should copy.
- Secrets stay outside the replicated vault (node.json, backup pass, compliance
  log, update cache); tar.gz VCS exclusion enforced both directions; backup
  password hygiene exemplary; quickstart makes zero network calls.
- learn.py can't be poisoned into allowing (learned rules only block, seed rules
  skipped, blanket-match rules rejected); graph/lint/unwritten are read-only;
  seeds match shipped behavior.
- Doctor, bench --quality, loopguard anti-wedge design, compliance log locking.

---

## Part 2 — Feature review: what "the multi-agent memory layer it can be" means

### Where omind already leads (surveyed systems don't have these)

Plain-Markdown source of truth + git P2P replication with Lamport revisions and
a deterministic symmetric merge driver is unique — Mem0/Zep/Cognee/LangMem are
server-centric, Basic Memory/claude-obsidian are single-machine. Enforcement at
the harness boundary (consult gate + deterministic PreToolUse rules) exists
nowhere else in the surveyed set. Temporal validity (Supersedes), tombstone TTL,
txn journal + recovery, token-budgeted paged tool output, and a real eval
harness are all more rigorous than the field. The gaps below are additive —
identity, conflict visibility, feedback, background maintenance, scoping — not
architectural.

### The gaps, ranked by leverage

1. **Agent identity & attribution.** Revs stamp a *node*, not an agent; two
   agents on one machine are indistinguishable; nothing can answer "who wrote
   this". In-idiom: `Author:`/`Agent:` front-matter written by OmiStore from the
   calling context; Rev gains an agent component `(counter, node_id, agent_id)`;
   MCP tools accept/report it; web shows last-writer. This is the foundation —
   conflict routing, feedback, audit, and fleet routing all want it.
2. **A usefulness feedback loop.** omind already logs every consult, gate clear,
   and MCP response — the raw data SOTA systems lack. In-idiom: a derived
   `usage` table (note × consults × outcomes) that feeds a rerank feature,
   decays never-consulted notes, and promotes repeatedly-consulted ones into
   the tiered core (#173's machinery already exists). Cheapest big win.
3. **Real write coordination.** Beyond finding 14: make `expected_version` the
   default (require it or loudly flag), surface merge LWW losses in-vault
   (finding 15), and let consolidation write the Supersedes chain (finding 11).
   A "conflict review queue" (a `conflicts/` note per non-trivial divergence)
   gives unattended fleets a place to reconcile contested memories.
4. **Sleep-time maintenance.** Letta's best idea, trivially in-idiom: a cron'd
   janitor pass — `consolidate --plan` (+ frontier scoring), vector backfill,
   lint, journal rollup, tombstone GC — all existing read-only tools plus the
   already-shipped txn-guarded apply. omind's cron integration makes this a
   config change, not a subsystem.
5. **Write-time hygiene.** On create/edit, query the existing vector index for
   near-duplicates/contradictions and propose `Supersedes` (A-MEM's
   note-evolution, without adopting it). Prevents the duplicate/rot problem at
   the source instead of linting later.
6. **Scope & blast radius.** `scope` is a retrieval filter, explicitly not a
   boundary (store.py:208–212). In-idiom: `write_scope:` front-matter enforced
   by the guard (the enforcement layer is omind's unfair advantage), an
   ephemeral `scratch` tier with auto-TTL via existing tombstones, per-agent
   state namespacing in state_dir (fixes the shared `nosid` gate), and honest
   docs that scope ≠ access control.
7. **Fleet ops maturity.** SHA-pinned signed self-update + rollback (finding 6),
   import quarantine (finding 31), per-machine index-compatibility reporting in
   doctor (finding 7), and optional canary (one machine takes updates first).
8. **Memory-quality eval over time.** `bench --quality` exists; track recall/MRR
   drift per release like the #193 gate did, so retrieval changes stay honest.

### Not recommended (rejections preserved)

External memory frameworks as storage; contextual-chunk prefixes (measured,
no gain); capability-contract file; source-capture/Canvas emitters; fine-tuning
on the violation corpus. Per-note hard ACLs are also premature for a
single-operator vault — record the decision rather than leaving it open.

---

## Part 3 — Suggested sequence

- **Batch 0 — correctness week (all small, verified above):** findings 1–11,
  plus 12/13 (expected_version), 18 (O_NOFOLLOW), 30 (`mesh purge --yes`),
  34 (rotation). Each is a few lines with a test; eleven are one-liners.
- **Batch 1 — identity & coordination:** findings 14/15/19/24 + gap 1 + gap 3.
- **Batch 2 — feedback loop:** gap 2 (usage table → rerank → decay/promote).
- **Batch 3 — sleep-time janitor:** gap 4 + vector backfill (finding 7's fix).
- **Batch 4 — scope, write-time hygiene, fleet ops:** gaps 5–7.
- **Meta:** AGENTS.md's "Handoff" section still describes the July retrieval
  branch as unlanded — rewrite it (or point it at BACKLOG/CHANGELOG) before the
  next agent inherits a month-stale map.

---

*Proudly Made in Nebraska. Go Big Red! 🌽 <https://xkcd.com/2347/>*
