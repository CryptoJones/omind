# omind

OMI/Obsidian memory tooling for Claude Code: reproduce the integration on any machine, plus a local web app to view, edit, and add memory entries.

[![Tests](https://github.com/CryptoJones/omind/actions/workflows/test.yml/badge.svg)](https://github.com/CryptoJones/omind/actions/workflows/test.yml)
[![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg?logo=apache)](LICENSE)
[![Codeberg](https://img.shields.io/badge/Codeberg-CryptoJones%2Fomind-2185D0?logo=codeberg&logoColor=white)](https://codeberg.org/CryptoJones/omind)
[![GitHub](https://img.shields.io/badge/GitHub-CryptoJones%2Fomind-181717?logo=github&logoColor=white)](https://github.com/CryptoJones/omind)
[![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![Version](https://img.shields.io/badge/version-v0.2.0-orange)]()

> Mirrored on both [GitHub](https://github.com/CryptoJones/omind) and
> [Codeberg](https://codeberg.org/CryptoJones/omind). Issues filed on
> either are welcome; commits are pushed to both.

---

## What it does

**OMI** ("Open Mind Interface") is a folder of Markdown notes that an AI agent
reads and writes as long-term memory. `omind` does two things with it:

- **`omind setup`** — idempotently provisions the
  [`obsidian-mcp`](https://www.npmjs.com/package/obsidian-mcp) server for the
  Claude Code CLI, pointed at an OMI folder inside an Obsidian vault. After this,
  Claude Code can persist memory across sessions through the MCP tools.
- **`omind serve`** — a small local web app (FastAPI + Tailwind) to **view, edit,
  and add** memory entries in that same folder, without opening Obsidian.

Everything runs locally. No accounts, no cloud, no cost.

## Install

For end users — an isolated CLI install straight from the git remote:

```bash
# via uv (recommended)
uv tool install git+https://github.com/CryptoJones/omind.git

# or via pipx
pipx install git+https://github.com/CryptoJones/omind.git
```

Either puts the `omind` command on your `PATH` in its own virtualenv. Codeberg
works too — swap in `git+https://codeberg.org/CryptoJones/omind.git`.

For development, install editable from a clone (see [CONTRIBUTING.md](CONTRIBUTING.md)):

```bash
git clone https://github.com/CryptoJones/omind.git
cd omind
pip install -e ".[dev]"
```

## Quick start

Provision the Claude Code MCP wiring (idempotent; safe to re-run):

```bash
omind setup --vault "$HOME/Documents/Obsidian Vault"
```

Run the web UI over the same memory folder:

```bash
omind serve --vault "$HOME/Documents/Obsidian Vault"
# open http://127.0.0.1:8765
```

Preview what setup *would* do without changing anything:

```bash
omind setup --vault "$HOME/Documents/Obsidian Vault" --dry-run
```

## License

Apache 2.0. See [LICENSE](LICENSE).

Proudly Made in Nebraska. Go Big Red! 🌽 https://xkcd.com/2347/
