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

Requires Python 3.10+ (CI runs 3.10, 3.11, and 3.12).

```bash
git clone https://github.com/CryptoJones/omind.git
cd omind
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
```

The editable install puts the `omind` command on your `PATH` and pulls in the dev
tools (`pytest`, `ruff`, `mypy`, `httpx`).

## Quality gates

Every change must keep all three green:

```bash
ruff check .        # lint (line length 100; rules E F I N W UP B SIM)
mypy src            # static types, --strict
pytest -v           # tests in tests/
```

`ruff format` is fine to run, but the lint pass is the gate. Type-check `src`
(the `--strict` settings live in `pyproject.toml`). Tests should pass on every
supported Python version; if you only have one locally, CI covers the matrix.

## Project layout

```
src/omind/
├── cli.py          argparse entry point: `setup` and `serve` subcommands
├── provision.py    `omind setup` — idempotent obsidian-mcp wiring
├── seeds.py        captured .obsidian JSON + Memory Template / index constants
├── store.py        framework-free note CRUD + template parse/render + index
└── web/
    ├── app.py      FastAPI routes (JSON API) + static mount
    └── static/     the single-page UI (index.html, app.js, app.css)
tests/              pytest suites mirroring the modules above
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
