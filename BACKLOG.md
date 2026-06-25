# Backlog

This file and the GitHub **[Issues tab](https://github.com/CryptoJones/omind/issues)** are two
views of the same list and must stay in sync. Every backlog item below has a matching GitHub issue
and vice versa — when an item ships and its issue closes, check the box (or remove the line) here so
neither side drifts.

## Open

- [ ] **Guard hook: substring match on escalation keywords causes false positives** ([#98](https://github.com/CryptoJones/omind/issues/98)) — _bug_ — the Bash guard hook (`hooks/omi-guard.sh`) substring-matches escalation keywords and blocks benign commands.
- [ ] **Long game: fine-tune a model on the accumulated violation corpus** ([#91](https://github.com/CryptoJones/omind/issues/91)) — _roadmap (Phase 4)_ — `omind guard export-corpus` already emits instruction-tuning JSONL; what remains is the training run (needs an accumulated corpus + a GPU beyond an 8GB card). The only true in-weights fix.

## Done

- [x] **Knowledge Graph Functionality** ([#99](https://github.com/CryptoJones/omind/issues/99)) — _enhancement_ — `omind graph` (neighbors, path, orphans, dangling, stats, export) + `graph-*` MCP tools over the `[[wikilink]]` vault. Shipped in 3.2.0.

---

*Proudly Made in Nebraska. Go Big Red! 🌽 <https://xkcd.com/2347/>*
