# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Aaron K. Clark
"""Just-in-time relevance retrieval (Phase 3.2 of the enforcement roadmap).

When the gate denies an unconsulted action, "read any note" is exactly the
failure the roadmap set out to fix — the agent reads an arbitrary note to clear
the gate. Instead, this module maps the turn's task to the OMI notes most
relevant to it (a deterministic keyword/tag overlap over
:meth:`omind.store.OmiStore.list_notes`) so the block message can name them:
``consult OMI — relevant to your task: [[A]], [[B]], [[C]]``.

The same :func:`overlap_score` is the verifier's deterministic prefilter
(:mod:`omind.verify`), so "relevant" means the same thing in both places.

Credential/auth notes are de-prioritized unless the task itself is about
credentials — a load-bearing lesson: the gate must never coerce the agent toward
opening the secrets notes.
"""

from __future__ import annotations

import re
from pathlib import Path

#: Tiny stopword set — enough to stop "the/and/for" dominating the overlap.
_STOPWORDS = frozenset(
    {
        "and", "are", "but", "for", "from", "has", "have", "how", "into", "its",
        "that", "the", "their", "then", "there", "these", "this", "was", "were",
        "what", "when", "which", "who", "why", "with", "you", "your", "our", "does",
    }
)

#: Single-token terms that mark a note as credential/auth material (the tokenizer
#: drops hyphens, so terms here are single tokens). A note matching these is
#: heavily de-ranked unless the task is itself about credentials.
_CREDENTIAL_TERMS = frozenset(
    {
        "credential", "credentials", "secret", "secrets", "token", "tokens",
        "password", "passwords", "passphrase", "auth", "apikey", "keyfile",
        "keyring", "gpg", "pass",
    }
)

_WORD_RE = re.compile(r"[a-z0-9]+")
#: How far below a normal note a credential note is ranked when off-topic.
_CREDENTIAL_PENALTY = 0.1


def _tokens(text: str) -> set[str]:
    return {w for w in _WORD_RE.findall(text.lower()) if w not in _STOPWORDS and len(w) > 2}


def overlap_score(task: str, text: str) -> float:
    """Fraction of the task's meaningful terms covered by ``text`` (0..1).

    The verifier's deterministic prefilter and note ranking share this so a
    "relevant" consult means the same thing everywhere. Empty task → 0.0 (we
    can't judge relevance without knowing the task; callers fail open).
    """
    task_terms = _tokens(task)
    if not task_terms:
        return 0.0
    return len(task_terms & _tokens(text)) / len(task_terms)


def _looks_credential(*texts: str) -> bool:
    blob = _tokens(" ".join(texts))
    return bool(blob & _CREDENTIAL_TERMS)


def _score_note(
    task: str, *, title: str, summary: str, tags: list[str], stem: str, task_is_cred: bool
) -> float:
    """Weighted relevance of one note to the task. Title + tags count double."""
    task_terms = _tokens(task)
    if not task_terms:
        return 0.0
    strong = _tokens(title) | {t.lower() for t in tags} | _tokens(stem)
    weak = _tokens(summary)
    score = 2.0 * len(task_terms & strong) + len(task_terms & weak)
    if score and _looks_credential(title, summary, " ".join(tags)) and not task_is_cred:
        score *= _CREDENTIAL_PENALTY
    return score


def relevant_titles(task: str, omi_dir: Path | str, *, limit: int = 3) -> list[str]:
    """Titles of the notes most relevant to ``task`` (best first), or ``[]``.

    Best-effort over the note listing; any read/parse failure yields ``[]`` so
    the gate falls back to its generic message rather than wedging.
    """
    task_terms = _tokens(task)
    if not task_terms:
        return []
    try:
        from omind.store import OmiStore

        notes = OmiStore(omi_dir).list_notes()
    except Exception:
        return []
    task_is_cred = bool(task_terms & _CREDENTIAL_TERMS)
    scored: list[tuple[float, str]] = []
    for note in notes:
        stem = note.filename[:-3] if note.filename.endswith(".md") else note.filename
        score = _score_note(
            task,
            title=note.title,
            summary=note.summary,
            tags=note.tags,
            stem=stem,
            task_is_cred=task_is_cred,
        )
        if score > 0:
            scored.append((score, note.title or stem))
    scored.sort(key=lambda s: (-s[0], s[1].lower()))
    return [title for _score, title in scored[:limit]]


def suggest_message(task: str, omi_dir: Path | str, *, limit: int = 3) -> str:
    """The gate-deny message, naming the notes relevant to the turn's task.

    Falls back to the generic consult prompt when there is no captured task or
    nothing scores — never invents a note or steers toward credentials.
    """
    from omind.guard import GATE_MESSAGE

    titles = relevant_titles(task, omi_dir, limit=limit) if task else []
    if not titles:
        return GATE_MESSAGE
    links = ", ".join(f"[[{t}]]" for t in titles)
    return (
        "consult OMI before acting this turn — notes relevant to your task: "
        f"{links} (or another note you know is on-point), then retry. One consult "
        "clears the rest of the turn. This is NOT a prompt to open the "
        "credential/auth notes."
    )
