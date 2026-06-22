# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Aaron K. Clark
"""Tests for the semantic-embedding backend (omind 3.0.0).

The fail-open paths run everywhere; the real-model math is gated behind the
optional ``[embed]`` extra (model2vec + numpy), like the e2e suite.
"""

from __future__ import annotations

import importlib.util
from collections.abc import Iterator

import pytest

from omind import embed

_HAS_EMBED = (
    importlib.util.find_spec("model2vec") is not None
    and importlib.util.find_spec("numpy") is not None
)


@pytest.fixture(autouse=True)
def _reset_embed() -> Iterator[None]:
    """Embed caches its resolved backend in module globals — reset around each
    test so an override/env in one doesn't leak into the next."""
    embed.reset()
    yield
    embed.reset()


def test_fails_open_without_a_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OMI_EMBED_DISABLE", raising=False)
    embed.set_backend(None)  # pin unavailable without importing model2vec
    assert embed.available() is False
    assert embed.similarity("a release", "a version") is None
    assert embed.encode(["x"]) is None
    assert embed.status()["available"] is False


def test_disable_env_forces_keyword_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMI_EMBED_DISABLE", "1")
    embed.reset()
    assert embed.available() is False
    assert "disabled" in embed.status()["reason"]


def test_set_backend_makes_it_available_and_none_pins_it_off() -> None:
    class _Fake:
        def encode(self, texts: list[str]) -> list[list[float]]:
            return [[1.0, 0.0] for _ in texts]

    embed.set_backend(_Fake())
    assert embed.available() is True
    embed.set_backend(None)
    assert embed.available() is False


def test_similarity_and_encode_handle_empty_input() -> None:
    embed.set_backend(None)
    assert embed.similarity("", "x") is None
    assert embed.encode([]) is None


@pytest.mark.skipif(not _HAS_EMBED, reason="needs the [embed] extra (model2vec + numpy)")
def test_real_similarity_orders_paraphrase_above_unrelated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OMI_EMBED_DISABLE", raising=False)  # opt back in past the suite default
    embed.reset()
    para = embed.similarity(
        "cut a release and push to the forge", "publish a new version to the git remote"
    )
    unrelated = embed.similarity(
        "cut a release and push to the forge", "banana mango smoothie recipe"
    )
    assert para is not None and unrelated is not None
    assert para > unrelated  # meaning, not shared tokens
