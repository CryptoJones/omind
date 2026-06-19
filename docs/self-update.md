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
  - `--force` reinstalls even when not newer.
  - The update takes effect on the **next** server/agent start — a running
    process can't hot-swap its own code (which is why notify+restart, not magic).

## Channel & trust

The check and the install pull from **GitHub** (public, no auth for read) — the
channel the request named. Codeberg stays the canonical push target; this is a
read-only consumer. There is no PyPI package and no CI publish, so the git ref is
the install source. If omind is later published to an index, `uv tool upgrade`
becomes the native path and `update_command` gains that branch.

## Not (yet) done

- Silent/scheduled auto-apply (intentionally — notify-first).
- Rollback automation (the prior wheel remains in `dist/` for a manual revert).
- Signature/lockfile verification of the pulled ref.
