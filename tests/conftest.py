# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Aaron K. Clark
"""Suite-wide isolation.

No test may touch the developer's real machine state. Individual suites
already isolate the paths they know they hit (XDG_CONFIG_HOME, ~/.claude,
MCP server dirs); this guard catches the ones nobody thought about — found
live when local pytest runs left hook-failure breadcrumbs in the real
``~/.local/state/omind/`` and `omind doctor` started warning about them.
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolate_state_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Point XDG_STATE_HOME at a per-test temp dir for every test."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg-state"))


@pytest.fixture(autouse=True)
def _isolate_config_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Point XDG_CONFIG_HOME at a per-test temp dir for every test.

    `omind setup` now initializes the mesh (writing ~/.config/omind/node.json);
    without this, provisioning tests would mint node identities in the
    developer's real config — the same class of leak the state-home isolation
    was added for.
    """
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
