# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Aaron K. Clark
"""Tests for omind.txn: journaled multi-note writes and their recovery.

The interesting cases are all failure cases, so most of these interrupt a
transaction on purpose and then assert what `omind recover` does with the
wreckage.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from omind import txn
from omind.store import NoteFields, OmiStore, _atomic_write


@pytest.fixture
def omi(tmp_path: Path) -> Path:
    d = tmp_path / "OMI"
    d.mkdir()
    return d


def _write(path: Path, text: str) -> None:
    _atomic_write(path, text)


def test_commit_applies_every_write_and_leaves_no_journal(omi: Path) -> None:
    a, b = omi / "A.md", omi / "B.md"
    _write(a, "old A\n")
    t = txn.Transaction(omi)
    t.write(a, "new A\n")
    t.write(b, "new B\n")
    t.prepare()
    t.apply(_atomic_write)
    t.commit()

    assert a.read_text() == "new A\n"
    assert b.read_text() == "new B\n"
    assert txn.pending(omi) == []  # nothing left to recover


def test_prepare_changes_nothing_on_disk(omi: Path) -> None:
    """Preparing is the point where a crash becomes *recoverable*, not visible."""
    a = omi / "A.md"
    _write(a, "old A\n")
    t = txn.Transaction(omi)
    t.write(a, "new A\n")
    t.prepare()

    assert a.read_text() == "old A\n"
    assert len(txn.pending(omi)) == 1  # journaled, uncommitted


def test_an_interrupted_apply_is_rolled_back(omi: Path) -> None:
    """The whole point: partial state is undone, not left for a human to find."""
    a, b, c = omi / "A.md", omi / "B.md", omi / "C.md"
    _write(a, "old A\n")
    _write(b, "old B\n")

    t = txn.Transaction(omi)
    t.write(a, "new A\n")
    t.write(b, "new B\n")
    t.write(c, "created C\n")
    t.prepare()

    # Die after the first write lands: A is new, B is stale, C never existed.
    written = 0

    def dying_writer(path: Path, content: str) -> None:
        nonlocal written
        if written >= 1:
            raise KeyboardInterrupt("power loss")
        written += 1
        _atomic_write(path, content)

    with pytest.raises(KeyboardInterrupt):
        t.apply(dying_writer)
    assert a.read_text() == "new A\n"  # genuinely half-applied
    assert b.read_text() == "old B\n"

    reports = txn.recover(omi, _atomic_write)
    assert len(reports) == 1 and reports[0].clean
    assert a.read_text() == "old A\n"  # rolled back
    assert b.read_text() == "old B\n"  # untouched
    assert not c.exists()  # a file we created is removed again
    assert txn.pending(omi) == []


def test_recovery_refuses_to_clobber_an_edit_made_after_the_crash(omi: Path) -> None:
    """Someone's later edit is newer information than our pre-image (#194).

    Blind rollback here would be data loss wearing recovery's clothes.
    """
    a = omi / "A.md"
    _write(a, "old A\n")
    t = txn.Transaction(omi)
    t.write(a, "new A\n")
    t.prepare()
    t.apply(_atomic_write)  # applied, but the process died before commit()

    a.write_text("a human fixed this by hand\n", encoding="utf-8")

    reports = txn.recover(omi, _atomic_write)
    assert len(reports) == 1
    assert reports[0].conflicts == ["A.md"]
    assert not reports[0].clean
    assert a.read_text() == "a human fixed this by hand\n"  # preserved
    assert txn.pending(omi)  # journal kept, so the evidence survives for a human


def test_recovery_is_idempotent(omi: Path) -> None:
    a = omi / "A.md"
    _write(a, "old A\n")
    t = txn.Transaction(omi)
    t.write(a, "new A\n")
    t.prepare()
    t.apply(_atomic_write)

    assert txn.recover(omi, _atomic_write)[0].clean
    assert a.read_text() == "old A\n"
    assert txn.recover(omi, _atomic_write) == []  # second pass finds nothing
    assert a.read_text() == "old A\n"


def test_a_committed_transaction_is_never_rolled_back(omi: Path) -> None:
    """A crash between the commit record and the cleanup must not undo the work."""
    a = omi / "A.md"
    _write(a, "old A\n")
    t = txn.Transaction(omi)
    t.write(a, "new A\n")
    t.prepare()
    t.apply(_atomic_write)
    t._record(txn.COMMITTED)  # commit record fsynced; cleanup did not run

    assert txn.pending(omi) == []
    assert txn.recover(omi, _atomic_write) == []
    assert a.read_text() == "new A\n"


def test_dry_run_reports_without_changing_anything(omi: Path) -> None:
    a = omi / "A.md"
    _write(a, "old A\n")
    t = txn.Transaction(omi)
    t.write(a, "new A\n")
    t.prepare()
    t.apply(_atomic_write)

    reports = txn.recover(omi, _atomic_write, dry_run=True)
    assert len(reports) == 1 and reports[0].skipped == ["A.md"]
    assert a.read_text() == "new A\n"  # untouched
    assert txn.pending(omi)  # still pending


def test_the_journal_lives_outside_the_vault(omi: Path) -> None:
    """Invariant 1: derived state never lands in a mesh-synced vault."""
    t = txn.Transaction(omi)
    t.write(omi / "A.md", "x\n")
    t.prepare()
    assert not list(omi.rglob("journal.json"))
    assert txn.pending(omi)[0].is_relative_to(txn.paths.state_dir())


def test_consolidate_apply_rolls_back_a_failed_merge(omi: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """The write path that used to concede 'extra recoverable copies' (#194)."""
    store = OmiStore(omi)
    store.create_note(NoteFields(title="Source One", summary="first"))
    store.create_note(NoteFields(title="Source Two", summary="second"))
    sources = [
        ("Source One.md", store.note_version("Source One.md")),
        ("Source Two.md", store.note_version("Source Two.md")),
    ]
    before = {name: (omi / name).read_text() for name, _ in sources}

    real = txn.Transaction.apply
    calls = {"n": 0}

    def flaky(self: txn.Transaction, writer: object) -> None:
        calls["n"] += 1

        def half(path: Path, content: str) -> None:
            if calls["n"] and path.name == "Source Two.md":
                raise OSError("disk full")
            _atomic_write(path, content)

        real(self, half)

    monkeypatch.setattr(txn.Transaction, "apply", flaky)
    with pytest.raises(OSError):
        store.create_and_disable_sources(
            NoteFields(title="Merged", summary="merged"), sources
        )

    # Neither a stray merged note nor a half-archived source survives.
    assert not (omi / "Merged.md").exists()
    for name, _ in sources:
        assert (omi / name).read_text() == before[name]
    assert txn.pending(omi) == []  # rolled back in-process; no manual recover needed


def test_recover_cli_reports_and_exits_nonzero_on_conflict(
    omi: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from omind.cli import main

    a = omi / "A.md"
    _write(a, "old A\n")
    t = txn.Transaction(omi)
    t.write(a, "new A\n")
    t.prepare()
    t.apply(_atomic_write)
    a.write_text("hand-edited\n", encoding="utf-8")

    code = main(["recover", "--vault", str(omi.parent), "--folder", omi.name])
    out = capsys.readouterr()
    assert code == 1
    assert "CONFLICT" in out.out
    assert a.read_text() == "hand-edited\n"


def test_recover_cli_is_a_noop_on_a_clean_journal(
    omi: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from omind.cli import main

    assert main(["recover", "--vault", str(omi.parent), "--folder", omi.name]) == 0
    assert "nothing to recover" in capsys.readouterr().out


def test_a_corrupt_journal_is_reported_not_silently_skipped(omi: Path) -> None:
    t = txn.Transaction(omi)
    t.write(omi / "A.md", "x\n")
    t.prepare()
    journal = txn.pending(omi)[0] / "journal.json"
    journal.write_text("{not json", encoding="utf-8")

    assert txn.pending(omi) == []  # unparseable: not claimed as recoverable
    with pytest.raises(txn.TransactionError):
        txn._rollback_journal(journal.parent, _atomic_write)


def test_prepare_records_every_target_in_the_journal(omi: Path) -> None:
    a = omi / "A.md"
    _write(a, "old A\n")
    t = txn.Transaction(omi)
    t.write(a, "new A\n")
    t.write(omi / "B.md", "new B\n")
    t.prepare()

    payload = json.loads((txn.pending(omi)[0] / "journal.json").read_text())
    assert payload["state"] == txn.PREPARED
    entries = {Path(e["path"]).name: e for e in payload["entries"]}
    assert entries["A.md"]["existed"] is True and entries["A.md"]["prior_sha"]
    assert entries["B.md"]["existed"] is False and not entries["B.md"]["prior_sha"]
    assert all(e["new_sha"] for e in payload["entries"])  # the undo proof


def test_rollback_recognizes_its_own_write_after_line_ending_translation(
    omi: Path,
) -> None:
    """CRLF on disk must not read as "somebody else edited this" (Windows).

    `_atomic_write` writes in text mode, so on Windows every `\\n` lands as
    `\\r\\n`. Hashing raw bytes made the file we had just written match neither
    the pre-image nor our intended content, so recovery classified *everything*
    as a conflict and rolled back nothing — the feature was inert on Windows and
    said nothing about it. Both Windows CI legs caught it.

    Reproduced here on any platform by writing the translated bytes directly.
    """
    a = omi / "A.md"
    a.write_bytes(b"old A\r\n")
    t = txn.Transaction(omi)
    t.write(a, "new A\n")
    t.prepare()
    a.write_bytes(b"new A\r\n")  # what the writer leaves on a Windows disk

    report = txn.recover(omi, _atomic_write)[0]
    assert report.clean, f"CRLF read as a foreign edit: {report.conflicts}"
    assert report.restored == ["A.md"]
    assert a.read_text().replace("\r\n", "\n") == "old A\n"
