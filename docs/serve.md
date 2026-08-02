# `omind serve` — the local web UI, and its risk model

`omind serve` runs a small FastAPI app over an OMI memory folder so you can
view, edit, and add memories without opening Obsidian.

```bash
omind serve --vault "$HOME/Documents/Obsidian Vault"
# open http://127.0.0.1:8765
```

This page exists because the rest of the docs describe what `serve` *does*.
This one describes what it **is**, which is the part that matters if you ever
move it off your own machine.

---

## The one thing to know

**The JSON API is unauthenticated and destructive.** There is no login, no
token, no session, and no per-request authorization anywhere in
`omind.web.app`. Anything that can reach the port can read every memory in the
vault and rewrite or delete any of them.

That is a deliberate design choice, not an oversight: `serve` is a single-user
tool bound to `127.0.0.1`, and the security boundary is *the loopback
interface* — the same model as a Jupyter notebook started without a token, or
a desktop app's internal HTTP server. It is a sound boundary. It is also the
**only** boundary, so it has to hold.

### What an unauthenticated caller can do

| Route | Effect |
|---|---|
| `GET /api/notes`, `/api/notes/{name}`, `/api/tags`, `/api/graph` | Read the entire vault, including any credential notes in it |
| `GET /api/ai/usage`, `/api/ai/profile` | Read AI spend history and cost profile |
| `POST /api/notes` | Create a note |
| `PUT /api/notes/{name}` | Overwrite a note's fields |
| `PUT /api/notes/{name}/raw` | Overwrite a note's **entire Markdown body** |
| `DELETE /api/notes/{name}` | Soft-delete a note (`Disabled: true`) |
| `POST /api/notes/{name}/restore` | Undo a soft delete |
| `PUT /api/ai/profile` | Rewrite the cost profile |

Deletes archive rather than destroy — only `omind mesh purge` truly removes a
note — so a hostile `DELETE` is recoverable. A hostile `PUT .../raw` is
recoverable only from git or a backup.

And because the vault is mesh-replicated, **a write here propagates**: a note
poisoned through this API syncs to every other machine in your mesh on the next
`omind mesh sync`. The blast radius of this port is not one machine.

## What already protects you

- **Localhost bind by default.** `--host` defaults to `127.0.0.1`.
- **A `Host` header allowlist** (`localhost`, `127.0.0.1`, `[::1]`), enforced by
  Starlette's `TrustedHostMiddleware`. This is the DNS-rebinding defence: without
  it, a web page you visit could resolve its own hostname to `127.0.0.1` and
  drive this API from your browser, with your loopback interface as the
  attacker's transport. It is why the allowlist matters even though the bind is
  already local.
- **Path-traversal safety.** Every read and write goes through
  `OmiStore.safe_name`, so `../` in a note name cannot escape the vault. Encoded
  traversal against `/api/...` returns 404/400 rather than falling through to
  static file serving.
- **No CORS headers are set**, so a browser will not let another origin read
  responses cross-site.

Note what that list does *not* include: nothing checks **who** is calling.

## Exposing it beyond localhost

Don't, unless you have a specific reason. If you do:

```bash
omind serve --host 0.0.0.0        # all interfaces — Host check DISABLED
omind serve --host 192.168.1.10   # one interface — Host check keeps its allowlist
```

Both print a warning to stderr at startup. `0.0.0.0` sets the allowlist to
`["*"]` because you have explicitly opted into remote access and a Host check
would be theatre at that point.

**If you expose this port, put authentication in front of it.** A reverse proxy
with HTTP basic auth or mTLS, an SSH tunnel (`ssh -L 8765:127.0.0.1:8765
host`), or a WireGuard/Tailscale interface are all fine. An SSH tunnel is the
easiest and keeps the bind on localhost where it belongs — prefer it over
`--host 0.0.0.0` for "I want to reach my vault from my laptop".

Do not put this on a public IP. There is no rate limit, no audit log of API
callers, and no credential to rotate if something finds it.

## If you think it was exposed

1. Stop the server.
2. `git -C "$VAULT" status` and `git log` — the vault is a git repo; unexpected
   note changes show up as a diff.
3. `git -C "$VAULT" diff` any modified notes before syncing, so a poisoned note
   is not replicated to the rest of the mesh.
4. Rotate anything in a credential note. The read side of this API is
   unauthenticated too, and `GET /api/notes` returns everything.

## Related

- [docs/mesh.md](mesh.md) — why a write here reaches other machines.
- [docs/troubleshooting.md](troubleshooting.md) — operational problems, not
  security.
- Issue [#190](https://github.com/CryptoJones/omind/issues/190) — the review
  item this page answers: the risk model previously lived only in a transient
  stderr warning that a localhost user never sees.

---

*Proudly Made in Nebraska. Go Big Red! 🌽 <https://xkcd.com/2347/>*
