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
def _isolate_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Point HOME at a per-test temp dir for every test.

    Provisioning writes hooks, the skill, and the provision manifest under
    ``Path.home()/.claude`` (which resolves via ``$HOME`` on POSIX). A test that
    runs ``Provisioner.run()``/``integrate()`` without individually stubbing
    ``Path.home`` would otherwise clobber the developer's REAL ``~/.claude`` — a
    full local ``pytest`` once rewrote this machine's live ``omi-guard.sh`` with a
    pytest temp ``OMI_DIR``, wedging the consult gate. Same class of leak the
    state-home isolation below already guards; tests that need a specific home
    still override it after this fixture runs.

    Uses a dedicated dir name (not ``home``) so a test that makes its own
    ``tmp_path / "home"`` doesn't collide with this pre-created one."""
    home = tmp_path / "isolated-home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(home))


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
