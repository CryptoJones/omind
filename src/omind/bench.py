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
* **tokens** — the size of what a session actually pays for: the MCP tool schema,
  SessionStart capsule, one bounded recall, and the paged-versus-unpaged listing
  payload that used to be ~87k tokens in a single MCP tool result.

Read-only: it never writes a note. It does build/refresh the derived index, which
is disposable by design.
"""

from __future__ import annotations

import asyncio
import json
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

#: Labelled query→note pairs scored by ``--quality``, drawn from the durable
#: (non-generated) notes in CryptoJones's reference vault. Version-controlled
#: and human-readable on purpose: ranking changes should move these numbers,
#: not merely "look better."
#:
#: **Thirty cases, not five.** The original set was five, which sounds fine
#: until you notice each case is worth 20 percentage points — a change that
#: genuinely improved recall by 10% could not register at all, and did not:
#: the contextual-prefix experiment (#193) scored *identically* on five cases
#: while two of them moved several ranks underneath. Five cases cannot tell
#: "no effect" from "an effect this instrument cannot see."
#:
#: Queries are written the way an agent actually asks — "may I stop when I
#: reach a natural stopping point" — and deliberately avoid echoing their
#: target's title words, so a case cannot be won by literal title matching
#: alone. They were authored from note *contents* before anything was measured,
#: so the set is not tuned to what already ranks well.
#:
#: Two cases are known misses and are kept on purpose:
#:   * "sign in to Claude Code with a Max subscription" — a near-duplicate note
#:     outranks the labelled target. That is a *vault* problem (one fact in two
#:     notes), which is what ``consolidate`` and ``graph frontier`` are for.
#:   * "what quality bar must the code I write meet" — the note says
#:     "production-grade by default, hardened, fault-tolerant" and the query
#:     says "quality bar". No lexical overlap, and the semantic leg does not
#:     bridge it. A real retrieval gap, left visible rather than relabelled.
QUALITY_CASES = (
    (
        'where does long-term assistant memory live',
        'Omi Is The Memory.md',
    ),
    (
        'how does CryptoJones want the assistant to work',
        'Working Preferences - How CryptoJones Wants Me to Operate.md',
    ),
    (
        'what voice and persona should Dix use',
        'Voice and Persona - Dix and Shelly.md',
    ),
    (
        'rules for working in git repos and handling secrets',
        'Operational Rules - Git Repos and Secrets.md',
    ),
    (
        'how should durable memory notes be created',
        'Memory Workflow.md',
    ),
    (
        'how do I sign in to Claude Code with a Max subscription',
        'Claude Code auth Claude Max Pro subscriptions require OAuth.md',
    ),
    (
        'what colour theme did we build for the terminal CLI',
        'Cyberdeck — Claude Code custom theme (~ .claude themes cyberdeck.json).md',
    ),
    (
        'which repo do I clone to start a new MCP server',
        'mcp-server-baseline — private consulting repo for building MCP servers 2026-06-14.md',
    ),
    (
        'how many pull requests before Codeberg throttles me',
        'project-codeberg-rate-limit.md',
    ),
    (
        'what is GayHydra',
        'project-gayhydra.md',
    ),
    (
        'should I hand over commands or run them myself',
        "CJ preference — run commands yourself, don't hand them off 2026-06-13.md",
    ),
    (
        'who starts the Windows test virtual machine',
        'Win11 QEMU VM on pluto — Claude launches it (standing procedure).md',
    ),
    (
        'what quality bar must the code I write meet',
        'Engineering Standards - Scripts and Code.md',
    ),
    (
        'may I stop when I reach a natural stopping point',
        "CryptoJones operating mode — never stop at 'natural stopping points'; standing full "
        'pre-authorization to act.md',
    ),
    (
        'what should I do immediately after bouncing audio',
        'CJ music production ALWAYS auto-open renders in VLC + DRIVE the work (proactive, '
        "don't offload). 2026-07-04 (dix).md",
    ),
    (
        'is adding tests unprompted considered scope creep',
        'feedback-proactive-ci-testing.md',
    ),
    (
        'what art direction was locked for the sequel',
        'Flatline Sessions sequel art style LOCKED — heavy rotoscope plus retro 35mm sci-fi '
        'grammar (2026-07-02).md',
    ),
    (
        'how do I derive scales by dividing the octave equally',
        'Slonimsky — Thesaurus of Scales and Melodic Patterns (systematic-composition '
        'distillation).md',
    ),
    (
        'how does human memory map onto musical time scales',
        'Bob Snyder — Music and Memory An Introduction (psychoacoustics distillation).md',
    ),
    (
        'what decides whether a downbeat feels early or late',
        'Christopher Hasty — Meter as Rhythm (distillation).md',
    ),
    (
        'where is the public website hosted',
        'Web presence — www.cryptojones.dev cryptojones.dev.md',
    ),
    (
        'which machine runs the dedicated game server',
        'XSpaceWar-AI dedicated server — running it on makemake.md',
    ),
    (
        'do the course pipelines share one virtualenv',
        "Course render pipelines SHARE one venv assets models via symlinks — don't delete the "
        'anchor (2026-06-19).md',
    ),
    (
        'which local coding model won the benchmark',
        'MacminiM2Pro_ModelShowdown — benchmark matrix run + dedicated-machine protocol '
        '(2026-07-01).md',
    ),
    (
        'are there better uncensored models for the V620 yet',
        'Model re-sweep 2026-06-27 — late-June uncensored ≤32GB V620 candidates; incumbents '
        'still hold.md',
    ),
    (
        'which old game are we remaking in Godot',
        'Neuromancer Godot remake — contributors to credit + project basics.md',
    ),
    (
        'is the lora explainer translated into other languages',
        'lora-for-hackers-scope.md',
    ),
    (
        'when did the consult gate become graduated',
        'omind 2.45.0 — graduated consult-gate (warn-then-enforce, #98); fleet converged '
        '(2026-06-22).md',
    ),
    (
        'what visual style did UNSDF move to',
        'UNSDF — modern 2D JRPG art overhaul shipped to main (2026-06-30).md',
    ),
    (
        'how is rhythm modelled as interference of periodicities',
        'Schillinger — The Schillinger System of Musical Composition (systematic-composition '
        'distillation).md',
    ),
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
    from omind.server import build_server
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
    tools = asyncio.run(build_server(omi).list_tools())
    tool_schemas = json.dumps(
        [tool.model_dump(mode="json", exclude_none=True) for tool in tools],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    report.add("MCP tools exposed", len(tools), "count")
    report.add("MCP tool schemas", ai_usage.estimate_tokens(tool_schemas), "tokens")

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


def run_quality(
    omi_dir: Path | str,
    *,
    cases: tuple[tuple[str, str], ...] = QUALITY_CASES,
) -> Report:
    """Evaluate labelled query→expected-note pairs with recall@k and MRR."""
    from omind import searchindex

    omi = Path(omi_dir).expanduser()
    report = Report(vault=str(omi))
    index = searchindex.SearchIndex(omi)
    available = {path.name for path in omi.glob("*.md")}
    evaluable = [(query, expected) for query, expected in cases if expected in available]
    report.add("quality cases", len(evaluable), "count", f"{len(cases) - len(evaluable)} skipped")
    if not evaluable:
        report.add("recall@1", 0.0, "%", "no labelled target notes found")
        report.add("recall@5", 0.0, "%", "no labelled target notes found")
        report.add("MRR", 0.0, "score", "no labelled target notes found")
        return report

    at_one = 0
    at_five = 0
    reciprocal = 0.0
    misses: list[str] = []
    for query, expected in evaluable:
        hits = index.search(query, limit=50) or []
        ranked = [hit.filename for hit in hits]
        try:
            rank = ranked.index(expected) + 1
        except ValueError:
            rank = 0
        at_one += rank == 1
        at_five += 0 < rank <= 5
        reciprocal += 1.0 / rank if rank else 0.0
        if not rank or rank > 5:
            misses.append(f"{query!r}→{rank or 'miss'}")

    total = len(evaluable)
    detail = "; ".join(misses[:3])
    report.add("recall@1", at_one * 100.0 / total, "%")
    report.add("recall@5", at_five * 100.0 / total, "%", detail)
    report.add("MRR", reciprocal / total, "score")
    return report


def _listing_tokens(notes: list[Any]) -> tuple[int, int]:
    """``(paged, unpaged)`` token estimate for the listing payload — the tool
    result that was ~87k tokens on a 744-note vault before it was paged."""
    from omind import ai_usage
    from omind.server import DEFAULT_PAGE

    rows = [n.__dict__ for n in notes]
    paged = ai_usage.estimate_tokens(json.dumps(rows[:DEFAULT_PAGE], ensure_ascii=False))
    unpaged = ai_usage.estimate_tokens(json.dumps(rows, ensure_ascii=False))
    return paged, unpaged
