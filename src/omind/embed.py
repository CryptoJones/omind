# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Aaron K. Clark
"""Pluggable semantic-embedding backend (omind 3.0.0).

The verifier's relevance check and the vault's recall were both built on
keyword/stem overlap (:mod:`omind.retrieve`). That over-flags genuinely on-topic
consults whenever the wording differs from the note's wording — the friction the
2.45.0 graduated gate worked *around* rather than fixing. This module fixes it at
the source: a static-embedding backend (model2vec — numpy-only, no torch /
onnxruntime, fast and offline once the model is cached) turns text into vectors so
relevance can be measured by meaning, not shared tokens.

Everything here FAILS OPEN. If model2vec / numpy are not installed, the configured
model cannot load, or an encode raises, every entry point returns ``None`` (or an
empty result) and the caller falls back to the deterministic keyword path — i.e.
exactly omind 2.x behaviour. Semantic relevance is an *enhancement*, never a new
way to wedge. ``OMI_EMBED_DISABLE=1`` forces the keyword path; ``OMI_EMBED_MODEL``
overrides the model (default: a small static potion model).

The backend is resolved once and cached. The reason for any unavailability is kept
(:func:`status`) so ``omind doctor`` can explain why semantics are off, without the
hot path re-paying a slow/failing import per consult.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:  # pragma: no cover - typing only
    import numpy as np

#: Env: force the keyword path even when model2vec is installed.
_DISABLE_ENV = "OMI_EMBED_DISABLE"
#: Env: override the model2vec model loaded by the default backend.
_MODEL_ENV = "OMI_EMBED_MODEL"
#: A small static-embedding model: numpy-only, ~tens of MB, offline once cached.
_DEFAULT_MODEL = "minishlab/potion-base-8M"


class Backend(Protocol):
    """Anything that can turn a list of strings into a 2-D float vector array."""

    def encode(self, texts: list[str]) -> Any: ...  # -> np.ndarray (n, dim)


#: Resolution cache. ``_resolved`` records whether we have tried; ``_backend`` is
#: the result (``None`` = unavailable, keyword path). Distinguishing "not tried"
#: from "unavailable" stops us re-importing a missing dependency on every consult.
_resolved: bool = False
_backend: Backend | None = None
#: Why the backend is unavailable (for diagnostics), or ``""`` when it loaded.
_last_error: str = ""
#: A test / alternate-embedder seam: when active, used verbatim, skipping the
#: model2vec import. ``set_backend(None)`` pins the keyword (unavailable) path.
_override_active: bool = False
_override: Backend | None = None


def set_backend(backend: Backend | None) -> None:
    """Install an explicit backend (tests, or an alternate embedder), bypassing the
    model2vec auto-load. ``None`` pins the keyword fallback. :func:`reset` returns
    to auto-resolution."""
    global _override, _override_active, _resolved
    _override = backend
    _override_active = True
    _resolved = False


def reset() -> None:
    """Forget the resolved/overridden backend so the next call re-resolves (tests,
    or after an ``OMI_EMBED_*`` env change)."""
    global _resolved, _backend, _last_error, _override, _override_active
    _resolved = False
    _backend = None
    _last_error = ""
    _override = None
    _override_active = False


def _load_model2vec() -> Backend | None:
    """Load the default model2vec static model, or ``None`` if unavailable.

    Imports are local so the module (and all of omind) imports fine without the
    optional ``[embed]`` extra. Any failure — missing package, model not cached and
    no network, load error — records a reason and fails open to the keyword path.
    """
    global _last_error
    try:
        from model2vec import StaticModel
    except Exception as exc:  # ImportError, or a broken partial install
        _last_error = f"model2vec not importable ({type(exc).__name__}); pip install 'omind[embed]'"
        return None
    model_name = os.environ.get(_MODEL_ENV) or _DEFAULT_MODEL
    try:
        backend: Backend = StaticModel.from_pretrained(model_name)
    except Exception as exc:
        _last_error = f"model {model_name!r} failed to load ({type(exc).__name__})"
        return None
    _last_error = ""
    return backend


def _resolve() -> Backend | None:
    """Return the active backend (cached), or ``None`` for the keyword path."""
    global _resolved, _backend, _last_error
    if _override_active:
        return _override
    if os.environ.get(_DISABLE_ENV):
        _last_error = f"disabled via {_DISABLE_ENV}"
        return None
    if not _resolved:
        _backend = _load_model2vec()
        _resolved = True
    return _backend


def available() -> bool:
    """True when a semantic backend is loaded and usable this process."""
    return _resolve() is not None


def status() -> dict[str, Any]:
    """Diagnostic snapshot for ``omind doctor`` — whether semantics are active,
    the model, and (when off) why. Resolving the backend has no side effects beyond
    the one-time load it would do anyway."""
    ok = available()
    return {
        "available": ok,
        "model": os.environ.get(_MODEL_ENV) or _DEFAULT_MODEL,
        "reason": "" if ok else (_last_error or "unavailable"),
    }


def encode(texts: list[str]) -> np.ndarray | None:
    """Embed ``texts`` into an L2-normalised ``(n, dim)`` float array, or ``None``.

    ``None`` whenever there is no backend or the encode raises — the caller falls
    back to keyword scoring. Empty input yields ``None`` (nothing to embed).
    """
    global _last_error
    backend = _resolve()
    if backend is None or not texts:
        return None
    try:
        import numpy as np

        vecs = np.asarray(backend.encode(texts), dtype="float32")
        if vecs.ndim == 1:
            vecs = vecs.reshape(1, -1)
        norms = np.linalg.norm(vecs, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        return vecs / norms
    except Exception as exc:
        _last_error = f"encode failed ({type(exc).__name__})"
        return None


def similarity(a: str, b: str) -> float | None:
    """Cosine similarity (0..1, clamped) of two strings, or ``None`` if no backend.

    Both texts are embedded together so a single failure short-circuits to the
    keyword path. Empty strings → ``None`` (no signal to compare)."""
    if not a or not b:
        return None
    vecs = encode([a, b])
    if vecs is None or vecs.shape[0] != 2:
        return None
    try:
        import numpy as np

        sim = float(np.dot(vecs[0], vecs[1]))
    except Exception:
        return None
    # Cosine of L2-normalised vectors is in [-1, 1]; clamp to [0, 1] so it composes
    # with the keyword overlap score (also 0..1) under a plain ``max``.
    return max(0.0, min(1.0, sim))
