"""Every hook command omind writes into settings.json must survive a POSIX shell.

Claude Code can run hook strings through Git Bash on Windows, and bash eats the
backslash in an unquoted Windows path (-> ``C:Usersx``), so the hook dies with
"command not found" after every tool call. 10.0.1 fixed the script path and
missed the exe path; 10.0.2 fixed the exe path. These tests hand each written command to a real bash
for word-splitting and assert the executable and script paths arrive intact —
on every CI OS, including ``windows-latest`` with Git Bash.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path, PurePosixPath, PureWindowsPath

import pytest

from omind import provision
from omind.provision import Provisioner, SetupConfig

_BS = chr(92)  # built, not written: backslash escaping is what this file is about
_WIN_EXE = _BS.join(["C:", "Users", "ci", ".local", "bin", "omind.EXE"])
_WIN_SCRIPT = PureWindowsPath(_BS.join(["C:", "Users", "ci", ".claude", "hooks", "omi-enforce.py"]))


def _find_bash() -> str | None:
    """Git Bash on Windows (never WSL's System32 bash.exe), else whatever is on PATH."""
    if os.name == "nt":
        git = shutil.which("git")
        if git:
            candidate = Path(git).resolve().parents[1] / "bin" / "bash.exe"
            if candidate.exists():
                return str(candidate)
        found = shutil.which("bash")
        if found and "system32" not in found.lower():
            return found
        return None
    return shutil.which("bash")


@pytest.fixture
def bash() -> str:
    found = _find_bash()
    if found is None:
        if os.environ.get("CI"):
            pytest.fail("bash is required for hook shell-safety tests on CI")
        pytest.skip("no bash available")
    return found


def _words(bash: str, command: str) -> list[str]:
    """The argv bash would build for ``command`` (quote removal, no execution).

    The command goes in on stdin: argv and environment values are subject to
    MSYS path conversion on Windows, stdin is not.
    """
    out = subprocess.run(
        [bash, "-c", 'IFS= read -r line; eval "set -- $line"; printf "%s\n" "$@"'],
        input=command,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return out.splitlines()


def _commands(settings: Path) -> list[str]:
    """Every hook command in a settings.json that only omind has written to."""
    data = json.loads(settings.read_text(encoding="utf-8"))
    return [
        hook["command"]
        for entries in data["hooks"].values()
        for entry in entries
        for hook in entry.get("hooks", [])
    ]


def _install_everything(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, exe: str) -> Path:
    settings = tmp_path / "settings.json"
    monkeypatch.setattr(provision, "claude_settings_path", lambda: settings)
    monkeypatch.setattr(provision, "canonical_omind_exe", lambda: exe)
    monkeypatch.setattr(provision, "_resolve_python", lambda: "python")
    prov = Provisioner(SetupConfig(vault=tmp_path / "vault"), log=lambda _: None)
    prov.ensure_hooks_installed()
    prov.ensure_omi_guard_installed()
    return settings


def _check_paths(words: list[str], exe: str, script: str, *, must_exist: bool) -> None:
    for token in words:
        if token.endswith((".EXE", ".exe")) and "omind" in token.lower():
            assert token == exe, f"exe path mangled by the shell: {token!r}"
            assert (not must_exist) or Path(token).exists()
        if token.endswith("omi-enforce.py"):
            assert token == script, f"script path mangled by the shell: {token!r}"
    # POSIX setup wires the omi-guard.sh adapters where Windows calls omind directly.
    assert words[0] in (exe, "python") or words[0].endswith(".sh"), (
        f"argv[0] not intact: {words[0]!r}"
    )


def test_hook_commands_survive_bash_with_windows_style_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bash: str
) -> None:
    """Runs everywhere: a Windows-shaped exe and script path, tokenised by real bash."""
    monkeypatch.setattr(provision, "_enforce_hook_dest", lambda: _WIN_SCRIPT)
    settings = _install_everything(tmp_path, monkeypatch, _WIN_EXE)

    commands = _commands(settings)
    assert len(commands) >= 5  # PostToolUse x2, Stop, SessionStart, guard adapter, preflight
    for cmd in commands:
        _check_paths(_words(bash, cmd), _WIN_EXE, str(_WIN_SCRIPT), must_exist=False)


def test_hook_commands_resolve_to_real_files_under_bash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bash: str
) -> None:
    """Real paths of this machine (backslashes on windows-latest): they must exist
    after bash has had its say, i.e. the command would actually launch."""
    exe = sys.executable
    script = tmp_path / "hooks" / "omi-enforce.py"
    script.parent.mkdir()
    script.write_text("", encoding="utf-8")
    monkeypatch.setattr(provision, "_enforce_hook_dest", lambda: script)
    settings = _install_everything(tmp_path, monkeypatch, exe)

    seen_exe = seen_script = False
    for cmd in _commands(settings):
        words = _words(bash, cmd)
        assert words[0] in (exe, "python") or words[0].endswith(".sh")
        if words[0] == exe:
            seen_exe = True
            assert Path(words[0]).exists()
        for token in words:
            if token.endswith("omi-enforce.py"):
                seen_script = True
                assert Path(token) == script and Path(token).exists()
    assert seen_exe and seen_script


def test_helper_notices_an_unquoted_windows_path(bash: str) -> None:
    """Guard the guard: prove the bash step really does mangle an unquoted path."""
    assert _words(bash, f"{_WIN_EXE} hook Stop")[0] != _WIN_EXE
    assert _words(bash, f'"{_WIN_EXE}" hook Stop')[0] == _WIN_EXE
    assert str(PurePosixPath("/x")) == "/x"
