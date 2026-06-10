# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Aaron K. Clark
"""Tests for omind.transfer: export/import round-trips, conflicts, safety."""

from __future__ import annotations

import io
import json
import tarfile
from pathlib import Path

import pytest

from omind import paths
from omind.store import NoteFields, OmiStore
from omind.transfer import (
    EXPORT_VERSION,
    TransferError,
    detect_format,
    export_dataset,
    import_dataset,
)


def _quiet(_: str) -> None:
    pass


def _seed_omi(omi: Path) -> OmiStore:
    """Create an OMI folder with two notes (+ regenerated index)."""
    omi.mkdir(parents=True, exist_ok=True)
    store = OmiStore(omi)
    store.create_note(NoteFields(title="Alpha", summary="first", tags=["a"]))
    store.create_note(NoteFields(title="Beta", summary="second", tags=["b"]))
    return store


# -- format detection --------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "expected"),
    [("x.json", "json"), ("x.tar.gz", "targz"), ("x.tgz", "targz")],
)
def test_detect_format(name: str, expected: str) -> None:
    assert detect_format(Path(name)) == expected


def test_detect_format_rejects_unknown() -> None:
    with pytest.raises(TransferError):
        detect_format(Path("omi.zip"))


# -- json export -------------------------------------------------------------


def test_export_json_writes_manifest_and_excludes_index(tmp_path: Path) -> None:
    omi = tmp_path / "OMI"
    _seed_omi(omi)
    out = tmp_path / "omi.json"
    result = export_dataset(omi, out, fmt="json", log=_quiet)

    assert result.note_count == 2  # Alpha + Beta, NOT index.md
    bundle = json.loads(out.read_text(encoding="utf-8"))
    assert bundle["omind_export_version"] == EXPORT_VERSION
    assert bundle["note_count"] == 2
    names = {n["filename"] for n in bundle["notes"]}
    assert names == {"Alpha.md", "Beta.md"}
    assert paths.INDEX_FILENAME not in names
    # parsed fields ride along
    alpha = next(n for n in bundle["notes"] if n["filename"] == "Alpha.md")
    assert alpha["fields"]["title"] == "Alpha"
    assert "a" in alpha["fields"]["tags"]


def test_export_missing_folder_raises(tmp_path: Path) -> None:
    with pytest.raises(TransferError):
        export_dataset(tmp_path / "nope", tmp_path / "o.json", fmt="json", log=_quiet)


# -- json round-trip ---------------------------------------------------------


def test_json_round_trip_into_fresh_folder(tmp_path: Path) -> None:
    src = tmp_path / "src" / "OMI"
    _seed_omi(src)
    bundle = tmp_path / "omi.json"
    export_dataset(src, bundle, fmt="json", log=_quiet)

    dest = tmp_path / "dest" / "OMI"
    result = import_dataset(dest, bundle, log=_quiet)

    assert sorted(result.added) == ["Alpha.md", "Beta.md"]
    assert (dest / "Alpha.md").is_file()
    # index.md is regenerated, not imported
    assert (dest / paths.INDEX_FILENAME).is_file()
    assert "[[Alpha]]" in (dest / paths.INDEX_FILENAME).read_text(encoding="utf-8")


def test_reimport_is_all_unchanged(tmp_path: Path) -> None:
    src = tmp_path / "OMI"
    _seed_omi(src)
    bundle = tmp_path / "omi.json"
    export_dataset(src, bundle, fmt="json", log=_quiet)

    first = import_dataset(src, bundle, log=_quiet)
    assert first.unchanged and not first.added  # already on disk
    second = import_dataset(src, bundle, log=_quiet)
    assert sorted(second.unchanged) == ["Alpha.md", "Beta.md"]
    assert not second.conflicts


# -- conflict handling -------------------------------------------------------


def test_conflict_skipped_without_force_then_overwritten_with_force(tmp_path: Path) -> None:
    src = tmp_path / "OMI"
    _seed_omi(src)
    bundle = tmp_path / "omi.json"
    export_dataset(src, bundle, fmt="json", log=_quiet)

    # mutate one note so its content differs from the bundle
    (src / "Alpha.md").write_text("# Alpha\n\ntotally different\n", encoding="utf-8")

    skipped = import_dataset(src, bundle, log=_quiet)
    assert skipped.conflicts == ["Alpha.md"]
    assert "totally different" in (src / "Alpha.md").read_text(encoding="utf-8")  # disk kept

    forced = import_dataset(src, bundle, force=True, log=_quiet)
    assert forced.overwritten == ["Alpha.md"]
    assert "totally different" not in (src / "Alpha.md").read_text(encoding="utf-8")


def test_invalid_json_raises(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(TransferError):
        import_dataset(tmp_path / "OMI", bad, log=_quiet)


def test_json_without_notes_list_raises(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"note_count": 0}), encoding="utf-8")
    with pytest.raises(TransferError):
        import_dataset(tmp_path / "OMI", bad, log=_quiet)


# -- targz -------------------------------------------------------------------


def test_targz_round_trip_includes_obsidian_config(tmp_path: Path) -> None:
    src = tmp_path / "src" / "OMI"
    _seed_omi(src)
    (src / ".obsidian").mkdir()
    (src / ".obsidian" / "app.json").write_text("{}", encoding="utf-8")
    bundle = tmp_path / "omi.tar.gz"
    export_dataset(src, bundle, fmt="targz", log=_quiet)

    dest = tmp_path / "dest" / "OMI"
    import_dataset(dest, bundle, log=_quiet)
    assert (dest / "Alpha.md").is_file()
    assert (dest / ".obsidian" / "app.json").is_file()  # full-fidelity restore


def test_targz_rejects_path_traversal(tmp_path: Path) -> None:
    """A crafted archive with a ../ member must be refused, not extracted."""
    evil = tmp_path / "evil.tar.gz"
    payload = b"pwned"
    with tarfile.open(evil, "w:gz") as tar:
        info = tarfile.TarInfo(name="../escape.md")
        info.size = len(payload)
        tar.addfile(info, io.BytesIO(payload))

    with pytest.raises(TransferError):
        import_dataset(tmp_path / "OMI", evil, log=_quiet)
    assert not (tmp_path / "escape.md").exists()
