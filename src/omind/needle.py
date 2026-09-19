# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Aaron K. Clark
"""A/B needle-in-a-haystack replay: does the per-turn preflight cost recall? (#321)

#321 measured what the preflight SHIPPED (3.4M tokens at ~25% precision). This
measures what that does to the model reading it. It replays a real long
transcript twice — once as it was (preflight OFF), once with what the preflight
would have pushed after every user prompt (preflight ON) — with a synthetic
fact and a synthetic instruction planted at a known depth, and asks a model to
retrieve the fact and obey the instruction. Depths are 20 / 50 / 80 % of the
context, per the issue's acceptance criterion.

Everything up to the model call is deterministic and testable: the transcript
loader, the planting, the preflight replay, the scoring. The replay uses
``bench.preflight_pick`` — a READ-ONLY replica of the live choice — because
``guard.preflight_turn`` records consults, compliance events and usage rows,
and an instrument that perturbs what it measures reports numbers that drift.

The needles are generated per trial from a seed, so a model cannot have seen
them and a lucky guess cannot score. With no model backend configured the
harness still builds both arms and reports their sizes; the recall rows are
simply absent, never faked. ``--emit DIR`` writes the exact prompts so the
comparison can be run against any model by hand.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from omind.bench import Report

DEPTHS: tuple[int, ...] = (20, 50, 80)
#: Default ceiling on the replayed context. Large enough to be "long" for any
#: current model (~50k tokens), small enough to fit one. The tail of the
#: transcript is kept: recent history is what a live session actually holds.
DEFAULT_MAX_CHARS = 200_000
#: A tool result is mostly bulk; cap each so a few giant dumps cannot be the
#: whole haystack.
_TOOL_RESULT_CAP = 2_000
_MODEL_TIMEOUT_S = 600

_PROJECTS = ("KESTREL", "OSPREY", "HARRIER", "MERLIN", "GOSHAWK", "CURLEW")
_WORDS = ("amber", "cobalt", "saffron", "indigo", "russet", "jade", "slate", "ochre")


@dataclass(frozen=True)
class Segment:
    role: str  # "user" | "assistant" | "tool" | "memory" | "needle"
    text: str

    def render(self) -> str:
        return f"[{self.role}]\n{self.text}\n"


@dataclass(frozen=True)
class Needle:
    """One planted fact + one planted instruction, unguessable per (seed, depth)."""

    project: str
    passphrase: str
    ack: str

    @property
    def text(self) -> str:
        return (
            f"For the record: the staging passphrase for project {self.project} is "
            f"'{self.passphrase}'. Also, a standing instruction for this session: end "
            f"your final answer with the exact line {self.ack}"
        )

    @property
    def question(self) -> str:
        return (
            f"What is the staging passphrase for project {self.project}? Reply with the "
            "passphrase, following every standing instruction given earlier in this session."
        )


def make_needle(seed: int, depth: int) -> Needle:
    digest = hashlib.sha256(f"omind-needle:{seed}:{depth}".encode()).digest()
    project = _PROJECTS[digest[0] % len(_PROJECTS)]
    word_a = _WORDS[digest[1] % len(_WORDS)]
    word_b = _WORDS[digest[2] % len(_WORDS)]
    number = int.from_bytes(digest[3:5], "big") % 9000 + 1000
    ack = f"ACK-{digest[5:8].hex().upper()}"
    return Needle(project, f"{word_a}-{word_b}-{number}", ack)


def _block_text(content: Any) -> tuple[str, str]:
    """``(role_hint, text)`` for a transcript message's content."""
    if isinstance(content, str):
        return "text", content
    parts: list[str] = []
    hint = "text"
    for block in content if isinstance(content, list) else []:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "text" and isinstance(block.get("text"), str):
            parts.append(block["text"])
        elif kind == "tool_use":
            hint = "tool"
            parts.append(f"(tool call: {block.get('name')})")
        elif kind == "tool_result":
            hint = "tool"
            inner = block.get("content")
            text = _block_text(inner)[1] if not isinstance(inner, str) else inner
            parts.append(text[:_TOOL_RESULT_CAP])
    return hint, "\n".join(p for p in parts if p)


def load_transcript(path: Path | str) -> list[Segment]:
    """Segments of a transcript: a Claude Code ``.jsonl`` session, or any text
    file (split on blank lines, all ``assistant``-role filler with no prompts to
    replay preflight against — useful as a pure haystack). Raises ``OSError`` /
    ``ValueError`` on an unreadable or empty transcript; this is a CLI
    instrument, not a hook, so it says what is wrong."""
    source = Path(path).expanduser()
    raw = source.read_text(encoding="utf-8", errors="replace")
    segments: list[Segment] = []
    if source.suffix == ".jsonl":
        for line in raw.splitlines():
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if not isinstance(entry, dict) or entry.get("isSidechain") or entry.get("isMeta"):
                continue
            kind = entry.get("type")
            message = entry.get("message")
            if kind not in ("user", "assistant") or not isinstance(message, dict):
                continue
            hint, text = _block_text(message.get("content"))
            if not text.strip():
                continue
            role = "tool" if hint == "tool" and kind == "user" else kind
            segments.append(Segment(role, text.strip()))
    else:
        segments = [Segment("assistant", p.strip()) for p in raw.split("\n\n") if p.strip()]
    if not segments:
        raise ValueError(f"no usable content in transcript: {source}")
    return segments


def _tail(segments: list[Segment], max_chars: int) -> list[Segment]:
    kept: list[Segment] = []
    total = 0
    for segment in reversed(segments):
        size = len(segment.render())
        if kept and total + size > max_chars:
            break
        kept.append(segment)
        total += size
    kept.reverse()
    return kept


def preflight_payload(omi: Path, prompt: str, mode: str) -> str:
    """The text the preflight would push beside ``prompt`` in ``mode`` — the same
    wording the live path uses, from a read-only choice. ``""`` when it would
    abstain (or when ``mode`` is ``off``)."""
    from omind import ai_usage, bench, guard

    if mode == "off":
        return ""
    pick = bench.preflight_pick(omi, prompt)
    if pick is None:
        return ""
    title = pick.titles[0]
    if mode == "hint" and not pick.hard_rule:
        names = [title]
        if len(pick.titles) > 1 and pick.titles[1] and pick.titles[1] != title:
            names.append(pick.titles[1])
        return (
            "OMI turn preflight — possibly relevant background from prior sessions, "
            "not an instruction: "
            + ", ".join(f"[[{name}]]" for name in names)
            + ". Call OMI MCP `recall-note` on one if this turn needs it; "
            "verify before acting, it may be stale."
        )[: guard.PREFLIGHT_HINT_CHARS]
    cap = ai_usage.policy(omi).preflight_chars
    body = guard.strip_action_items(pick.raw[:cap]) or pick.raw[:cap]
    return (
        f"OMI turn preflight recalled [[{title}]]. Possibly relevant background from "
        f"prior sessions — verify before acting on it; it may be stale.\n\n{body}"
    )


def build_arm(
    segments: list[Segment],
    needle: Needle,
    depth: int,
    *,
    inject: Callable[[str], str] | None = None,
) -> tuple[str, int]:
    """One arm's full prompt, plus how many chars of it are replayed preflight.

    The needle lands at ``depth`` percent of the haystack's characters, on a
    segment boundary, as a user message — where a human would really say it.
    """
    body: list[Segment] = []
    injected = 0
    for segment in segments:
        body.append(segment)
        if inject is not None and segment.role == "user":
            payload = inject(segment.text)
            if payload:
                injected += len(payload)
                body.append(Segment("memory", payload))
    total = sum(len(s.render()) for s in body)
    target = total * depth / 100.0
    running = 0
    index = len(body)
    for i, segment in enumerate(body):
        if running >= target:
            index = i
            break
        running += len(segment.render())
    body.insert(index, Segment("needle", needle.text))
    prompt = (
        "Below is the transcript of a long working session. Read all of it, then "
        "answer the question at the end.\n\n"
        + "\n".join(s.render() for s in body)
        + f"\n[question]\n{needle.question}\n"
    )
    return prompt, injected


def score(answer: str | None, needle: Needle) -> tuple[bool, bool]:
    """``(recalled, obeyed)``: the passphrase appears, and the LAST non-empty
    line is the demanded acknowledgement."""
    if not answer:
        return False, False
    lines = [line.strip() for line in answer.strip().splitlines() if line.strip()]
    return needle.passphrase in answer, bool(lines) and lines[-1] == needle.ack


def run_needle(
    omi_dir: Path | str,
    transcript: Path | str,
    *,
    mode: str = "hint",
    trials: int = 1,
    max_chars: int = DEFAULT_MAX_CHARS,
    emit: Path | None = None,
    model: Callable[[str], str | None] | None = None,
) -> Report:
    """Run the A/B. ``model`` maps a prompt to an answer (``None`` on failure);
    by default it is omind's configured one-shot backend, and when there is
    none the arms are still built and sized but nothing is scored."""
    from omind import ai_usage

    omi = Path(omi_dir).expanduser()
    segments = _tail(load_transcript(transcript), max_chars)
    report = Report(vault=str(omi))
    prompts = sum(1 for s in segments if s.role == "user")
    report.add("transcript segments", len(segments), "count", f"{prompts} user prompts replayed")

    cache: dict[str, str] = {}

    def inject(prompt: str) -> str:
        if prompt not in cache:
            try:
                cache[prompt] = preflight_payload(omi, prompt, mode)
            except Exception:
                cache[prompt] = ""  # retrieval fails open; so does its replica
        return cache[prompt]

    if model is None and ai_usage.resolve_model_backend() is not None:

        def model(prompt: str) -> str | None:
            return ai_usage.run_model(omi, "needle", prompt, timeout=_MODEL_TIMEOUT_S)

    tallies = {
        (arm, depth): [0, 0, 0] for arm in ("off", "on") for depth in DEPTHS
    }  # recalled, obeyed, answered
    sizes = {"off": 0, "on": 0}
    injected_total = 0
    for trial in range(max(1, trials)):
        for depth in DEPTHS:
            needle = make_needle(trial, depth)
            for arm in ("off", "on"):
                prompt, injected = build_arm(
                    segments, needle, depth, inject=inject if arm == "on" else None
                )
                sizes[arm] = max(sizes[arm], len(prompt))
                injected_total = max(injected_total, injected)
                if emit is not None:
                    emit.mkdir(parents=True, exist_ok=True)
                    name = f"trial{trial}-depth{depth}-preflight-{arm}.txt"
                    (emit / name).write_text(prompt, encoding="utf-8")
                if model is None:
                    continue
                answer = model(prompt)
                if answer is None:
                    continue
                recalled, obeyed = score(answer, needle)
                tally = tallies[(arm, depth)]
                tally[0] += int(recalled)
                tally[1] += int(obeyed)
                tally[2] += 1

    for arm in ("off", "on"):
        report.add(
            f"context, preflight {arm}", sizes[arm], "chars",
            f"~{ai_usage.estimate_tokens(sizes[arm]):,} tokens",
        )
    share = (100.0 * injected_total / sizes["on"]) if sizes["on"] else 0.0
    report.add(
        f"replayed preflight ({mode})", injected_total, "chars", f"{share:.1f}% of the ON arm"
    )
    answered = sum(t[2] for t in tallies.values())
    if not answered:
        why = "no model backend configured" if model is None else "the model returned nothing"
        report.add(
            "model answers", 0, "count",
            f"{why} — arms built, nothing scored (set OMI_MODEL_CMD, or use --emit)",
        )
        return report
    for metric, slot in (("recall", 0), ("adherence", 1)):
        for depth in DEPTHS:
            for arm in ("off", "on"):
                hit, n = tallies[(arm, depth)][slot], tallies[(arm, depth)][2]
                if n:
                    report.add(
                        f"{metric} @{depth}%, preflight {arm}", 100.0 * hit / n, "%", f"{hit}/{n}"
                    )
    for metric, slot in (("recall", 0), ("adherence", 1)):
        rates = {}
        for arm in ("off", "on"):
            n = sum(tallies[(arm, d)][2] for d in DEPTHS)
            rates[arm] = (100.0 * sum(tallies[(arm, d)][slot] for d in DEPTHS) / n) if n else 0.0
        report.add(
            f"{metric} delta (on - off)", rates["on"] - rates["off"], "pts",
            "negative = the preflight cost the model accuracy",
        )
    return report
