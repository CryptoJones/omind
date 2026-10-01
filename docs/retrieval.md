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

## The name index

Ranked search dilutes a rare exact name (a drive label, a host, a serial) among every
other word in the query. The name index answers the narrower question directly:
*which notes mention this exact identifier?*

```bash
omind entity As30p              # every live note that mentions it, newest first
omind entity As30p --json       # the same, machine-readable
omind entity utf8 --all         # list it even if it is too common to be an entity
```

At index time each note's title, tags and body are scanned for identifier-shaped
tokens only — mixed letters and digits (`As30p`, `WX81A255JFYD`), hostnames and
domains under a known TLD, IPv4 addresses, `owner/repo` slugs, `/Volumes/<label>`
and `[[wikilink]]` targets. Plain words never qualify; units (`16GB`), bare versions
(`v10`), long hex hashes, filenames (`store.py`) and the `- Rev:` / `- Agent:` writer
stamps are skipped. Matching is exact on the token, case-insensitive, with no
embeddings, so it is cheap enough to run on every tool call.

A token in more than 15% of the vault's notes (never fewer than 10) is reported as
too common rather than as an entity. Tune with `OMI_ENTITY_MAX_DF=<fraction>`;
turn the whole index off with `OMI_ENTITY_INDEX=0`. On a 1,432-note vault it adds
~1.4 MiB (+4.5%) to the index and ~0.5 s (+12%) to a full rebuild; incremental
refreshes are unchanged.

### A rare name clears the preflight threshold

The per-turn preflight stays silent unless the prompt and its best note share at
least 3 meaningful terms (`OMIND_PREFLIGHT_MIN_TERMS`). One exact hit on a rare
identifier is a single term, so "is As30p mounted?" used to be reported as a weak
match even though the vault had notes about exactly that drive. Now an identifier
from the prompt that the name index lists under its rarity ceiling, and that the
candidate note mentions, clears the threshold on its own
([#386](https://github.com/CryptoJones/omind/issues/386)). Plain-word overlap keeps
the 3-term floor, and a token over the ceiling counts as a plain word.

A turn cleared only by a name is always a hint (note titles, never the body, even
under `OMIND_PREFLIGHT=inject`), and it counts against the session's injection
budget like any other hint. `OMIND_PREFLIGHT_RARE_TERMS=0` (or `OMI_ENTITY_INDEX=0`)
restores the plain threshold.

### Names in tool output

The preflight sees only the prompt. A name that turns up only in what a tool
printed, such as a volume label in `diskutil list`, a host in `ssh` output or a
serial in `ioreg`, used to reach no hook at all
([#388](https://github.com/CryptoJones/omind/issues/388)). Claude Code's PostToolUse
hook now extracts identifier-shaped tokens from the first 16,000 chars of each tool
response, using the same extractor as the index. It looks them up in one read-only
query: the hook opens the index file read-only, so it loads no embedding model and
runs no refresh. Each new name with notes gets one titles-only line:

```
OMI: As30p → 118 notes; about it: [[WD Blue As30p drive check 2026-09-27 — clean, and the WD to pluto copy FAILED]]; [[…]]. recall-note before asserting facts about As30p.
```

"about it" lists notes whose title names the thing (not as a path component), newest
first; a name no title carries shows the newest notes that mention it ("newest").

What keeps it quiet:

- A name is hinted once per session, and a name the preflight already hinted for a
  prompt counts. At most 3 names are hinted per tool call, and name hints stop after
  8,000 chars per session (`namehints.SESSION_BUDGET_CHARS`) or when the 60,000-char
  push budget is spent.
- Pull is never a trigger: the agent's own `mcp__omi__*` results, `omind` CLI output,
  and files inside the vault.
- File tools (Read, Grep, Glob, Write, Edit) are skipped. Their output is code the
  agent chose to open, or its own write echoed back.
- Names the agent typed into the call itself are skipped. The hint is for names that
  arrive only in the output.
- Some tokens look like identifiers but never name a thing: formats and platforms
  (`utf-8`, `arm64`, `python3`), lower-case hex (commit hashes, colours), MIME types,
  and wikilink titles already spelled out. These are skipped.
- A name that no note's title carries must be in at most 10 notes.
- Salience: in an output of 500+ chars, a name mentioned only once is skipped.

Hints go to the usage ledger as operation `namehint` on the push channel. `omind audit`
judges them as their own surface. `OMIND_TOOL_NAME_HINTS=0` turns them off.

`omind bench --tool-hints --transcript PATH` replays a Claude Code `.jsonl` session, or
a directory of them (`--days N` keeps recent ones), through the same picker. Each
replayed hint only sees notes dated on or before its tool result. The replay reports
how often hints fire, the added latency, and precision. Precision here means the share
of hints whose name the agent itself used later in the session. On a 1,432-note vault,
347 sessions from one week gave these numbers:

- hints on 6.7% of all tool results, which is 8.5% of the ~10,700 the hook
  considers (not pull, not a file tool);
- precision 58.6%, against 56.5% for the preflight's labelled `bench --precision` on
  the same vault;
- added latency 0.49 ms median and 1.9 ms p95.

### Names in a write

`create-note` and `edit-note` answer with what other notes already say about the
names in the write ([#389](https://github.com/CryptoJones/omind/issues/389)). The agent
has just stated a claim and is still in the turn, so this is the cheapest place to
catch a contradiction:

```json
"related_by_entity": [
  {"name": "As30p",
   "title": "WD Blue As30p drive check 2026-09-27 — clean, and the WD to pluto copy FAILED",
   "summary": "…", "updated": "2026-09-27"}
],
"related_by_entity_note": "Advisory only; the write succeeded. …"
```

Names are taken from the title, summary, tags and details (`edit-note`: the note's
title plus the fields the call passed) with the name-index extractor and looked up in
one read-only query. Each name lists up to 3 other notes. Notes titled after the name
come first and are the only ones listed when any exist; otherwise its newest mentions
are listed. Rules:

- At most 3 names, summaries cut to 200 chars, the whole list at most 2,400 chars
  (`writecontext.MAX_CHARS`). A note listed for one name is not repeated for another.
- The note being written and archived notes are never listed.
- The same noise rules and rarity rules as tool-output name hints apply. Also skipped:
  sequence tokens (`run4`, `round-3`, `ch13`), names the write mentions only as a path
  component (`/Volumes/As30p/courses`), and a name that appears just once in a
  `details` of 500+ chars.
- It is advisory. It fails open and never blocks or changes the write.

It is **pull**: it is part of the response to the agent's own `mcp__omi__*` call, so
the ledger counts it with that result under `"channel": "pull"` and it never spends
the push budget. `OMIND_WRITE_CONTEXT=0` turns it off.

`omind bench --write-context --transcript PATH` replays the create-note and edit-note
calls in a session, or a directory of them (`--days N`), through the same picker. Each
replayed write only sees notes dated on or before its call. On a 1,432-note vault, 347
sessions from one week (239 writes) gave:

- a list on 54.0% of writes, 3.3 notes and 1,184 chars median when listed (p95 2,293);
- 2.6 ms median added latency, 8.5 ms p95;
- in a hand-judged sample of 50 listed notes, 29 (58%) were about the same thing as
  the write. The bench's own proxy (the session read or linked the listed note) is
  16.9%; it undercounts because the agents never saw the list. The misses are mostly
  a name with several senses: `As30p` is a drive label, a user, a DJ name and a
  channel.

## Push and pull budgets

The session injection budget (`guard.SESSION_INJECTION_BUDGET_CHARS`, 60,000 chars)
limits **push**: context omind decides to send — SessionStart priming, the per-turn
preflight, name hints. The agent's own `mcp__omi__*` reads are **pull**: the usage
ledger marks them `"channel": "pull"`, and they no longer count against the budget
([#387](https://github.com/CryptoJones/omind/issues/387)). Before this, a diligent
session that read its way to 60K chars switched off even 500-char title hints.
`omind audit` judges the two separately (`session.p99_tokens` for push,
`session.pull_p99_tokens` for pull); pull has its own threshold but never suppresses
a hint. Ledger events written before the split carry no channel and count as push.
`OMIND_SPLIT_BUDGET=0` restores the combined budget.

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
