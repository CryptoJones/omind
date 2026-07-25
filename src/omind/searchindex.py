# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Aaron K. Clark
"""Derived hybrid search index over an OMI folder (omind 4.3.0).

The notes on disk stay the single source of truth; **this file is disposable**
and rebuildable from them at any time. That separation — storage in Markdown,
search in a derived index — is the one design choice every current
open-source memory layer (memvid, Cognee, Graphiti, the SQLite/FTS5 Obsidian
retrievers) converged on, and the thing omind was missing: ``store.search``
read and parsed *every* note on *every* query only to run ``needle in haystack``,
then ordered the hits by date rather than relevance.

What replaces it, in one SQLite file:

* **Chunks, not documents.** Notes are split at ``## headings`` (via the same
  fence-aware :func:`omind.store.split_sections` the parser and merge driver
  use), so a fact stated only in ``## Details`` is retrievable — the old
  :mod:`omind.vectorindex` embedded title+summary+tags and could never find it.
* **BM25** through SQLite's stdlib FTS5 module, over title/heading/tags/text
  plus a *stems* column fed by :func:`omind.retrieve._stem`, so the existing
  stemmer still earns its keep and ``scoring``/``scored`` match ``score``.
* **Dense vectors** — packed ``float32`` BLOBs scored with one numpy matmul,
  replacing the old JSON-float-list store and its pure-Python ``sum(x*y)``
  loop over every entry. Written only when :func:`omind.embed.available`.
* **Reciprocal Rank Fusion** of the keyword, vector, and recency rankings.
  RRF needs no score calibration between legs, which is exactly why it is the
  fusion everyone reaches for; recency enters as a weak *leg* instead of being
  the sort key that used to bury relevant old notes.
* **The link graph**, so ``store.backlinks``, :mod:`omind.graph` and
  :mod:`omind.lint` stop being three independent full-vault scanners.

Incremental: a note is re-read only when its ``(mtime_ns, size)`` differs and
its content hash actually changed — the persisted form of the ``(mtime_ns,
size)`` trick :meth:`omind.store.OmiStore._cached_summary` already plays
in-process.

FAILS OPEN, like the rest of the retrieval layer: every public method returns
``None`` when the index is unavailable, unbuildable, locked by another process,
or corrupt, and the caller keeps its pre-index behaviour. The index lives in the
state dir (never the vault): it is derivable, machine-local, and specific to the
embedding model, so it is neither mesh-synced nor committed.
"""

from __future__ import annotations

import contextlib
import functools
import hashlib
import os
import sqlite3
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Concatenate, ParamSpec, TypeVar, cast

from omind import paths, retrieve

#: Bumped whenever the schema below changes shape; a mismatch rebuilds from scratch.
SCHEMA_VERSION = 1


def _is_reserved_note_name(name: str) -> bool:
    """Return whether a note filename is reserved for internal/system use."""
    stem = Path(name).stem.lower()
    return stem in {"index", "template", "_template"}

#: RRF constant. 60 is the value from the original TREC paper and what every
#: hybrid-retrieval implementation in the survey uses; it damps the difference
#: between rank 1 and rank 2 enough that no single leg dictates the fusion.
RRF_K = 60
#: Per-leg weights. Keyword leads (it is the precise leg on identifiers and
#: names), semantics is a near-equal partner, and recency is deliberately weak:
#: a tie-breaker and freshness nudge, never the ordering.
_W_KEYWORD = 1.0
_W_VECTOR = 0.9
_W_RECENCY = 0.25
#: How many candidates each leg contributes before fusion.
_LEG_DEPTH = 60
#: Soft cap on one indexed chunk. Long ``## Details`` bodies are split further so
#: an excerpt stays excerpt-sized and one huge section can't swamp BM25 lengths.
_MAX_CHUNK_CHARS = 1_200
#: Characters of matched text returned per hit — the payload the agent actually
#: reads. Bounded here because "return excerpts, not documents" is where the
#: token savings in the literature (Memori, SimpleMem) come from.
EXCERPT_CHARS = 180

_MODEL_ENV = "OMI_EMBED_MODEL"
#: Set to disable the index entirely and fall back to the scanning search paths.
DISABLE_ENV = "OMI_INDEX_DISABLE"

_fts5_ok: bool | None = None


def _fts5_available() -> bool:
    """Whether this interpreter's sqlite3 was built with FTS5 (cached)."""
    global _fts5_ok
    if _fts5_ok is None:
        try:
            with contextlib.closing(sqlite3.connect(":memory:")) as probe:
                probe.execute("CREATE VIRTUAL TABLE probe USING fts5(x)")
            _fts5_ok = True
        except sqlite3.Error:
            _fts5_ok = False
    return _fts5_ok


def available() -> bool:
    """True when a hybrid index can be used at all on this machine."""
    return not os.environ.get(DISABLE_ENV) and _fts5_available()


@dataclass
class Hit:
    """One ranked chunk. ``excerpt`` is the matched text, not the whole note."""

    filename: str
    heading: str
    excerpt: str
    score: float
    #: Per-leg 1-based ranks, 0 when the leg did not return this chunk. Surfaced
    #: by ``omind search --explain`` so a bad ranking is diagnosable, not magic.
    keyword_rank: int = 0
    vector_rank: int = 0
    recency_rank: int = 0


@dataclass
class Refresh:
    """What a :meth:`SearchIndex.refresh` actually did."""

    notes: int = 0
    reindexed: int = 0
    removed: int = 0
    embedded: int = 0
    seconds: float = 0.0


@dataclass
class _Chunk:
    heading: str
    ordinal: int
    text: str
    start_line: int
    end_line: int


@dataclass
class _NoteRow:
    filename: str
    title: str
    created: str
    okf_type: str = ""
    tags: list[str] = field(default_factory=list)
    disabled: bool = False


_SCHEMA = """
CREATE TABLE IF NOT EXISTS notes (
    filename TEXT PRIMARY KEY,
    title    TEXT NOT NULL DEFAULT '',
    created  TEXT NOT NULL DEFAULT '',
    okf_type TEXT NOT NULL DEFAULT '',
    disabled INTEGER NOT NULL DEFAULT 0,
    mtime_ns INTEGER NOT NULL DEFAULT 0,
    size     INTEGER NOT NULL DEFAULT 0,
    sha      TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS note_tags (
    filename TEXT NOT NULL,
    tag      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS note_tags_tag ON note_tags(tag);
CREATE INDEX IF NOT EXISTS note_tags_file ON note_tags(filename);
CREATE TABLE IF NOT EXISTS chunks (
    id         INTEGER PRIMARY KEY,
    filename   TEXT NOT NULL,
    heading    TEXT NOT NULL DEFAULT '',
    ord        INTEGER NOT NULL DEFAULT 0,
    start_line INTEGER NOT NULL DEFAULT 0,
    end_line   INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS chunks_file ON chunks(filename);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    title, heading, tags, text, stems,
    tokenize = "unicode61 remove_diacritics 2"
);
CREATE TABLE IF NOT EXISTS vectors (
    chunk_id INTEGER PRIMARY KEY,
    vec      BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS links (
    src    TEXT NOT NULL,
    target TEXT NOT NULL,   -- lowercased, for resolution
    raw    TEXT NOT NULL    -- as written, for reporting a dangling link
);
CREATE INDEX IF NOT EXISTS links_src ON links(src);
CREATE INDEX IF NOT EXISTS links_target ON links(target);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


_P = ParamSpec("_P")
_R = TypeVar("_R")


def _locked(
    method: Callable[Concatenate[SearchIndex, _P], _R],
) -> Callable[Concatenate[SearchIndex, _P], _R]:
    """Serialize a public entry point on the index's re-entrant lock.

    One sqlite3 connection is shared per :class:`SearchIndex`, and the web app
    answers requests on a thread pool — without this, two concurrent reads on the
    same connection raise or interleave. Re-entrant because the query paths call
    :meth:`SearchIndex.refresh` from inside an already-held lock.
    """

    @functools.wraps(method)
    def wrapper(self: SearchIndex, *args: _P.args, **kwargs: _P.kwargs) -> _R:
        with self._lock:
            return method(self, *args, **kwargs)

    return cast("Callable[Concatenate[SearchIndex, _P], _R]", wrapper)


def _vault_id(omi_dir: Path | str) -> str:
    raw = str(Path(omi_dir).expanduser())
    return hashlib.blake2s(raw.encode("utf-8"), digest_size=8).hexdigest()


def index_path(omi_dir: Path | str) -> Path:
    """Where this vault's derived index lives (state dir, never the vault)."""
    return paths.state_dir() / f"searchindex-{_vault_id(omi_dir)}.sqlite3"


def _stems_of(text: str) -> str:
    """The stem bag indexed alongside raw text, from omind's own stemmer."""
    return " ".join(sorted(retrieve._tokens(text)))


def _query_words(query: str) -> list[list[str]]:
    """The query's meaningful words, each as its own ``[word, stem]`` group.

    Grouping matters: matching must be *all words*, but any spelling of a word —
    ``colorblindness`` and its stem ``colorblind`` are one requirement, not two.
    """
    groups: list[list[str]] = []
    seen: set[str] = set()
    for word in retrieve._WORD_RE.findall(query.lower()):
        if len(word) < 2 or word in retrieve._STOPWORDS or word in seen:
            continue
        seen.add(word)
        stem = retrieve._stem(word)
        groups.append([word] if stem == word else [word, stem])
    return groups


def _fts_query(query: str, *, require_all: bool = False) -> str:
    """A safe FTS5 MATCH expression for free-text ``query``, or ``""``.

    Every term is double-quoted (so ``NEAR``, ``-``, ``:`` and quotes in user text
    can never be read as FTS5 operators — the injection/syntax-error class that
    makes naive MATCH building fail) and prefix-matched, so the *stems* column can
    answer inflected queries.

    ``require_all`` ANDs the word groups instead of ORing them. The keyword leg
    runs both: chunks matching *every* word rank above chunks matching only some.
    That grading is what keeps a filler word from dragging in noise — in "how do I
    handle colorblindness", a chunk about ``handle`` alone lands strictly below
    every chunk mentioning both — and unlike a document-frequency cutoff it needs
    no threshold, so it behaves the same on a 10-note vault and a 10,000-note one.
    """
    groups = _query_words(query)
    if not groups:
        return ""
    joined = [" OR ".join(f'"{term}"*' for term in group) for group in groups]
    if not require_all:
        return " OR ".join(joined)
    return " AND ".join(f"({part})" for part in joined)


def _pack(vec: Sequence[float]) -> bytes:
    import numpy as np

    return bytes(np.asarray(vec, dtype="float32").tobytes())


def _query_vector(text: str) -> Any:
    """Embed one query as a 1-D float32 row, or ``None``.

    Coerces whatever the backend returned (ndarray, list of lists) rather than
    assuming ndarray, so a substitute backend can't crash a query path.
    """
    from omind import embed

    try:
        import numpy as np

        vecs = embed.encode([text])
        if vecs is None:
            return None
        row = np.asarray(vecs, dtype="float32")
        if row.ndim == 1:
            row = row.reshape(1, -1)
        return row[0] if row.shape[0] else None
    except (ImportError, ValueError, TypeError):
        return None


class SearchIndex:
    """A vault's derived hybrid index. Cheap to construct; opens lazily."""

    def __init__(self, omi_dir: Path | str, *, model: str | None = None) -> None:
        from omind import embed

        self.omi_dir = Path(omi_dir)
        self.model = model or os.environ.get(_MODEL_ENV) or embed._DEFAULT_MODEL
        self._db: sqlite3.Connection | None = None
        # Per-process cache of the packed vector matrix, keyed by the index
        # generation so a refresh (here or in another process) invalidates it.
        self._matrix: tuple[str, list[int], Any] | None = None
        # One connection shared across threads (the web app serves requests on a
        # thread pool), so every public entry point serializes on this lock —
        # sqlite3 connections are not safe to use concurrently.
        self._lock = threading.RLock()

    # -- plumbing -----------------------------------------------------------

    def path(self) -> Path:
        return index_path(self.omi_dir)

    def _connect(self) -> sqlite3.Connection | None:
        if self._db is not None:
            return self._db
        if not available():
            return None
        try:
            path = self.path()
            path.parent.mkdir(parents=True, exist_ok=True)
            db = sqlite3.connect(
                path, timeout=5.0, isolation_level=None, check_same_thread=False
            )
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=NORMAL")
            db.execute("PRAGMA busy_timeout=5000")
            db.executescript(_SCHEMA)
            if self._meta(db, "schema") != str(SCHEMA_VERSION) or self._meta(db, "model") != (
                self.model
            ):
                self._wipe(db)
            self._db = db
            return db
        except (sqlite3.Error, OSError):
            return None

    @staticmethod
    def _meta(db: sqlite3.Connection, key: str) -> str:
        row = db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return str(row["value"]) if row else ""

    def _wipe(self, db: sqlite3.Connection) -> None:
        """Drop everything: the schema or the embedding model changed, so every
        stored vector and every FTS row is suspect."""
        for table in ("notes", "note_tags", "chunks", "chunks_fts", "vectors", "links", "meta"):
            with contextlib.suppress(sqlite3.Error):
                db.execute(f"DELETE FROM {table}")
        db.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES ('schema', ?), ('model', ?)",
            (str(SCHEMA_VERSION), self.model),
        )
        self._bump(db)

    @staticmethod
    def _bump(db: sqlite3.Connection) -> None:
        db.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES ('generation', ?)",
            (f"{time.time_ns()}",),
        )

    @_locked
    def close(self) -> None:
        if self._db is not None:
            with contextlib.suppress(sqlite3.Error):
                self._db.close()
            self._db = None

    @_locked
    def drop(self) -> None:
        """Delete the index file (``omind reindex --rebuild``)."""
        self.close()
        for suffix in ("", "-wal", "-shm"):
            with contextlib.suppress(OSError):
                Path(f"{self.path()}{suffix}").unlink()

    # -- ingest -------------------------------------------------------------

    def _note_paths(self) -> Iterator[Path]:
        if not self.omi_dir.is_dir():
            return
        for path in self.omi_dir.glob("*.md"):
            if _is_reserved_note_name(path.name) or path.name.startswith("."):
                continue
            yield path

    @_locked
    def refresh(self, *, vectors: bool = True) -> Refresh | None:
        """Bring the index in line with the notes on disk.

        Only notes whose ``(mtime_ns, size)`` moved *and* whose content hash
        changed are re-read, so the steady-state cost is one ``stat()`` per note.
        ``None`` when the index is unavailable or another process holds the write
        lock — the caller falls back to scanning rather than blocking a query.
        """
        db = self._connect()
        if db is None:
            return None
        started = time.perf_counter()
        try:
            known = {
                str(row["filename"]): (int(row["mtime_ns"]), int(row["size"]), str(row["sha"]))
                for row in db.execute("SELECT filename, mtime_ns, size, sha FROM notes")
            }
            stats = Refresh()
            db.execute("BEGIN IMMEDIATE")
            seen: set[str] = set()
            pending_embed: list[tuple[int, str]] = []
            for path in self._note_paths():
                try:
                    st = path.stat()
                except OSError:
                    continue  # deleted mid-scan
                seen.add(path.name)
                stats.notes += 1
                prior = known.get(path.name)
                if prior is not None and prior[0] == st.st_mtime_ns and prior[1] == st.st_size:
                    continue
                ingested = self._ingest(db, path, prior_sha=prior[2] if prior else "")
                if ingested is None:
                    continue
                stats.reindexed += 1
                pending_embed.extend(ingested)
            for stale in [name for name in known if name not in seen]:
                self._forget(db, stale)
                stats.removed += 1
            if vectors and pending_embed:
                stats.embedded = self._embed_chunks(db, pending_embed)
            self._bump(db)
            db.execute("COMMIT")
            stats.seconds = time.perf_counter() - started
            self._matrix = None
            return stats
        except sqlite3.Error:
            with contextlib.suppress(sqlite3.Error):
                db.execute("ROLLBACK")
            return None

    def _ingest(
        self, db: sqlite3.Connection, path: Path, *, prior_sha: str
    ) -> list[tuple[int, str]] | None:
        """(Re-)index one note. Returns ``(chunk_id, text)`` pairs needing an
        embedding, or ``None`` when the note's bytes are unchanged (a touch)."""
        from omind.store import _read_text, derive_okf_type, parse_note

        try:
            text = _read_text(path)
        except OSError:
            return None
        sha = hashlib.blake2s(text.encode("utf-8", "replace"), digest_size=16).hexdigest()
        st = path.stat()
        if sha == prior_sha:  # mtime moved but content didn't — just re-stamp
            db.execute(
                "UPDATE notes SET mtime_ns = ?, size = ? WHERE filename = ?",
                (st.st_mtime_ns, st.st_size, path.name),
            )
            return None
        fields = parse_note(text)
        row = _NoteRow(
            filename=path.name,
            title=fields.title or path.stem,
            created=fields.created,
            # Derived when undeclared, the same rule render_fields applies, so a
            # graph node built from the index always carries a non-empty type.
            okf_type=fields.okf_type.strip() or derive_okf_type(fields.tags),
            tags=fields.tags,
            disabled=fields.disabled,
        )
        self._forget(db, path.name)
        db.execute(
            "INSERT INTO notes(filename, title, created, okf_type, disabled, mtime_ns, size, sha)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                row.filename,
                row.title,
                row.created,
                row.okf_type,
                int(row.disabled),
                st.st_mtime_ns,
                st.st_size,
                sha,
            ),
        )
        db.executemany(
            "INSERT INTO note_tags(filename, tag) VALUES (?, ?)",
            [(row.filename, tag.lower()) for tag in row.tags],
        )
        db.executemany(
            "INSERT INTO links(src, target, raw) VALUES (?, ?, ?)",
            [(row.filename, raw.lower(), raw) for raw in link_targets(text)],
        )
        tag_text = " ".join(row.tags)
        pending: list[tuple[int, str]] = []
        for chunk in chunk_note(text, path.stem):
            cursor = db.execute(
                "INSERT INTO chunks(filename, heading, ord, start_line, end_line)"
                " VALUES (?, ?, ?, ?, ?)",
                (row.filename, chunk.heading, chunk.ordinal, chunk.start_line, chunk.end_line),
            )
            chunk_id = int(cursor.lastrowid or 0)
            embed_text = "\n".join([row.title, chunk.heading, tag_text, chunk.text]).strip()
            db.execute(
                "INSERT INTO chunks_fts(rowid, title, heading, tags, text, stems)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (
                    chunk_id,
                    row.title,
                    chunk.heading,
                    tag_text,
                    chunk.text,
                    _stems_of(f"{row.title} {chunk.heading} {tag_text} {chunk.text}"),
                ),
            )
            pending.append((chunk_id, embed_text))
        return pending

    def _forget(self, db: sqlite3.Connection, filename: str) -> None:
        rows = db.execute("SELECT id FROM chunks WHERE filename = ?", (filename,))
        ids = [int(r["id"]) for r in rows]
        db.executemany("DELETE FROM chunks_fts WHERE rowid = ?", [(i,) for i in ids])
        db.executemany("DELETE FROM vectors WHERE chunk_id = ?", [(i,) for i in ids])
        db.execute("DELETE FROM chunks WHERE filename = ?", (filename,))
        db.execute("DELETE FROM note_tags WHERE filename = ?", (filename,))
        db.execute("DELETE FROM links WHERE src = ?", (filename,))
        db.execute("DELETE FROM notes WHERE filename = ?", (filename,))

    def _embed_chunks(self, db: sqlite3.Connection, pending: list[tuple[int, str]]) -> int:
        """Embed the given chunks in one batch. 0 when there is no backend."""
        from omind import embed

        if not embed.available():
            return 0
        vecs = embed.encode([text for _id, text in pending])
        if vecs is None:
            return 0
        try:
            db.executemany(
                "INSERT OR REPLACE INTO vectors(chunk_id, vec) VALUES (?, ?)",
                [(cid, _pack(row)) for (cid, _text), row in zip(pending, vecs, strict=True)],
            )
        except (sqlite3.Error, ValueError, ImportError):
            return 0
        return len(pending)

    # -- query --------------------------------------------------------------

    @_locked
    def search(
        self,
        query: str,
        *,
        tag: str | None = None,
        limit: int = 25,
        include_disabled: bool = False,
    ) -> list[Hit] | None:
        """Ranked chunks for ``query``, best first, or ``None`` (caller falls back).

        An empty ``query`` is a *listing* (optionally tag-filtered) in date order,
        which is what the tag-only search surface has always meant.
        """
        db = self._connect()
        if db is None:
            return None
        if self.refresh() is None and not self._has_rows(db):
            return None  # nothing indexed and we couldn't build it
        try:
            allowed = self._candidates(db, tag=tag, include_disabled=include_disabled)
            if allowed is not None and not allowed:
                return []
            if not query.strip():
                return self._listing(db, allowed, limit)
            expr = _fts_query(query)  # the OR form; also snips the excerpts
            keyword = self._keyword_leg(db, query, allowed)
            vector = self._vector_leg(db, query, allowed)
            matched = set(keyword) | set(vector)
            if not matched:
                return []
            # Recency re-ranks what the content legs matched; it never *adds* a
            # note. Left unrestricted it made every query return the whole vault
            # newest-first — the exact failure this index exists to end.
            recency = [cid for cid in self._recency_leg(db, allowed) if cid in matched]
            fused = _fuse(
                [(_W_KEYWORD, keyword), (_W_VECTOR, vector), (_W_RECENCY, recency)]
            )
            if not fused:
                return []
            ranks = {
                "keyword": {cid: i + 1 for i, cid in enumerate(keyword)},
                "vector": {cid: i + 1 for i, cid in enumerate(vector)},
            }
            return self._materialize(db, query, expr, fused, ranks, limit)
        except (sqlite3.Error, ValueError):
            return None

    @staticmethod
    def _has_rows(db: sqlite3.Connection) -> bool:
        row = db.execute("SELECT 1 FROM chunks LIMIT 1").fetchone()
        return row is not None

    def _candidates(
        self, db: sqlite3.Connection, *, tag: str | None, include_disabled: bool
    ) -> set[str] | None:
        """Filenames passing the tag/archived filters, or ``None`` for "no filter"."""
        clean = (tag or "").lstrip("#").strip().lower()
        if not clean and include_disabled:
            return None
        sql = "SELECT filename FROM notes WHERE 1=1"
        args: list[object] = []
        if not include_disabled:
            sql += " AND disabled = 0"
        if clean:
            sql += " AND filename IN (SELECT filename FROM note_tags WHERE tag = ?)"
            args.append(clean)
        return {str(r["filename"]) for r in db.execute(sql, args)}

    def _keyword_leg(
        self, db: sqlite3.Connection, query: str, allowed: set[str] | None
    ) -> list[int]:
        """BM25 over the FTS index, graded: chunks matching every query word
        first, then chunks matching any of them."""
        ranked: list[int] = []
        seen: set[int] = set()
        for require_all in (True, False):
            expr = _fts_query(query, require_all=require_all)
            if not expr:
                break
            for chunk_id in self._bm25(db, expr, allowed):
                if chunk_id not in seen:
                    seen.add(chunk_id)
                    ranked.append(chunk_id)
            if len(ranked) >= _LEG_DEPTH:
                break
        return ranked[:_LEG_DEPTH]

    @staticmethod
    def _bm25(db: sqlite3.Connection, expr: str, allowed: set[str] | None) -> list[int]:
        """Chunk ids for one MATCH expression, best BM25 first. Column weights put
        a title/tag match well above a body mention of the same word."""
        try:
            rows = db.execute(
                "SELECT f.rowid AS id, c.filename AS filename FROM chunks_fts f"
                " JOIN chunks c ON c.id = f.rowid"
                " WHERE chunks_fts MATCH ?"
                " ORDER BY bm25(chunks_fts, 5.0, 2.0, 4.0, 1.0, 1.0)"
                " LIMIT ?",
                (expr, _LEG_DEPTH * 3),
            )
        except sqlite3.Error:
            return []  # an unparseable expression is no matches, never a crash
        return [
            int(r["id"]) for r in rows if allowed is None or str(r["filename"]) in allowed
        ]

    def _vector_leg(
        self, db: sqlite3.Connection, query: str, allowed: set[str] | None
    ) -> list[int]:
        from omind import embed

        if not embed.available():
            return []
        try:
            import numpy as np

            packed = self._vector_matrix(db)
            if packed is None:
                return []
            ids, matrix = packed
            qv = _query_vector(query)
            if qv is None or int(qv.shape[0]) != int(matrix.shape[1]):
                return []
            scores = matrix @ qv
            order = np.argsort(-scores)[: _LEG_DEPTH * 3]
            owner = self._owners(db)
            ranked = [ids[int(i)] for i in order]
            return [
                cid
                for cid in ranked
                if allowed is None or owner.get(cid, "") in allowed
            ][:_LEG_DEPTH]
        except (ImportError, ValueError, sqlite3.Error):
            return []

    def _vector_matrix(self, db: sqlite3.Connection) -> tuple[list[int], Any] | None:
        """``(chunk_ids, (n, dim) float32 matrix)`` for every stored vector.

        One matmul beats the per-entry Python dot product the old vector index
        did; the matrix is cached per process and invalidated by the generation.
        """
        import numpy as np

        generation = self._meta(db, "generation")
        if self._matrix is not None and self._matrix[0] == generation:
            return self._matrix[1], self._matrix[2]
        ids: list[int] = []
        rows: list[Any] = []
        width = 0
        for row in db.execute("SELECT chunk_id, vec FROM vectors ORDER BY chunk_id"):
            vec = np.frombuffer(row["vec"], dtype="float32")
            if width and vec.size != width:
                continue  # a stale/corrupt row rather than a wrong score
            width = width or int(vec.size)
            ids.append(int(row["chunk_id"]))
            rows.append(vec)
        if not rows:
            return None
        matrix = np.vstack(rows)
        self._matrix = (generation, ids, matrix)
        return ids, matrix

    def _owners(self, db: sqlite3.Connection) -> dict[int, str]:
        rows = db.execute("SELECT id, filename FROM chunks")
        return {int(r["id"]): str(r["filename"]) for r in rows}

    def _recency_leg(self, db: sqlite3.Connection, allowed: set[str] | None) -> list[int]:
        rows = db.execute(
            "SELECT c.id AS id, c.filename AS filename FROM chunks c"
            " JOIN notes n ON n.filename = c.filename"
            " WHERE c.ord = 0 ORDER BY n.created DESC, n.filename LIMIT ?",
            (_LEG_DEPTH * 3,),
        )
        return [
            int(r["id"]) for r in rows if allowed is None or str(r["filename"]) in allowed
        ][:_LEG_DEPTH]

    def _listing(
        self, db: sqlite3.Connection, allowed: set[str] | None, limit: int
    ) -> list[Hit]:
        rows = db.execute(
            "SELECT filename, title, created FROM notes"
            " ORDER BY created DESC, lower(title) DESC"
        )
        hits: list[Hit] = []
        for row in rows:
            name = str(row["filename"])
            if allowed is not None and name not in allowed:
                continue
            hits.append(Hit(filename=name, heading="", excerpt="", score=0.0))
            if len(hits) >= limit:
                break
        return hits

    def _materialize(
        self,
        db: sqlite3.Connection,
        query: str,
        expr: str,
        fused: list[tuple[int, float]],
        ranks: dict[str, dict[int, int]],
        limit: int,
    ) -> list[Hit]:
        """Best chunk per note, credential-penalised, with a bounded excerpt."""
        task_is_cred = bool(retrieve._tokens(query) & retrieve._CREDENTIAL_STEMS)
        hits: list[Hit] = []
        seen: set[str] = set()
        for chunk_id, score in fused:
            row = db.execute(
                "SELECT c.filename AS filename, c.heading AS heading, n.title AS title,"
                " (SELECT group_concat(tag, ' ') FROM note_tags t WHERE t.filename = c.filename)"
                " AS tags FROM chunks c JOIN notes n ON n.filename = c.filename WHERE c.id = ?",
                (chunk_id,),
            ).fetchone()
            if row is None:
                continue
            name = str(row["filename"])
            if name in seen:
                continue  # one hit per note: the agent recalls notes, not chunks
            seen.add(name)
            if not task_is_cred and retrieve._looks_credential(
                str(row["title"]), str(row["tags"] or "")
            ):
                score *= retrieve._CREDENTIAL_PENALTY
            hits.append(
                Hit(
                    filename=name,
                    heading=str(row["heading"]),
                    excerpt=self._excerpt(db, chunk_id, expr, filename=name),
                    score=score,
                    keyword_rank=ranks["keyword"].get(chunk_id, 0),
                    vector_rank=ranks["vector"].get(chunk_id, 0),
                )
            )
        # The credential penalty is applied after fusion, so re-sort before cutting.
        hits.sort(key=lambda h: (-h.score, h.filename))
        return hits[:limit]

    def _excerpt(self, db: sqlite3.Connection, chunk_id: int, expr: str, *, filename: str) -> str:
        """FTS5's own snippet around the match, or the chunk's head for a
        vector-only hit (which by definition shares no literal term).

        A hit on the identity chunk (heading ``""``) means the *name* matched, and
        echoing the name back teaches the agent nothing — so that case shows the
        note's Summary instead, which is what it wants to know.
        """
        row = db.execute(
            "SELECT f.text AS text, c.heading AS heading FROM chunks_fts f"
            " JOIN chunks c ON c.id = f.rowid WHERE f.rowid = ?",
            (chunk_id,),
        ).fetchone()
        if row is None:
            return ""
        if not str(row["heading"]):
            summary = db.execute(
                "SELECT f.text AS text FROM chunks c JOIN chunks_fts f ON f.rowid = c.id"
                " WHERE c.filename = ? AND c.heading = 'Summary' ORDER BY c.ord LIMIT 1",
                (filename,),
            ).fetchone()
            if summary is not None and str(summary["text"]).strip():
                return _collapse(str(summary["text"]))
            return _collapse(str(row["text"]))
        if expr:
            snippet = db.execute(
                "SELECT snippet(chunks_fts, 3, '', '', '…', 24) AS s FROM chunks_fts"
                " WHERE rowid = ? AND chunks_fts MATCH ?",
                (chunk_id, expr),
            ).fetchone()
            if snippet is not None and str(snippet["s"] or "").strip():
                return _collapse(str(snippet["s"]))
        return _collapse(str(row["text"]))

    # -- link graph ---------------------------------------------------------

    @_locked
    def backlinks(self, filename: str, *, title: str = "") -> list[str] | None:
        """Filenames whose ``[[wikilinks]]`` resolve to this note, or ``None``."""
        db = self._connect()
        if db is None or self.refresh() is None:
            return None
        try:
            stem = filename[:-3] if filename.endswith(".md") else filename
            wanted = {stem.strip().lower()}
            if title:
                wanted.add(title.strip().lower())
            placeholders = ", ".join("?" * len(wanted))
            rows = db.execute(
                f"SELECT DISTINCT src FROM links WHERE target IN ({placeholders}) ORDER BY src",
                sorted(wanted),
            )
            return [str(r["src"]) for r in rows if str(r["src"]) != filename]
        except sqlite3.Error:
            return None

    @_locked
    def notes(self) -> list[_NoteRow] | None:
        """Every indexed note's identity row (filename, title, created, tags)."""
        db = self._connect()
        if db is None or self.refresh() is None:
            return None
        return self._notes(db)

    @staticmethod
    def _notes(db: sqlite3.Connection) -> list[_NoteRow]:
        tags: dict[str, list[str]] = {}
        for row in db.execute("SELECT filename, tag FROM note_tags"):
            tags.setdefault(str(row["filename"]), []).append(str(row["tag"]))
        return [
            _NoteRow(
                filename=str(r["filename"]),
                title=str(r["title"]),
                created=str(r["created"]),
                okf_type=str(r["okf_type"]),
                tags=tags.get(str(r["filename"]), []),
                disabled=bool(r["disabled"]),
            )
            for r in db.execute(
                "SELECT filename, title, created, okf_type, disabled FROM notes"
            )
        ]

    @_locked
    def link_rows(self) -> tuple[list[_NoteRow], list[tuple[str, str]]] | None:
        """``(notes, (src, raw_target))`` for the whole vault — the single scan
        :mod:`omind.graph` and :mod:`omind.lint` share instead of doing their own.

        Targets come back **as written** so a dangling-link report names the link
        the way the author typed it; resolution lowercases at comparison time.
        """
        db = self._connect()
        if db is None or self.refresh() is None:
            return None
        try:
            notes = self._notes(db)
            links = [
                (str(r["src"]), str(r["raw"]))
                for r in db.execute("SELECT src, raw FROM links")
            ]
            return notes, links
        except sqlite3.Error:
            return None

    # -- dedup --------------------------------------------------------------

    @_locked
    def nearest(
        self, text: str, *, exclude: str | None = None, limit: int = 3
    ) -> list[tuple[str, float]] | None:
        """Existing notes most similar to ``text`` (for dedup), or ``None`` with
        no embedding backend — the API :mod:`omind.vectorindex` used to provide."""
        from omind import embed

        if not embed.available():
            return None
        db = self._connect()
        if db is None or self.refresh() is None:
            return None
        try:
            import numpy as np

            packed = self._vector_matrix(db)
            qv = _query_vector(text)
            if packed is None or qv is None:
                return None
            ids, matrix = packed
            if int(qv.shape[0]) != int(matrix.shape[1]):
                return None
            scores = matrix @ qv
            owner = self._owners(db)
            best: dict[str, float] = {}
            for idx in np.argsort(-scores):
                name = owner.get(ids[int(idx)], "")
                if not name or name == exclude:
                    continue
                best.setdefault(name, float(scores[int(idx)]))
                if len(best) >= limit:
                    break
            return sorted(best.items(), key=lambda kv: -kv[1])[:limit]
        except (ImportError, ValueError, sqlite3.Error):
            return None

    # -- diagnostics --------------------------------------------------------

    @_locked
    def stats(self) -> dict[str, object] | None:
        db = self._connect()
        if db is None:
            return None
        try:

            def count(table: str) -> int:
                row = db.execute(f"SELECT count(*) AS n FROM {table}").fetchone()
                return int(row["n"]) if row else 0

            size = 0
            with contextlib.suppress(OSError):
                size = self.path().stat().st_size
            return {
                "path": str(self.path()),
                "notes": count("notes"),
                "chunks": count("chunks"),
                "vectors": count("vectors"),
                "links": count("links"),
                "bytes": size,
                "model": self.model,
            }
        except sqlite3.Error:
            return None


_shared: dict[str, SearchIndex] = {}
_shared_lock = threading.Lock()


def shared(omi_dir: Path | str) -> SearchIndex | None:
    """The process-wide index for a vault, or ``None`` where unavailable.

    The per-turn guard, the hooks and the CLI each reach for the index from a
    different call site; without sharing they would each open their own sqlite
    connection and re-pack the vector matrix on every user prompt.
    """
    if not available():
        return None
    key = str(index_path(omi_dir))
    with _shared_lock:
        found = _shared.get(key)
        if found is None:
            if len(_shared) > 8:  # a long-lived process only ever has a few vaults
                for stale in list(_shared.values()):
                    stale.close()
                _shared.clear()
            found = _shared[key] = SearchIndex(omi_dir)
        return found


def chunk_note(md: str, stem: str) -> list[_Chunk]:
    """Split a note into indexable chunks at its ``## headings``.

    Chunk 0 is always the note's identity (stem + title + lead), so a note with
    an empty body is still findable by name; each ``## section`` follows, split
    again at :data:`_MAX_CHUNK_CHARS` on paragraph boundaries so one long
    ``## Details`` neither swamps BM25's length normalisation nor produces an
    excerpt nobody can read.
    """
    from omind.store import _scan_note

    _frontmatter, title, lead, sections = _scan_note(md)
    parts = (stem, title, lead)
    identity = "\n".join(dict.fromkeys(part for part in parts if part.strip()))
    chunks: list[_Chunk] = [
        _Chunk(heading="", ordinal=0, text=identity.strip(), start_line=1, end_line=1)
    ]
    lines = md.splitlines()
    ordinal = 1
    for heading, body in sections.items():
        text = "\n".join(body).strip()
        if not text:
            continue
        start = _first_line_of(lines, heading)
        for piece in _split_long(text):
            chunks.append(
                _Chunk(
                    heading=heading,
                    ordinal=ordinal,
                    text=piece,
                    start_line=start,
                    end_line=start + piece.count("\n"),
                )
            )
            ordinal += 1
    return chunks


def _first_line_of(lines: list[str], heading: str) -> int:
    wanted = f"## {heading}"
    for number, line in enumerate(lines, start=1):
        if line.strip() == wanted:
            return number
    return 1


def _split_long(text: str) -> list[str]:
    """Break text over the chunk cap at blank lines, never mid-paragraph."""
    if len(text) <= _MAX_CHUNK_CHARS:
        return [text]
    pieces: list[str] = []
    current = ""
    for para in text.split("\n\n"):
        if current and len(current) + len(para) + 2 > _MAX_CHUNK_CHARS:
            pieces.append(current)
            current = para
        else:
            current = f"{current}\n\n{para}" if current else para
        while len(current) > _MAX_CHUNK_CHARS:  # a single giant paragraph
            pieces.append(current[:_MAX_CHUNK_CHARS])
            current = current[_MAX_CHUNK_CHARS:]
    if current:
        pieces.append(current)
    return pieces


def link_targets(md: str) -> list[str]:
    """The ``[[wikilink]]`` targets in a note, alias/heading stripped, **as written**.

    Case is preserved so a dangling-link report can quote the link the author
    typed; de-duplication and resolution are case-insensitive. Resolution against
    real filenames happens at query time, so a link written before its target
    exists starts working the moment the target is created.
    """
    from omind.store import _WIKILINK_RE

    targets: dict[str, str] = {}
    for raw in _WIKILINK_RE.findall(md):
        target = raw.split("|", 1)[0].split("#", 1)[0].strip()
        if target:
            targets.setdefault(target.lower(), target)
    return [targets[key] for key in sorted(targets)]


def _fuse(legs: list[tuple[float, list[int]]]) -> list[tuple[int, float]]:
    """Reciprocal Rank Fusion over weighted ranked lists, best first."""
    scores: dict[int, float] = {}
    for weight, ranked in legs:
        for position, chunk_id in enumerate(ranked, start=1):
            scores[chunk_id] = scores.get(chunk_id, 0.0) + weight / (RRF_K + position)
    return sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))


def _collapse(text: str) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= EXCERPT_CHARS else flat[: EXCERPT_CHARS - 1].rstrip() + "…"
