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

from collections.abc import Iterator
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
    ``tmp_path / "home"`` doesn't collide with this pre-created one.

    Sets BOTH ``HOME`` (POSIX ``Path.home()``) and ``USERPROFILE`` (Windows
    ``Path.home()`` reads ``%USERPROFILE%``, NOT ``$HOME``). Without the latter,
    the isolation silently no-op'd on Windows CI: provisioning tried to write the
    real ``C:\\Users\\runneradmin\\.claude`` and the 2.40.1 ``_guard_test_isolation``
    guard (rightly) refused — turning the runner red on windows-latest only."""
    home = tmp_path / "isolated-home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    # Windows GUI-app config lives under %APPDATA%/%LOCALAPPDATA%, NOT the home
    # dir — and the windows-latest runner ships VS Code, so its real
    # ``%APPDATA%\\Code\\User`` exists. Without pinning these too, the MCP-only
    # provisioners (VS Code, Claude Desktop) resolved to that live dir and the
    # "errors when not installed" tests saw the prereq as satisfied and never
    # raised. POSIX is unaffected (those paths hang off the isolated HOME/XDG).
    monkeypatch.setenv("APPDATA", str(home / "AppData" / "Roaming"))
    monkeypatch.setenv("LOCALAPPDATA", str(home / "AppData" / "Local"))


@pytest.fixture(autouse=True)
def _isolate_state_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Point XDG_STATE_HOME at a per-test temp dir for every test."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg-state"))


@pytest.fixture(autouse=True)
def _embed_off_by_default(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin the semantic backend (3.0.0) OFF for the default suite.

    The keyword-path tests (recall, relevance, dedup) were written before semantics
    and assert keyword behavior; with the optional ``[embed]`` extra installed they'd
    see semantic results and fail nondeterministically depending on the dev/CI env.
    Disabling embed by default makes every suite deterministic; the embed/vectorindex
    tests opt back in (``embed.set_backend`` / monkeypatching ``embed.available`` /
    ``delenv OMI_EMBED_DISABLE``), and ``embed.reset()`` clears the cached resolution
    so one test's backend can't leak into the next."""
    monkeypatch.setenv("OMI_EMBED_DISABLE", "1")
    from omind import embed

    embed.reset()
    yield None
    embed.reset()


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
def _isolate_codex_home(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drop ``CODEX_HOME`` so the Codex provisioner's ``codex_config_dir()`` falls
    back to the (isolated) HOME ``~/.codex`` instead of a developer's real Codex
    config — the same class of leak the HOME/CLAUDE_CONFIG_DIR isolation guards.
    A test exercising ``CODEX_HOME`` sets it itself after this fixture runs."""
    monkeypatch.delenv("CODEX_HOME", raising=False)


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
def _isolate_verify_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clear the verifier's env knobs so a developer/CI machine that runs with
    ``OMI_VERIFY_REQUIRE=1`` (or custom thresholds/allowlist) in settings.json
    can't leak that into the test process. Tests that exercise them set their own."""
    for var in (
        "OMI_VERIFY_REQUIRE",
        "OMI_VERIFY_ALWAYS_RELEVANT",
        "OMI_VERIFY_HIGH",
        "OMI_VERIFY_LOW",
        "OMI_VERIFY_MAX_RECLOSE",
    ):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture(autouse=True)
def _no_update_check(monkeypatch: pytest.MonkeyPatch) -> None:
    """Disable the GitHub version check in every test — no network, no flake.

    ``build_session_start_context`` now surfaces the update nudge, so without this
    every priming test would otherwise incur a (fail-open) network round-trip."""
    monkeypatch.setenv("OMIND_NO_UPDATE_CHECK", "1")


@pytest.fixture
def live_launchers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Treat every pinned omind launcher as live (``--version`` exits 0).

    Opt-in, never autouse: the probe (#377) is real behaviour, and a suite-wide
    stub would hide it. Use it where a test bakes a fake absolute launcher such
    as ``/usr/bin/omind`` and then runs ``doctor``, which probes pins and would
    otherwise report the fake as dead.
    """
    from omind import provision

    monkeypatch.setattr(provision, "_launcher_runs", lambda _exe: True)


@pytest.fixture(autouse=True)
def _git_bash_present(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Pin "Git Bash is installed" for every test (#425).

    On Windows, Claude Code hooks render for PowerShell when no Git Bash is
    found, so a test that pins ``_windows`` True would otherwise depend on
    whether the machine running it has Git for Windows. Tests of the
    no-Git-Bash form patch ``provision.git_bash_path`` or drop this variable.
    """
    bash = tmp_path / "git-bash" / "bash.exe"
    bash.parent.mkdir(parents=True, exist_ok=True)
    bash.write_text("", encoding="utf-8")
    monkeypatch.setenv("CLAUDE_CODE_GIT_BASH_PATH", str(bash))
