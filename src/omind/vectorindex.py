# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Aaron K. Clark
"""Semantic vector index over OMI note metadata (omind 3.0.0).

Embeds each note's *metadata* (title + summary + tags — the searchable essence,
not the whole body) into a vector with :mod:`omind.embed`, persists the vectors
machine-locally, and ranks notes against a query by cosine similarity. Powers
semantic **recall** (``store.search`` / ``retrieve.relevant_titles``) and **dedup**
(nearest existing note to a new one).

Incremental: each note's vector is keyed by a hash of its metadata text + the
model name, so :meth:`refresh` only re-embeds notes whose metadata actually
changed (and drops vectors for deleted notes / a changed model). The index lives
in the state dir (not the vault) — it is derivable from the notes and specific to
the embedding model, so it is neither synced over the mesh nor committed.

FAILS OPEN like everything in the semantic layer: with no embed backend every
method returns ``None`` and the caller uses the keyword path. The cosine math is
pure Python over the stored vectors (numpy lives only inside ``embed.encode``), so
the index itself carries no heavy dependency.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
from pathlib import Path
from typing import Any

from omind import embed, paths


def _safe(omi_dir: Path | str) -> str:
    """A filesystem-safe, per-vault index id from the resolved OMI path."""
    raw = str(Path(omi_dir).expanduser())
    digest = hashlib.blake2s(raw.encode("utf-8"), digest_size=8).hexdigest()
    return digest


def _meta_text(title: str, summary: str, tags: list[str]) -> str:
    """The text embedded for a note: its searchable metadata, not the full body."""
    return "\n".join([title, summary, " ".join(tags)]).strip()


def _key(text: str, model: str) -> str:
    """Change token for a note's embedding: invalidates when the metadata text or
    the model changes, so :meth:`refresh` re-embeds exactly what it must."""
    return hashlib.blake2s(f"{model}\x00{text}".encode(), digest_size=16).hexdigest()


def _rows(vecs: Any) -> list[list[float]]:
    """Coerce whatever ``embed.encode`` returns (numpy array or list) to plain
    float rows, so the rest of this module needs no numpy."""
    return [[float(x) for x in row] for row in vecs]


def _dot(a: list[float], b: list[float]) -> float:
    """Dot product = cosine for the L2-normalised vectors ``embed.encode`` returns."""
    return sum(x * y for x, y in zip(a, b, strict=False))


_MODEL_ENV = "OMI_EMBED_MODEL"


class VectorIndex:
    """A per-vault semantic index over note metadata. Cheap to construct; the
    on-disk index is loaded lazily per operation."""

    def __init__(self, omi_dir: Path | str, *, model: str | None = None) -> None:
        self.omi_dir = Path(omi_dir)
        self.model = model or os.environ.get(_MODEL_ENV) or embed._DEFAULT_MODEL

    def _path(self) -> Path:
        return paths.state_dir() / f"vindex-{_safe(self.omi_dir)}.json"

    def _load(self) -> dict[str, object]:
        try:
            data = json.loads(self._path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"model": self.model, "entries": {}}
        if not isinstance(data, dict) or not isinstance(data.get("entries"), dict):
            return {"model": self.model, "entries": {}}
        # A changed model invalidates every vector — start fresh.
        if data.get("model") != self.model:
            return {"model": self.model, "entries": {}}
        return data

    def _save(self, data: dict[str, object]) -> None:
        with contextlib.suppress(OSError):
            path = self._path()
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(data), encoding="utf-8")
            os.replace(tmp, path)

    def refresh(self) -> int | None:
        """Re-embed new/changed notes, drop deleted ones. Returns how many were
        (re-)embedded, or ``None`` when there is no backend (keyword path)."""
        if not embed.available():
            return None
        try:
            from omind.store import OmiStore

            notes = OmiStore(self.omi_dir).list_notes()
        except Exception:
            return None
        data = self._load()
        entries = data["entries"]
        assert isinstance(entries, dict)
        current: dict[str, str] = {}
        pending: list[tuple[str, str]] = []  # (filename, text) needing an embed
        for note in notes:
            text = _meta_text(note.title, note.summary, note.tags)
            key = _key(text, self.model)
            current[note.filename] = key
            existing = entries.get(note.filename)
            if isinstance(existing, dict) and existing.get("key") == key:
                continue
            pending.append((note.filename, text))
        for filename in [f for f in entries if f not in current]:  # reap deleted notes
            del entries[filename]
        if pending:
            vecs = embed.encode([text for _, text in pending])
            if vecs is None:
                return None
            for (filename, text), row in zip(pending, _rows(vecs), strict=True):
                entries[filename] = {"key": _key(text, self.model), "vec": row}
        self._save(data)
        return len(pending)

    def _ranked(self, query: str) -> list[tuple[str, float]] | None:
        """All indexed notes scored against ``query`` (filename, score), best first,
        or ``None`` with no backend."""
        if not embed.available():
            return None
        self.refresh()
        entries = self._load()["entries"]
        assert isinstance(entries, dict)
        if not entries:
            return []
        qv = embed.encode([query])
        if qv is None:
            return None
        qrow = _rows(qv)[0]
        scored = [
            (filename, _dot(qrow, e["vec"]))
            for filename, e in entries.items()
            if isinstance(e, dict)
            and isinstance(e.get("vec"), list)
            and len(e["vec"]) == len(qrow)  # skip a corrupt / wrong-dim entry rather than misscore
        ]
        scored.sort(key=lambda fs: -fs[1])
        return scored

    def rank(
        self, query: str, *, limit: int = 5, candidates: set[str] | None = None
    ) -> list[tuple[str, float]] | None:
        """Top notes for ``query`` as ``(filename, cosine)`` best-first, or ``None``
        with no backend. ``candidates`` restricts to a filename subset (e.g. notes
        that already passed a tag filter)."""
        ranked = self._ranked(query)
        if ranked is None:
            return None
        if candidates is not None:
            ranked = [fs for fs in ranked if fs[0] in candidates]
        return ranked[:limit]

    def nearest(
        self, text: str, *, exclude: str | None = None, limit: int = 3
    ) -> list[tuple[str, float]] | None:
        """Existing notes most similar to ``text`` (for dedup), excluding the note
        being written. ``None`` with no backend."""
        ranked = self._ranked(text)
        if ranked is None:
            return None
        return [fs for fs in ranked if fs[0] != exclude][:limit]
