# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Aaron K. Clark
"""``omind audit`` — a self-assessment that can indict omind (#326).

Every omind surface that spends the agent's context or attention gets one or
more rows: a measured number, a DECLARED threshold, and a verdict. The design
constraint is the whole point — a dashboard that can only look green is
decoration, so this one exits non-zero and names the remedy, up to and
including "turn this off".

Why it exists: #321 (~3.4M tokens of unrequested recall at ~25% precision) sat
invisible for months inside a usage ledger that had been written faithfully
since 4.0.0 and never read. Nothing here collects new telemetry; it reads what
``ai_usage`` and ``compliance`` already record.

Rules this module holds itself to:

* **Thresholds are declared, not inferred.** :data:`THRESHOLDS` is the one
  version-controlled place they live (documented in ``docs/audit.md``). Deriving
  them from current numbers would grade on a curve: yesterday's regression
  becomes today's baseline.
* **Three verdicts, not two.** ``ok`` / ``fail`` / ``unmeasured``. A surface the
  recorded telemetry cannot judge says so, with what is missing — it is never
  rounded up to ``ok``. Too few samples is ``unmeasured`` too.
* **Read-only.** No access stats, consults, compliance events or usage rows are
  written; precision is measured through ``bench.run_precision``, which reads
  notes through ``OmiStore`` for exactly that reason.
* **Fails open.** A surface whose measurement raises is reported ``unmeasured``
  with the error; one broken instrument never hides the others.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

OK = "ok"
FAIL = "fail"
UNMEASURED = "unmeasured"

#: Default look-back. Long enough to have samples, short enough that a fixed
#: regression stops failing the audit within a month.
DEFAULT_DAYS = 30


@dataclass(frozen=True)
class Threshold:
    """One declared limit. ``direction`` is ``"max"`` (value must be <= limit)
    or ``"min"`` (value must be >= limit)."""

    key: str
    surface: str
    label: str
    limit: float
    direction: str
    unit: str
    #: Fewer samples than this and the row is ``unmeasured``, not judged.
    min_samples: int
    #: What to do about a failure — concrete, and allowed to say "turn it off".
    remedy: str
    #: Where the limit comes from, so changing it is a reviewed decision.
    basis: str


#: THE thresholds. Change one only in a commit that says why (docs/audit.md
#: mirrors this table). They are anchored to omind's own declared budgets and
#: to targets set in the issues that found each problem — never to whatever the
#: numbers happened to be on the day this was written.
THRESHOLDS: tuple[Threshold, ...] = (
    Threshold(
        "preflight.median_chars", "preflight recall", "per-turn push, median",
        1_200, "max", "chars", 20,
        "set OMIND_PREFLIGHT=hint (the default since 9.2.0); if it already is, "
        "the hint is carrying excerpts it should not",
        "hint mode is capped at guard.PREFLIGHT_HINT_CHARS (500); 1,200 leaves "
        "room for hard-rule notes, which keep their full excerpt",
    ),
    Threshold(
        "preflight.p99_chars", "preflight recall", "per-turn push, p99",
        6_000, "max", "chars", 20,
        "a fat tail means whole notes are being pushed unasked — find them with "
        "`omind ai usage`, and set OMIND_PREFLIGHT=off if it persists",
        "#321 was a tail problem (p99 6,809 / max 16,406 chars), invisible in totals",
    ),
    Threshold(
        "preflight.precision_pct", "preflight recall", "injection precision",
        70.0, "min", "%", 5,
        "below target the push costs more than it returns: set "
        "OMIND_PREFLIGHT=off and let the agent pull with search-vault",
        "the 70% target #321 set; measured by `omind bench --precision`",
    ),
    Threshold(
        "session.p99_tokens", "preflight recall", "omind context per session, p99",
        15_000, "max", "tokens", 10,
        "long sessions are accumulating omind context; lower the session budget "
        "or set OMIND_PREFLIGHT=off for long-running agents",
        "guard.SESSION_INJECTION_BUDGET_CHARS is 60,000 chars = 15,000 tokens",
    ),
    Threshold(
        "priming.median_tokens", "SessionStart priming", "capsule size, median",
        2_000, "max", "tokens", 10,
        "`omind ai profile economy`, or trim the priming notes (Playbook, Rules)",
        "the default `balanced` capsule is budgeted at 8,000 chars = 2,000 tokens",
    ),
    Threshold(
        "priming.p99_tokens", "SessionStart priming", "capsule size, p99",
        4_000, "max", "tokens", 10,
        "some sessions start on a capsule twice the budget — check which "
        "profile those hosts run (`omind ai profile`)",
        "twice the `balanced` budget; the `full` profile is an explicit opt-in",
    ),
    Threshold(
        "mcp.median_chars", "MCP tool responses", "response size, median",
        6_000, "max", "chars", 20,
        "prefer recall-note over read-note, and smaller `limit`s on list tools",
        "recall-note defaults to 4,000 chars; a median above 6,000 means the "
        "expensive tools are the habitual ones",
    ),
    Threshold(
        "mcp.p99_chars", "MCP tool responses", "response size, p99",
        20_000, "max", "chars", 20,
        "a tool is returning near-unbounded payloads — see AGENTS.md invariant 8",
        "read-note's default body cap is 20,000 chars (invariant 8)",
    ),
    Threshold(
        "gate.offtopic_per_100", "consult gate", "off-topic verdicts per 100 OMI reads",
        25.0, "max", "per 100", 20,
        "most forced consults are being scored off-topic, so the gate is buying "
        "ceremony, not recall: `omind guard pause` to confirm, then fix retrieval "
        "(`omind bench --quality`) or relax the verifier",
        "#326: 'if most forced consults are scored off-topic, the gate is "
        "spending attention for nothing'; 1 in 4 is already generous",
    ),
    Threshold(
        "gate.ceremony_pct", "consult gate", "denies that were pure ceremony",
        80.0, "max", "%", 20,
        "the read-the-rules ritual dominates enforcement: move standing rules "
        "into the harness's own instructions with `omind rules export`",
        "a guard whose denies are >80% satisfy-and-retry is mostly friction",
    ),
    Threshold(
        "gate.auto_clear_pct", "consult gate", "turns cleared with nothing injected",
        90.0, "max", "%", 20,
        "the per-turn gate almost never binds — see #296 (consult continuity)",
        "#296 measured 362 auto-cleared turns carrying 2,037 tool calls",
    ),
    Threshold(
        "rules.never_fired_pct", "learned + note rules", "rules that have never fired",
        50.0, "max", "%", 3,
        "a rule that never fires is dead weight or never binds: review with "
        "`omind rules list` and delete or fix the pattern",
        "#326: 'how many exist vs. how many have ever fired'",
    ),
    Threshold(
        "verifier.p99_chars", "verifier", "prompt size, p99",
        6_000, "max", "chars", 20,
        "the relevance verifier is reading more than it needs; lower "
        "OMI_VERIFY_ACTIVITY (recent-activity bullets fed to each judgement)",
        "the verifier judges one consult against one task; 6,000 chars is ample",
    ),
    Threshold(
        "schema.tokens", "MCP tool schema", "fixed per-session cost",
        6_000, "max", "tokens", 1,
        "every session pays this before its first turn: merge or remove MCP tools "
        "(precedent: #177 folded four graph tools into one)",
        "measured by `omind bench`; #177/#181 exist because this only grows",
    ),
)

_BY_KEY = {t.key: t for t in THRESHOLDS}


@dataclass
class Row:
    """One judged (or explicitly un-judged) measurement."""

    key: str
    surface: str
    label: str
    status: str
    value: float | None = None
    limit: float | None = None
    direction: str = ""
    unit: str = ""
    samples: int = 0
    detail: str = ""
    remedy: str = ""

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass
class AuditReport:
    vault: str
    days: int
    rows: list[Row] = field(default_factory=list)

    @property
    def failed(self) -> list[Row]:
        return [r for r in self.rows if r.status == FAIL]

    @property
    def exit_code(self) -> int:
        return 1 if self.failed else 0

    def to_dict(self) -> dict[str, Any]:
        counts = {s: sum(1 for r in self.rows if r.status == s) for s in (OK, FAIL, UNMEASURED)}
        return {
            "vault": self.vault,
            "days": self.days,
            "verdict": FAIL if self.failed else OK,
            "counts": counts,
            "rows": [r.to_dict() for r in self.rows],
        }

    def format(self) -> str:
        lines = [f"omind audit: {self.vault}  (last {self.days} days)", ""]
        surface = ""
        for row in self.rows:
            if row.surface != surface:
                surface = row.surface
                lines.append(f"{surface}")
            mark = {OK: "ok  ", FAIL: "FAIL", UNMEASURED: "??  "}[row.status]
            if row.value is None:
                shown = "unmeasured"
            else:
                sign = "<=" if row.direction == "max" else ">="
                limit = f"limit {sign} {row.limit:,.0f}, n={row.samples}"
                shown = f"{row.value:,.1f} {row.unit} ({limit})"
            lines.append(f"  [{mark}] {row.label:<40} {shown}")
            if row.detail and row.status != OK:
                lines.append(f"         {row.detail}")
            if row.status == FAIL:
                lines.append(f"         remedy: {row.remedy}")
        counts = self.to_dict()["counts"]
        lines.append("")
        lines.append(
            f"{counts[FAIL]} failing, {counts[OK]} ok, {counts[UNMEASURED]} unmeasured"
            + ("" if not self.failed else " — exit 1")
        )
        return "\n".join(lines)


def _percentile(values: list[int], fraction: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    return ordered[max(0, min(len(ordered) - 1, math.ceil(fraction * len(ordered)) - 1))]


def _judge(key: str, value: float, samples: int, detail: str = "") -> Row:
    t = _BY_KEY[key]
    row = Row(
        key=key, surface=t.surface, label=t.label, status=OK, value=float(value),
        limit=t.limit, direction=t.direction, unit=t.unit, samples=samples,
        detail=detail, remedy=t.remedy,
    )
    if samples < t.min_samples:
        row.status = UNMEASURED
        row.detail = f"only {samples} sample(s); need {t.min_samples} to judge. {detail}".strip()
    elif (t.direction == "max" and value > t.limit) or (t.direction == "min" and value < t.limit):
        row.status = FAIL
    return row


def _unmeasured(key: str, why: str, *, surface: str = "", label: str = "") -> Row:
    t = _BY_KEY.get(key)
    return Row(
        key=key,
        surface=t.surface if t else surface,
        label=t.label if t else label,
        status=UNMEASURED,
        limit=t.limit if t else None,
        direction=t.direction if t else "",
        unit=t.unit if t else "",
        detail=why,
        remedy=t.remedy if t else "",
    )


def _in_window(event: dict[str, Any], since: datetime) -> bool:
    try:
        return datetime.fromisoformat(str(event.get("ts") or "")) >= since
    except ValueError:
        return False


def _chars(events: list[dict[str, Any]], operation: str) -> list[int]:
    out: list[int] = []
    for event in events:
        if event.get("operation") != operation:
            continue
        try:
            out.append(max(0, int(event.get("characters") or 0)))
        except (TypeError, ValueError):
            continue
    return out


def _ledger_rows(usage: list[dict[str, Any]]) -> list[Row]:
    from omind import ai_usage

    recall = _chars(usage, "recall")
    priming = [ai_usage.estimate_tokens(c) for c in _chars(usage, "priming")]
    mcp = _chars(usage, "mcp")
    verifier = _chars(usage, "verifier")
    per_session: dict[str, int] = {}
    for event in usage:
        session = str(event.get("session_id") or "")
        if session and event.get("operation") in ai_usage.CONTEXT_OPERATIONS:
            try:
                per_session[session] = per_session.get(session, 0) + max(
                    0, int(event.get("characters") or 0)
                )
            except (TypeError, ValueError):
                continue
    session_tokens = [ai_usage.estimate_tokens(c) for c in per_session.values()]
    worst = max(session_tokens) if session_tokens else 0
    return [
        _judge("preflight.median_chars", _percentile(recall, 0.5), len(recall)),
        _judge(
            "preflight.p99_chars", _percentile(recall, 0.99), len(recall),
            f"max {max(recall):,} chars" if recall else "",
        ),
        _judge(
            "session.p99_tokens", _percentile(session_tokens, 0.99), len(session_tokens),
            f"worst session {worst:,} tokens",
        ),
        _judge("priming.median_tokens", _percentile(priming, 0.5), len(priming)),
        _judge("priming.p99_tokens", _percentile(priming, 0.99), len(priming)),
        _judge("mcp.median_chars", _percentile(mcp, 0.5), len(mcp)),
        _judge(
            "mcp.p99_chars", _percentile(mcp, 0.99), len(mcp),
            f"max {max(mcp):,} chars" if mcp else "",
        ),
        _judge("verifier.p99_chars", _percentile(verifier, 0.99), len(verifier)),
    ]


def _precision_row(omi_dir: Path) -> Row:
    from omind import bench

    report = bench.run_precision(omi_dir)
    by_name = {m.name: m for m in report.measurements}
    precision = by_name.get("injection precision")
    speaks = by_name.get("preflight speaks")
    if precision is None or speaks is None:
        return _unmeasured("preflight.precision_pct", "bench --precision reported nothing")
    evaluable = len(bench.QUALITY_CASES)
    spoke = round(speaks.value * evaluable / 100.0)
    return _judge(
        "preflight.precision_pct", precision.value, spoke,
        f"spoke on {speaks.value:.0f}% of labelled turns; wrong: {precision.detail or 'none'}",
    )


def _gate_rows(
    events: list[dict[str, Any]], usage: list[dict[str, Any]], days: int, now: datetime
) -> list[Row]:
    from omind import compliance

    offtopic = sum(1 for e in events if e.get("rule_id") == "off-topic-consult")
    reads = len(_chars(usage, "mcp"))
    denies = [e for e in events if e.get("outcome") == "deny"]
    ceremony = [e for e in denies if str(e.get("rule_id") or "") in compliance.CEREMONY_RULES]
    rows = [
        _judge(
            "gate.offtopic_per_100", (100.0 * offtopic / reads) if reads else 0.0, reads,
            f"{offtopic:,} off-topic verdicts against {reads:,} OMI MCP responses "
            "(a ratio, not a share: vault Reads are judged too)",
        ),
        _judge(
            "gate.ceremony_pct", (100.0 * len(ceremony) / len(denies)) if denies else 0.0,
            len(denies),
            f"{len(ceremony):,} of {len(denies):,} denies were satisfy-and-retry; "
            f"{len(denies) - len(ceremony):,} actually refused work",
        ),
    ]
    continuity = compliance.gate_continuity(days=days, now=now)
    if continuity["turns"] and not (continuity["injected"] or continuity["carried"]):
        # Pre-9.5.0 installs log the auto-clear but not the inject, so 100% here
        # is an artifact of what was recorded, not a finding. Say so.
        rows.append(
            _unmeasured(
                "gate.auto_clear_pct",
                f"{continuity['turns']:,} auto-clears logged but no inject/carry events at "
                "all — this install predates inject logging (9.5.0); upgrade to measure",
            )
        )
    else:
        rows.append(
            _judge("gate.auto_clear_pct", continuity["auto_clear_pct"], continuity["turns"])
        )
    return rows


def _rules_row(omi_dir: Path, all_events: list[dict[str, Any]]) -> Row:
    from omind import policy, rules

    ids = [rule.id for rule in policy.load_learned()]
    ids += [f"note-rule:{rule.id}" for rule in rules.load_rules(omi_dir)]
    fired = {str(e.get("rule_id") or "") for e in all_events}
    never = [rule_id for rule_id in ids if rule_id not in fired]
    return _judge(
        "rules.never_fired_pct", (100.0 * len(never) / len(ids)) if ids else 0.0, len(ids),
        ("never fired: " + ", ".join(never[:5])) if never else "",
    )


def _schema_row(omi_dir: Path) -> Row:
    import asyncio
    import json

    from omind import ai_usage
    from omind.server import build_server

    tools = asyncio.run(build_server(omi_dir).list_tools())
    blob = json.dumps(
        [tool.model_dump(mode="json", exclude_none=True) for tool in tools],
        ensure_ascii=False, separators=(",", ":"),
    )
    return _judge("schema.tokens", ai_usage.estimate_tokens(blob), 1, f"{len(tools)} MCP tools")


#: Questions #326 asks that the recorded telemetry cannot answer. Listed so the
#: gap is part of the verdict instead of an omission nobody notices.
_BLIND_SPOTS: tuple[tuple[str, str, str, str], ...] = (
    (
        "priming.readback", "SessionStart priming", "capsules consulted later in the session",
        "priming events carry no session id and no note names, so a capsule nothing "
        "ever reads back is indistinguishable from a useful one",
    ),
    (
        "mcp.acted_on", "MCP tool responses", "payloads acted on vs discarded",
        "the ledger stores sizes only — no tool name, no payload (by design: it is "
        "privacy-safe) — so per-tool waste cannot be attributed",
    ),
)


def run_audit(
    omi_dir: Path | str, *, days: int = DEFAULT_DAYS, now: datetime | None = None
) -> AuditReport:
    """Measure every surface and judge it. Read-only; never raises."""
    from omind import ai_usage, compliance

    omi = Path(omi_dir).expanduser()
    moment = now or datetime.now()
    since = moment - timedelta(days=max(0, days))
    report = AuditReport(vault=str(omi), days=days)

    try:
        usage = [e for e in ai_usage.read_events(omi) if _in_window(e, since)]
    except Exception:
        usage = []
    try:
        all_events = compliance.read_events()
    except Exception:
        all_events = []
    events = [e for e in all_events if _in_window(e, since)]

    def guarded(keys: tuple[str, ...], measure: Callable[[], list[Row]]) -> None:
        try:
            report.rows.extend(measure())
        except Exception as exc:  # one broken instrument never hides the others
            report.rows.extend(_unmeasured(k, f"measurement failed: {exc}") for k in keys)

    ledger_keys = (
        "preflight.median_chars", "preflight.p99_chars", "session.p99_tokens",
        "priming.median_tokens", "priming.p99_tokens", "mcp.median_chars",
        "mcp.p99_chars", "verifier.p99_chars",
    )
    guarded(ledger_keys, lambda: _ledger_rows(usage))
    guarded(("preflight.precision_pct",), lambda: [_precision_row(omi)])
    guarded(
        ("gate.offtopic_per_100", "gate.ceremony_pct", "gate.auto_clear_pct"),
        lambda: _gate_rows(events, usage, days, moment),
    )
    # Whether a rule has EVER fired is a lifetime question, not a windowed one.
    guarded(("rules.never_fired_pct",), lambda: [_rules_row(omi, all_events)])
    guarded(("schema.tokens",), lambda: [_schema_row(omi)])
    for key, surface, label, why in _BLIND_SPOTS:
        report.rows.append(_unmeasured(key, why, surface=surface, label=label))

    order = {t.surface: i for i, t in enumerate(THRESHOLDS)}
    report.rows.sort(key=lambda r: order.get(r.surface, len(order)))
    return report
