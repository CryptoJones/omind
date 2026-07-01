# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Aaron K. Clark
"""Maintain the OMI ``Journal/`` subfolder: stray-journal migration and rollups.

Daily session journals (written by :mod:`omind.hooks`) live in
``<omi_dir>/Journal/`` so the top-level-only glob in
:meth:`omind.store.OmiStore._note_paths` keeps them out of listings and the
regenerated ``index.md``. This module owns the two maintenance jobs around
that layout:

* :func:`migrate_journals` — move stray ``Session Journal *.md`` files from
  the vault-folder root (where omind wrote them before the ``Journal/``
  layout) and from any legacy ``logs/`` experiment into ``Journal/``, then
  regenerate the index. Idempotent; runs under the shared ``.omi.lock``.
* :func:`rollup_journals` — compact each week of dailies into one
  template-shaped summary note (counts per tool, errors, sessions, notable
  targets), then archive (default) or delete the dailies. Raw dailies are
  kept for :data:`DEFAULT_RETAIN_DAYS` days by default.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

from omind import paths
from omind.hooks import JOURNAL_TAGS, action_bullets, journal_dir
from omind.store import NoteFields, OmiStore, _atomic_write, render_fields, today

# Places older layouts left daily journals: the vault-folder root, plus the
# short-lived ``logs/`` journal-location experiment some live vaults carry.
LEGACY_JOURNAL_DIRNAMES = ("logs",)
ARCHIVE_DIRNAME = "Archive"
DEFAULT_RETAIN_DAYS = 30
_NOTABLE_TARGET_LIMIT = 10

_JOURNAL_FILE_RE = re.compile(
    rf"^{re.escape(paths.JOURNAL_PREFIX)} (\d{{4}}-\d{{2}}-\d{{2}})\.md$"
)
_ACTION_LINE_RE = re.compile(r"^- \d\d:\d\d \[session (?P<session>\w+)\] (?P<rest>.+)$")
_TOOL_RE = re.compile(
    r"^PostToolUse (?P<tool>\S+)(?: -> (?P<target>.*?))? \((?P<outcome>ok|error)\)$"
)
_STOP_LINE = "Stop -> turn ended"


def journal_date(filename: str) -> date | None:
    """The day a ``Session Journal YYYY-MM-DD.md`` filename covers, else None."""
    m = _JOURNAL_FILE_RE.match(filename)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y-%m-%d").date()
    except ValueError:
        return None


def iso_week(day: date) -> str:
    """ISO week label for a day, e.g. ``2026-W24``."""
    year, week, _ = day.isocalendar()
    return f"{year}-W{week:02d}"


def rollup_name(week: str) -> str:
    """Deterministic per-week rollup filename: ``Session Journal Rollup YYYY-Www.md``."""
    return f"{paths.JOURNAL_PREFIX} Rollup {week}.md"


# -- migration ----------------------------------------------------------------


def find_stray_journals(omi_dir: Path | str) -> list[Path]:
    """Daily journals outside ``Journal/``: the vault-folder root and legacy dirs.

    Pure scan — touches nothing, so ``omind setup --dry-run`` can report what a
    real run would move.
    """
    omi = Path(omi_dir)
    strays: list[Path] = []
    for directory in (omi, *(omi / name for name in LEGACY_JOURNAL_DIRNAMES)):
        if not directory.is_dir():
            continue
        strays.extend(
            path
            for path in sorted(directory.glob(paths.JOURNAL_GLOB))
            if journal_date(path.name) is not None
        )
    return strays


def migrate_journals(omi_dir: Path | str) -> list[str]:
    """Move stray daily journals into ``Journal/`` and regenerate the index.

    Idempotent (a clean vault is a no-op) and serialized against other writers
    via the store's ``.omi.lock``. When a same-day journal already exists in
    ``Journal/`` (one session wrote before the layout change, another after),
    the stray's action bullets are appended to the relocated journal so neither
    trail is lost. Returns the moved filenames.
    """
    store = OmiStore(omi_dir)
    target_dir = journal_dir(store.omi_dir)
    moved: list[str] = []
    # Same-package use of the store's private lock/index helpers: the move plus
    # index regeneration must be one critical section against other writers.
    with store._write_lock():
        for stray in find_stray_journals(store.omi_dir):
            target = target_dir / stray.name
            if target.is_file():
                bullets = action_bullets(stray.read_text(encoding="utf-8", errors="replace"))
                if bullets:
                    with target.open("a", encoding="utf-8") as fh:
                        fh.write("\n".join(bullets) + "\n")
                stray.unlink()
            else:
                target_dir.mkdir(parents=True, exist_ok=True)
                stray.rename(target)
            moved.append(stray.name)
        if moved:
            store._write_index()
    return moved


# -- rollup ---------------------------------------------------------------------


@dataclass
class JournalStats:
    """Aggregated counts across one week of daily journals."""

    actions: int = 0
    errors: int = 0
    stops: int = 0
    tools: Counter[str] = field(default_factory=Counter)
    sessions: set[str] = field(default_factory=set)
    targets: Counter[str] = field(default_factory=Counter)


@dataclass
class WeekRollup:
    """The outcome of rolling up one ISO week of dailies."""

    week: str
    days: list[str]
    rollup_filename: str
    archived: list[str]
    deleted: list[str]


def _tally(text: str, stats: JournalStats) -> None:
    """Fold one daily journal's action bullets into ``stats``."""
    for line in text.splitlines():
        m = _ACTION_LINE_RE.match(line)
        if not m:
            continue
        stats.sessions.add(m.group("session"))
        rest = m.group("rest")
        if rest == _STOP_LINE:
            stats.stops += 1
            continue
        t = _TOOL_RE.match(rest)
        if not t:
            continue
        stats.actions += 1
        stats.tools[t.group("tool")] += 1
        if t.group("outcome") == "error":
            stats.errors += 1
        target = t.group("target")
        if target and target.strip():
            stats.targets[target.strip()] += 1


def render_rollup(week: str, days: list[str], stats: JournalStats) -> str:
    """Render the weekly summary through the canonical note renderer.

    ``render_fields`` is what parse_note and the mesh merge driver expect a
    note to look like; a hand-built template here would drift the moment the
    template grows a field. (Daily journals are different: their trailing
    ``## Actions`` section is the O_APPEND hot path and deliberately bypasses
    the store — see :mod:`omind.hooks`.)
    """
    details_lines = [
        "Days rolled up:",
        *(f"- {d}" for d in days),
        "",
        "Actions per tool:",
        *(f"- {tool}: {count}" for tool, count in stats.tools.most_common()),
        "",
        "Sessions:",
        *(f"- {session}" for session in sorted(stats.sessions)),
        "",
        "Notable targets:",
        *(
            f"- {target} ({count})"
            for target, count in stats.targets.most_common(_NOTABLE_TARGET_LIMIT)
        ),
    ]
    fields = NoteFields(
        title=rollup_name(week)[:-3],
        summary=(
            f"Weekly rollup of {len(days)} daily session journal(s) for {week}: "
            f"{stats.actions} action(s), {stats.errors} error(s), "
            f"{len(stats.sessions)} session(s), {stats.stops} turn end(s)."
        ),
        details="\n".join(details_lines),
        created=today(),
        tags=[*JOURNAL_TAGS, "rollup"],
    )
    return render_fields(fields)


def rollup_journals(
    omi_dir: Path | str,
    *,
    week: str | None = None,
    retain_days: int = DEFAULT_RETAIN_DAYS,
    delete: bool = False,
    now: datetime | None = None,
) -> list[WeekRollup]:
    """Compact weeks of daily journals in ``Journal/`` into per-week summary notes.

    By default only weeks whose newest daily is older than ``retain_days``
    (keep-raw-dailies retention) are rolled up; pass ``week`` (``YYYY-Www``) to
    compact exactly that ISO week now. Each rolled-up week gets a
    ``Session Journal Rollup YYYY-Www.md`` summary note in ``Journal/``, and its
    dailies are moved to ``Journal/Archive/`` — or removed with ``delete=True``.
    Runs under the shared ``.omi.lock``; the index never lists ``Journal/``
    files, so no regeneration is needed.
    """
    store = OmiStore(omi_dir)
    directory = journal_dir(store.omi_dir)
    cutoff = (now or datetime.now()).date() - timedelta(days=retain_days)
    results: list[WeekRollup] = []
    with store._write_lock():
        groups: dict[str, list[tuple[date, Path]]] = {}
        if directory.is_dir():
            for path in sorted(directory.glob(paths.JOURNAL_GLOB)):
                day = journal_date(path.name)
                if day is not None:
                    groups.setdefault(iso_week(day), []).append((day, path))
        for wk in sorted(groups):
            dated_paths = groups[wk]
            if week is not None:
                if wk != week:
                    continue
            elif max(day for day, _ in dated_paths) >= cutoff:
                continue  # retention: keep raw dailies for retain_days
            # A week may have rolled up before (its dailies now live in
            # Archive/) and then receive a late daily — e.g. one synced from
            # an offline peer. Recompute over the archived dailies too, so
            # rewriting the rollup never shrinks the earlier aggregate.
            archive_dir = directory / ARCHIVE_DIRNAME
            archived_dated: list[tuple[date, Path]] = []
            if archive_dir.is_dir():
                for path in sorted(archive_dir.glob(paths.JOURNAL_GLOB)):
                    day = journal_date(path.name)
                    if day is not None and iso_week(day) == wk:
                        archived_dated.append((day, path))
            stats = JournalStats()
            for _, path in [*archived_dated, *dated_paths]:
                _tally(path.read_text(encoding="utf-8", errors="replace"), stats)
            days = sorted({day.isoformat() for day, _ in [*archived_dated, *dated_paths]})
            filename = rollup_name(wk)
            _atomic_write(directory / filename, render_rollup(wk, days, stats))
            archived: list[str] = []
            deleted: list[str] = []
            for _, path in dated_paths:
                if delete:
                    path.unlink()
                    deleted.append(path.name)
                else:
                    archive_dir.mkdir(parents=True, exist_ok=True)
                    path.replace(archive_dir / path.name)
                    archived.append(path.name)
            results.append(
                WeekRollup(
                    week=wk,
                    days=days,
                    rollup_filename=filename,
                    archived=archived,
                    deleted=deleted,
                )
            )
    return results
