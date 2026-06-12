# Architecture: Git-Backed Memory Mesh

> **Status:** **Implemented** as of 2.0.0 (see [mesh-ops.md](mesh-ops.md) for
> day-to-day operation).
> **Release:** 2.0.0 (architecture shift).
> **Supersedes:** the per-machine, stdio-only model where each box runs its own
> `obsidian-mcp` against a separately-synced copy of the OMI folder.
> **Date:** 2026-06-10.

This document records the design for turning omind from a single-machine memory
tool into a **mesh**: every machine in the house runs a full local memory node,
and the nodes replicate to one another peer-to-peer over git. Originally a
scope/decision record; the design shipped in 2.0.0 essentially as written
(deviations are noted inline).

## Motivation

The end goal is **shared memory across every Claude client in the house** (this
laptop, Pluto, the macmini, and any future node). The pre-mesh reality fell
short on two fronts at once:

- A **stdio** MCP server is a per-client *local subprocess* — it cannot be
  shared across machines at all. "Sharing" therefore depended entirely on a
  file-sync layer (Hermes' cron) keeping N copies of the vault in agreement,
  which can drift, conflict, or lag.
- That same stdio server (`obsidian-mcp` 1.0.6, now an abandoned upstream) was
  the source of intermittent multi-minute tool-call hangs — see
  [troubleshooting.md](troubleshooting.md).

## Considered and rejected: the central hub

The first design was a **hub** — one always-on HTTP/SSE MCP server (on the
macmini) that every client connects to over the LAN. It is simpler (one source
of truth, strong consistency) but was rejected because it makes the always-on
box a **single point of failure**: if it is down, *no* machine has memory, and
nothing works offline. We deliberately traded strong consistency for
availability and partition-tolerance.

## Chosen: a git-backed mesh

Every machine runs an identical **omind node**:

```
            ┌─────────────────────── omind node (per machine) ───────────────────────┐
            │                                                                          │
  Claude ──▶│  local MCP server  ──▶  OmiStore  ──▶  OMI folder  (a git working tree)  │
  clients   │   (omind, stdio)                              │                          │
            │                                               ▼                          │
            │                        replication daemon  (commit + sync)               │
            └───────────────────────────────────┬──────────────────────────────────────┘
                                                 │  git fetch/merge/push over ssh
                          ┌──────────────────────┼──────────────────────┐
                          ▼                       ▼                      ▼
                     other node              other node             other node
```

- Claude clients talk **only to their local node**. Reads and writes never cross
  the network — they are fast and work fully offline.
- The OMI folder *is* a git working tree. The replication daemon commits local
  changes and syncs with whatever peers are reachable; partitions heal on
  reconnect. Git's decentralization **is** the mesh — there is no central node.
- Plain Markdown stays the canonical on-disk form, so Obsidian and the existing
  `omind serve` web UI keep working unchanged.

## Components (new)

| Module | Responsibility |
| --- | --- |
| `omind/server.py` | Local **node MCP server** exposing `OmiStore` as MCP tools over stdio. Replaces the provisioned `obsidian-mcp`. (Since 2.27.0 the debounced-sync write signal lives in `OmiStore` itself, so every write surface — MCP, web UI, `omind note`, import — triggers it.) |
| `omind/clock.py` | **Logical versioning** — a Lamport counter + stable node-id stamped into each note, the source of ordering truth for merges. |
| `omind/merge.py` | The **git merge driver** for OMI notes (the core of the project). Field-level 3-way merge over `NoteFields`. |
| `omind/mesh.py` | **Replication daemon** — `init`, `commit_local`, `sync(peers)`, `daemon`, `clone`, peer membership. |

### Reused unchanged

- `OmiStore` (`store.py`) — the entire read/write/search/tag/backlink API.
  ("Unchanged" means the API shape and call sites; the locked Rev-in-Metadata
  decision requires `NoteFields`/`parse_note`/`render_fields` to learn the
  `Rev` and `Disabled` lines, added as defaulted fields so legacy notes
  round-trip byte-identical.)
- `parse_note` → `NoteFields` → `render_fields` — the structured model the merge
  driver operates on.
- The CLI scaffold, the ruff + mypy-strict + pip-audit CI bar, and the
  dual-mirror (GitHub + Codeberg) shipping flow.

## Node types & the single-writer rule

A node is any machine that holds a replica and runs the store + the replication
daemon. Two kinds of writer live on a node:

- **Claude Code** (plus the `omind serve` web UI and cron) — already write
  through `OmiStore`, so they inherit the `.omi.lock` flock, the atomic
  `os.replace`, and the `note_version` compare-and-swap.
- **hermes-agent** — Hermes is a **first-class node**, not merely a sync source.
  It is Python and already uses the same primitives (`os.replace`,
  `fcntl.flock`), so it runs omind natively.

**Invariant: every OMI writer on a node goes through `OmiStore` — no raw
writes.** The within-node concurrency guarantee holds only if all writers
serialize under the one shared lock; a single raw `write_file()` into the OMI
folder can interleave with the replication daemon's git commit or clobber a
concurrent store write, defeating the `note_version` CAS.

Today Hermes' `hermes-omi-memory-sync` skill writes raw
(`write_file(".../OMI/<title>.md", ...)`). To make Hermes a safe node, it writes
**in-process through omind** instead:

```python
from omind.store import OmiStore, NoteFields
store = OmiStore(omi_dir)
store.create_note(NoteFields(title=..., summary=..., details=..., tags=[...]))
```

This inherits the lock, the atomic write, and the `note_version` CAS — and, as a
bonus, emits omind's clean note format rather than Hermes' §-block dump, ending
the long-standing format divergence between the two writers.

## The conflict model (the core)

A mesh accepts **eventual consistency**: two nodes can edit the same note while
partitioned, and the merge must converge them **without losing data**.

### Concurrency: SQL-style isolation, mapped to the topology

SQL Server keeps concurrent writes from interfering with one authoritative
transaction manager — locks (pessimistic) or row-versioning under snapshot
isolation (optimistic), all serialized through a single log. That **same idea
applies inside a node, but not across the mesh**, and the split is the whole
point:

- **Within a node (yes — already built):** multiple local writers — several
  Claude clients, the web UI, the cron, the sync daemon — are kept from
  clobbering each other by **optimistic concurrency control**, which is exactly
  SQL Server's snapshot/row-versioning model. As of the inter-process
  write-safety work, `OmiStore` already serializes every write under an advisory
  `flock` on a shared `.omi.lock`, routes note/index writes through an atomic
  same-dir temp-file + `os.replace`, and **re-validates the `note_version`
  compare-and-swap inside the lock** (write only if unchanged, else
  `NoteConflictError`); reads stay lock-free. The mesh adds only one local piece
  on top: the replication daemon's git operations are serialized behind that same
  per-node lock, so a sync never interleaves with a half-written note.
- **Across the mesh (the idea, not the mechanism):** a SQL-style transaction
  needs one coordinator that sees and serializes every write — which is the
  **hub we rejected**. By the CAP theorem you cannot keep that single-authority
  strong consistency *and* stay available while partitioned (a node editing
  offline cannot acquire a lock held on a node it cannot reach). So the mesh uses
  the *distributed* cousin of row-versioning: the **per-note Lamport clock**.
  Writes proceed independently and conflicts are **merged after the fact** rather
  than **prevented by locking** — optimistic replication.

Distributed locks / leases (only the lease-holder may write note X) would
serialize cross-node writes, but they reintroduce a coordinator and forbid
offline writes, so they are rejected for the same reason as the hub.

### Logical clock, not wall-clock

`OmiStore.note_version` is `size-blake2(content)` — a *local* token (content-
based since 2.15.0: mtime+size collided for same-size writes within one
timestamp tick on coarse filesystems). It carries no causality and is kept
only for intra-node optimistic locking. Cross-node ordering uses a **Lamport counter +
node-id** stamped into each note's `## Metadata` section (Obsidian-visible,
keeps notes plain-Markdown). Clock skew across the laptop / Pluto / macmini is
real and is never trusted.

### Field-level merge over `NoteFields`

Because notes round-trip through a structured model, the merge driver parses
base/ours/theirs into `NoteFields` and merges **field by field** rather than
diffing raw text:

- `tags`, `connections`, `references` → **set union** (conflict-free).
- `action_items` → union by text; `done` is logical-OR.
- `title`, `summary`, `related_to` → scalar **last-writer-wins** by Lamport rev,
  tie-broken by node-id.
- `details` (free text) → diff3; disjoint additions concatenate, conflict
  markers only when the *same* region truly diverges.
- `index.md` and any generated file → `merge=ours` in `.gitattributes`, then
  **regenerated** after merge. Generated files are never merged.
- **Disable, not delete** → a "deletion" sets a `disabled: true` flag in the
  note's `## Metadata` rather than removing the file (see *Disable instead of
  delete* below). It is an ordinary field edit, so it merges with the same
  last-writer-wins-by-Lamport rule — **no tombstone, no delete-vs-edit race**.

The whole driver is **lossless-biased and loud**: when unsure, keep both and
report it. The merge driver is where the bugs will live, so it gets the heaviest
test coverage.

### Disable instead of delete

Hard-removing a file is the worst case in a mesh: a peer that still holds the
note resurrects it on the next sync unless every node remembers the deletion
forever (a **tombstone**), and tombstones carry their own garbage-collection and
delete-vs-edit headaches. So omind does **not** hard-delete by default — it
**disables**:

- The delete tool sets `disabled: true` (with the disabling node-id + Lamport
  rev) in the note's `## Metadata`. The file stays in git.
- The local MCP server and the `omind serve` web UI **hide disabled notes** from
  listings and search by default, with an explicit "show archived" /
  **restore** path. Restore just clears the flag.
- Because disabling is an ordinary field edit, it **merges conflict-free** with
  the same machinery as any other change; a concurrent disable + edit resolves by
  Lamport rev on a note that is still present — the delete-vs-edit race is gone.
- True removal still exists as a rare, deliberate **`omind mesh purge`** — an
  explicit hard-delete-with-tombstone for the unusual case where a note must
  really leave every node. It is the exception, not the common path.

This is strictly simpler than tombstoning every delete, and it is reversible.

## Peer transport & topology

- **Transport:** **ssh git remotes** between boxes — reuses the LAN and existing
  ssh keys, encrypted in transit, durable.
- **Partitions are normal**, not exceptional: the laptop roams, Pluto is a
  dual-boot box that is often off. Sync is best-effort, idempotent, and
  order-independent; an unreachable peer is skipped and retried.
- The macmini, being always-on, is a natural **seed** for `omind mesh clone`
  when bootstrapping a new node — but it is a seed, not a hub. Any node can sync
  to any reachable peer.
- A seed can also be a **dedicated passive bare repo** provisioned with
  `omind mesh add-seed`: nodes push their outbox refs to it, its post-receive
  hook points `main` at the freshest one (so fetching from it yields a
  mergeable branch), and `--mirror` replicates the whole seed to a hosted git
  repo (keep it **private** — notes travel in plaintext). Losing it loses
  nothing: every node still holds the full history.

## CLI surface

| Command | Does |
| --- | --- |
| `omind node` | Run the local node MCP server (stdio). |
| `omind mesh init` | Make OMI a git repo; install the merge driver + `.gitattributes`; write node config (node-id, peers). |
| `omind mesh add-peer <name> <git-url>` / `remove-peer` | Manage peer remotes. |
| `omind mesh add-seed <name> <url> [--mirror <git-url>]` | Provision a passive bare seed repo (local path or over ssh) — init, post-receive hook (main pointer + optional mirror push), register as a peer here. Converges on re-run. |
| `omind mesh sync` | One-shot fetch + merge + push against reachable peers. |
| `omind mesh daemon` | Interval sync loop + on-write debounce trigger. |
| `omind mesh clone <url>` | Seed a fresh node from a peer. |
| `omind mesh purge <note>` | Rare, deliberate hard-delete-with-tombstone. The default "delete" only disables; this is the exception. |
| `omind setup` | Register the **local** node with Claude Code (stdio). |
| `omind doctor` | Extended: git health, merge driver installed, per-peer ahead/behind, last-sync time, unresolved-conflict list. |

## Testing

- `tests/test_merge.py` — base/ours/theirs fixtures for every field type;
  concurrent disable + edit, and restore; assert deterministic and lossless.
- `tests/test_mesh.py` — two temp repos as peers; partitioned edits to the same
  note; sync; assert convergence with no data loss.
- Existing `test_store` / `test_web` / `test_provision` stay.
- Git is driven via **subprocess** (not a native binding such as pygit2) to keep
  the `pip-audit` dependency surface clean.

## Phasing

1. **Local node** — `omind node` (stdio MCP over `OmiStore`) + `mesh init`
   (OMI → git) + Lamport rev in notes. *Immediate win: retires the flaky
   `obsidian-mcp` locally.*
2. **Merge engine** — `merge.py` + `.gitattributes` + full `test_merge.py`. The
   core.
3. **Replication** — `add-peer` / `sync` / `daemon` / `clone`, ssh remotes,
   partition tolerance, `test_mesh.py`.
4. **Integration** — `setup` / `doctor` extensions, launchd (macmini) + systemd
   (Linux nodes) service units, docs.
5. **Release** — dual-remote-pr → 2.0.0.

Rough sizing: ~4–5 focused sessions. The merge driver and convergence tests are
the real work; the storage engine and CI already exist.

## Decisions (locked 2026-06-10)

- **Topology:** full peer-to-peer — every node knows every node.
- **Concurrency:** SQL-style optimistic compare-and-swap *within* a node;
  per-note Lamport versioning + merge *across* the mesh; no global or pessimistic
  locking (that would be the rejected hub). See *Concurrency* above.
- **Deletion:** **disable (soft-delete)** — hidden by default, restorable; hard
  removal only via the explicit `omind mesh purge`. (Supersedes the earlier
  "delete-wins-unless-newer-edit" tombstone default.)
- **Lamport rev placement:** in the note's `## Metadata` section
  (Obsidian-visible).
- **Sync trigger:** daemon interval + on-write debounce.
- **Version bump:** 2.0.0.

## Migration & versioning

This is a breaking architecture change → **2.0.0**. Existing nodes migrate by
running `omind mesh init` on each box and adding peers; the per-machine
`obsidian-mcp` registration is replaced by the local `omind node` server.
