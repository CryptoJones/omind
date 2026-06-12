# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Aaron K. Clark
"""Session plumbing for the e2e suite.

Every test here skips unless ``OMIND_E2E_PROVIDER`` is set, so plain
``pytest`` runs and CI are untouched. The session builds one wheel from the
working tree and provisions nodes per test through the selected provider,
tearing them down in a ``finally`` (unless ``OMIND_E2E_KEEP=1``).
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from e2e.providers import NodeHandle, make_provider

REPO_ROOT = Path(__file__).resolve().parent.parent


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if os.environ.get("OMIND_E2E_PROVIDER"):
        return
    skip = pytest.mark.skip(reason="e2e: set OMIND_E2E_PROVIDER=podman|runpod to run")
    for item in items:
        if Path(item.fspath).is_relative_to(REPO_ROOT / "e2e"):
            item.add_marker(skip)


@pytest.fixture(scope="session")
def wheel(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The working tree built once — scenarios test exactly the local code."""
    out = tmp_path_factory.mktemp("dist")
    subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(out)],
        cwd=REPO_ROOT, check=True, capture_output=True,
    )
    wheels = sorted(out.glob("omind-*.whl"))
    assert wheels, "uv build produced no wheel"
    return wheels[-1]


@pytest.fixture
def nodes(tmp_path: Path) -> Iterator[Callable[[int], list[NodeHandle]]]:
    """A provisioner: call ``nodes(n)`` for n fresh hosts; teardown is automatic."""
    provider = make_provider(os.environ["OMIND_E2E_PROVIDER"], tmp_path)
    try:
        yield provider.provision
    finally:
        if os.environ.get("OMIND_E2E_KEEP") == "1":
            print(f"\nOMIND_E2E_KEEP=1: leaving nodes up (run id {provider.run_id})")
        else:
            provider.teardown()
