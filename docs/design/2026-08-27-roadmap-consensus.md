# Roadmap design consensus — FlatlineRoundtable, 2026-08-27

Five design briefs (one per feature-gap item) put to the 12-lane panel with
`--diff` (two independent readers). Verdicts: **zero REJECTs**; every item is
ADOPT or ADOPT-WITH-CHANGES. Where lanes split, the MAJORITY ruling is adopted
and the dissent recorded. One lane (MasterControl) repeatedly cited invented
`file:line` specifics — flagged by the readers, discounted here. Raw transcripts:
`~/.local/share/flatline-roundtable/transcripts/`.

## 1. Agent identity — SHIPPED (`feat/agent-identity`)

Metadata-only `Agent:` line (never in the Rev), optional/best-effort, advisory
only (never consumed for ranking/access/trust), 64-char slug cap, merge follows
the Rev winner and BLANKS on the equal-rev tiebreak. Implemented; 1,010 tests
green.

## 2. Usefulness feedback loop — ratified, not started

- Counts live in a **separate state-dir table**, never inside the search-index DB
  (11/11: a schema bump must never wipe usage history); joined at query time.
- **Decay-only, no boost** (8/11 majority): a term that only subtracts cannot
  self-reinforce. Dissent (HAL9000): decay punishes new/never-surfaced notes —
  answered by a decay floor and onset at 30 days unread.
- **Self-read exclusion is mandatory**: per-session dedupe of reads; capsule/
  hook-injected reads do not count as organic usefulness signals (Cortana/
  Wintermute's position-basis concern).
- Influence bounded, **reorder-never-add** (same contract as the recency leg —
  HAL9000 independently named `test_recency_only_reorders_matches...` as the
  guard to extend).
- #173 tiered-core promotion **shares the counts table but keeps its own scorer**
  — never blend the two effects (double-counting).
- Every weight visible in `search --explain` (counted and filtered reads).

## 3. Sleep-time janitor (`omind maintain`) — ratified, not started

- **Consolidation merges are never auto-applied** — unanimous; propose-and-review
  is permanent.
- **Refuse to run while a mesh sync is in flight** (explicit in-flight check;
  10/11 vs HAL9000's per-stage-timeout dissent, recorded).
- **No per-run report notes in the vault** (8/11: "the janitor janitors its own
  garbage" — reports become lint/dedup candidates and replicate). Stdout +
  state-dir; opt-in `--report-note` only.
- Index maintenance must **build-and-swap, never rebuild in place** — a searcher
  seeing an empty in-place index silently degrades (the #210 class again).
- **Fail-closed pipeline**: abort before GC if any earlier step fails.
- **GC/mesh-sync stays OUT of `--apply`** (majority): it is the only step that
  propagates fleet-wide and is irreversible — opt-in `--sync`.
- Rollups: a genuine 5/5 split (deterministic+replayable vs lossy history-squash)
  → resolved conservative: **opt-in `--rollup`**, not in the default apply set.
- Fleet: one designated machine per vault; PID/flock mutex against double-janitor.

## 4. Write-time near-duplicate warnings — ratified, not started

- Advisory, fail-open, never blocks the write, **on by default** — unanimous.
- **create-only for v1** (8/11: edits fire far more often and aren't dups; the
  edit-instead-of-create bypass is accepted as the v1 gap).
- Threshold: fixed camp (0.85–0.90) vs calibrate camp (p99 of NN scores) —
  **0.88 placeholder + calibration follow-up** (`omind lint --calibrate-dup`).
- **Drop the `hint` field for v1** (Cortana/HAL9000/Wintermute: cosine cannot
  distinguish "replaces" from "disagrees"; a fabricated `Supersedes:` corrupts
  the graph retrieval leans on). Return candidates + excerpt only.
- **Exclude archived notes** (7/4) and notes already carrying `Superseded by:`
  (Cortana's refinement). Top-3 candidates.

## 5. Scoped writes + scratch tier — ratified, not started

- Docs reframed (unanimous): scope becomes a **guard-enforced operational
  interlock against accidents** — and the doc must say explicitly that it is
  bypassable (env var, direct file edits, hook-skipping processes). "Not a
  security boundary" survives; the bare one-liner does not.
- **Deny** out-of-scope writes when both sides declare scope (7/3 majority, with
  an escape hatch: `OMIND_SCOPE_MODE=warn`); undeclared `OMIND_SCOPE` stays
  fail-open; deny messages include remediation.
- **Scratch is machine-local by default** (8/3): never committed by the mesh —
  "ephemeral + committed to permanent git history is a contradiction". Dissent
  (cross-host mid-task context loss) answered by an opt-in replicate flag.
- **TTL: 7 days from last modification** (not creation — HAL9000's clock
  refinement). Expiry **archives, never deletes**.
- Exempt system-originated writes (mesh sync, tombstones) from the guard check,
  or the first merge trips its own guard (HAL9000).

---

*Proudly Made in Nebraska. Go Big Red! 🌽 <https://xkcd.com/2347/>*
