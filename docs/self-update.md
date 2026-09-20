# Self-update — version check + `omind self-update`

## Why

The MCP server (`omind node`) runs from a **pinned install** — today a `uv tool`
wheel built locally and installed with `uv tool install`. A tagged release on the
forge (Codeberg/GitHub) does **not** reach that install on its own: the running
server stays on whatever was installed until someone rebuilds the wheel and
reinstalls. So a release can sit unused indefinitely (this is exactly how a box
ended up running 2.33.0 while the repo was at 2.37.0).

This feature closes the gap with **check + notify**, plus an **explicit**
updater. It deliberately does *not* silently auto-apply: omind backs the OMI
memory for every agent on the machine (Claude Code, Hermes, OpenClaw), so a bad
release auto-deployed everywhere is the failure mode we refuse to risk.

## What it does

`omind.update`:

- **`check_for_update()`** — compares `omind.__version__` to the latest version on
  GitHub. Cached once per day in `state_dir()/update-check.json`; **fail-open**
  (offline / rate-limited / disabled → `latest=None`, treated as "unknown"). Set
  `OMIND_NO_UPDATE_CHECK=1` to disable the network call entirely.
  - Source of truth: the newest **Release** via the GitHub API, falling back to
    the highest **git tag** (`/tags`) — because a pushed tag does not create a
    Release object, so a tags-only repo has no `releases/latest`.
- **Notify**:
  - `omind doctor` prints a trailing line when a newer version exists.
  - `omind node` prints a one-line nudge **to stderr** on startup (cached, never
    blocks; never stdout — that is the MCP/JSON-RPC channel).
- **`omind self-update`** — the explicit updater:
  - `--check` reports current vs. latest and stops.
  - otherwise it detects the install method and reinstalls the latest tag from
    the public GitHub repo:
    - **uv-tool** → `uv tool install --force --from git+https://github.com/CryptoJones/omind@<tag> omind`
    - **pip** → `python -m pip install --upgrade --force-reinstall git+…@<tag>`
    - **editable** checkout → tells you to `git pull` (nothing to reinstall)
  - `--force` reinstalls even when not newer. It does **not** skip the preflight
    below — nothing does.
  - The update takes effect on the **next** server/agent start — a running
    process can't hot-swap its own code (which is why notify+restart, not magic).

## Preflight — refuse, don't force

`uv tool install --force` is **not transactional**: it deletes the existing tool
environment first and builds the replacement after. Anything that goes wrong in
between costs the working install, and on a machine where omind backs every agent's
memory and hooks, that is an outage. So before the install command runs, `self-update`
(and `--rollback`, and `scripts/bootstrap.sh`) checks everything that can be checked
while a refusal is still free:

| Check | Why |
|---|---|
| `git` on `PATH`; `uv` on `PATH` (uv-tool) or `pip` importable (pip) | The release is installed from a git ref by that tool |
| **Windows + uv-tool: always refused** | `omind self-update` runs from `tools\omind\Scripts\python.exe`. Windows will not delete a running executable, and uv discovers that only after removing the rest of the environment. 9.4.0 -> 9.7.5 left `Scripts\python.exe` and nothing else (#375). Dropping `--force` is no better: uv swaps the package, then fails on the existing `omind.exe` and leaves a broken shim |
| **Trial install** (everywhere else) | The target is installed into a throwaway `UV_TOOL_DIR` and started; `omind --version` must answer with the target version. Catches a release that cannot resolve, build, or import *on this machine*, and a network that drops mid-download. Warms uv's cache, so the real install is mostly offline |
| Post-install start check | The result is run in a fresh interpreter. If it does not start, self-update says the install is **BROKEN** and prints the repair command |

A refusal exits 1, changes nothing, and does not overwrite the `--rollback` record.

### Updating on Windows

Self-update refuses and prints these steps with the exact command filled in:

1. Close every agent session, MCP server and `omind serve` — anything running out of
   the tool environment (the refusal lists the pids it can see).
2. From a plain terminal: `uv tool install --force --from git+https://github.com/CryptoJones/omind@<ref> omind`
3. `omind setup`

`scripts/bootstrap.sh` (Git Bash) does the same with guards: it refuses while any
executable in the environment is in use, and runs the trial install first.

**Windows machines on 9.7.5 or older carry the old updater — do not run
`omind self-update` there;** use the steps above. They also repair an install the old
updater already gutted (see [troubleshooting](troubleshooting.md)).

## Channel & trust

The check and the install pull from **GitHub** (public, no auth for read) — the
channel the request named. Codeberg stays the canonical push target; this is a
read-only consumer. There is no PyPI package and no CI publish, so the git ref is
the install source. If omind is later published to an index, `uv tool upgrade`
becomes the native path and `update_command` gains that branch.

## Not (yet) done

- Silent/scheduled auto-apply (intentionally — notify-first).
- A hand-off updater for Windows (a helper outside the venv that waits for omind to
  exit, then installs) — today Windows refuses and prints the manual route.
- Signature/lockfile verification of the pulled ref.
