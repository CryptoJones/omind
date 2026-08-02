# AGENTS.md — orientation for AI agents working on omind

Read this before changing code. It covers what a contributor doc can't: the
invariants that break *silently*, the current state of in-flight work, and the
dead ends someone already walked down.

For dev setup, the four quality gates, the module map, SemVer rules, and PR
conventions, see **[CONTRIBUTING.md](CONTRIBUTING.md)** — this file does not
repeat them.

---

## The one-paragraph version

omind gives AI agents durable memory as **plain Markdown notes** in an Obsidian
vault, replicated peer-to-peer over git, exposed to agents through a local MCP
server (`omind node`) and enforced by a hook-based guard. Roughly a third of the
repo is memory; the rest is agent enforcement (`guard.py`, `policy.py`,
`verify.py`), fleet provisioning (`provision.py`, `agents.py`), and mesh
replication (`mesh.py`, `merge.py`). Know which subsystem you are in.

## Invariants — violate these and something breaks quietly

1. **The Markdown files are the source of truth.** Indexes, caches, and vectors
   are derived and disposable. Anything derived lives in the state dir
   (`paths.state_dir()`), never in the vault: vault contents are committed and
   mesh-synced, so a derived file there would replicate machine-specific junk
   fleet-wide.
2. **Retrieval fails open.** Every layer of search returns `None` on any problem
   and the caller falls back to the older, dumber path. A missing model, a
   corrupt index, a locked database, a missing FTS5 build — all degrade search;
   none may break it. Test the failure branch, not just the happy one.
3. **Multi-note writes go through `omind.txn`.** One note at a time is already
   atomic (same-dir temp + `os.replace`). Two or more is not, and a crash
   between them used to leave partial state with no way back. Any new operation
   that writes several notes must journal its pre-images through
   `txn.Transaction` under `store.write_lock()`, or it reintroduces the gap
   `omind recover` exists to close. Recovery refuses to overwrite a note edited
   after the crash — that edit is newer than the pre-image.
4. **All note writes go through `OmiStore`** (`notes.upsert_note` for external
   writers) — flock, atomic rename, Lamport `Rev:` stamping, soft delete.
   Deletes archive (`Disabled: true`); only `omind mesh purge` truly removes.
5. **`OmiStore.safe_name` guards every read and write.** Path traversal must stay
   impossible; there are tests enforcing this. Don't route around them.
6. **`store.py` stays framework-free.** No FastAPI, no MCP. The CLI and the web
   app both build on it.
7. **Credential notes are de-prioritised** in search and in the gate's
   suggestions unless the query is itself about credentials
   (`retrieve._CREDENTIAL_PENALTY`). The gate must never steer an agent into the
   secrets notes. This is load-bearing, not decoration.
8. **No MCP tool may return unbounded output.** Every list-shaped tool pages
   (`limit`, `offset`, `total`, `has_more`) via `server._page`. `list-notes` once
   returned ~90,800 tokens in a single result; `tests/test_server.py::
   test_every_list_tool_is_bounded` exists so a new tool cannot regress that.
9. **Reserved files are not memories.** `index.md` and `Memory Template.md` are
   scaffolding; reading one does *not* clear the consult gate
   (`paths.NON_CONSULT_FILENAMES` — an anti-dodge measure, issue #109).

---

## Handoff: the retrieval subsystem (2026-07-24)

### State

Branch **`feat/hybrid-retrieval-token-budgets`**, one commit (`2eea146`),
**not pushed to either mirror**. 808 tests + ruff + mypy strict green.
Version deliberately **not bumped** — changes sit under `## [Unreleased]` in
`CHANGELOG.md`; the next release is 4.3.0 (module docstrings already say so).

### What changed and why

Search used to read and parse *every note on every query* to run `needle in
haystack`, then sort by **date**. A natural-language question with no literal
substring returned nothing. `omind.searchindex` replaces that with a derived
SQLite index: FTS5/BM25 over heading-split chunks + packed `float32` chunk
vectors + the resolved `[[wikilink]]` graph, fused with Reciprocal Rank Fusion.

Read **[docs/retrieval.md](docs/retrieval.md)** first — it is the design doc.

Measured on the live 744-note vault: search 268 ms → 18 ms; natural-language
queries 0 hits → 10 ranked hits; `list-notes` payload ~90,800 → 3,136 tokens.

### Things that will bite you

- **`search()` refreshes the index on every call.** That's a `stat()` per note
  (~5 ms of an ~18 ms query). Intentional — it keeps results correct when
  another process wrote a note — but it is the throttling target in #176.
- **Recency is a *leg*, not a sort key, and it may only re-rank what the content
  legs matched.** An early version let it add notes, and every query returned the
  whole vault newest-first — exactly the bug the index exists to kill. Guarded by
  `test_recency_only_reorders_matches_it_never_adds_notes`.
- **The index does *not* strip code fences from `[[wikilinks]]`; `lint.py` does.**
  That asymmetry is why lint was left as the last independent full-vault scanner
  (#174). Swapping it naively resurrects false broken-link errors.
- **`link_targets()` preserves case.** Dangling-link reports quote links as the
  author wrote them; only resolution lowercases. Lowercasing at ingest broke
  three tests.
- **Never mutate a `NoteSummary` from `_cached_summary`** — it is the shared
  cached instance. Use `dataclasses.replace` (see `store._indexed_search`).
- **`embed.encode` may return a plain list** in tests that fake the backend.
  Coerce through `searchindex._query_vector`, never assume `.shape`.
- **The `[embed]` extra reaches huggingface.co** on first use to check the model
  revision (cached afterwards). It fails open when offline.

### Dead end already walked

Filtering query terms by **document frequency** (drop terms appearing in >N% of
chunks, keep the rare ones) to clean up noisy results. It cannot work: on a real
vault `handle` sits at 1.6% and `colorblind` at 0.32% — both "rare" in absolute
terms, so no fixed ratio separates filler from signal, and a relative cutoff
drops genuinely meaningful common words like `release` (12%). It was replaced by
**graded matching** (all-words-match ranks above any-words-match), which is
threshold-free and behaves the same on a 10-note vault and a 10,000-note one.
Don't reintroduce the df filter; the real fix is reranking (#167).

### Verifying retrieval changes

```bash
omind bench                                        # latency + token cost, real vault
omind search "why did release signing fail" --explain   # per-leg ranks behind each hit
omind reindex --rebuild                            # discard and rebuild the index
OMI_INDEX_DISABLE=1 omind search "…"               # prove the fallback path still works
```

Always check both paths: with the index and with `OMI_INDEX_DISABLE=1`. A change
that only works when the index is healthy has broken invariant #2.

### Where to pick up

Twelve issues, [#167–#178](https://github.com/CryptoJones/omind/issues), mirrored
in [BACKLOG.md](BACKLOG.md). Highest leverage first:

1. **#168 — eval harness.** Do this *before* #167. Ranking is currently judged by
   eye on one vault, so any weight/fusion/chunking change is unmeasurable.
2. **#167 — rerank the fused top-k.** RRF gets rank 1 right but admits weak
   matches at ranks 4–10. Local only; never an API call on the query path.
3. **#176, #175** — cheap, contained perf/size wins.

**One item needs the user's decision before any code:**

- **#172** (`omind consolidate`) merges notes. Propose-and-review only — a wrong
  merge destroys memory that exists nowhere else.

---

## Working agreements

- **BACKLOG.md and the GitHub Issues tab are two views of one list.** File an
  issue for every backlog line and link it both ways; check the box when it
  ships. Both had drifted badly before 2026-07-24 (every "Open" item was already
  closed upstream) — don't let that happen again.
- **Two mirrors:** GitHub and Codeberg, kept at the same `main`. Commits land on
  both.
- **Commit/push only when asked.** Branch off `main` (`feat/`, `fix/`, `docs/`,
  `chore/`, `refactor/`), conventional-commit subjects.
- **Docs carry the footer** (`*Proudly Made in Nebraska. Go Big Red! 🌽
  <https://xkcd.com/2347/>*`); the README carries the centred banner version.
- **Report honestly.** If a gate fails, say so with the output. If you skipped
  part of the scope, say which part and why — see how #174 and #177 were left
  open rather than half-done.

---

*Proudly Made in Nebraska. Go Big Red! 🌽 <https://xkcd.com/2347/>*
