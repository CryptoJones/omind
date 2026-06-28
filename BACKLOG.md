# Backlog

This file and the GitHub **[Issues tab](https://github.com/CryptoJones/omind/issues)** are two
views of the same list and must stay in sync. Every backlog item below has a matching GitHub issue
and vice versa — when an item ships and its issue closes, check the box (or remove the line) here so
neither side drifts.

## Open

- [ ] **LICENSE was paraphrased (non-canonical) Apache 2.0 — replaced with verbatim text** ([Codeberg #91](https://codeberg.org/CryptoJones/omind/issues/91), [GitHub #113](https://github.com/CryptoJones/omind/issues/113)) — _bug_ — the repo-root `LICENSE` declared `Apache-2.0` but the body was a reworded rendering missing the entire `1. Definitions` section (150 lines vs. the canonical ~201), which breaks the SPDX identifier and license scanners. Replaced with the verbatim canonical Apache License 2.0, preserving `Copyright 2026 Aaron K. Clark`. Same bad text also propagated to `120xSocrates`, `MacminiM2Pro_LocalModelConfig`, `TimeTrackerAPI`.
- [ ] **Rotate `MCP_CONFORMANCE_TOKEN` before it expires** ([Codeberg #88](https://codeberg.org/CryptoJones/omind/issues/88), [GitHub #105](https://github.com/CryptoJones/omind/issues/105)) — _chore_ — the conformance CI job installs the private `mcp-conformance` repo via the `MCP_CONFORMANCE_TOKEN` PAT; the job now skips gracefully on auth failure (commit `e9e5763`), so an expired token stops running the suite without redding CI. Rotate the PAT and re-set the secret before expiry.
- [ ] **Guard hook: substring match on escalation keywords causes false positives** ([#98](https://github.com/CryptoJones/omind/issues/98)) — _bug_ — the Bash guard hook (`hooks/omi-guard.sh`) substring-matches escalation keywords and blocks benign commands.
- [ ] **Long game: fine-tune a model on the accumulated violation corpus** ([#91](https://github.com/CryptoJones/omind/issues/91)) — _roadmap (Phase 4)_ — `omind guard export-corpus` already emits instruction-tuning JSONL; what remains is the training run (needs an accumulated corpus + a GPU beyond an 8GB card). The only true in-weights fix.

## Done

- [x] **GitHub-PR hard-block: allow third-party OSS PRs (owner-aware exception)** ([Codeberg #87](https://codeberg.org/CryptoJones/omind/issues/87), [GitHub #104](https://github.com/CryptoJones/omind/issues/104)) — _enhancement_ — the `gh-pr-create-merge` and `gh-api-pr-create` guard rules now BLOCK PRs only to `CryptoJones`-owned repos (Codeberg-only) and ALLOW PRs to third-party repos named explicitly with `--repo <owner>/<repo>` (or `gh api repos/<owner>/<repo>/pulls`); bare `gh pr create|merge` stays BLOCKED. Existing DELETE/push red-team rules untouched. Shipped in 3.5.0.
- [x] **New `secret-output-guard.sh` PreToolUse(Bash) hook** ([Codeberg #86](https://codeberg.org/CryptoJones/omind/issues/86), [GitHub #103](https://github.com/CryptoJones/omind/issues/103)) — _enhancement_ — portable bash guard wired through `omind setup` (registered first in the `Bash` matcher, ahead of `git-fresh-base.sh`); blocks Bash commands that would print a credential VALUE to the transcript (`pass show X | head`, `gh auth token`, literal tokens) while allowing safe forms (`TOK=$(pass show X)`, redirects, curl headers), with an audited `OMI_SECRET_OK=1` override. Shipped in 3.5.0.
- [x] **Interactive `[[wikilink]]` graph view in the web UI** ([#101](https://github.com/CryptoJones/omind/issues/101), [Codeberg #82](https://codeberg.org/CryptoJones/omind/issues/82)) — _enhancement_ — clickable canvas force-graph in `omind serve` (`/api/graph` + dependency-free renderer; click→open note, hover/drag/zoom, theme-aware). Shipped in 3.4.0.
- [x] **Sidebar tag bar pushes the note list off-screen on large vaults** ([#102](https://github.com/CryptoJones/omind/issues/102), [Codeberg #83](https://codeberg.org/CryptoJones/omind/issues/83)) — _bug_ — `#tag-bar` had no height cap; now capped + scrollable. Shipped in 3.4.0.
- [x] **More `omind setup --agent` targets: Claude Desktop, Kiro, VS Code, Amazon Q** ([#100](https://github.com/CryptoJones/omind/issues/100), [Codeberg #79](https://codeberg.org/CryptoJones/omind/issues/79)) — _enhancement_ — register the `omi` MCP server into each tool's config (`claude-desktop`, `kiro`, `vscode`, `q`); MCP-registration only, idempotent, with `quickstart`/`doctor` support. Shipped in 3.3.0.
- [x] **Knowledge Graph Functionality** ([#99](https://github.com/CryptoJones/omind/issues/99)) — _enhancement_ — `omind graph` (neighbors, path, orphans, dangling, stats, export) + `graph-*` MCP tools over the `[[wikilink]]` vault. Shipped in 3.2.0.

---

*Proudly Made in Nebraska. Go Big Red! 🌽 <https://xkcd.com/2347/>*
