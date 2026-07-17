# omind

OMI/Obsidian memory tooling for AI agents: reproduce the integration on any machine, plus a local web app to view, edit, and add memory entries.

[![Tests](https://github.com/CryptoJones/omind/actions/workflows/test.yml/badge.svg)](https://github.com/CryptoJones/omind/actions/workflows/test.yml)
[![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg?logo=apache)](LICENSE)
[![Codeberg](https://img.shields.io/badge/Codeberg-CryptoJones%2Fomind-2185D0?logo=codeberg&logoColor=white)](https://codeberg.org/CryptoJones/omind)
[![GitHub](https://img.shields.io/badge/GitHub-CryptoJones%2Fomind-181717?logo=github&logoColor=white)](https://github.com/CryptoJones/omind)
[![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![Open Knowledge Format](https://img.shields.io/badge/format-OKF%20v0.1-4285F4?logo=googlecloud&logoColor=white)](https://github.com/GoogleCloudPlatform/knowledge-catalog/tree/main/okf)
[![Version](https://img.shields.io/github/v/tag/CryptoJones/omind?label=version&color=orange)](https://github.com/CryptoJones/omind/tags)

> Mirrored on both [GitHub](https://github.com/CryptoJones/omind) and
> [Codeberg](https://codeberg.org/CryptoJones/omind). Issues filed on
> either are welcome; commits are pushed to both.

---

![omind's knowledge graph over a vault's [[wikilinks]]](docs/graph.png)

*`omind graph` over an OMI vault — every note a node **coloured by its OKF `type`** (and sized by link degree), every `[[wikilink]]` an edge. Rendered from `omind graph export` (see [docs/graph-demo](docs/graph-demo/)).*

## What it does

**OMI** ("Open Mind Interface") is a folder of Markdown notes that an AI agent
reads and writes as long-term memory. `omind` does two things with it:

- **`omind setup`** — idempotently registers **omind's own node MCP server**
  (`omind node`) with your agent — **Claude Code** by default, or **Hermes,
  OpenClaw, OpenCode, Codex CLI, Gemini CLI, Claude Desktop, Kiro, VS Code, and
  Amazon Q** via `--agent` (see *Other agents* below) —
  pointed at an OMI folder inside an Obsidian vault, and initializes the folder
  as a **mesh node** (see below). After this, the agent persists memory across
  sessions through the MCP tools — and across machines through the mesh. Setup
  also installs a
  PreToolUse(Bash) **fresh-base git guard** (`git-fresh-base.sh`) that blocks
  branching off a local `main`/`master`/`develop` that is behind its
  `origin/*` counterpart (it fetches first, fails open otherwise).
- **`omind mesh`** — peer-to-peer replication: every machine runs a full local
  node and nodes sync over git+ssh, with per-note Lamport versioning and a
  field-level merge driver. No central server, full offline operation.
  Deleting archives (restorable) instead of removing. See
  [docs/mesh.md](docs/mesh.md) (design) and [docs/mesh-ops.md](docs/mesh-ops.md)
  (operation).
- **`omind serve`** — a small local web app (FastAPI + Tailwind) to **view, edit,
  and add** memory entries in that same folder, without opening Obsidian. Ships
  with five themes and a switchable UI in six languages (English, Spanish,
  French, Arabic, Russian, Chinese), including right-to-left layout for Arabic.
- **`omind doctor`** — diagnose the wiring in one shot: Claude CLI + git on
  `PATH`, the `omi` MCP server registered at user scope with the right command,
  the OMI folder readable, mesh health (node identity, merge driver, per-peer
  ahead/behind, last-sync age, unresolved conflicts), backup health
  (unconfigured / last-success age / failing), and whether the auto-memory
  hooks have recorded any recent failures.
- **`omind backup`** — encrypted, unattended off-machine backup of the OMI
  folder, wrapping [restic](https://restic.net/) (see
  [Encrypted backup](#encrypted-backup) below).
- **`omind guard`** — the **OMI-compliance guard**: a layered set of agent hooks
  that make the agent *consult memory before it acts* and *hard-block* dangerous
  commands, enforcing identically across Claude Code, Hermes, OpenCode, and Codex
  (see [The OMI-compliance guard](#the-omi-compliance-guard) below).
- **`omind checkpoint`** — a scheduled job that records what the agent has been
  doing every N minutes into a daily worklog note, by mining the trails the hooks
  already capture (see [Activity checkpoints](#activity-checkpoints) below).
- **`omind ai`** — account for tokens attributable to OMI and select a manual
  low/medium/high model-expense profile that bounds priming and optional model calls.

The web UI works **fully offline** (fonts, styles, and the Markdown renderer are
vendored — no CDN). It shows **backlinks** for the open note, refreshes the list
live as other tools write the folder, guards against clobbering external edits,
and has keyboard shortcuts (`/` search, `n` new, `j`/`k` to move, `Ctrl`/`Cmd`+`S`
to save, `Esc` to cancel).

Everything runs locally. No accounts, no cloud, no cost.

## Install

**One-step bootstrap** (checks/installs dependencies, installs omind, verifies):

```bash
# clone, then:
scripts/bootstrap.sh                       # or: --remote codeberg, --vault PATH
```

It auto-installs `uv` (user-local, no root — and it bootstraps Python ≥3.10 for
you), checks for `git`/`claude` with install guidance if either is missing,
then runs `omind setup` + `omind doctor`. Note: omind itself has **no Docker and
no Node.js dependency** — it needs only `git` and an agent CLI (Claude Code on
the default path; Hermes, OpenClaw, OpenCode, and Codex are each wired with
`omind setup --agent <name>` once their own CLI is installed — see *Other
agents* below).

**Manual** — an isolated CLI install straight from the git remote:

```bash
# via uv (recommended — also provides a compatible Python if the system one is <3.10)
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

Provision the MCP wiring for your agent — Claude Code by default; add
`--agent hermes|openclaw|opencode|codex|gemini|claude-desktop|kiro|vscode|q`
for the others (see *Other agents* below). Idempotent; safe to re-run:

```bash
omind setup --vault "$HOME/Documents/Obsidian Vault"
```

Prefer to wire things in yourself? Print the same steps as copy-paste shell
commands and JSON, personalized to your paths — nothing is changed for you:

```bash
omind quickstart --vault "$HOME/Documents/Obsidian Vault"
```

It covers all five pieces (memory folder scaffold, mesh initialization,
user-scope MCP registration, auto-memory hooks, fresh-base git guard hook),
each independently
applicable. The annotated walkthrough lives in
[docs/manual-setup.md](docs/manual-setup.md).

Run the web UI over the same memory folder:

```bash
omind serve --vault "$HOME/Documents/Obsidian Vault"
# open http://127.0.0.1:8765
```

Preview what setup *would* do without changing anything:

```bash
omind setup --vault "$HOME/Documents/Obsidian Vault" --dry-run
```

Check that everything is wired up correctly:

```bash
omind doctor --vault "$HOME/Documents/Obsidian Vault"
```

Add or update a single memory note safely — it creates the note, or updates it
in place if the title already exists, through the same locked, atomic write path
every other tool uses (body comes from stdin):

```bash
echo "the body of the note" | omind note --title "An Insight" --tags thesis,attention
```

Back up or migrate the whole memory dataset:

```bash
# export — json (default; portable & diffable) or targz (full-fidelity snapshot)
omind export --vault "$HOME/Documents/Obsidian Vault" --out omi-export.json
omind export --vault "$HOME/Documents/Obsidian Vault" --format targz --out omi.tar.gz

# import — format auto-detected by extension
omind import omi-export.json --vault "$HOME/Documents/Obsidian Vault"
```

Import adds new notes and leaves identical ones untouched; a note whose content
differs is kept as-is on disk and reported, unless you pass `--force`. Imports
never delete.

## Encrypted backup

The vault is long-term memory on one disk; `omind backup` keeps an encrypted
copy off-machine, wrapping [restic](https://restic.net/):

```bash
# one-time: generate the password file (0600) and create the encrypted repo
omind backup init --repo sftp:host:/path     # or a local path, s3:, b2:, …

# snapshot now, with 7-daily / 4-weekly / 6-monthly retention
omind backup run

# restic check + restore the latest snapshot's index.md and diff it live
omind backup verify

# unattended: a daily systemd user timer running `backup run`
omind backup install-timer
```

Every external command runs with a timeout, so a restic hung on a dead link
fails loudly instead of wedging the timer; three consecutive failures write a
`BACKUP FAILING` note into the vault so the problem surfaces in session
priming, and `omind doctor` reports backup health either way. If restic is
absent, `run` degrades to unencrypted rsync `--link-dest` snapshots and doctor
warns about the degradation.

**Copy `~/.config/omind/backup.pass` somewhere safe off-machine.** It encrypts
every snapshot; losing it with the disk makes the backups unreadable.

## How memory is stored

OMI is just a folder of plain Markdown notes — one note per memory, fully
human-editable (open the folder as an Obsidian vault). It is also a conformant
**[Open Knowledge Format](https://github.com/GoogleCloudPlatform/knowledge-catalog/tree/main/okf)
bundle** (see below). Each note has a stable shape so tools and the merge driver
can read and write individual fields without stepping on each other:

- a **YAML frontmatter block** — the note's OKF metadata: the required `type`
  plus `title`, `description`, `tags`, and `timestamp`;
- a `# Title` and a `## Metadata` block (created date, `#tags`, and the mesh
  `Rev:` Lamport stamp), kept alongside the frontmatter so existing tooling and
  un-upgraded mesh peers keep reading it unchanged;
- `## Summary` / `## Details` free text;
- `## Connections` — `[[wikilinks]]` to related notes (the graph the web UI and
  `omind lint` traverse);
- `## Action Items` (a checkbox list) and `## References`.

**Open Knowledge Format (OKF).** omind speaks
[OKF](https://github.com/GoogleCloudPlatform/knowledge-catalog/tree/main/okf) —
Google Cloud's vendor-neutral, Apache-2.0
[specification](https://github.com/GoogleCloudPlatform/knowledge-catalog/blob/main/okf/SPEC.md)
for representing knowledge as a directory of Markdown files with YAML
frontmatter, readable by any agent or tool with no SDK, runtime, or lock-in.
Every note omind writes leads with a frontmatter block carrying the one field
OKF requires (`type`) plus the recommended `title` / `description` / `tags` /
`timestamp`, and `index.md` is the OKF directory listing — so an omind vault
drops straight into any OKF-aware consumer. Migrate a pre-OKF vault in place with
**`omind convert`** (idempotent; `--check` validates the three conformance rules,
`--dry-run` previews). More: the
[OKF spec](https://github.com/GoogleCloudPlatform/knowledge-catalog/blob/main/okf/SPEC.md)
and [okf.md](https://okf.md/).

**One writer, always.** Every write — `omind note`, the `omind node` MCP server an
agent calls, the web UI, the mesh merge — goes through a single locked, atomic
path: an advisory `flock`, an atomic `os.replace`, and a `note_version`
compare-and-swap that rejects a write made against a stale read. That is what lets
several agents and tools touch the same folder at once without corrupting it.
"Deleting" a note **archives** it (hidden, restorable) rather than removing it.

A maintained `index.md` lists the most recent notes; that index plus the latest
session state is exactly what the guard's priming injects at session start. Run
`omind lint` to catch drift (broken wikilinks, orphaned or near-duplicate notes,
missing titles). The cross-machine replication story — git-backed mesh, per-note
Lamport versioning, the field-level merge driver — is its own section:
[The memory mesh](#the-memory-mesh-20).

## The OMI-compliance guard

Memory only helps if the agent actually reads it *before* acting — and an agent
left to its own devices consults memory inconsistently and will happily run a
destructive command. The guard closes both gaps with a small stack of hooks
around every tool call. `omind setup` installs it for Claude Code; the other
harnesses wire it through [the per-agent adapters](#other-agents-hermes-agent-openclaw-opencode-and-codex-cli).
It enforces the same rules everywhere and **fails open** at every layer — a
broken hook can never wedge the agent.

- **Priming (injection).** On session start the agent is handed the recent-memory
  index + latest session state, so the relevant memory is in front of it before
  the first turn (Claude Code `SessionStart`, Hermes `pre_llm_call`, …).
- **The consult gate.** A `PreToolUse` hook blocks the *first* action of each turn
  until the agent reads OMI; one OMI search/read clears the gate for the rest of
  the turn. The gate is a per-turn sentinel under the state dir, reset on the
  harness's turn boundary.
- **The verifier.** Clearing the gate by reading *any* note isn't enough, so a
  `PostToolUse` verifier judges whether the consult was actually **relevant** to
  the turn's task — a deterministic keyword-overlap prefilter decides the clear
  cases, and only the ambiguous middle shells out to headless `claude -p`
  (fail-open). Default is **WARN** (it logs the off-topic consult and nudges
  toward better notes); opt-in **REQUIRE** (`OMI_VERIFY_REQUIRE=1`) re-closes the
  gate until a relevant consult happens. The thresholds
  (`OMI_VERIFY_HIGH`/`OMI_VERIFY_LOW`) and an always-relevant allowlist
  (`OMI_VERIFY_ALWAYS_RELEVANT`) are tunable, and the check primes on the agent's
  own recent off-topic consults.
- **Hard blocks.** A policy of high-risk command patterns (deleting a repo, a
  destructive git push, rewriting auth config, …) is blocked **unconditionally**,
  regardless of the gate, with the reason and the on-point notes to read.
- **The compliance log + learning loop.** Every guard-relevant action is appended
  to `compliance.jsonl`; a `PostToolUse` detector records soft-rule matches as
  evidence, and recidivism escalates a rule (soft → hard → verifier) over time
  (`omind guard learn` / `escalate`).

**Cross-harness by construction.** Each harness is described as data — a
`HarnessSpec` (can it hard-block? which block-output format?) — and its hook pipes
its event to `omind guard adapter --harness <name>`, so a rule learned under one
agent blocks under all of them. `omind guard selftest` replays canned deny events
through every harness's renderer to verify the wiring without a live agent.

Inspect and operate it with `omind guard log` (recent denies/violations),
`omind guard policy` (active rules), `omind guard status` (which harnesses are
guarded), `omind guard explain "<cmd>"` (dry-run a command), and
`omind guard verify --explain` (why a consult scored relevant or off-topic).
`omind guard repair` (and `omind doctor`) re-heal a wedged or drifted hook-set.

When the guard is impeding critical work, temporarily pause the consult gate and
verifier instead of editing hook config by hand:

```bash
omind guard pause --for 15m   # also accepts 90s, 2h, or bare minutes like 15
omind guard status            # shows the remaining pause window
omind guard resume            # re-arm immediately
```

Running `omind guard pause` without `--for` uses a 30-minute window. The pause is
machine-global, auto-expires, and is written to the compliance log. Hard
destructive blocks remain active while paused; this only skips the per-turn OMI
consult gate and relevance verifier.

## The Playbook

The **Playbook** is the small set of always-on operator rules — the cross-cutting
procedures (sudo, secrets, forges, "pull before you work", "do it yourself") that a
fresh agent instance otherwise keeps re-learning the hard way. It lives as a priming
file, `Playbook.md`, in the OMI vault, and omind surfaces it two ways:

- **Always in context.** `Playbook.md` is injected verbatim into *every* session's
  SessionStart context (alongside `index.md`), so the rules are present whether or
  not the agent thinks to search for them — they do not depend on the per-turn
  gate's relevance matching to surface.
- **Enforced at the action.** The guard backs the most-violated rules with hard
  blocks keyed on the *command*, not the task. Raw `sudo` is blocked and redirected
  to the installed **`fleet-sudo`** wrapper, which reads the fleet sudo password
  from `pass` itself — so no instance ever guesses a per-host `pass` entry or hands
  the user a command to paste. A deliberate raw `sudo` opts in with `OMI_SUDO_OK=1`,
  exactly like the Codeberg-mirror `OMI_PUSH_GITHUB=1` escape hatch.

Edit the rules by editing `Playbook.md` in the vault; add enforcement with a seed
rule in `omind.policy`. The Playbook is the guard's priming made explicit: *don't
ask a fresh instance to remember — put the rule in front of it, and block the wrong
action.*

## Activity checkpoints

You can't reliably *force* a running agent to do something on a wall clock —
agents are turn-driven and idle between messages. So to "record recent work every
N minutes," omind doesn't ask the agent: `omind checkpoint` is a scheduled job
that mines the two trails the hooks already capture — the per-action **journal**
(`Journal/Session Journal <date>.md`) and the cross-harness **compliance log** —
and upserts a per-day **`Worklog <date>`** note with a timestamped section per
run (one note per day, so it stays a single recent-memory slot instead of
flooding the index).

```bash
# summarize the last 15 minutes into today's worklog note, now
omind checkpoint --since 15m

# or run it unattended every 15 minutes via a systemd user timer
omind checkpoint install-timer --every 15m
omind checkpoint uninstall-timer            # stop + remove it
```

The summary is deterministic (action counts by tool + guard denies/violations);
`--llm` adds a one-paragraph `claude -p` narrative, fail-open to the deterministic
text. Because it's a scheduled job — the same systemd-user-timer mechanism as
`omind backup` and `omind mesh` — it doesn't depend on the agent's cooperation,
which is what makes it a reliable *record* rather than a hopeful instruction.

## AI token usage and expense profiles

omind keeps a machine-local, per-vault ledger for the AI tokens it causes: session
priming injected into an agent plus the verifier and optional checkpoint
`claude -p` calls. It does **not** parse or claim the rest of an agent session.
Provider-reported subprocess usage is recorded exactly; provider-neutral priming
is shown as an estimate (`ceil(characters / 4)`) and never mixed into the
provider-reported subtotal. The ledger contains counts and operational metadata
only — never prompts, responses, note contents, or credentials.

```bash
omind ai profile                         # show saved/effective profile
omind ai profile medium                  # persist low, medium, or high per vault
omind ai usage --since 7d                # 24h, 7d, 30d, or all
omind ai usage --since all --json        # machine-readable report
```

Profiles describe the expense of the model already selected; they do not select a
model. `low` is the backward-compatible default (48,000-character priming cap and
all optional calls), `medium` halves the priming/prompt inputs, and `high` caps
priming at 8,000 characters and uses deterministic verifier/checkpoint behavior
without optional model calls. Set `OMI_AI_EXPENSE=low|medium|high` for a temporary
override; it wins over the saved profile. The web app's **AI Usage** view exposes
the same profile control, time windows, exact/estimated breakdown, per-operation
totals, and estimated avoided tokens.

## Other agents: Hermes, OpenClaw, OpenCode, Codex, Gemini, Claude Desktop, Kiro, VS Code, Amazon Q

[Claude Code](https://github.com/anthropics/claude-code) is the default, but the
same OMI folder can back any agent. `omind setup --agent ...` provisions several
more out of the box —
[Hermes Agent](https://github.com/NousResearch/hermes-agent),
[OpenClaw](https://github.com/openclaw/openclaw),
[OpenCode](https://github.com/sst/opencode), and
[OpenAI Codex CLI](https://github.com/openai/codex):

```bash
omind setup --agent hermes   --vault "$HOME/Documents/Obsidian Vault"   # Hermes Agent
omind setup --agent openclaw --vault "$HOME/Documents/Obsidian Vault"   # OpenClaw
omind setup --agent opencode --vault "$HOME/Documents/Obsidian Vault"   # OpenCode
omind setup --agent codex    --vault "$HOME/Documents/Obsidian Vault"   # OpenAI Codex CLI
```

The **OMI-compliance guard** (hard-blocks + the per-turn consult gate) enforces
across harnesses, not just Claude Code: Hermes via its `pre_tool_call` hook,
OpenCode via a `tool.execute.before` plugin, and **Codex CLI** (>= 0.117) via its
Claude-schema `PreToolUse`/`PermissionRequest` command hooks in
`~/.codex/hooks.json` — so a rule learned under one agent blocks under all of
them. `omind setup --agent codex` also persists Codex `[hooks.state]` trust for
the omind-owned hook definitions it just wrote, so a fresh Codex session can run
the guard and OMI priming hooks without a manual `/hooks` approval pass. The hash
is computed from the exact hook definition on that machine, including the local
`omind` executable path; if you later edit the hook by hand, re-run setup or use
Codex's `/hooks` UI to review the changed definition. `omind guard selftest`
replays a canned deny through every harness's renderer to confirm the wiring
without a live agent.

Codex also gets the `omi` MCP server registered — under `[mcp_servers.omi]` in
`~/.codex/config.toml`, the same table `codex mcp add` writes. Unlike every
other agent config omind touches, `config.toml` is **TOML**, so this merge is
done with `tomlkit` (round-trip parsing) instead of the JSON idiom the rest
share; only the `mcp_servers.omi` table is ever touched, and a `config.toml`
that doesn't parse is never overwritten. Codex has no memory skill because it
reads the MCP tools directly.

For Codex, one command installs the whole integration:

```bash
omind setup --agent codex --vault "$HOME/Documents/Obsidian Vault"
```

That command idempotently:

1. Registers the `omi` MCP server in `~/.codex/config.toml`.
2. Installs the OMI guard on `PreToolUse` and `PermissionRequest` in
   `~/.codex/hooks.json`.
3. Installs `SessionStart` OMI priming, using the same recent-memory context
   Claude Code receives.
4. Writes a managed global `~/.codex/AGENTS.md` bootstrap pointer that tells
   fresh Codex sessions to read OMI first.
5. Persists Codex hook trust for those omind-owned hook groups under
   `[hooks.state]` in `~/.codex/config.toml`.

Then restart Codex and verify:

```bash
omind doctor --agent codex --vault "$HOME/Documents/Obsidian Vault"
```

`omind setup --agent <name>` adapts to where each agent keeps its config. The
three **memory-backing** agents — Hermes, OpenClaw, and OpenCode — get the full
treatment:

1. The shared steps — OMI folder scaffold + mesh initialization — identical to
   the Claude Code path, so every agent talks to **one** memory folder through
   the same `omind node` server.
2. Registers the `omi` MCP server where the agent looks for it: `mcp_servers` in
   `~/.hermes/config.yaml` (Hermes), `mcp.servers` in `~/.openclaw/openclaw.json`
   (OpenClaw — legacy `~/.clawdbot` / `~/.moltbot` installs detected too), or the
   `mcp` block in `~/.config/opencode/opencode.json` (OpenCode). Only omind's own
   entry is ever touched; a config file that doesn't parse is never overwritten.
3. Installs an `omind-omi-memory` skill that teaches the agent to **read** memory
   through the MCP tools and **write** it through `omind note` — the single-writer
   path that keeps concurrently running agents from corrupting the folder (see
   [docs/mesh.md](docs/mesh.md) → "Node types & the single-writer rule").
4. **Wires session-start priming** so the agent reads OMI *first* — Hermes via a
   `pre_llm_call` hook (pre-approved in its `shell-hooks-allowlist.json` so it
   loads without a prompt), OpenClaw via a managed `MEMORY.md` bootstrap. It is
   the same `omind hook` payload (recent-memory index + latest session state)
   Claude Code injects through its `SessionStart` hook.

The **Gemini CLI** is wired **guard-only** — just the hard-block hook described
above (the `BeforeTool` hook under `hooks` in `~/.gemini/settings.json`); its
MCP-memory registration, skill, and priming are a separate follow-up. **Codex
CLI** gets guard, MCP-memory registration, SessionStart priming, and the global
AGENTS bootstrap pointer (see above), but no memory skill because it uses the
MCP tools directly. **OpenCode** priming is likewise not wired yet (its MCP
server and skill are). The cross-harness **guard** reaches Claude Code, Hermes,
OpenCode, Codex, and Gemini as hard-block; **OpenClaw** is wired
**detect-only** — its POST `/hooks/agent` gateway receives the guard verdict but
deny-enforcement is unverified against a live gateway, so the verdict is advisory
until hard-block is proven.

### MCP-only targets: Claude Desktop, Kiro, VS Code, Amazon Q

Four more agents are wired by **MCP registration alone** — omind drops the `omi`
server into the tool's own config file and nothing else (no guard, no skill); the
agent reaches memory through the MCP tools the server exposes:

```bash
omind setup --agent claude-desktop --vault "$HOME/Documents/Obsidian Vault"  # Claude Desktop app
omind setup --agent kiro           --vault "$HOME/Documents/Obsidian Vault"  # Kiro IDE
omind setup --agent vscode         --vault "$HOME/Documents/Obsidian Vault"  # VS Code (native MCP)
omind setup --agent q              --vault "$HOME/Documents/Obsidian Vault"  # Amazon Q
```

Each writes only the `omi` entry it owns and refuses to overwrite a config file it
can't parse. Config locations: Claude Desktop's `claude_desktop_config.json` (under
`~/Library/Application Support/Claude` on macOS, `~/.config/Claude` on Linux,
`%APPDATA%\Claude` on Windows; `mcpServers` block); Kiro's `~/.kiro/settings/mcp.json`
(`mcpServers`); VS Code's user-level `mcp.json` (under the same per-OS app-support dir
as Claude Desktop but `Code/User`; a `servers` block with `type: stdio`); and Amazon
Q's `~/.aws/amazonq/mcp.json` (`mcpServers`). Restart the tool afterward to load the
server.

`omind doctor --agent hermes|openclaw|opencode|codex|gemini|claude-desktop|kiro|vscode|q`
diagnoses that agent's wiring, and `omind quickstart --agent <name>` prints the manual
steps (YAML/JSON snippets personalized to your paths) if you'd rather merge them in
yourself.

The auto-memory **journal** hooks (the per-action trail) remain Claude Code-only;
the other agents' actions reach OMI through the MCP skill instead.

See [CHANGELOG.md](CHANGELOG.md) for release notes.

## The memory mesh (2.0)

2.0.0 turned omind from a single-machine tool into a **git-backed mesh** —
every machine runs a full local memory node, and the nodes replicate to one
another **peer-to-peer over git**, so memory is shared across the house with
**no central server** and full offline operation. Concurrent writes build on
the per-node write safety (advisory `flock` + atomic `os.replace` +
`note_version` compare-and-swap) and add cross-node **Lamport versioning**
with a field-level merge; "deleting" a note **archives** it (hidden,
restorable) rather than tombstoning it. Design:
**[docs/mesh.md](docs/mesh.md)**; operation:
**[docs/mesh-ops.md](docs/mesh-ops.md)**.

## License

Apache 2.0. See [LICENSE](LICENSE).

Proudly Made in Nebraska. Go Big Red! 🌽 https://xkcd.com/2347/
