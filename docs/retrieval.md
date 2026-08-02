# Retrieval: how omind finds a memory

omind stores memories as plain Markdown and *searches* them through a derived
SQLite index. Those are two different jobs, and keeping them separate is the
whole design: the notes in your vault are the source of truth, and the index is
a disposable cache you can delete at any time.

```
~/Documents/Obsidian Vault/OMI/*.md        source of truth — synced, committed, yours
        │  (mtime + content hash)
        ▼
$XDG_STATE_HOME/omind/searchindex-<id>.sqlite3
        ├── chunks_fts   FTS5 / BM25 over title, heading, tags, text, stems
        ├── vectors      int8 embeddings + per-vector scale (optional)
        ├── links        resolved [[wikilinks]]
        └── notes        identity, tags, created, archived flag
```

The index is machine-local. It is never committed and never crosses the mesh —
it is derivable from the notes and specific to one embedding model.

## What a query does

A search runs three independent rankings and fuses them:

| Leg | What it is good at | Weight |
| --- | --- | --- |
| **Keyword (BM25)** | exact terms, identifiers, filenames, tags | 1.0 |
| **Semantic (vectors)** | paraphrase — a question sharing no word with the note | 0.9 |
| **Recency** | breaking ties toward what you wrote lately | 0.25 |

They are combined with **Reciprocal Rank Fusion** (`score = Σ weight / (60 +
rank)`), which needs no score calibration between legs — a rank-1 BM25 hit and a
rank-1 cosine hit are comparable even though 12.4 and 0.83 are not.

Two rules keep the result honest:

- **Recency re-ranks; it never adds.** A note that matched nothing cannot ride
  the recency leg into your results. (Before the index, search sorted purely by
  date, so the newest notes came back regardless of relevance.)
- **Credential notes are de-prioritised** unless your query is itself about
  credentials — the same rule the consult gate applies. Search must never steer
  an agent toward the secrets notes.
- **Superseded facts remain history, not current truth.** A note carrying
  `Superseded by:`—or targeted by another note's `Supersedes:` metadata—stays
  searchable but receives a strong ranking penalty.
- **Self-declared low confidence loses a tie, not the race.** `Confidence: low`
  applies a gentle penalty (0.8) so a comparable verified note wins. Low
  confidence is not obsolescence, so it is nothing like the superseded penalty.
- **Conflicts are surfaced, never resolved.** When a note carries
  `Conflicts with: [[Other]]`, *both* notes come back marked with the other's
  name — the claim is symmetric even when only one side wrote it down. Ranking
  does not pick a winner: the agent is told the two memories disagree and can
  read both. `omind lint` reports a conflict whose target does not exist
  (`conflict-broken`) and one the other side never acknowledged
  (`conflict-one-sided`).

The keyword leg is graded: chunks matching *every* word of your query rank above
chunks matching only some. That keeps a filler word ("how do I **handle**…")
from dragging in noise, without any hand-tuned stopword list.

## Chunks, not documents

Each note is split at its `## headings` (with long sections split again at
paragraph boundaries), and every chunk is indexed separately. This is why a fact
written only in `## Details` is findable, and why a hit can tell you *which
section* matched.

Every hit carries a bounded **excerpt** — the matched text, snipped by FTS5
around your terms. That excerpt is usually enough to answer the question without
opening the note at all, which is where the token savings come from.

An actual `read-note` or `recall-note` access updates a separate machine-local
frequency/recency counter. SessionStart uses that derived signal to promote at
most three earned notes into a bounded dynamic core; notes untouched for 90 days
age out. This state lives beside the index, never in the vault, and credential
or generated notes are never promoted.

## Semantic search is optional

Without the `[embed]` extra, the vector leg is simply skipped: BM25, recency,
links, and excerpts all work. To turn semantics on:

```bash
uv tool install --with 'omind[embed]' git+https://github.com/CryptoJones/omind
# or, in a checkout:  pip install -e '.[embed]'
```

The model is `minishlab/potion-base-8M` (a ~30 MB static embedding — no GPU, no
API, no network at query time). Override with `OMI_EMBED_MODEL`; changing it
invalidates every stored vector and rebuilds.

## Reviewing near-duplicates

The same chunk vectors power a guarded consolidation workflow:

```bash
omind consolidate --limit 3
# edit the reported machine-local .md draft, then:
omind consolidate --apply 0123456789abcdef
```

The first command does not edit the vault. It writes a JSON plan and an editable
Markdown draft under omind's machine-local state directory. Applying a plan
rechecks opaque content versions for both source notes, creates the reviewed
draft through `OmiStore`, then archives the originals with `Disabled: true`.
If either source changed during review, apply refuses the stale plan. It never
silently merges or hard-deletes memory.

## Operating it

```bash
omind reindex                    # refresh index.md AND the search index
omind reindex --index-only       # just the search index
omind reindex --rebuild          # discard and rebuild from scratch
omind search "why did signing fail" --explain    # per-leg ranks behind each hit
omind bench                      # latency + token cost on your real vault
```

You rarely need any of these: every search refreshes the index first, and the
refresh only re-reads notes whose bytes actually changed.

**Turning it off.** Set `OMI_INDEX_DISABLE=1` and every path falls back to the
pre-index full-vault substring scan. The same fallback happens automatically if
FTS5 is missing, the index file is corrupt, or another process holds the write
lock — a broken index degrades search, it never breaks it.

## Measured on a 744-note vault

| | before | after |
| --- | --- | --- |
| `search "nebraska"` | 268 ms | 18 ms |
| a natural-language question | 276 ms, **0 hits** | 45 ms, 10 ranked hits |
| full index build | — | 1.5 s (5,691 chunks, 13 MiB) |
| incremental refresh | — | 5 ms |
| `list-notes` tool payload | ~90,800 tokens | 3,136 tokens (one page) |

Reproduce with `omind bench` on your own vault.

*Proudly Made in Nebraska. Go Big Red! 🌽 <https://xkcd.com/2347/>*
