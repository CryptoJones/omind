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
