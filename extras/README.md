# extras

Reference helpers that are **not** part of the installed `omind` package (they
are excluded from the wheel) but are tracked here so integrators can reuse them.

## `omi_write.py`

A standalone script that writes a single OMI note through the safe path —
`omind.notes.upsert_note`, i.e. the `.omi.lock` flock + atomic `os.replace` +
`note_version` re-check, rendering the canonical note format.

It is equivalent to `omind note`, but packaged as one self-contained file an
external agent can drop next to a skill and call, with environment-based vault
resolution (`OMIND_OMI_DIR`, else `OBSIDIAN_VAULT_PATH/OMI`, else omind's default
vault) and a source-tree import fallback. Used by Hermes' `hermes-omi-memory-sync`
skill so it never writes OMI raw. See
[`docs/mesh.md`](../docs/mesh.md) → "Node types & the single-writer rule".

```bash
echo "the body of the note" | python extras/omi_write.py --title "An Insight" --tags thesis,attention
```

## `omi_enforce.py`

A `PostToolUse` Claude Code hook that intercepts any `.md` file Claude's
built-in memory system writes to `~/.claude/projects/*/memory/`, verifies a
matching note already exists in the OMI vault (by title/filename), migrates
it via `omind note` if not, and then deletes the built-in file. This makes
OMI the **exclusive** memory system — the built-in system can write all it
likes, but nothing persists there past the end of a tool call.

`omind setup` installs this hook automatically (writes it to
`~/.claude/hooks/omi-enforce.py` and wires it into `settings.json`). This
copy in `extras/` is the reference version for direct inspection or manual
deployment.
