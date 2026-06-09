# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Aaron K. Clark
"""Export/import the entire OMI dataset.

Two formats:

* ``json``  — one human-readable bundle: a manifest plus each note's raw
  Markdown and parsed fields. Portable and diffable. The derived ``index.md``
  is omitted (it is regenerated on import); everything else under the OMI root
  that is a top-level ``*.md`` file is included.
* ``targz`` — a byte-for-byte snapshot of the whole OMI folder, including the
  ``.obsidian/`` config, the template, and the index. Full-fidelity migration.

Import identity is the **filename**. For each incoming note:

* no file with that name        -> added
* same name, identical bytes    -> unchanged (no-op)
* same name, different bytes     -> conflict: skipped (disk wins) unless ``force``

Imports never delete. Path traversal is rejected on both formats.
"""

from __future__ import annotations

import json
import tarfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from omind import __version__, seeds
from omind.store import OmiStore, parse_note, today

Logger = Callable[[str], None]

EXPORT_VERSION = 1
FORMATS = ("json", "targz")


class TransferError(Exception):
    """Raised on a bad format, unreadable bundle, or unsafe member path."""


@dataclass
class ExportResult:
    path: Path
    fmt: str
    note_count: int


@dataclass
class ImportResult:
    """What an import did, keyed by the affected filename/relative path."""

    added: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)  # differing content, skipped
    overwritten: list[str] = field(default_factory=list)  # differing content, --force

    @property
    def changed(self) -> bool:
        return bool(self.added or self.overwritten)


def detect_format(path: Path) -> str:
    """Infer the bundle format from a path's extension."""
    name = path.name.lower()
    if name.endswith(".json"):
        return "json"
    if name.endswith(".tar.gz") or name.endswith(".tgz"):
        return "targz"
    raise TransferError(
        f"cannot infer format from {path.name!r}; use a .json or .tar.gz extension"
    )


def default_export_name(fmt: str) -> str:
    return "omi-export.json" if fmt == "json" else "omi-export.tar.gz"


def _exportable_md(omi_dir: Path) -> list[Path]:
    """Top-level ``*.md`` notes, excluding the derived index and dotfiles."""
    if not omi_dir.is_dir():
        return []
    return sorted(
        p
        for p in omi_dir.glob("*.md")
        if p.name != seeds.INDEX_FILENAME and not p.name.startswith(".")
    )


# -- export ------------------------------------------------------------------


def export_dataset(
    omi_dir: Path | str,
    out_path: Path | str,
    fmt: str = "json",
    log: Logger = print,
) -> ExportResult:
    omi = Path(omi_dir).expanduser()
    if not omi.is_dir():
        raise TransferError(f"OMI folder not found: {omi}")
    if fmt not in FORMATS:
        raise TransferError(f"unknown format {fmt!r} (expected one of {', '.join(FORMATS)})")
    out = Path(out_path).expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)

    note_count = _export_json(omi, out) if fmt == "json" else _export_targz(omi, out)

    log(f"exported {note_count} note(s) from {omi} -> {out} ({fmt})")
    return ExportResult(path=out, fmt=fmt, note_count=note_count)


def _export_json(omi: Path, out: Path) -> int:
    notes: list[dict[str, Any]] = []
    for path in _exportable_md(omi):
        content = path.read_text(encoding="utf-8")
        notes.append(
            {
                "filename": path.name,
                "content": content,
                "fields": parse_note(content).to_dict(),
            }
        )
    bundle = {
        "omind_export_version": EXPORT_VERSION,
        "omind_version": __version__,
        "exported_at": today(),
        "source": str(omi),
        "note_count": len(notes),
        "notes": notes,
    }
    out.write_text(json.dumps(bundle, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return len(notes)


def _export_targz(omi: Path, out: Path) -> int:
    note_count = 0
    with tarfile.open(out, "w:gz") as tar:
        for path in sorted(omi.rglob("*")):
            if not path.is_file():
                continue
            arcname = path.relative_to(omi).as_posix()
            tar.add(path, arcname=arcname)
            if (
                path.suffix == ".md"
                and path.parent == omi
                and path.name != seeds.INDEX_FILENAME
                and not path.name.startswith(".")
            ):
                note_count += 1
    return note_count


# -- import ------------------------------------------------------------------


def import_dataset(
    omi_dir: Path | str,
    src_path: Path | str,
    *,
    force: bool = False,
    log: Logger = print,
) -> ImportResult:
    omi = Path(omi_dir).expanduser()
    src = Path(src_path).expanduser()
    if not src.is_file():
        raise TransferError(f"import file not found: {src}")
    fmt = detect_format(src)
    omi.mkdir(parents=True, exist_ok=True)
    result = ImportResult()

    if fmt == "json":
        _import_json(omi, src, force, result)
    else:
        _import_targz(omi, src, force, result)

    # index.md is derived — rebuild it from whatever notes now exist on disk.
    OmiStore(omi).update_index()

    log(
        f"import: +{len(result.added)} added, "
        f"{len(result.unchanged)} unchanged, "
        f"{len(result.overwritten)} overwritten, "
        f"{len(result.conflicts)} conflict(s) skipped"
    )
    if result.conflicts and not force:
        log("  conflicts (on-disk kept; re-run with --force to overwrite):")
        for name in result.conflicts:
            log(f"    ~ {name}")
    return result


def _classify_and_write(
    target: Path, data: bytes, label: str, force: bool, result: ImportResult
) -> None:
    """Write ``data`` to ``target`` per the content-aware import rules."""
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        result.added.append(label)
        return
    existing = target.read_bytes()
    if existing == data:
        result.unchanged.append(label)
    elif force:
        target.write_bytes(data)
        result.overwritten.append(label)
    else:
        result.conflicts.append(label)


def _import_json(omi: Path, src: Path, force: bool, result: ImportResult) -> None:
    try:
        data = json.loads(src.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise TransferError(f"invalid JSON export: {exc}") from exc
    notes = data.get("notes") if isinstance(data, dict) else None
    if not isinstance(notes, list):
        raise TransferError("invalid export: top-level 'notes' list is missing")

    store = OmiStore(omi)
    for entry in notes:
        if not isinstance(entry, dict):
            continue
        filename = str(entry.get("filename", "")).strip()
        content = entry.get("content")
        if not filename or not isinstance(content, str):
            continue
        if filename == seeds.INDEX_FILENAME:  # derived; never import
            continue
        try:
            target = store.safe_name(filename)  # rejects traversal, ensures .md, in-dir
        except Exception as exc:  # noqa: BLE001 - store raises NoteError
            raise TransferError(f"unsafe filename in export: {filename!r} ({exc})") from exc
        _classify_and_write(target, content.encode("utf-8"), target.name, force, result)


def _import_targz(omi: Path, src: Path, force: bool, result: ImportResult) -> None:
    omi_resolved = omi.resolve()
    with tarfile.open(src, "r:gz") as tar:
        for member in tar.getmembers():
            if not member.isfile():
                continue
            rel = member.name
            target = (omi / rel).resolve()
            # Traversal guard: every member must land inside the OMI dir.
            if target != omi_resolved and omi_resolved not in target.parents:
                raise TransferError(f"archive member escapes the OMI directory: {rel!r}")
            if Path(rel).name == seeds.INDEX_FILENAME and Path(rel).parent == Path("."):
                continue  # derived top-level index; regenerated after import
            extracted = tar.extractfile(member)
            if extracted is None:
                continue
            _classify_and_write(target, extracted.read(), rel, force, result)
