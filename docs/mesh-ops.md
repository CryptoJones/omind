# Mesh operations

How to run the 2.0 memory mesh day to day. The architecture and the reasoning
behind it live in [mesh.md](mesh.md).

## Bootstrap the first node

```bash
omind setup            # scaffolds the folder, initializes the mesh node,
                       # registers the `omi` MCP server (omind node)
omind mesh install-service   # start the replication daemon (systemd/launchd)
omind doctor           # everything should be green
```

`omind setup --no-mesh` keeps a machine single-node (no git, hard deletes —
the 1.x behavior).

## Add a second machine

On the new machine (with ssh access to an existing node):

```bash
omind mesh clone ssh://firstbox/home/you/Documents/Obsidian\ Vault/OMI
omind setup                       # register the MCP server + hooks here
omind mesh add-peer firstbox ssh://firstbox/...   # if clone's origin isn't enough
omind mesh install-service
```

Back on the first machine, point it at the new one too:

```bash
omind mesh add-peer newbox ssh://newbox/home/you/Documents/Obsidian\ Vault/OMI
```

Every node should know every node (full peer-to-peer). An unreachable peer is
skipped and retried on the next cycle — partitions are normal, not errors.

## Daily operation

There is nothing to do. The daemon commits local writes (debounced) and syncs
on an interval. To force a pass:

```bash
omind mesh sync
```

`omind doctor` shows: node identity, merge-driver health, per-peer
ahead/behind, last-sync age, unresolved conflict markers, archived-note count.

## Deleting notes

- **Delete = archive.** Deleting from any client sets `Disabled: true`; the
  note vanishes from listings but stays on disk and merges conflict-free.
  Restore from the web UI (show archived → Restore) or the `restore-note`
  MCP tool.
- **Purge is the rare exception** — removes the file from *every* node via a
  replicated tombstone:

```bash
omind mesh purge "Old Note.md"
```

## Conflicts

Concurrent edits to the same note merge field by field (see mesh.md). When the
same lines of Details truly diverge, both versions are kept under conflict
markers, the note is tagged `#merge-conflict`, and doctor warns. Open the
note, keep what's right, remove the markers and the tag, save — the fix
replicates like any other edit.

## Privacy

- Meshes never interact unless explicitly peered: there is no discovery and no
  network listener — replication is outbound git over ssh, gated by your keys.
- `mesh init` locks the OMI folder to owner-only (0700) on POSIX: on a shared
  host, a traversable folder would let another local user read the whole
  memory history via a `file://` fetch. Doctor warns if permissions loosen.
- Never `add-peer` a repository you don't own; a peer can read everything.

## Windows

Replication works (git + the same commands); the daemon has no auto-installed
service — run it at logon, e.g.:

```
schtasks /Create /SC ONLOGON /TN omind-mesh /TR "omind mesh daemon"
```
