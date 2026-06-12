# Contributing to omind

Thanks for your interest. omind is small and the bar to contribute is low — file
an issue, send a patch, or open a pull request. This page covers the dev setup
and the checks a change has to pass.

## Mirrors

omind lives on two forges, kept in sync:

- GitHub — <https://github.com/CryptoJones/omind>
- Codeberg — <https://codeberg.org/CryptoJones/omind>

Issues and pull requests on **either** are welcome. Commits land on both.

## Development setup

Requires Python 3.10+ (CI runs 3.10–3.14 on Linux, plus 3.10 and 3.14 on
Windows — omind is supported on both).

```bash
git clone https://github.com/CryptoJones/omind.git
cd omind
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
```

The editable install puts the `omind` command on your `PATH` and pulls in the dev
tools (`pytest`, `ruff`, `mypy`, `httpx`, `pip-audit`, `types-PyYAML`).

## Quality gates

Every change must keep all four green (the same gates CI runs on every PR):

```bash
ruff check .        # lint (line length 100; rules E F I N W UP B SIM)
mypy src            # static types, --strict
pip-audit           # dependency vulnerability scan
pytest -v           # tests in tests/
```

`ruff format` is fine to run, but the lint pass is the gate. Type-check `src`
(the `--strict` settings live in `pyproject.toml`). Tests should pass on every
supported Python version; if you only have one locally, CI covers the matrix.

## Project layout

```
src/omind/
├── cli.py          argparse entry point: one `_run_*` handler per subcommand
├── provision.py    `omind setup`/`doctor` — idempotent MCP + mesh wiring
├── server.py       `omind node` — the local mesh-node MCP server (mcp SDK)
├── mesh.py         `omind mesh` — git replication: init/sync/daemon/peers
├── merge.py        the field-level 3-way note merge driver (git merge=omi)
├── clock.py        per-note Lamport revisions (cross-node ordering truth)
├── agents.py       Hermes/OpenClaw provisioners (subclass provision.py's)
├── quickstart.py   `omind quickstart` — the manual steps `setup` automates
├── backup.py       `omind backup` — encrypted restic backup + systemd timer
├── hooks.py        `omind hook` — auto-journal hook handlers + SessionStart priming
├── journal.py      journal migration + weekly `omind rollup`
├── transfer.py     `omind export`/`import` — json / tar.gz dataset bundles
├── notes.py        `upsert_note` — the single write entry point for external writers
├── store.py        framework-free note CRUD + template parse/render + index
├── seeds.py        seed content: captured .obsidian JSON + note templates
├── paths.py        canonical filenames (single source of truth for names)
├── proc.py         shared subprocess runner: capture, timeouts, Windows shims
├── filelock.py     portable flock shim (fcntl on POSIX, msvcrt on Windows)
└── web/
    ├── app.py      FastAPI routes (JSON API) + static mount
    └── static/     the single-page UI (index.html, app.js, app.css)
tests/              pytest suites mirroring the modules above (+ conftest.py
                    isolating XDG_STATE_HOME for every test)
e2e/                opt-in end-to-end suite against real disposable nodes;
                    every test skips unless OMIND_E2E_PROVIDER=podman|runpod
                    is set (excluded from the default `pytest` run — see
                    e2e/README.md)
```

`store.py` has no web dependency — it is the file-I/O core that both the CLI and
the web app build on. Keep it framework-free.

## Conventions

- **License header.** Every source file starts with the two-line SPDX block:
  ```python
  # SPDX-License-Identifier: Apache-2.0
  # Copyright 2026 Aaron K. Clark
  ```
- **Python 3.10 floor.** No PEP 695 generic syntax (`def f[T]`) — use `TypeVar`.
- **Path safety.** Anything that reads or writes a note must go through
  `OmiStore.safe_name` so path traversal stays impossible. There are tests that
  enforce this; don't route around them.
- **Tests with changes.** New behavior comes with a test. Bug fixes come with a
  regression test.

## Pull requests

1. Branch off `main` with a conventional name: `feat/…`, `fix/…`, `test/…`,
   `docs/…`, `chore/…`, `refactor/…`.
2. Make the change; keep the three quality gates green.
3. Use conventional-commit subjects (`feat:`, `fix:`, `docs:`, …).
4. Open the PR against `main` on either mirror. Describe the *what* and *why*,
   and reference any issue it closes.

A maintainer squash-merges and reconciles the two mirrors so their `main`
branches stay at the same commit.

## License

By contributing, you agree your contributions are licensed under the project's
[Apache 2.0](LICENSE) license.

Proudly Made in Nebraska. Go Big Red! 🌽 https://xkcd.com/2347/
