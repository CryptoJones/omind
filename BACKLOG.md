# Backlog

This file and the GitHub **[Issues tab](https://github.com/CryptoJones/omind/issues)** are two
views of the same list and must stay in sync. Every backlog item below has a matching GitHub issue
and vice versa — when an item ships and its issue closes, check the box (or move it to `Done`)
here so neither side drifts.

## Open

_Mirrors the [GitHub Issues tab](https://github.com/CryptoJones/omind/issues).
Reconciled 2026-09-08: 44 shipped items that were still sitting here moved to
[Done](#done), and #297 was closed as a duplicate its own fix (#306) had already
resolved._

Two **tracking issues** group the clustered work; their children are nested below them and
mirror GitHub's sub-issue hierarchy, so the parent's `n/m` progress bar and this file agree.

- [ ] **Tracking: memory that knows when it is wrong** ([#328](https://github.com/CryptoJones/omind/issues/328)) — _tracking (0/2)_ —
  omind detecting and reporting its own retrieval failures instead of assuming its
  instrumentation is sound. Both children share one thesis: omind was measuring itself
  the whole time and nobody read the meter.
  - [ ] **`omind audit`: a self-assessment that can indict omind** ([#326](https://github.com/CryptoJones/omind/issues/326)) — _enhancement (instrument)_ —
    every omind surface that spends the agent's context gets a measured number, a
    declared threshold and a verdict — and the tool must be able to return a failing
    one, including "turn this off". Generalizes the two instruments 9.2.0 shipped for
    the one surface that happened to get caught. The others are in exactly the state
    preflight was in the day before #321: instrumented, unexamined, assumed fine —
    priming 2.88M tokens, MCP responses 2.49M, verifier 642K, none with a threshold.
    #321 was found by accident after months invisible; the ledger that proved it had
    been written faithfully since 4.0.0 and never read.
  - [ ] **Preflight injection is a context-rot engine** ([#321](https://github.com/CryptoJones/omind/issues/321)) — _bug + enhancement_ —
    the per-turn push shipped ~3.4M tokens of unrequested recall across 5,816 turns at
    ~25% precision, framed as binding instruction and never removed. Invisible by
    construction; surfaced as "the model has gotten worse in long sessions". **The
    push→pull rework, the framing revert, stale/action-item filtering, the session
    budget, `omind bench --precision` and `omind rules export` shipped in v9.2.0.**
    Still open: the A/B needle-in-haystack replay harness (planted needles at 20/50/80%
    depth, preflight on vs off) — the one acceptance criterion that needs an
    instrument, not a change.

- [x] **Guard: `git -C "<quoted path>" commit|push` not classified as repo work** ([#333](https://github.com/CryptoJones/omind/issues/333)) — _bug (enforcement)_ —
  the literal-blanked command (#317) hid a quoted `-C` path from `_GIT_GLOBAL_OPTS`, and the
  freshness message teaches that exact form; found in the 2026-09-11 guard experiment. Shipped
  in 9.2.1 ([PR #334](https://github.com/CryptoJones/omind/pull/334)).
- [x] **Guard: REQUIRE-mode verifier re-closes the gate over the read the guard itself demanded, then demands page-shaped notes** ([#335](https://github.com/CryptoJones/omind/issues/335)) — _bug (enforcement)_ —
  with `OMI_VERIFY_REQUIRE=1` a commit turn became: rules-note demand → read → verifier scores it
  off-topic against pasted page text → re-close → demand whatever the page resembles. Measured
  51 blocks / 85 forced reads / 648K chars in 55 turns, compaction every ~12 turns. WARN mode is fine.
- [x] **Hooks: SessionStart re-primes on every resumed headless turn** ([#336](https://github.com/CryptoJones/omind/issues/336)) — _enhancement (tokens)_ —
  `claude -p --resume` fires `SessionStart:resume` per turn; the capsule (~5.1K chars) was
  most of a guard-on lane's injected volume (~590K chars per 100 turns). Prime on startup and
  compact, and on resume only if the session was never primed.
- [ ] **Tracking: the guard's blind spots** ([#329](https://github.com/CryptoJones/omind/issues/329)) — _tracking (0/2)_ —
  the enforcement gaps where the guard does not see part of the session it governs. Both
  children are the same defect class: the guard reads a slice of the session, treats it as
  the whole, and reports itself as functioning.
  - [ ] **Guard: long sessions run dozens of tool calls with no memory contact** ([#296](https://github.com/CryptoJones/omind/issues/296)) — _bug (enforcement)_ —
    measured on hermes across every transcript since 2026-08-24: 362 turns where the
    per-turn gate auto-cleared with nothing injected carried 2,037 tool calls and only
    97 consults. Longest single turn: 152 tool calls. Compaction is ruled out —
    `SessionStart(source=compact)` re-primes correctly. The gate keys off continuation
    prompts, so a long turn is one gate event no matter how much work happens inside it.
  - [ ] **Guard: mid-turn user messages are invisible to the authorization classifier** ([#290](https://github.com/CryptoJones/omind/issues/290)) — _bug (enforcement)_ —
    authorization is classified from the *opening* message of a turn, but Claude Code
    delivers messages sent while a turn is running alongside a tool result. An explicit
    mid-turn imperative therefore cannot lift a block the opening message armed.

- [ ] **`edit-note` silently guts a note when `details` contains a `## ` heading** ([#292](https://github.com/CryptoJones/omind/issues/292)) — _bug (data loss)_ —
  content after the first `## ` is relocated out of `## Details` and re-emitted after
  `## References`; a second edit leaves both the stale and the new copy. Hit for real
  on 2026-08-31: a note ended up with two contradictory copies of its body, the
  superseded one still reading as current, while `## Details` was empty.
- [ ] **Flaky: `test_concurrent_appends_serialize` drops one append on `windows-latest`** ([#319](https://github.com/CryptoJones/omind/issues/319)) — _bug (CI / possibly filelock)_ —
  `assert 39 == 40` on `main` run 34268329669; the same content passed on its PR
  branch and the next `main` run. Either a harness race or a real `msvcrt.locking`
  gap — and if it is the latter, the journal, compliance log and AI-usage log have
  the same hole on Windows, where a dropped line looks like inaction, not a bug.
- [ ] **First PyPI publish** ([#267](https://github.com/CryptoJones/omind/issues/267)) — _chore_ —
  needs CJ's PyPI account; details in [PyPI Publish Setup](#pypi-publish-setup-2026-08-24-267) below.

## Not planned

- [ ] **Machine-readable capability contract verified by `doctor`** ([#196](https://github.com/CryptoJones/omind/issues/196), closed not-planned) — _closed: solved by other work_ —
  they declare every capability's tier, read/write scope, network need, and
  destructiveness in `config/capabilities.json`, verify it, and state explicitly
  where no automated verifier exists. omind's `doctor` checks are hand-written per
  concern with no declaration of what each surface may touch, and nothing fails when
  code and declaration drift. Natural home for the #190 `serve` risk model.
  **Closed not-planned 2026-08-02.** The concrete gap it named — nothing states which
  surfaces can destroy a memory — was closed by other work rather than by a declaration
  file: `docs/serve.md` (v6.4.0) states the risk model for the unauthenticated destructive
  API this issue called out as its natural home.

- [ ] **Contextual-prefix indexed chunks (Anthropic Contextual Retrieval)** ([#193](https://github.com/CryptoJones/omind/issues/193), closed not-planned) — _rejected on measurement_ —
  built behind `OMI_CONTEXTUAL_CHUNKS` and evaluated on the live 784-note vault, both
  ways, with the semantic leg on. **recall@1 60% → 60%, recall@5 60% → 60%, MRR 0.640 →
  0.640**, for +31% index size (13,908 → 18,248 KiB) and +20% rebuild time. Of the five
  labelled cases, three were already rank 1 in both; the two misses went 7→8 and 13→11.
  Noise, not signal.

  The reason is the part worth remembering: **the issue's premise did not hold for omind.**
  It assumed a mid-note chunk "competes on its own words alone", which is true of
  claude-obsidian but not here — `_ingest` has always embedded `title + heading + tags +
  chunk.text`, and `chunks_fts` has always given BM25 separate title/heading/tags columns.
  omind already had contextual retrieval without the name. The prefix's only genuine
  addition is the note's `Summary`, which on descriptive titles is largely a restatement
  of a signal already indexed. The upstream 35–49% figure is measured against a baseline
  that indexes bare chunks; omind is not that baseline.

  Caveat kept honest: the labelled set is only 5 cases, so each is worth 20pp and an
  effect under ~15% could hide. A 25–40 case set is worth building for retrieval work in
  general — and would be the thing that could reopen this.

- [ ] **Long game: fine-tune a model on the accumulated violation corpus** ([#91](https://github.com/CryptoJones/omind/issues/91), closed not-planned) — _roadmap (Phase 4)_ — deferred: the blocker is data, not compute. The live `compliance.jsonl` corpus is ~91% relevance-noise, ~6% real denies, and 100% DENY (zero ALLOW), so training on it as-is yields an always-deny model. Revisit only after `export-corpus` is reworked to synthesize balanced ALLOW examples (from the deterministic `guard.decide()`) and split the relevance corpus from the action corpus. The mechanical guard remains the backstop.
- [ ] **claude-obsidian's source capture, Canvas/Bases emitters, and methodology filing modes** — _rejected_ —
  evaluated during the 2026-08-02 comparison. Their `capture` (immutable content-addressed
  copies of PDFs/images/URLs under `.raw/`), their Obsidian Canvas and `.base` emitters, and
  their PARA/LYT/Zettelkasten routing modes are all well built, and all solve a problem omind
  does not have. omind's notes are written *by an agent about its own work*, not ingested from
  external documents, so there is no source to retain and no filing taxonomy to pick. The
  Canvas/Bases emitters are Obsidian-presentation features; omind already ships a web graph
  view and leaves presentation to Obsidian itself. Revisit only if omind ever grows an ingest
  path for external material.
- [ ] **Adopt an external memory framework (Mem0 / Cognee / Zep) as the storage layer** — _rejected_ — evaluated during the 2026-07-24 survey. Every one of them wants to own storage, and omind's whole premise is that the Markdown vault is the source of truth: plain files, git-replicated across the mesh, readable in Obsidian, with no service to run. The techniques are worth copying; the dependency is not.

## Done

### Shipped — 2026-09-09

- [x] **Guard: a git verb inside an ssh payload or a string literal is judged as LOCAL repo work** ([#317](https://github.com/CryptoJones/omind/issues/317)) — _bug_ —
  `_is_repo_sensitive_action` matches a git verb anywhere in the command text, so
  `ssh host '...'` running a commit on another machine is classified as local repo
  work; `_repo_root_for_action` then falls back to `Path.cwd()` and demands a
  freshness fetch of an unrelated local repo. The check the operator is forced to
  satisfy is vacuous, and the guard records the remote commit as having a fresh
  base. Same family as the escalation-keyword substring bug; `policy._CMD_POSITION`
  is the existing anchoring primitive. Reproduced four times while filing it.
  **Fixed** by `policy.shell_code_text()`: a quoted body, and a heredoc body fed to
  anything that is not a shell, are blanked before any command-position test, so the
  separators inside a payload stop counting as separators. Routed the local-repo
  classifiers and every `match="command"` rule through it; the side-effect gate keeps
  the raw text on purpose (a remote restart is a real side effect). Reproduced three
  more times by the installed guard while fixing it.

### Shipped — moved out of Open on 2026-09-08

_These landed and were closed upstream; they sat under `## Open` because the
box was checked but the item never moved. Grouped by the review round that
produced them, as they were originally filed._

### From the 2026-08-27 multi-agent review (code round — fixes in the working tree)

_A nine-slice review (memory core, MCP surface, mesh, enforcement, retrieval,
fleet/ops, web/CLI, lifecycle) plus a 2025–26 SOTA survey. 35 code findings — all
fixed, gates green (1,004 tests / ruff / mypy strict). Full report:
[docs/reviews/2026-08-27-multi-agent-review.md](docs/reviews/2026-08-27-multi-agent-review.md).
Issues [#272](https://github.com/CryptoJones/omind/issues/272)–[#284](https://github.com/CryptoJones/omind/issues/284)
filed and linked below; they close when the fix branch merges._

- [x] **Force-refspec push bypassed the public-main deny** ([#272](https://github.com/CryptoJones/omind/issues/272)) — `+main` stripped
- [x] **`omind guard reset` was an unlogged, agent-reachable gate clear** ([#273](https://github.com/CryptoJones/omind/issues/273)) — logged
- [x] **A failed git fetch still satisfied the freshness gate** ([#274](https://github.com/CryptoJones/omind/issues/274)) — retracted on PostToolUse
- [x] **txn `prepare` didn't fsync the journal dir entry** ([#275](https://github.com/CryptoJones/omind/issues/275)) — parent fsynced
- [x] **Self-update installed from a mutable tag with no rollback** ([#276](https://github.com/CryptoJones/omind/issues/276)) — SHA-pinned + `--rollback`
- [x] **Vector spaces could silently mix; vector-less notes never backfilled** ([#277](https://github.com/CryptoJones/omind/issues/277)) — model identity + backfill
- [x] **Tombstones destroyed edits that raced the purge** ([#278](https://github.com/CryptoJones/omind/issues/278)) — Rev capture + keep/report
- [x] **Node-id minting / backup / settings merges were unlocked RMWs** ([#279](https://github.com/CryptoJones/omind/issues/279)) — `filelock.exclusive` everywhere
- [x] **Gate sentinels, re-close/off-topic counters, loopguard lost increments** ([#280](https://github.com/CryptoJones/omind/issues/280)) — locked RMW
- [x] **Consolidation bypassed the #169 Supersedes chain** ([#281](https://github.com/CryptoJones/omind/issues/281)) — wired in
- [x] **`edit-note` was silent last-write-wins without a token; `read-note` unbounded** ([#282](https://github.com/CryptoJones/omind/issues/282)) — flagged / capped
- [x] **Merge scalar LWW losses invisible; `mesh purge` ungated; verifier injectable** ([#283](https://github.com/CryptoJones/omind/issues/283)) — `merge-lww` tag / `--yes` / fenced
- [x] **Web cross-site POST; ai-usage ledger unbounded; journal rollup stranded appends; okf/checkpoint lost-update** ([#284](https://github.com/CryptoJones/omind/issues/284)) — Origin check / rotation / flocks / version pinning

_(Feature roadmap from the same review — agent identity, usefulness feedback
loop, sleep-time consolidation, write-time dedup, scoped writes — is Part 2 of
the report. Agent identity is SHIPPED on `feat/agent-identity`; the other four
are design-ratified by the FlatlineRoundtable panel (zero REJECTs, majority
rulings on every split) in
[docs/design/2026-08-27-roadmap-consensus.md](docs/design/2026-08-27-roadmap-consensus.md)
and await implementation.)_

### Windows fresh-install test (2026-08-15, first fully-cold install)

- [x] **`setup --dry-run` warns about a missing claude CLI but the real run hard-fails** — fixed in 8.7.1: claude is a soft prerequisite, real run degrades like the dry-run promises ([#258](https://github.com/CryptoJones/omind/issues/258)) — either fail the dry-run too, or degrade the real run gracefully (do vault/seed work, skip only MCP registration)
- [x] **Windows: POSIX `.sh` hooks + `fleet-sudo` installed unverified** — fixed in 8.7.2: direct omind hook commands on Windows, sh-gated bash guards, doctor probe, UTF-8 console ([#259](https://github.com/CryptoJones/omind/issues/259)) — doctor should probe hook executability (needs an `sh` on PATH); setup should warn or ship PowerShell equivalents; also `�` mojibake in doctor output under PS 5.1
- [x] **Windows: codex hook verifier false-negatives on its own SessionStart/PostToolUse entries** — fixed in 8.7.1: shared Windows-tolerant command_is_omind_hook predicate ([#261](https://github.com/CryptoJones/omind/issues/261)) — hooks are written correctly but setup/--force/doctor all report them missing (quoting-form mismatch in the matcher); guard selftest passes for all six harnesses on Windows

### From the 2026-08-01 top-to-bottom code review

_A full read-only pass over every module (memory core, retrieval, enforcement, mesh, ops,
shell hooks, web frontend). The codebase held up unusually well — no correctness or security
bugs found. What follows are the five findings worth tracking; all are perf, test-coverage,
hardening, or docs, not defects._

- [x] **Per-query full-table Python scans in the search weighting passes** ([#186](https://github.com/CryptoJones/omind/issues/186)) — _perf (retrieval)_ —
  `_weight_generated`, `_weight_superseded`, and `_owners` now share one map built once
  per index `generation` and cached per process, like the packed vector matrix. Query
  cost tracks the fused candidate set instead of the vault size.
- [x] **A `SCHEMA_VERSION` bump that adds a column wedged an existing search index** ([#210](https://github.com/CryptoJones/omind/issues/210)) — _bug (retrieval)_ —
  shipped in 6.6.0 and caught while setting up the #193 eval gate: the baseline read
  `recall@1 = 0%`. `_wipe` deleted rows but never dropped tables, so a new column never
  materialised and every ingest failed silently forever. Retrieval fell back to the
  substring scan, so it degraded quietly instead of erroring. `_wipe` now drops and
  recreates; a test exercises the upgrade path from the previous shape.
- [x] **Compliance-log rotation silently never fired on Windows** ([#202](https://github.com/CryptoJones/omind/issues/202)) — _bug (enforcement)_ —
  found by CI on the #188 PR, before it shipped. The rotation renamed the log while this
  process still held its fd; Windows refuses that, and the `PermissionError` was swallowed
  by the never-raise-into-the-agent handler, so the log would have grown forever there with
  no breadcrumb. Rotation now runs after the fd is closed, under a separate lockfile.
- [x] **compliance.py recidivism helpers re-parse the whole append-only log per call** ([#188](https://github.com/CryptoJones/omind/issues/188)) — _perf (enforcement)_ —
  `read_events()` is memoized against the log's `(mtime_ns, size)`, and `summary()` counts
  the list it already read. The log now rotates at 8 MiB to `compliance.jsonl.1`; readers
  span both generations so escalation counts survive a rotation.
- [x] **Append-only hot-path writers flock the data fd after open (TOCTOU hardening)** ([#187](https://github.com/CryptoJones/omind/issues/187)) — _hardening (low)_ —
  the journal, compliance, and ai-usage writers now share one
  `filelock.append_locked` context manager that opens with `O_NOFOLLOW`, so a symlink
  swapped in at the path cannot redirect the append. One definition of the discipline
  instead of three hand-copied copies.
- [x] **No regression test that run_hook(PostToolUse) invokes the compliance detector** ([#189](https://github.com/CryptoJones/omind/issues/189)) — _testing_ —
  one spy test now pins all four side effects of that branch — `loopguard.reset`,
  `ai_usage.record_mcp_response`, `compliance.record_post_tool`, `verify.verify_consult` —
  independently of policy content. The detector was covered only indirectly (via a seed
  rule that a policy change had already forced an edit to); the other two were not covered
  at all.
- [x] **One failing PostToolUse side effect silently cancels the rest** ([#204](https://github.com/CryptoJones/omind/issues/204)) — _hardening_ —
  found while writing the #189 test. Each side effect now runs isolated through
  `hooks._best_effort`, which leaves a breadcrumb naming which one failed, so
  `hook-failures.log` tells "it ran and failed" from "it never ran". The `Stop` branch
  is isolated the same way.
- [x] **Document that `omind serve` is an unauthenticated destructive API (localhost-only by design)** ([#190](https://github.com/CryptoJones/omind/issues/190)) — _docs_ —
  `docs/serve.md` states the risk model: every route an unauthenticated caller reaches,
  what already protects you and why, how to expose the port safely, and what to check if
  it was exposed. Also in `--help`, the module docstring, the README, and a startup line
  on every run. A test fails if a new destructive route is added without documenting it.

### From the 2026-08-02 claude-obsidian comparison

_A read of [`AgriciDaniel/claude-obsidian`](https://github.com/AgriciDaniel/claude-obsidian)
v2.1.0 (~23k lines of Python, MIT) against omind, working from its source rather than its
README. It is the closest serious analogue to omind — plain-Markdown Obsidian vault, Claude
Code host, local-first, no service to run — but aimed at a research wiki built from external
sources rather than at durable agent memory. omind is ahead on retrieval mechanics (FTS5 +
quantized int8 vectors + RRF beats their JSON BM25 index), on multi-machine replication
(they have none), on enforcement, and on shipping a real MCP server. What follows are the
five places their design is genuinely better and the idea transfers._

- [x] **Journaled plan→apply→recover transactions for multi-note operations** ([#194](https://github.com/CryptoJones/omind/issues/194)) — _enhancement (durability)_ —
  shipped as `omind.txn` + `omind recover`. Pre-images captured and fsynced before the
  first write, atomic per-file replace, a commit record, deterministic rollback. Recovery
  refuses to overwrite a note edited after the crash — that edit is newer than the
  pre-image — reporting a conflict and keeping the journal instead. In-process failures
  roll themselves back. `create_and_disable_sources` migrated; the docstring that conceded
  "a process crash can still leave extra recoverable copies" is gone. Skipped their
  `approved_plan_sha256` handshake as planned.
- [x] **Frontier/boundary scoring to rank what to consolidate next** ([#197](https://github.com/CryptoJones/omind/issues/197)) — _enhancement (efficiency)_ —
  shipped as `omind graph frontier` and `graph(op="frontier")`:
  `(out - in) * 0.5 ** (days/30)`, generated notes excluded by default, read-only,
  no new scan or state. Original description follows.
  `(out_degree - in_degree) * recency_weight` finds notes that point outward, are
  pointed at by few, and were touched recently. Every `omind graph` op answers a
  structural yes/no question; none rank what to work on next. Complements
  `consolidate`, which finds candidates by similarity — this finds them by structure.
  Cheap: the `links` table is already built. Read-only, no write path.

_From a 2026-07-24 survey of open-source AI memory layers (Mem0, Zep/Graphiti, Letta/MemGPT,
Cognee, memvid, Memori) and the 2025–2026 agent-memory literature (Memori arXiv:2603.19935 —
81.95% LoCoMo at 1,294 tokens/query; SimpleMem arXiv:2601.02553 — 26.4% F1 gain at 30× fewer
tokens; H-MEM, EACL 2026; RecMem arXiv:2605.16045; Multi-Layer Memory arXiv:2603.29194), scoped
to what omind actually needs. The foundation — a derived SQLite hybrid index (BM25 + vectors +
RRF), excerpt-returning search, and paged MCP payloads — shipped first; see `docs/retrieval.md`.
These are the next tier, ranked by leverage._

### Memory shape

_No open items._

### Efficiency

- [x] **Remove the deprecated `graph-*` MCP compatibility aliases after one release**
  ([#181](https://github.com/CryptoJones/omind/issues/181)) — _chore (tokens)_ —
  Removed `graph-path`, `graph-orphans`, `graph-dangling`, and `graph-stats`
  after the 5.0 bridge release; `graph-neighbors` stays.
- [x] **Graph view collapsed into a hairball above 1,800 notes**
  ([#300](https://github.com/CryptoJones/omind/issues/300)) — _perf (web UI)_ —
  The O(n^2) all-pairs repulsion was switched off past `REPEL_LIMIT = 1800`, so
  large vaults laid out on springs and gravity alone (radius of gyration 29 vs
  461 at n=2,500). Replaced with a Barnes-Hut quadtree, O(n log n): the cap is
  gone and `graph.js` stays dependency-free — the algorithm, not the library.

### From the 2026-08-10 ten-model hive review

_Ten ephemeral lanes, one per model family, compared omind's feature set against Mem0,
Zep/Graphiti, Letta/MemGPT, LangMem, Cognee, Memary, txtai and Basic Memory. Two of the
review's headline recommendations are deliberately NOT tracked here: temporal validity
already shipped (#169), and adopting an external memory framework as the storage layer is
already rejected below._

- [x] **Capture the agent's own work without requiring it to remember a tool call** ([#221](https://github.com/CryptoJones/omind/issues/221)) — _enhancement (memory)_ —
  7 of 10 lanes named this the largest gap. Every write needs the agent to *decide* to call
  `create-note`; a forgotten call loses the memory silently, with no error and no warning.
  Explicitly NOT the external-document ingest rejected below — this captures the agent's own
  work, which is what omind's notes already are. Open question is whether it belongs here or
  in the harness; a cheaper middle option is a detector that flags "this session wrote no
  notes" without omind ever reading a transcript.
- [x] **Flat note namespace: every agent on every machine sees every note** ([#222](https://github.com/CryptoJones/omind/issues/222)) — _enhancement (mesh)_ —
  single-lane finding. Two axes: retrieval precision (unrelated notes compete in every
  search, where Mem0/LangMem/Letta all partition by user/agent/run) and blast radius (no way
  to scope a note to a project, machine, or agent). Distinct from #196, which covered the
  scope of the MCP *surfaces* rather than of the notes. May well not be worth it for a
  single-operator vault — if so, record that rather than leaving it to be re-proposed.

### From the 2026-08-14 "reads OMI, then ignores it" investigation

_A three-box investigation (hermes, pluto, makemake) into why the agent reads injected memory
and then acts against it. Headline finding: on the `economy` profile (the shipped default,
live on two of the three boxes) the 4,000-char capsule slices priming notes into
preamble-only stubs — the agent was "ignoring" rules that were never actually in its context.
Secondary mechanisms: dead-end truncation markers, enforcement that verifies the reading
ceremony rather than compliance, rules injected 200 turns away from the action they govern,
self-discounting framing, and one box silently failing every vault write since 2026-08-09.
Each issue below is written to be executable by any agent without further context._

- [x] **Capsule budget shreds priming notes into preamble-only stubs** ([#238](https://github.com/CryptoJones/omind/issues/238)) — _fix (hooks)_ —
  shipped in 8.3.0 (#245). The allocator now fits sections whole in priority order and
  replaces an oversized note with an omitted-stub naming the exact `recall-note` call
  (index.md, a catalog, may still truncate mid-list); an upstream-clipped digest is
  stubbed, never shipped partial. Default profile is `balanced` (8k), and `Rules.md`
  joins `PRIMING_FILES` first so a compact operator rules note always arrives whole.
- [x] **Truncation markers are dead ends; a truncated read satisfies the gate** ([#239](https://github.com/CryptoJones/omind/issues/239)) — _fix (recall/guard)_ —
  shipped in 8.3.2 (#248). The recall marker names the note and the exact follow-up call;
  a guard-demanded note that comes back truncated records as an incomplete consult that
  keeps the gate armed, with a deterministic un-wedge (truncated:false, a section
  drill-down, or max_chars at the 8k API cap).
- [x] **Compile machine-readable note rules into deterministic PreToolUse checks** ([#240](https://github.com/CryptoJones/omind/issues/240)) — _feat (guard)_ —
  shipped in 8.6.0 (#251). `omind.rules` compiles fenced `omind-rule` YAML blocks in
  vault notes into deny/warn PreToolUse checks evaluated before everything else;
  visibility via gh (24h cache, fail-open on unknown), `omind rules list`, seed rule for
  the public-main-push incident class (note rules replace seeds by id).
- [x] **Place governing rule text adjacent to the action it governs** ([#241](https://github.com/CryptoJones/omind/issues/241)) — _feat (guard)_ —
  shipped in 8.4.0 (#249). Repo-work denies embed the git-rules note's summary +
  leading excerpt after the demand sentence; action-shaped turns re-inject the full
  preflight excerpt; preflight surfaces a runner-up match as title+summary (skipped on
  economy).
- [x] **Injected-memory framing invites the model to discount it** ([#242](https://github.com/CryptoJones/omind/issues/242)) — _fix (hooks, text-only)_ —
  shipped in 8.3.1 (#247). Both preambles now lead with what the content IS —
  standing operator instructions, follow as if typed at session start — keeping the
  explicit-override clause plus "silence is not an override."
- [x] **Loudly surface sustained vault-write failures** ([#243](https://github.com/CryptoJones/omind/issues/243)) — _feat (doctor)_ —
  shipped in 8.5.0 (#250). Shared streak parser drives a doctor `vault_writes` check
  (fail at ≥5 append_entry failures/24h, macOS Full-Disk-Access hint, direct write
  probe) and a SessionStart "MEMORY WRITES ARE FAILING" banner. The manual FDA grant on
  makemake remains operator work.

### Harness + repo hygiene (2026-09-07)

- [x] **`ToolError` text masked by mcp >= 2.1, reddening CI on every PR**
  ([#294](https://github.com/CryptoJones/omind/issues/294)) — _fix (server)_ —
  Anticipated domain failures re-raised as `ToolError` so their message survives.
- [x] **Windows CI broken underneath #294's redness**
  ([#306](https://github.com/CryptoJones/omind/issues/306)) — _fix (journal/test)_ —
  Journal rollup re-opened a file it held a mandatory Windows lock on; and a test
  moved `HOME` without `USERPROFILE`.
- [x] **Version lockstep unguarded for `uv.lock`; two releases had no CHANGELOG section**
  ([#307](https://github.com/CryptoJones/omind/issues/307)) — _fix (test/docs)_ —
  `test_version_is_set` now checks `uv.lock` as well; 8.10.0 and 8.10.1 backfilled
  from their GitHub release bodies.

- [x] **Poolside's `pool` CLI wasn't connected to omind at all**
  ([#302](https://github.com/CryptoJones/omind/issues/302)) — _feat (agents)_ —
  `pool mcp list` reported "No MCP servers configured", so the CmdrData/Laguna
  roundtable lane ran with no memory. `PoolsideProvisioner` registers the omi
  MCP server in `~/.config/poolside/settings.yaml`.
- [x] **Nothing enforced the `Co-authored-by` trailer**
  ([#303](https://github.com/CryptoJones/omind/issues/303)) — _chore (ci)_ —
  Five commits in `v8.8.0..v8.10.1` carry no trailer, so 8.10.0 and 8.10.1 are
  unattributable. `scripts/check-attribution.sh` now backs a `commit-msg` hook
  and a CI job; a commit declares `Co-authored-by:` or `No-agent: true`.
- [x] **Poolside can't be hard-blocked — the guard needs an ACP proxy**
  ([#304](https://github.com/CryptoJones/omind/issues/304)) — _feat (guard)_ —
  Superseded by #311: `pool` 1.0.16 does have pre-tool hooks, so no proxy is
  needed. Closed in favour of the hook mount.
- [x] **Poolside ships a hooks system; mount the guard directly**
  ([#311](https://github.com/CryptoJones/omind/issues/311)) — _feat (guard)_ —
  `pool` >= 1.0.16 runs Claude-shaped `PreToolUse`/`PostToolUse`/
  `UserPromptSubmit`/`Stop`/`SessionStart` hooks from a `hooks:` key in
  `settings.yaml`. `omind setup --agent poolside` now mounts all five with
  `--harness poolside`, which translates pool's payload (`<server>__<tool>`,
  `tool_input.cmd`, `tool_output`) onto the Claude shape and replies in pool's.
- [x] **The consult gate blocks Poolside's `exit`/`todo_action` control tools**
  ([#313](https://github.com/CryptoJones/omind/issues/313)) — _fix (guard)_ —
  `pool exec` ends a run through the `exit` tool; gating it aborted trivial runs
  with `exit_tool_called: unexpected error`. Both are now in `_GATE_EXEMPT_TOOLS`
  beside `ToolSearch`; hard rules still apply.

- [x] **`self-update`: post-update heal ran in the outgoing interpreter; immutable hint never fired on macOS** ([#315](https://github.com/CryptoJones/omind/issues/315)) — _bug_ —
  8.10.1 -> 9.1.1 ended on `re-provision failed (module 'omind.filelock' has no
  attribute 'exclusive')`: the heal ran in-process after uv had already swapped
  the package, so new `provision.py` got the old release's `filelock` out of
  `sys.modules`. It now re-enters a clean interpreter via a hidden
  `self-update --heal`. Separately, `is_immutable` shelled to `lsattr` (absent on
  macOS), so a `chflags uchg` hook produced a bare "Operation not permitted"
  instead of the unlock instructions — it now reads `st_flags` and prints
  `chflags`, not `chattr`. Shipped in 9.1.2.
- [x] **Consolidate near-duplicate notes instead of only listing them** ([#172](https://github.com/CryptoJones/omind/issues/172)) — _enhancement (memory)_ —
  `omind consolidate` creates machine-local JSON plans and editable Markdown
  drafts without changing the vault. Explicit `--apply PLAN_ID` revalidates
  both source versions, creates the reviewed merged note through OmiStore, and
  archives rather than destroys the originals.
- [x] **Consolidate the four `graph-*` audit tools into one paged `graph` tool** ([#177](https://github.com/CryptoJones/omind/issues/177)) — _chore (tokens)_ —
  `graph(op=path|orphans|dangling|stats)` is the new surface; list operations
  remain paged. The old names are deprecated compatibility aliases for one
  release, with their removal tracked in #181.
- [x] **Tiered memory: a small always-loaded core, a large searched archive** ([#173](https://github.com/CryptoJones/omind/issues/173)) — _enhancement
  (memory)_ — actual note reads update machine-local frequency/recency state;
  SessionStart promotes at most three earned notes and ages them out after 90
  days. Generated, credential-looking, archived, missing, and unsafe targets are
  excluded; the fixed operational/persona core stays pinned.
- [x] **Wire `lint` to the index (one scan, vector-based near-duplicate detection)** ([#174](https://github.com/CryptoJones/omind/issues/174)) — _chore
  (perf)_ — the index stores a fence-stripped lint link view alongside the raw
  graph and exposes title-presence/archive state. Lint uses quantized chunk
  centroids for semantic duplicate pairs, falling back to title Jaccard when
  embeddings are off. Indexed and fallback live-vault issue counts match.
- [x] **Temporal validity on facts, so superseded memories stop being retrieved as current** ([#169](https://github.com/CryptoJones/omind/issues/169)) —
  _enhancement (retrieval)_ — `Supersedes:` / `Superseded by:` metadata
  round-trips through Markdown, CLI, MCP, and mesh merges. The index resolves
  those relationships and de-ranks obsolete notes without deleting history.
- [x] **Quantize stored embeddings (int8) and shrink the index** ([#175](https://github.com/CryptoJones/omind/issues/175)) — _enhancement (efficiency)_ —
  vectors use symmetric int8 storage with a per-vector scale and residual-error
  tie-breaker. The live 5,691-vector index rebuilt at 13 MiB versus roughly
  19 MiB before, with unchanged quality metrics.
- [x] **Adaptive retrieval scope by query complexity** ([#171](https://github.com/CryptoJones/omind/issues/171)) — _enhancement (efficiency)_ — simple,
  normal, and multi-hop queries now use progressively larger candidate depths,
  result caps, and excerpt budgets (20/5/120, 60/10/180, 90/25/240).
- [x] **Rerank the fused top-k so the result tail is clean** ([#167](https://github.com/CryptoJones/omind/issues/167)) — _enhancement (retrieval)_ — the
  fused top 20 receive one bounded, local embedding pass over each whole matched
  chunk body. Weak candidates are rescaled without an API call; an unavailable
  or malformed embedding backend fails open to the original RRF order.
- [x] **Weight auto-generated notes below hand-curated ones** ([#170](https://github.com/CryptoJones/omind/issues/170)) — _enhancement (retrieval)_ —
  journal/worklog/checkpoint OKF types and the established Session Journal /
  Worklog filename conventions receive a modest score penalty, keeping them
  retrievable while comparable curated notes win.
- [x] **A retrieval-quality eval harness** ([#168](https://github.com/CryptoJones/omind/issues/168)) — _chore (testing)_ — `omind bench --quality`
  evaluates a version-controlled labelled query set against the live vault and
  reports recall@1, recall@5, MRR, skipped targets, and the worst misses.
- [x] **Throttle the per-query index refresh** ([#176](https://github.com/CryptoJones/omind/issues/176)) — _enhancement (perf)_ — a burst of indexed reads
  pays the full-vault stat sweep once. OmiStore writes invalidate the process
  cache immediately through the existing signal; direct external edits are
  discovered after a one-second bound.
- [x] **Adversarial review hardening: MCP/web transport deadlocks and API fallback** — _bug (availability/security)_ — `omind node` no longer depends on the SDK's AnyIO file-wrapper stdio path; it uses fd readiness and still feeds the normal MCP session streams, so stdin handshakes and EOF shutdown cannot wedge. The web API no longer relies on Starlette's thread-backed sync handlers or `StaticFiles` fallback; malformed encoded `/api/...` traversal paths return API 404/400 instead of falling into static serving, and packaged assets are served by a direct path-resolved responder that rejects escapes before reading bytes.
- [x] **Adversarial review hardening: transfer archives + subprocess error redaction** — _bug (security/perf)_ — tar.gz export now excludes VCS control directories (`.git`, `.hg`, `.svn`) so mesh vault exports do not leak git history or produce giant bundles; tar.gz import now rejects control-directory members so crafted bundles cannot plant git config/hooks. Shared subprocess failures now redact URL userinfo, GitHub tokens, and Authorization headers before surfacing command/error text.
- [x] **`omind doctor`: report search-index health** ([#178](https://github.com/CryptoJones/omind/issues/178)) — _chore_ — reports FTS5 availability,
  semantic-leg status and its disabled reason, index size/age/note/vector counts,
  and stale, corrupt, or incompatible files with the one-line
  `omind reindex --rebuild` fix.
- [x] **Hybrid search index + MCP token budgets** — _enhancement_ — `omind.searchindex`: FTS5/BM25 over heading-split chunks, quantized chunk vectors, RRF fusion with a weak recency leg, excerpt-returning hits, and the link graph, all in one disposable state-dir SQLite file. Retired `omind.vectorindex` (metadata-only embeddings, JSON float storage, per-query refresh, pure-Python cosine). Paged every list-shaped MCP tool; `read-note` stopped returning the body twice. Added `omind bench`, `omind search --explain`, `omind reindex --index-only/--rebuild`, `docs/retrieval.md`. Measured on a 744-note vault: search 268 ms → 18 ms, natural-language queries 0 hits → ranked answers, `list-notes` ~90,800 → 3,136 tokens.
- [x] **Deferred adversarial-review batch: #125–#131** — _meta_ — all seven shipped and closed upstream: web XSS/Host allowlist ([#125](https://github.com/CryptoJones/omind/issues/125)), macOS CI + wheel smoke ([#126](https://github.com/CryptoJones/omind/issues/126)), tombstone GC ([#127](https://github.com/CryptoJones/omind/issues/127)), per-session loopguard ([#128](https://github.com/CryptoJones/omind/issues/128)), web graph O(n²) ([#129](https://github.com/CryptoJones/omind/issues/129)), vault I/O off the event loop + store lock ([#130](https://github.com/CryptoJones/omind/issues/130)), dependency pinning ([#131](https://github.com/CryptoJones/omind/issues/131)).
- [x] **Hardening batch from adversarial code review** ([#132](https://github.com/CryptoJones/omind/issues/132), [PR #124](https://github.com/CryptoJones/omind/pull/124)) — _meta_ — v3.7.6: note data-integrity (frontmatter/lead + fence-aware parse, symmetric mesh-merge convergence), one-bad-byte read hardening, guard false-positive fixes (freshness `-C`/compound forms, command-anchored forge rules, bare `>` side-effect, project-vs-global `.claude/`, negation-aware auth), guard crash-hardening, enforcement fail-open holes (adapter fail-closed, contentless-consult gate-dodge, secret-output `2>/dev/null` leak), atomic config/hook/backup writes, checkpoint/mesh/update/lint availability, and the migrate-hook data-loss. 714 tests + ruff + mypy green. Deferred items → #125–#131. Shipped in 3.7.6.
- [x] **Codex CLI: `omind setup` only wired the guard, not the `omi` MCP server** ([GitHub #114](https://github.com/CryptoJones/omind/issues/114)) — _enhancement_ — `omind setup --agent codex` now also merges `[mcp_servers.omi]` into `~/.codex/config.toml` (via `tomlkit`, TOML round-trip preserved) so Codex can call the OMI memory tools directly, not just get blocked by the guard. `doctor --agent codex` reports `codex_mcp_registration` alongside `codex_guard`. Shipped in 3.7.0.
- [x] **Rotate `MCP_CONFORMANCE_TOKEN` before it expires** ([Codeberg #88](https://codeberg.org/CryptoJones/omind/issues/88), [GitHub #105](https://github.com/CryptoJones/omind/issues/105)) — _chore_ — `MCP_CONFORMANCE_TOKEN` is set on the omind repo's Actions secrets and verified live: a re-run of `test.yml` installed the private `mcp-conformance` package and ran the suite (`10 passed, 1 skipped`), no graceful-skip. The PAT is non-expiring (Contents:Read on `CryptoJones/mcp-conformance`), so there is no rotation-before-expiry deadline.
- [x] **Guard false-positives on an escalation keyword anywhere in the command** ([#98](https://github.com/CryptoJones/omind/issues/98), [Codeberg #94](https://codeberg.org/CryptoJones/omind/issues/94) / [GitHub #108](https://github.com/CryptoJones/omind/issues/108)) — _bug_ — #98 and #108 were the same root cause and shipped as one PR: the `TIER_SUDO` rules matched `sudo`/`su`/`pkexec`/`doas`/`run0` as a token anywhere in the command (grep args, paths, commit messages, `pass show sudo/...`, the sanctioned `fleet-sudo --entry`). Both rules now use a `Rule.match="command"` mode anchoring to command position (`policy._CMD_POSITION`). Codeberg PR #95.
- [x] **`omind setup` wedges the agent on machines without `jq`** ([Codeberg #93](https://codeberg.org/CryptoJones/omind/issues/93), [GitHub #107](https://github.com/CryptoJones/omind/issues/107)) — _bug_ — the `"*"` guard hook failed closed without `jq`, blocking even the `Bash` call to install it. The hook now routes through the pure-Python `omind guard adapter` when `jq` is missing (enforcement preserved); `doctor` warns instead of failing; `jq` stays out of `REQUIRED_TOOLS`. Codeberg PR #96.
- [x] **LICENSE was paraphrased (non-canonical) Apache 2.0 — replaced with verbatim text** ([Codeberg #91](https://codeberg.org/CryptoJones/omind/issues/91), [GitHub #113](https://github.com/CryptoJones/omind/issues/113)) — _bug_ — the repo-root `LICENSE` declared `Apache-2.0` but the body was a reworded rendering missing the entire `1. Definitions` section (150 lines vs. the canonical ~201), which breaks the SPDX identifier and license scanners. Replaced with the verbatim canonical Apache License 2.0, preserving `Copyright 2026 Aaron K. Clark`. Same bad text also propagated to other repos (`120xSocrates`, `MacminiM2Pro_LocalModelConfig`, `TimeTrackerAPI`, the `scaffold-apache-project` skill, and more) — tracked separately. Shipped in 3.5.3.
- [x] **GitHub-PR hard-block: allow third-party OSS PRs (owner-aware exception)** ([Codeberg #87](https://codeberg.org/CryptoJones/omind/issues/87), [GitHub #104](https://github.com/CryptoJones/omind/issues/104)) — _enhancement_ — the `gh-pr-create-merge` and `gh-api-pr-create` guard rules now BLOCK PRs only to `CryptoJones`-owned repos (Codeberg-only) and ALLOW PRs to third-party repos named explicitly with `--repo <owner>/<repo>` (or `gh api repos/<owner>/<repo>/pulls`); bare `gh pr create|merge` stays BLOCKED. Existing DELETE/push red-team rules untouched. Shipped in 3.5.0.
- [x] **New `secret-output-guard.sh` PreToolUse(Bash) hook** ([Codeberg #86](https://codeberg.org/CryptoJones/omind/issues/86), [GitHub #103](https://github.com/CryptoJones/omind/issues/103)) — _enhancement_ — portable bash guard wired through `omind setup` (registered first in the `Bash` matcher, ahead of `git-fresh-base.sh`); blocks Bash commands that would print a credential VALUE to the transcript (`pass show X | head`, `gh auth token`, literal tokens) while allowing safe forms (`TOK=$(pass show X)`, redirects, curl headers), with an audited `OMI_SECRET_OK=1` override. Shipped in 3.5.0.
- [x] **Interactive `[[wikilink]]` graph view in the web UI** ([#101](https://github.com/CryptoJones/omind/issues/101), [Codeberg #82](https://codeberg.org/CryptoJones/omind/issues/82)) — _enhancement_ — clickable canvas force-graph in `omind serve` (`/api/graph` + dependency-free renderer; click→open note, hover/drag/zoom, theme-aware). Shipped in 3.4.0.
- [x] **Sidebar tag bar pushes the note list off-screen on large vaults** ([#102](https://github.com/CryptoJones/omind/issues/102), [Codeberg #83](https://codeberg.org/CryptoJones/omind/issues/83)) — _bug_ — `#tag-bar` had no height cap; now capped + scrollable. Shipped in 3.4.0.
- [x] **More `omind setup --agent` targets: Claude Desktop, Kiro, VS Code, Amazon Q** ([#100](https://github.com/CryptoJones/omind/issues/100), [Codeberg #79](https://codeberg.org/CryptoJones/omind/issues/79)) — _enhancement_ — register the `omi` MCP server into each tool's config (`claude-desktop`, `kiro`, `vscode`, `q`); MCP-registration only, idempotent, with `quickstart`/`doctor` support. Shipped in 3.3.0.
- [x] **Knowledge Graph Functionality** ([#99](https://github.com/CryptoJones/omind/issues/99)) — _enhancement_ — `omind graph` (neighbors, path, orphans, dangling, stats, export) + `graph-*` MCP tools over the `[[wikilink]]` vault. Shipped in 3.2.0.

---

## PyPI Publish Setup (2026-08-24) ([#267](https://github.com/CryptoJones/omind/issues/267))

- [ ] **`omind` is not yet on PyPI — first publish pending.** The package
  (now at v8.8.0 with DSH agent support) has never been uploaded to PyPI
  (HTTP 404 on the simple index). To publish:
  1. Register the `omind` package name on https://pypi.org/ (account required)
  2. Create a PyPI API token (`pypi-…`) or set up [trusted publishing](https://docs.pypi.org/trusted-publishers/)
     (recommended: GitHub Actions with `permissions: id-token: write` +
     `uv publish --trusted-publishing always` in a `publish` job)
  3. Add a `publish` job to `.github/workflows/test.yml` (see the trusted-publishing
     snippet in the PR description)
  4. Run `uv publish dist/omind-8.8.0-py3-none-any.whl --token pypi-…` (or
     `uv publish --trusted-publishing always` in CI)
  - No credentials exist on any machine (Ronin28, makemake, pluto, telesto) or in
    the `pass` keyring, macOS keychains, GitHub secrets, or `.pypirc` files.
  - Build artifacts are ready in `dist/` (8.8.0 wheel + sdist).

*Proudly Made in Nebraska. Go Big Red! 🌽 <https://xkcd.com/2347/>*
