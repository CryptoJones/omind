# Security

## Reporting a vulnerability

Email **akclark@thenetwerk.net** with details and a reproduction. Please do not
open a public issue for a security report. Expect an acknowledgement within a few
days.

## Security posture

omind is a **local, single-user** tool: a stdio MCP server, a CLI, and a
localhost web UI over a Markdown memory folder, with optional git-based mesh
replication. It makes no outbound network calls in the core server — all I/O is
the local filesystem plus `git`/`restic` subprocesses.

Controls in place:

- **Path-traversal protection** — `store.safe_name()` rejects illegal characters
  and validates every note path against the OMI root.
- **Atomic, single-writer writes** — `_atomic_write()` (tempfile + `os.replace`)
  plus an advisory `flock` serialize concurrent writers; edits use a
  compare-and-swap `expected_version` to detect concurrent changes.
- **No committed secrets** — backup passwords and mesh node IDs are generated at
  runtime with `secrets.token_*` and stored outside the repo. CI runs `pip-audit`
  (dependency CVEs) and `gitleaks` (secret scan, full history).
- **Soft delete** — notes are archived, never hard-deleted, and are restorable.
- **Localhost-bound web UI** — `omind serve` binds loopback; it is not an
  authenticated remote service and should not be exposed to a network.

## Supply chain

- GitHub Actions are pinned by commit SHA and kept current by Dependabot.
- CodeQL (`security-and-quality`) runs on push/PR to `main` and weekly.
