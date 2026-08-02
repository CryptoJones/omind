# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Aaron K. Clark
"""Journaled multi-note transactions with deterministic recovery (#194).

Multi-file updates cannot be truly atomic on the filesystems omind runs on.
This module does not pretend otherwise. What it provides instead is a contract
that can actually be kept:

* every target's **pre-image** is captured and fsynced *before* the first write,
* the writes then happen through the store's existing atomic per-file replace,
* a **commit record** marks the point of no return, and
* an interrupted run is rolled back deterministically by :func:`recover`.

So an interrupted apply lands in one of two states, never a third: fully
applied, or fully rolled back once ``omind recover`` runs.

**What this fixes.** ``OmiStore`` already had same-dir temp + ``os.replace``, an
advisory write lock, and version preconditions — the first half. It had no
journal and no rollback, so an interrupted multi-note operation left partial
state with no recovery path. ``store.create_and_disable_sources`` conceded this
in its own docstring: *"a process crash can still leave extra recoverable
copies."* That failed toward keeping data, which is the right direction, but it
still meant a human had to notice and reconcile by hand.

**The rule that keeps recovery safe.** Rollback restores a pre-image *only*
when the file on disk still holds either that pre-image (nothing to do) or
exactly the bytes this transaction intended to write (ours to undo). If it
holds anything else, someone edited the note after the crash, and their edit is
newer information than our pre-image. We refuse, report, and leave it alone.
Blind rollback would be data loss dressed up as recovery.

The journal lives in the state dir, never the vault: it describes this
machine's interrupted filesystem work, is meaningless to a mesh peer, and must
not replicate.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from omind import paths

#: Journal record states. A record that is not ``COMMITTED`` when found is, by
#: definition, an interrupted run: the process died between preparing and
#: committing, because both transitions are fsynced.
PREPARED = "prepared"
COMMITTED = "committed"


def _sha(text: str) -> str:
    """Identity of a note's *content*, independent of line-ending translation.

    ``_atomic_write`` writes in text mode, so on Windows every ``\n`` reaches
    the disk as ``\r\n``. Hashing the raw bytes therefore never matched what we
    had just written, every file looked like somebody else's edit, and recovery
    refused to roll back anything at all — the feature was inert on Windows and
    silent about it. Normalise, then hash.
    """
    return hashlib.sha256(text.replace("\r\n", "\n").encode("utf-8")).hexdigest()


def _sha_bytes(data: bytes) -> str:
    """Content identity for bytes read back off disk."""
    return _sha(data.decode("utf-8", errors="replace"))


def _fsync_path(path: Path) -> None:
    """Best-effort fsync of a file, so a crash cannot lose the journal itself."""
    with contextlib.suppress(OSError):
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _fsync_dir(directory: Path) -> None:
    with contextlib.suppress(OSError, AttributeError):
        fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    """Restore exact bytes: same-dir temp + ``os.replace``, binary mode.

    Deliberately NOT the store's text-mode ``_atomic_write``. A pre-image is
    bytes, and routing it through a text writer re-translates line endings — on
    Windows, restoring ``b"old A\r\n"`` that way produced ``b"old A\r\r\n"``,
    growing a blank line into the note on every rollback. Recovery must put back
    exactly what was there, byte for byte.
    """
    directory = path.parent
    directory.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".tmp-recover-", suffix=".md")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        _fsync_dir(directory)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


class TransactionError(Exception):
    """A transaction could not be prepared, committed, or recovered."""


@dataclass
class _Entry:
    """One file this transaction will replace, and how to undo that."""

    path: Path
    #: Bytes to write. ``None`` means "this file should not exist afterwards",
    #: which recovery reads as "delete it on rollback if we created it".
    content: str
    #: sha256 of the file's bytes before we touched it; "" when it did not exist.
    prior_sha: str = ""
    existed: bool = False
    #: sha256 of what we intend to write — the proof, at recovery time, that a
    #: file's current bytes are ours to undo rather than someone else's edit.
    new_sha: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "prior_sha": self.prior_sha,
            "existed": self.existed,
            "new_sha": self.new_sha,
        }


@dataclass
class RecoveryReport:
    """What one :func:`recover` pass did, per journal."""

    transaction_id: str
    restored: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    #: Files whose bytes match neither our pre-image nor what we wrote: edited
    #: after the crash, so rolling them back would destroy newer work.
    conflicts: list[str] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not self.conflicts

    def format(self) -> str:
        bits = [
            f"transaction {self.transaction_id}",
            f"restored {len(self.restored)}",
            f"removed {len(self.removed)}",
            f"skipped {len(self.skipped)}",
        ]
        if self.conflicts:
            bits.append(f"CONFLICTS {len(self.conflicts)}")
        return ", ".join(bits)


class Transaction:
    """Collects writes, journals their pre-images, then applies them.

    Not a general database transaction: there is no isolation and no concurrent
    reader guarantee. It buys exactly one thing — that an interrupted multi-note
    write can be put back the way it was.

    The caller MUST hold ``OmiStore.write_lock()`` around
    :meth:`prepare`/:meth:`commit`, which is what makes "no other omind writer
    is touching these files" true for the duration.
    """

    def __init__(self, omi_dir: Path | str) -> None:
        self.omi_dir = Path(omi_dir)
        self.id = f"{time.strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}"
        self._entries: list[_Entry] = []
        self._prepared = False

    # -- building -----------------------------------------------------------

    def write(self, path: Path, content: str) -> None:
        """Queue ``path`` to be replaced with ``content`` on commit."""
        if self._prepared:
            raise TransactionError("cannot add writes after prepare()")
        self._entries.append(_Entry(path=Path(path), content=content))

    # -- storage ------------------------------------------------------------

    def _dir(self) -> Path:
        return paths.transaction_dir(self.omi_dir) / self.id

    def _journal_path(self) -> Path:
        return self._dir() / "journal.json"

    def _preimage_path(self, index: int) -> Path:
        return self._dir() / f"{index:04d}.pre"

    def _record(self, state: str) -> None:
        payload = {
            "id": self.id,
            "state": state,
            "vault": str(self.omi_dir),
            "updated": time.time(),
            "entries": [entry.to_json() for entry in self._entries],
        }
        journal = self._journal_path()
        tmp = journal.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        _fsync_path(tmp)
        os.replace(tmp, journal)
        _fsync_dir(journal.parent)

    # -- lifecycle ----------------------------------------------------------

    def prepare(self) -> None:
        """Capture and durably record every target's pre-image.

        Nothing in the vault has changed when this returns — but from here on a
        crash is recoverable, because the bytes needed to undo the writes are on
        disk and fsynced.
        """
        if self._prepared:
            return
        directory = self._dir()
        directory.mkdir(parents=True, exist_ok=True)
        for index, entry in enumerate(self._entries):
            entry.new_sha = _sha(entry.content)
            try:
                prior = entry.path.read_bytes()
            except FileNotFoundError:
                entry.existed = False
                continue
            except OSError as exc:
                raise TransactionError(f"cannot read {entry.path.name}: {exc}") from exc
            entry.existed = True
            entry.prior_sha = _sha_bytes(prior)
            preimage = self._preimage_path(index)
            preimage.write_bytes(prior)
            _fsync_path(preimage)
        _fsync_dir(directory)
        self._record(PREPARED)
        self._prepared = True

    def apply(self, writer: Any) -> None:
        """Perform the writes with ``writer(path, content)`` (the store's
        atomic replace). Must follow :meth:`prepare`."""
        if not self._prepared:
            raise TransactionError("apply() before prepare()")
        for entry in self._entries:
            writer(entry.path, entry.content)

    def commit(self) -> None:
        """Mark the transaction complete and drop the journal.

        Past this point there is nothing to recover: every write landed. The
        commit record is fsynced *before* the journal directory is removed, so a
        crash between the two leaves a committed record that :func:`recover`
        correctly does nothing with.
        """
        if not self._prepared:
            raise TransactionError("commit() before prepare()")
        self._record(COMMITTED)
        self.discard()

    def discard(self) -> None:
        """Remove this transaction's journal and pre-images. Never raises."""
        directory = self._dir()
        with contextlib.suppress(OSError):
            for child in directory.iterdir():
                with contextlib.suppress(OSError):
                    child.unlink()
        with contextlib.suppress(OSError):
            directory.rmdir()

    def rollback(self) -> RecoveryReport:
        """Undo a prepared-but-uncommitted transaction in this process."""
        report = _rollback_journal(self._dir())
        self.discard()
        return report


# -- recovery ---------------------------------------------------------------


def pending(omi_dir: Path | str) -> list[Path]:
    """Journal directories for interrupted transactions, oldest first."""
    root = paths.transaction_dir(Path(omi_dir))
    if not root.is_dir():
        return []
    found: list[Path] = []
    for directory in sorted(root.iterdir()):
        journal = directory / "journal.json"
        if not journal.is_file():
            continue
        try:
            payload = json.loads(journal.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if str(payload.get("state")) != COMMITTED:
            found.append(directory)
    return found


def _rollback_journal(directory: Path) -> RecoveryReport:
    """Restore one journal's pre-images. See the module docstring's safety rule."""
    journal = directory / "journal.json"
    try:
        payload = json.loads(journal.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise TransactionError(f"unreadable journal at {directory}: {exc}") from exc
    report = RecoveryReport(transaction_id=str(payload.get("id") or directory.name))
    for index, raw in enumerate(payload.get("entries") or []):
        path = Path(str(raw.get("path")))
        prior_sha = str(raw.get("prior_sha") or "")
        new_sha = str(raw.get("new_sha") or "")
        existed = bool(raw.get("existed"))
        try:
            current = path.read_bytes()
            current_sha = _sha_bytes(current)
        except FileNotFoundError:
            current_sha = ""
        except OSError:
            report.conflicts.append(path.name)
            continue

        if existed and current_sha == prior_sha:
            report.skipped.append(path.name)  # never written, or already undone
            continue
        if not existed and current_sha == "":
            report.skipped.append(path.name)  # we never created it
            continue
        # Only bytes we wrote are ours to undo. Anything else is someone's edit
        # made after the crash, and it is newer than our pre-image.
        if current_sha != new_sha:
            report.conflicts.append(path.name)
            continue
        if existed:
            preimage = directory / f"{index:04d}.pre"
            try:
                _atomic_write_bytes(path, preimage.read_bytes())
            except OSError:
                report.conflicts.append(path.name)
                continue
            report.restored.append(path.name)
        else:
            with contextlib.suppress(OSError):
                path.unlink()
            report.removed.append(path.name)
    return report


def recover(omi_dir: Path | str, *, dry_run: bool = False) -> list[RecoveryReport]:
    """Roll back every interrupted transaction for this vault.

    A no-op when the journal is clean, which is the normal case. Journals whose
    rollback hit a conflict are **kept**, so a human can look rather than having
    the evidence deleted underneath them.
    """
    reports: list[RecoveryReport] = []
    for directory in pending(omi_dir):
        if dry_run:
            try:
                payload = json.loads((directory / "journal.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            entries = payload.get("entries") or []
            reports.append(
                RecoveryReport(
                    transaction_id=str(payload.get("id") or directory.name),
                    skipped=[Path(str(e.get("path"))).name for e in entries],
                )
            )
            continue
        report = _rollback_journal(directory)
        reports.append(report)
        if report.clean:
            with contextlib.suppress(OSError):
                for child in directory.iterdir():
                    with contextlib.suppress(OSError):
                        child.unlink()
                directory.rmdir()
    return reports
