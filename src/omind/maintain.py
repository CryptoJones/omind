# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Aaron K. Clark
"""Sleep-time janitor — ``omind maintain`` (2026-08-27 roundtable, item #3).

One opt-in maintenance pass over a vault, built to the ratified invariants:

* **Consolidation is propose-only, always.** ``maintain`` may surface merge
  candidates but never applies them — propose-and-review is permanent, so even
  ``--apply`` only proposes.
* **Single janitor per vault.** A non-blocking flock mutex refuses a second
  concurrent run rather than double-maintaining.
* **Refuse while a mesh sync (or any vault write) is in flight** — an explicit
  probe of the vault write-lock, not a guess.
* **Fail-closed pipeline.** A failed step aborts the rest; in particular the
  fleet-propagating ``--sync`` never runs after an earlier failure.
* **GC / mesh-sync stays out of the default apply set** — it is the only step
  that propagates fleet-wide and is irreversible, so it is opt-in via ``--sync``.
* **Rollups are opt-in** (``--rollup``), never in the default set — a lossy
  history squash is a deliberate choice, not a default.
* **No per-run report notes in the vault.** The report goes to stdout and the
  state dir; ``--report-note`` is the only way one lands in the vault.
"""

from __future__ import annotations

import contextlib
import json
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path

from omind import consolidate, filelock, journal, mesh, paths, searchindex
from omind.store import LOCK_FILENAME, SCRATCH_SUFFIX, NoteFields, OmiStore

#: Scratch-tier TTL (item #5 part 2): 7 days from LAST MODIFICATION (HAL9000's
#: clock refinement — not creation). Expiry ARCHIVES, never deletes.
_SCRATCH_TTL_DAYS = 7


@dataclass
class StepResult:
    name: str
    ok: bool
    detail: str


@dataclass
class MaintainReport:
    ran: bool = False
    #: Non-empty when the janitor declined to run (mutex held, or sync in flight).
    refused: str = ""
    apply: bool = False
    aborted: bool = False
    steps: list[StepResult] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _mesh_sync_in_flight(omi_dir: Path) -> bool:
    """Whether the vault write-lock is held right now (a sync or a note write)."""
    with filelock.try_exclusive(omi_dir / LOCK_FILENAME) as acquired:
        return not acquired


def _reindex_detail(omi_dir: Path) -> str:
    """Refresh the derived index. ``refresh`` is incremental and live — it never
    empties the index in place, so a concurrent searcher never sees a hole (the
    build-and-swap invariant)."""
    index = searchindex.shared(omi_dir)
    if index is None:
        return "index unavailable on this machine — skipped"
    result = index.refresh()
    if result is None:
        return "index busy — skipped"
    return f"{result.reindexed} reindexed, {result.removed} reaped, {result.notes} notes"


def _expire_scratch(omi_dir: Path, *, apply: bool) -> str:
    """Archive scratch notes untouched for the TTL. Machine-local, so this is
    safe outside ``--sync``; archives (never deletes) via the soft-delete path."""
    store = OmiStore(omi_dir)
    cutoff = time.time() - _SCRATCH_TTL_DAYS * 86_400
    expired: list[str] = []
    for path in sorted(omi_dir.glob(f"*{SCRATCH_SUFFIX}")):
        with contextlib.suppress(Exception):
            if store.read_fields(path.name).disabled:
                continue  # already archived
            if path.stat().st_mtime >= cutoff:
                continue
            expired.append(path.name)
            if apply:
                store.disable_note(path.name)
    verb = "archived" if apply else "would expire"
    return f"{len(expired)} scratch note(s) {verb} (>{_SCRATCH_TTL_DAYS}d since last change)"


def _sync_detail(omi_dir: Path, node_id: str) -> str:
    report = mesh.sync(omi_dir, node_id, log=lambda *_: None)
    return f"synced against {len(report.peers)} peer(s)"


def _persist(omi_dir: Path, report: MaintainReport) -> None:
    path = paths.maintain_state_path(omi_dir)
    with contextlib.suppress(OSError):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report.to_dict(), indent=2), encoding="utf-8")


def _write_report_note(omi_dir: Path, report: MaintainReport) -> None:
    """Opt-in only: a vault note summarising the run. Off by default because a
    report note is itself lint/dedup fodder that then replicates fleet-wide."""
    lines = [f"- {s.name}: {'ok' if s.ok else 'FAILED'} — {s.detail}" for s in report.steps]
    OmiStore(omi_dir).create_note(
        NoteFields(
            title="Maintenance Report",
            summary="Latest `omind maintain` run.",
            details="\n".join(lines) or "no steps ran",
            tags=["omi", "maintenance"],
        )
    )


def run(
    omi_dir: Path | str,
    *,
    node_id: str = "",
    apply: bool = False,
    sync: bool = False,
    rollup: bool = False,
    report_note: bool = False,
    log: Callable[[str], None] = print,
) -> MaintainReport:
    """Run one maintenance pass. Safe by default: with no flags it proposes and
    reports, changing nothing in the vault."""
    omi = Path(omi_dir).expanduser()
    report = MaintainReport(apply=apply)

    with filelock.try_exclusive(paths.maintain_lock_path(omi)) as got_mutex:
        if not got_mutex:
            report.refused = "another `omind maintain` is already running for this vault"
            log(report.refused)
            return report
        if _mesh_sync_in_flight(omi):
            report.refused = "a mesh sync or vault write is in flight — try again shortly"
            log(report.refused)
            return report
        report.ran = True

        def step(name: str, action: Callable[[], str]) -> None:
            if report.aborted:
                return
            try:
                detail = action()
                report.steps.append(StepResult(name, True, detail))
                log(f"[maintain] {name}: {detail}")
            except Exception as exc:  # noqa: BLE001 — fail-closed: record and stop
                report.steps.append(StepResult(name, False, f"aborted: {exc}"))
                report.aborted = True
                log(f"[maintain] {name} FAILED, aborting: {exc}")

        # Always propose-only, in every mode (propose-and-review is permanent).
        step(
            "propose-consolidations",
            lambda: f"{len(consolidate.propose(omi))} merge review plan(s) proposed"
            " (propose-only; never auto-applied)",
        )
        # Scratch expiry runs in every mode: reports what would expire on a dry
        # run, archives on --apply. Machine-local, so it is safe outside --sync.
        step("expire-scratch", lambda: _expire_scratch(omi, apply=apply))
        if apply:
            step("reindex", lambda: _reindex_detail(omi))
        if rollup:
            step("rollup", lambda: f"{len(journal.rollup_journals(omi))} week(s) rolled up")
        # The only fleet-propagating, irreversible step: opt-in, and last, so a
        # failure above (report.aborted) keeps it from ever running.
        if sync:
            step("mesh-sync", lambda: _sync_detail(omi, node_id))

        _persist(omi, report)
        if report_note and not report.aborted:
            with contextlib.suppress(Exception):
                _write_report_note(omi, report)
    return report
