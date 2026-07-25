# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Aaron K. Clark
"""``omind bench`` — measure this vault's retrieval cost, in time and in tokens.

Every performance fix in omind's history so far was reactive: someone noticed a
slow path, it got a cache, the CHANGELOG recorded it, and nothing measured it
again. This module makes the two numbers that actually matter observable on a
real vault:

* **latency** — build the index, refresh it incrementally, and run queries both
  through the index and through the pre-index full-vault scan, so the speedup is
  a measurement rather than a claim.
* **tokens** — the size of what a session actually pays for: the SessionStart
  capsule, one bounded recall, and the paged-versus-unpaged listing payload that
  used to be ~87k tokens in a single MCP tool result.

Read-only: it never writes a note. It does build/refresh the derived index, which
is disposable by design.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: Queries used when the caller names none. Deliberately mixed: an exact term, a
#: natural-language question (the case that returned [] before the index), and a
#: multi-word phrase.
SAMPLE_QUERIES = (
    "nebraska",
    "how should I handle a release that fails signing",
    "mesh sync conflict",
)


@dataclass
class Measurement:
    """One measured number, with the unit it is measured in."""

    name: str
    value: float
    unit: str
    detail: str = ""

    def format(self) -> str:
        shown = f"{self.value:,.2f}" if self.unit != "count" else f"{int(self.value):,}"
        tail = f"  ({self.detail})" if self.detail else ""
        return f"  {self.name:<34} {shown:>12} {self.unit}{tail}"


@dataclass
class Report:
    vault: str
    measurements: list[Measurement] = field(default_factory=list)

    def add(self, name: str, value: float, unit: str, detail: str = "") -> None:
        self.measurements.append(Measurement(name, value, unit, detail))

    def to_dict(self) -> dict[str, Any]:
        return {
            "vault": self.vault,
            "measurements": [
                {"name": m.name, "value": m.value, "unit": m.unit, "detail": m.detail}
                for m in self.measurements
            ],
        }

    def format(self) -> str:
        lines = [f"omind bench: {self.vault}", ""]
        lines.extend(m.format() for m in self.measurements)
        return "\n".join(lines)


def _number(value: object) -> int:
    """An int from a loosely-typed stats value (0 for anything unusable)."""
    return value if isinstance(value, int) else 0


def _timed(call: Any) -> tuple[float, Any]:
    """``(milliseconds, result)`` for one call."""
    started = time.perf_counter()
    result = call()
    return (time.perf_counter() - started) * 1000.0, result


def run(omi_dir: Path | str, *, queries: tuple[str, ...] = SAMPLE_QUERIES) -> Report:
    """Measure retrieval on ``omi_dir`` and return the report."""
    from omind import ai_usage, embed, hooks, recall, searchindex
    from omind.store import OmiStore

    omi = Path(omi_dir).expanduser()
    report = Report(vault=str(omi))
    store = OmiStore(omi)

    notes = store.list_notes()
    report.add("notes in vault", len(notes), "count")
    report.add(
        "semantic leg",
        1 if embed.available() else 0,
        "count",
        str(embed.status()["reason"] or "on"),
    )

    if not searchindex.available():
        report.add("search index", 0, "count", "unavailable — search scans the vault")
    else:
        index = searchindex.SearchIndex(omi)
        index.drop()
        cold_ms, built = _timed(index.refresh)
        report.add(
            "index build (from scratch)",
            cold_ms,
            "ms",
            f"{getattr(built, 'reindexed', 0)} notes, {getattr(built, 'embedded', 0)} embedded",
        )
        warm_ms, _ = _timed(index.refresh)
        report.add("index refresh (no changes)", warm_ms, "ms")
        stats = index.stats() or {}
        report.add(
            "index size", _number(stats.get("bytes")) / 1024.0, "KiB",
            f"{_number(stats.get('chunks'))} chunks",
        )

        for query in queries:
            # Bound each lambda to this iteration's values (ruff B023).
            fresh = searchindex.SearchIndex(omi)  # cold: no packed matrix cached
            cold, hits = _timed(lambda q=query, ix=fresh: ix.search(q, limit=10))
            warm, _ = _timed(lambda q=query: index.search(q, limit=10))
            report.add(
                f"search {query[:22]!r}",
                warm,
                "ms",
                f"cold {cold:.1f} ms, {len(hits or [])} hit(s)",
            )
            scan, scanned = _timed(lambda q=query: store._scan_search(q))
            report.add("  same query, pre-index scan", scan, "ms", f"{len(scanned)} hit(s)")

    # Token cost of what a session actually pays for.
    capsule_ms, capsule = _timed(lambda: hooks.build_session_start_context(omi))
    report.add(
        "SessionStart capsule", ai_usage.estimate_tokens(capsule or ""), "tokens",
        f"{len(capsule or '')} chars in {capsule_ms:.0f} ms",
    )
    if notes:
        payload = recall.compact_recall(omi, notes[0].filename)
        text = str(payload.get("content", "")) if isinstance(payload, dict) else ""
        report.add("one bounded recall", ai_usage.estimate_tokens(text), "tokens")
        listing = _listing_tokens(notes)
        report.add("list-notes, one page", listing[0], "tokens", "limit 25")
        report.add("list-notes, unpaged (was)", listing[1], "tokens", f"all {len(notes)} notes")
    return report


def _listing_tokens(notes: list[Any]) -> tuple[int, int]:
    """``(paged, unpaged)`` token estimate for the listing payload — the tool
    result that was ~87k tokens on a 744-note vault before it was paged."""
    import json

    from omind import ai_usage
    from omind.server import DEFAULT_PAGE

    rows = [n.__dict__ for n in notes]
    paged = ai_usage.estimate_tokens(json.dumps(rows[:DEFAULT_PAGE], ensure_ascii=False))
    unpaged = ai_usage.estimate_tokens(json.dumps(rows, ensure_ascii=False))
    return paged, unpaged
