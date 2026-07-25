# Backlog

This file and the GitHub **[Issues tab](https://github.com/CryptoJones/omind/issues)** are two
views of the same list and must stay in sync. Every backlog item below has a matching GitHub issue
and vice versa — when an item ships and its issue closes, check the box (or move it to `Done`)
here so neither side drifts.

## Open

_From a 2026-07-24 survey of open-source AI memory layers (Mem0, Zep/Graphiti, Letta/MemGPT,
Cognee, memvid, Memori) and the 2025–2026 agent-memory literature (Memori arXiv:2603.19935 —
81.95% LoCoMo at 1,294 tokens/query; SimpleMem arXiv:2601.02553 — 26.4% F1 gain at 30× fewer
tokens; H-MEM, EACL 2026; RecMem arXiv:2605.16045; Multi-Layer Memory arXiv:2603.29194), scoped
to what omind actually needs. The foundation — a derived SQLite hybrid index (BM25 + vectors +
RRF), excerpt-returning search, and paged MCP payloads — shipped first; see `docs/retrieval.md`.
These are the next tier, ranked by leverage._

### Retrieval quality

- [ ] **Rerank the fused top-k so the result tail is clean** ([#167](https://github.com/CryptoJones/omind/issues/167)) — _enhancement (retrieval)_ — RRF over
  an OR-matched keyword leg gets rank 1 right but admits weakly-related notes at ranks 4–10 (a
  query for "how do I handle colorblindness" pulls in notes matching only "handle"). Reranking is
  the one technique universal across the surveyed systems. Do it locally and cheaply — score the
  top ~20 fused candidates against the query with the existing embedding model over the *whole
  matched chunk* (not the note metadata), or a small cross-encoder behind the `[embed]` extra —
  never an API call on the query path.
- [ ] **A retrieval-quality eval harness** ([#168](https://github.com/CryptoJones/omind/issues/168)) — _chore (testing)_ — ranking changes are currently
  judged by eye on one vault. Add a small labelled query set (query → the note that should win,
  LoCoMo/LongMemEval in miniature) plus a `omind bench --quality` mode reporting recall@1/@5 and
  MRR, so a change to weights, fusion, or chunking is measurable instead of a vibe. Prerequisite
  for tuning `_W_*`, `RRF_K`, or chunk size with any confidence.
- [ ] **Temporal validity on facts, so superseded memories stop being retrieved as current** ([#169](https://github.com/CryptoJones/omind/issues/169)) —
  _enhancement (retrieval)_ — Zep/Graphiti's central idea: a fact has a validity interval, not just
  a creation date. omind has many notes that supersede earlier ones ("v1.2.0 released" → "v1.3.0
  released") and search happily returns the stale one. Model supersession explicitly (a
  `Supersedes:`/`Superseded by:` metadata line, indexed), and de-rank superseded chunks instead of
  deleting them.
- [ ] **Weight auto-generated notes below hand-curated ones** ([#170](https://github.com/CryptoJones/omind/issues/170)) — _enhancement (retrieval)_ — session
  journals, worklogs, and checkpoint rollups are numerous, dense, and topically broad, so they
  crowd genuine memories out of BM25 results. The OKF `type` and the `Session Journal` naming
  convention already distinguish them; make that a ranking signal.
- [ ] **Adaptive retrieval scope by query complexity** ([#171](https://github.com/CryptoJones/omind/issues/171)) — _enhancement (efficiency)_ — SimpleMem's
  third stage: a one-word lookup and a multi-hop question should not retrieve the same `k`. Vary
  depth and returned excerpt budget by query shape, so simple recalls get cheaper rather than
  everything getting more expensive.

### Memory shape

- [ ] **Consolidate near-duplicate notes instead of only listing them** ([#172](https://github.com/CryptoJones/omind/issues/172)) — _enhancement (memory)_ —
  SimpleMem/RecMem both show recursive consolidation is where lifelong memory stops degrading.
  `omind lint` reports near-duplicates and stops; the index now has the chunk vectors needed to
  detect real semantic duplicates (not just similar titles). Add an `omind consolidate` that
  proposes merges and applies them under review — never silently.
- [ ] **Tiered memory: a small always-loaded core, a large searched archive** ([#173](https://github.com/CryptoJones/omind/issues/173)) — _enhancement
  (memory)_ — Letta/MemGPT's core/archival split. omind approximates it with `index.md` +
  `Playbook.md` priming, but tier membership is hand-maintained. Promote/demote by access
  frequency and recency, so the always-injected set stays small and earns its tokens.
- [ ] **Wire `lint` to the index (one scan, vector-based near-duplicate detection)** ([#174](https://github.com/CryptoJones/omind/issues/174)) — _chore
  (perf)_ — `lint_vault` is now the last independent full-vault scanner, and its near-duplicate
  pass is O(n²) over titles (~553k comparisons on 744 notes). Deliberately left alone during the
  index work: lint strips code fences before extracting links, and the index does not, so a naive
  swap would resurrect the false broken-link errors that fix exists to prevent. Do it properly —
  index a fence-stripped link set alongside the raw one.

### Efficiency

- [ ] **Quantize stored embeddings (int8) and shrink the index** ([#175](https://github.com/CryptoJones/omind/issues/175)) — _enhancement (efficiency)_ — the
  index is ~19 MB for 5,678 chunks, almost all of it `float32` vectors. int8 with a per-vector
  scale is standard practice (and what the low-storage retrievers in the survey do), cutting the
  file ~4× and speeding the matmul, with negligible ranking loss at this scale. Add a residual-norm
  tiebreaker for near-equal cosines.
- [ ] **Throttle the per-query index refresh** ([#176](https://github.com/CryptoJones/omind/issues/176)) — _enhancement (perf)_ — every `search()` calls
  `refresh()`, which stats every note (~5 ms of a ~18 ms query on 762 notes). Skip the refresh when
  the vault's cheap signature is unchanged since the last one within the same process, and let the
  existing write-signal file invalidate it, so a burst of queries pays the stat sweep once.
- [ ] **Consolidate the four `graph-*` audit tools into one paged `graph` tool** ([#177](https://github.com/CryptoJones/omind/issues/177)) — _chore (tokens)_
  — 16 always-resident tool definitions cost ~1.2–1.8k tokens of every context. `graph-path`,
  `graph-orphans`, `graph-dangling` and `graph-stats` are one tool with an `op` argument. **Needs a
  decision first:** it renames tools that the OMI vault's `Playbook.md`, the managed `omind` skill,
  and other fleet machines reference, so it is a coordinated change, not a local one.
- [ ] **`omind doctor`: report search-index health** ([#178](https://github.com/CryptoJones/omind/issues/178)) — _chore_ — doctor knows nothing about the
  index. Report FTS5 availability, whether the semantic leg is on, index size/age, and any stale
  or corrupt file (with the one-line `omind reindex --rebuild` fix), since a silently-degraded
  index shows up only as worse answers.

## Not planned

- [ ] **Long game: fine-tune a model on the accumulated violation corpus** ([#91](https://github.com/CryptoJones/omind/issues/91), closed not-planned) — _roadmap (Phase 4)_ — deferred: the blocker is data, not compute. The live `compliance.jsonl` corpus is ~91% relevance-noise, ~6% real denies, and 100% DENY (zero ALLOW), so training on it as-is yields an always-deny model. Revisit only after `export-corpus` is reworked to synthesize balanced ALLOW examples (from the deterministic `guard.decide()`) and split the relevance corpus from the action corpus. The mechanical guard remains the backstop.
- [ ] **Adopt an external memory framework (Mem0 / Cognee / Zep) as the storage layer** — _rejected_ — evaluated during the 2026-07-24 survey. Every one of them wants to own storage, and omind's whole premise is that the Markdown vault is the source of truth: plain files, git-replicated across the mesh, readable in Obsidian, with no service to run. The techniques are worth copying; the dependency is not.

## Done

- [x] **Hybrid search index + MCP token budgets** — _enhancement_ — `omind.searchindex`: FTS5/BM25 over heading-split chunks, packed `float32` chunk vectors, RRF fusion with a weak recency leg, excerpt-returning hits, and the link graph, all in one disposable state-dir SQLite file. Retired `omind.vectorindex` (metadata-only embeddings, JSON float storage, per-query refresh, pure-Python cosine). Paged every list-shaped MCP tool; `read-note` stopped returning the body twice. Added `omind bench`, `omind search --explain`, `omind reindex --index-only/--rebuild`, `docs/retrieval.md`. Measured on a 744-note vault: search 268 ms → 18 ms, natural-language queries 0 hits → ranked answers, `list-notes` ~90,800 → 3,136 tokens.
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

*Proudly Made in Nebraska. Go Big Red! 🌽 <https://xkcd.com/2347/>*
