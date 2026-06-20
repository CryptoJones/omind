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


@pytest.fixture(autouse=True)
def _isolate_claude_config_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drop ``CLAUDE_CONFIG_DIR`` so Claude config/settings/skill paths fall back
    to the (isolated) HOME instead of the developer's real config dir.

    The HOME isolation alone was NOT enough: ``claude_settings_path`` /
    ``claude_config_path`` / ``claude_skill_dir`` key off ``CLAUDE_CONFIG_DIR``
    FIRST, while the hook destinations key off ``Path.home()``. A test that ran
    provisioning therefore wrote hook *files* to the temp HOME but rewrote the
    REAL ``settings.json`` (under ``$CLAUDE_CONFIG_DIR``) to point at those temp
    hooks — wedging the live consult gate a second time. Tests that exercise
    ``CLAUDE_CONFIG_DIR`` set it themselves after this fixture runs."""
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)


@pytest.fixture(autouse=True)
def _no_update_check(monkeypatch: pytest.MonkeyPatch) -> None:
    """Disable the GitHub version check in every test — no network, no flake.

    ``build_session_start_context`` now surfaces the update nudge, so without this
    every priming test would otherwise incur a (fail-open) network round-trip."""
    monkeypatch.setenv("OMIND_NO_UPDATE_CHECK", "1")
