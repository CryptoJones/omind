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

import json
import re
from pathlib import Path
from typing import Any

#: Stopword set — common function words plus the *instruction filler* that wraps
#: a request ("please fix … before we move any further") and otherwise inflates
#: the task's term count, dragging the recall-based overlap score down so a
#: genuinely-relevant consult reads as off-topic.
_STOPWORDS = frozenset(
    {
        # function words
        "and", "are", "but", "for", "from", "has", "have", "how", "into", "its",
        "that", "the", "their", "then", "there", "these", "this", "was", "were",
        "what", "when", "which", "who", "why", "with", "you", "your", "our", "does",
        "than", "them", "they", "here", "over", "out",
        # instruction filler / generic verbs that never carry the task's topic
        "please", "before", "after", "again", "also", "just", "now", "more",
        "most", "want", "wants", "need", "needs", "make", "makes", "made", "let",
        "lets", "use", "uses", "used", "using", "can", "will", "would", "should",
        "could", "must", "may", "might", "get", "gets", "got", "about", "any",
        "all", "further",
    }
)

#: Inflectional + light derivational suffixes, folded (longest match first, one
#: per word) onto a shared stem so the overlap score treats morphological
#: variants as the same term — consult/consults/consulted, score/scored/scoring,
#: relevance/relevant, gate/gating. Without this, an on-topic consult scores near
#: zero purely from word-form mismatch and the verifier re-closes the gate. It is
#: deliberately conservative (never strips below a 3-char root); an occasional
#: over-merge of two unrelated words only clears the gate more easily — the safe
#: failure direction, since what we are fixing is a *real* consult judged off-topic.
_SUFFIXES: tuple[str, ...] = tuple(
    sorted(
        {
            "ization", "isation", "ational",
            "fulness", "iveness", "ousness",
            "ation", "ition", "ement",
            "ance", "ence", "able", "ible",
            "ingly", "edly", "fully",
            "tion", "sion", "ness", "ment", "ical",
            "ing", "ies", "ied", "ity", "ive", "ous", "ant", "ent",
            "er", "or", "al", "ed", "es", "ly", "s",
        },
        key=len,
        reverse=True,
    )
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
#: A leading ``cd <dir> &&|;|||`` on a blocked command — pure scaffolding, stripped
#: from the pending intent (#97) so the directory never enters the score.
_CD_PREFIX_RE = re.compile(r"^\s*cd\s+\S+\s*(?:&&|\|\||;)\s*", re.IGNORECASE)
#: How far below a normal note a credential note is ranked when off-topic.
_CREDENTIAL_PENALTY = 0.1


def _stem(word: str) -> str:
    """Fold a word onto a shared stem by stripping one inflectional/derivational
    suffix (longest first) and a trailing ``e``. Conservative: a ≤3-char word is
    untouched, no strip leaves a <3-char root, and the ``s`` of a double-``s``
    ending is kept (``pass``/``address`` stay whole)."""
    if len(word) <= 3:
        return word
    for suf in _SUFFIXES:
        if not word.endswith(suf):
            continue
        if suf == "s" and word.endswith("ss"):
            continue  # plural-vs-"ss": keep "pass", "address"
        if len(word) - len(suf) >= 3:
            word = word[: -len(suf)]
            break
    if len(word) > 3 and word.endswith("e"):
        word = word[:-1]  # score/scoring/scored -> "scor"; gate/gating -> "gat"
    return word


#: ``_CREDENTIAL_TERMS`` run through the same stemmer the tokenizer applies, so
#: credential detection still fires once tokens are stemmed (``credentials`` ->
#: ``credential``, ``credential`` -> ``credenti``). Comparing raw terms against
#: stemmed tokens would silently miss, re-opening the "gate steers toward the
#: secrets notes" failure the de-prioritization exists to prevent.
_CREDENTIAL_STEMS = frozenset(_stem(t) for t in _CREDENTIAL_TERMS)


def _tokens(text: str) -> set[str]:
    return {
        _stem(w)
        for w in _WORD_RE.findall(text.lower())
        if w not in _STOPWORDS and len(w) > 2
    }


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


def normalize_intent(text: str) -> str:
    """Strip command scaffolding from a gate-blocked action before it is scored
    as the turn's *pending intent* (#97). Drops a leading ``cd <dir> &&|;`` and
    reduces each path-like token to its basename, so directory components
    (``prototype/corpus/bin``) stop padding the overlap-score denominator and a
    path-heavy command (``…/mathlib.elf …/mathlib.elf | grep bsim``) clears the
    gate as cleanly as a keyword-rich one. Deterministic; no model."""
    if not text:
        return text
    text = _CD_PREFIX_RE.sub("", text)
    return " ".join(tok.rsplit("/", 1)[-1] or tok for tok in text.split())


def _looks_credential(*texts: str) -> bool:
    blob = _tokens(" ".join(texts))  # stemmed -> compare against stemmed terms
    return bool(blob & _CREDENTIAL_STEMS)


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


def _semantic_titles(
    task: str, omi_dir: Path | str, notes: list[Any], *, task_is_cred: bool, limit: int
) -> list[str] | None:
    """Semantic-similarity ranking of notes to the task (3.0.0) — better gate/nudge
    suggestions than keyword overlap, which surfaced off-topic notes. Preserves the
    credential de-prioritization (never steer to secrets unless the task is about
    them). ``None`` when no embed backend, so the caller falls back to keyword."""
    try:
        from omind import embed, vectorindex

        if not embed.available():
            return None
        title_by_file: dict[str, str] = {}
        cred_files: set[str] = set()
        for note in notes:
            stem = note.filename[:-3] if note.filename.endswith(".md") else note.filename
            title_by_file[note.filename] = note.title or stem
            if not task_is_cred and _looks_credential(
                note.title, note.summary, " ".join(note.tags)
            ):
                cred_files.add(note.filename)
        ranked = vectorindex.VectorIndex(omi_dir).rank(task, limit=limit + len(cred_files) + 1)
        if ranked is None:
            return None
        titles = [
            title_by_file[fn] for fn, _ in ranked if fn in title_by_file and fn not in cred_files
        ]
        return titles[:limit]
    except Exception:
        return None


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
    task_is_cred = bool(task_terms & _CREDENTIAL_STEMS)
    semantic = _semantic_titles(task, omi_dir, notes, task_is_cred=task_is_cred, limit=limit)
    if semantic is not None:
        return semantic
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
    call = json.dumps({"name": titles[0]}, ensure_ascii=False, separators=(",", ":"))
    alternatives = ", ".join(f"[[{title}]]" for title in titles[1:])
    extra = f" Other candidates: {alternatives}." if alternatives else ""
    return (
        f"ACTION BLOCKED. Relevant memory: [[{titles[0]}]]. "
        f"Next call OMI MCP `recall-note` with `{call}`, then retry."
        f"{extra} Do not open credential/auth notes unless the task is explicitly "
        "about credentials."
    )
