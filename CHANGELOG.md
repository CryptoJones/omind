# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.0] - 2026-06-03

First stable release. The web UI now runs fully offline and tolerates the OMI
folder being written by Claude Code's MCP and Hermes' cron at the same time.

### Added

- Offline asset vendoring: the SPA no longer loads Tailwind, fonts, or the
  Markdown renderer from a CDN. Tailwind is compiled to a committed stylesheet,
  fonts are served as local `woff2`, and `marked` is bundled. Build inputs live
  under `src/omind/web/tailwind/` and are excluded from the wheel.
- External-change guard: each note carries an opaque version token (mtime +
  size). Saves send the token they last read; if the file changed underneath
  them the API answers `409 Conflict` and the UI offers to overwrite.
- Live list refresh: the sidebar polls for changes every few seconds so notes
  written by other tools appear without a manual reload. Polling pauses while an
  editor is open or the tab is hidden.
- Keyboard shortcuts: `/` focuses search, `n` opens a new note, `Esc` cancels an
  edit, `Ctrl`/`Cmd`+`S` saves, and `j`/`k` move through the list.
- Backlinks panel: the note view lists other notes that `[[wikilink]]` to it.
- `omind doctor`: diagnoses the setup — Node/npx availability, MCP registration
  at user scope, and OMI folder/`.obsidian` config readability.

## [0.3.0] - 2026-06-03

### Added

- Switchable UI in six languages — English, Spanish, French, Arabic, Russian,
  and Chinese — with right-to-left layout for Arabic. The choice persists and
  auto-detects from the browser on first visit.

## [0.2.0] - 2026-06-03

### Changed

- Redesigned the web UI as a themeable, modern interface with five colour
  themes (midnight, carbon, dusk, paper, mint).

### Added

- README screenshot of the web UI.

## [0.1.0] - 2026-06-03

### Added

- `omind setup`: idempotent provisioning of the `obsidian-mcp` server for the
  Claude Code CLI at user scope, over an OMI folder in an Obsidian vault.
- `omind serve`: a localhost FastAPI + Tailwind web app to view, edit, and add
  OMI memory notes, with structured-form and raw-Markdown editing.
- End-user install methods and a `CONTRIBUTING` guide.

[1.0.0]: https://github.com/CryptoJones/omind/releases/tag/v1.0.0
[0.3.0]: https://github.com/CryptoJones/omind/releases/tag/v0.3.0
[0.2.0]: https://github.com/CryptoJones/omind/releases/tag/v0.2.0
[0.1.0]: https://github.com/CryptoJones/omind/releases/tag/v0.1.0
