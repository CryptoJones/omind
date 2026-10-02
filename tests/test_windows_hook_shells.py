"""Run each harness's rendered Windows hook command through the real shell (#425).

The unit tests in test_agents.py pin the rendered STRINGS. These run them: on
a real Windows box (the windows-latest CI runners) every hook command is
spawned the way its harness spawns it, against an interpreter that lives under
a directory with a space in its name, and the argv that arrives at
``python -m omind`` must be exactly the argv the command meant.

The interpreter is a real venv's ``python.exe`` under ``...\\J D\\``. A stub
``omind`` package on ``PYTHONPATH`` stands in for the real one and prints its
argv as JSON, so nothing here touches a vault.

Spawn paths:

- ``cmd /c`` as Go runs it (agy 1.2.14, pool >= 1.0.16 are Go programs calling
  ``exec.Command("cmd", "/c", command)``). Go escapes that argument by the MSVC
  rules, which is what :func:`subprocess.list2cmdline` produces too; when the
  Go toolchain is on PATH the test also builds and runs a real Go spawner.
- ``cmd /d /s /c "<command>"`` verbatim (Node's ``shell: true``), so the
  rendering does not depend on which of the two a harness uses.
- ``powershell.exe -NoProfile -NonInteractive -Command`` (and ``pwsh`` when
  present): Gemini, Codex, and Claude Code without Git Bash.
- Git Bash ``bash -c``: Claude Code's default on Windows.

Exit codes (#425 review): a guard deny exits 2 for Claude Code, and
``powershell -Command`` reports any native exit code other than 0 as 1. So
:func:`test_powershell_hooks_hand_back_exit_2` runs a stub that exits 2 through
each PowerShell harness's exact spawn and asserts 2 arrives. It needs only a
PowerShell, so it also runs off Windows wherever ``pwsh`` is installed.

These are simulated harness spawners (the argv each harness's source builds),
not real agy, pool, Claude Code, Codex or Gemini processes.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from omind import agents, provision
from omind.provision import SetupConfig

_WINDOWS = sys.platform == "win32"
windows_only = pytest.mark.skipif(not _WINDOWS, reason="spawns real Windows shells")

#: The stub prints its argv, then exits with ``OMIND_STUB_EXIT`` (default 0).
_STUB_MAIN = (
    "import json, os, sys\n"
    "print(json.dumps(sys.argv[1:]))\n"
    "sys.exit(int(os.environ.get('OMIND_STUB_EXIT', '0')))\n"
)

_GO_SPAWNER = """package main

import (
\t"os"
\t"os/exec"
)

func main() {
\tcmd := exec.Command("cmd", "/c", os.Args[1])
\tcmd.Stdout = os.Stdout
\tcmd.Stderr = os.Stderr
\tif err := cmd.Run(); err != nil {
\t\tos.Exit(1)
\t}
}
"""


class Rig:
    """A spaced interpreter, a spaced vault and a stub omind package."""

    def __init__(self, root: Path) -> None:
        self.root = root
        venv = root / "venv"
        self.python = venv / "Scripts" / "python.exe" if _WINDOWS else venv / "bin" / "python"
        self.vault = root / "Obsidian Vault"
        self.pkg = root / "stub"
        self.go_spawner: Path | None = None

    @property
    def env(self) -> dict[str, str]:
        env = dict(os.environ)
        env["PYTHONPATH"] = str(self.pkg)
        return env


@pytest.fixture(scope="module")
def rig(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Rig]:
    root = tmp_path_factory.mktemp("hookshell") / "J D"
    root.mkdir()
    r = Rig(root)
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(root / "venv")], check=True)
    (r.vault / "OMI").mkdir(parents=True)
    (r.pkg / "omind").mkdir(parents=True)
    (r.pkg / "omind" / "__init__.py").write_text("", encoding="utf-8")
    (r.pkg / "omind" / "__main__.py").write_text(_STUB_MAIN, encoding="utf-8")
    go = shutil.which("go") if _WINDOWS else None
    if go:
        src = root.parent / "spawner.go"
        src.write_text(_GO_SPAWNER, encoding="utf-8")
        exe = root.parent / "spawner.exe"
        built = subprocess.run(
            [go, "build", "-o", str(exe), str(src)], capture_output=True, text=True
        )
        if built.returncode == 0:
            r.go_spawner = exe
    yield r


@pytest.fixture
def rendered(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> Rig:
    """Render hooks with the rig's spaced interpreter pinned, as Windows does
    (off Windows too, so the PowerShell forms can run under ``pwsh``)."""
    monkeypatch.setattr(provision, "_windows", lambda: True)
    monkeypatch.setattr(provision, "canonical_omind_argv", lambda: [str(rig.python), "-m", "omind"])
    return rig


def _config(rig: Rig, agent: str) -> SetupConfig:
    return SetupConfig(vault=rig.vault, folder="OMI", agent=agent)  # type: ignore[arg-type]


def _quiet(_msg: str) -> None:
    pass


def _intended_argv(command: str) -> list[str]:
    """What *command* means after ``-m omind``: its tail split on whitespace,
    honouring the single and double quotes the renderers use, no escapes. A
    ``''`` inside a single-quoted word is a PowerShell literal's escaped ``'``
    (``O''Brien``); no other renderer puts one there."""
    tail = command.split(" -m omind ", 1)[1].removesuffix(provision.POWERSHELL_EXIT_SUFFIX)
    words: list[str] = []
    i, n = 0, len(tail)
    while i < n:
        if tail[i].isspace():
            i += 1
            continue
        word: list[str] = []
        while i < n and not tail[i].isspace():
            quote = tail[i]
            if quote not in "'\"":
                word.append(quote)
                i += 1
                continue
            j = i + 1
            while True:
                k = tail.index(quote, j)
                if quote == "'" and tail[k + 1 : k + 2] == "'":
                    word.append(tail[j : k + 1])
                    j = k + 2
                    continue
                word.append(tail[j:k])
                i = k + 1
                break
        words.append("".join(word))
    return words


def test_intended_argv_reads_powershell_and_cmd_quoting() -> None:
    """The oracle itself: PowerShell's doubled ``'`` and cmd's double quotes."""
    ps = r"& 'C:\py.exe' -m omind hook X --vault 'C:\Users\O''Brien\V' --folder 'OMI'"
    assert _intended_argv(provision.powershell_hook(ps)) == [
        "hook",
        "X",
        "--vault",
        r"C:\Users\O'Brien\V",
        "--folder",
        "OMI",
    ]
    cmd = r'C:\py.exe -m omind hook X --vault "C:\J D\V" --folder OMI'
    assert _intended_argv(cmd) == ["hook", "X", "--vault", r"C:\J D\V", "--folder", "OMI"]


def _same(arrived: str, intended: str) -> bool:
    """Equal, or (for paths, which ``cmd`` hooks pass by 8.3 short name) the
    same file."""
    if arrived == intended:
        return True
    try:
        return os.path.samefile(arrived, intended)
    except OSError:
        return False


def _assert_arrives(result: subprocess.CompletedProcess[str], command: str) -> None:
    assert result.returncode == 0, (command, result.stdout, result.stderr)
    lines = [ln for ln in result.stdout.splitlines() if ln.strip()]
    assert lines, (command, result.stderr)
    arrived = json.loads(lines[-1])
    intended = _intended_argv(command)
    assert len(arrived) == len(intended), (command, arrived, intended)
    for got, want in zip(arrived, intended, strict=True):
        assert _same(got, want), (command, arrived, intended)


def _run(argv: list[str] | str, rig: Rig, exit_code: int = 0) -> subprocess.CompletedProcess[str]:
    env = {**rig.env, "OMIND_STUB_EXIT": str(exit_code)}
    return subprocess.run(argv, capture_output=True, text=True, env=env, timeout=60, check=False)


def _cmd_go_escaped(command: str, rig: Rig) -> subprocess.CompletedProcess[str]:
    # list2cmdline == Go's syscall.EscapeArg for these strings.
    return _run(["cmd", "/c", command], rig)


def _cmd_verbatim(command: str, rig: Rig) -> subprocess.CompletedProcess[str]:
    # A str is handed to CreateProcess untouched on Windows.
    return _run(f'cmd.exe /d /s /c "{command}"', rig)


def _cmd_real_go(command: str, rig: Rig) -> subprocess.CompletedProcess[str]:
    if rig.go_spawner is None:
        pytest.skip("no Go toolchain on PATH to build the real exec.Command spawner")
    return _run([str(rig.go_spawner), command], rig)


_CMD_SPAWNERS = {
    "go-escaped": _cmd_go_escaped,
    "verbatim": _cmd_verbatim,
    "real-go": _cmd_real_go,
}


def _agy_commands(rig: Rig) -> list[str]:
    block = agents.AgyProvisioner(_config(rig, "agy"), log=_quiet).desired_hook_block()
    return [h["command"] for groups in block.values() for g in groups for h in g.get("hooks", [g])]


def _pool_commands(rig: Rig) -> list[str]:
    pool = agents.PoolsideProvisioner(_config(rig, "poolside"), log=_quiet)
    return [pool._hook_command(e) for e in pool.HOOK_EVENTS]


@windows_only
@pytest.mark.parametrize("spawn", list(_CMD_SPAWNERS), ids=list(_CMD_SPAWNERS))
@pytest.mark.parametrize("harness", ["agy", "poolside"])
def test_cmd_hooks_reach_omind_intact(rendered: Rig, harness: str, spawn: str) -> None:
    commands = _agy_commands(rendered) if harness == "agy" else _pool_commands(rendered)
    assert commands
    for command in commands:
        assert '"' not in command, f"8.3 short names were expected here: {command}"
        _assert_arrives(_CMD_SPAWNERS[spawn](command, rendered), command)


@windows_only
@pytest.mark.xfail(
    strict=True,
    reason="known limitation (#425): with no 8.3 short name cmd_quote falls back to "
    "double quotes, which Go's cmd /c escaping breaks; doctor warns about it",
)
@pytest.mark.parametrize("spawn", ["go-escaped", "real-go"])
@pytest.mark.parametrize("harness", ["agy", "poolside"])
def test_cmd_quote_fallback_breaks_under_go(
    rendered: Rig, harness: str, spawn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The double-quote fallback (8.3 names disabled, ReFS Dev Drive, or a path
    not created yet) through a Go-style spawner. Strict xfail keeps the
    limitation visible: if this starts passing, the docs and doctor are wrong."""
    monkeypatch.setattr(provision, "windows_short_path", lambda _path: None)
    commands = _agy_commands(rendered) if harness == "agy" else _pool_commands(rendered)
    assert any('"' in c for c in commands), commands
    for command in commands:
        _assert_arrives(_CMD_SPAWNERS[spawn](command, rendered), command)


def _powershells() -> list[str]:
    """The PowerShells on this box, for the exit-code test (any OS)."""
    return [p for p in ("powershell.exe", "pwsh") if shutil.which(p)] or ["<none>"]


def _claude_commands(rig: Rig) -> list[str]:
    """Every omind command hook Claude Code runs, exactly as installed."""
    claude = provision.Provisioner(_config(rig, "claude"), log=_quiet)
    raw = [
        *(claude._hook_command(e) for e in provision.HANDLED_EVENTS),
        *claude._omi_guard_commands(),
    ]
    return [claude._claude_hook(c)["command"] for c in raw]


def _powershell_hook_commands(rig: Rig) -> dict[str, list[str]]:
    """Each PowerShell harness's omind hook commands, exactly as installed."""
    gemini = agents.GeminiProvisioner(_config(rig, "gemini"), log=_quiet)
    codex = agents.CodexProvisioner(_config(rig, "codex"), log=_quiet)
    return {
        "gemini": [gemini._guard_hook_group()["hooks"][0]["command"]],
        "codex": [
            codex._guard_hook_group()["hooks"][0]["command"],
            codex._hook_line(codex._omind_hook_command("SessionStart")),
            codex._hook_line(codex._omind_hook_command("PostToolUse")),
        ],
        "claude": _claude_commands(rig),
    }


def _is_pwsh(shell: str) -> bool:
    return Path(shell).name.lower().startswith("pwsh")


def _claude_spawn(shell: str, command: str) -> list[str]:
    # Claude Code 2.1.287 (bundle): Sae(MA(), Isn(command)), Isn = [...XG(),
    # "-Command", command], XG = [-NoProfile, -NonInteractive, -ExecutionPolicy,
    # Bypass]. Nothing wraps the command. Exit 2 blocks; any other non-zero is
    # a non-blocking error.
    return [
        shell,
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-Command",
        command,
    ]


def _codex_spawn(shell: str, command: str) -> list[str]:
    # Codex 0.154.0: Shell::derive_exec_args (codex-rs/core/src/shell.rs) gives
    # [pwsh, -NoProfile, -Command]; build_command
    # (codex-rs/hooks/src/engine/command_runner.rs) appends the command as one
    # argument, unwrapped.
    return [shell, "-NoProfile", "-Command", command]


def _gemini_spawn(shell: str, command: str) -> list[str]:
    # gemini-cli 0.46.0: getShellConfiguration gives `pwsh -NoProfile -Command`
    # (`powershell.exe -NoProfile -NonInteractive -Command`), and
    # executeCommandHook appends the exit-code guard itself.
    args = ["-NoProfile"] if _is_pwsh(shell) else ["-NoProfile", "-NonInteractive"]
    wrapped = f"{command}; if ($LASTEXITCODE -ne 0) {{ exit $LASTEXITCODE }}"
    return [shell, *args, "-Command", wrapped]


#: How each PowerShell harness spawns a hook command, from its own source.
_PS_SPAWN: dict[str, Callable[[str, str], list[str]]] = {
    "claude": _claude_spawn,
    "codex": _codex_spawn,
    "gemini": _gemini_spawn,
}


@windows_only
@pytest.mark.parametrize("shell", _powershells())
def test_powershell_hooks_reach_omind_intact(
    rendered: Rig, shell: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Gemini, Codex, and Claude Code on a box without Git Bash."""
    if shell == "<none>":
        pytest.skip("no PowerShell on this box")
    monkeypatch.setattr(provision, "git_bash_path", lambda: None)
    for harness, commands in _powershell_hook_commands(rendered).items():
        for command in commands:
            _assert_arrives(_run(_PS_SPAWN[harness](shell, command), rendered), command)


@pytest.mark.parametrize("harness", list(_PS_SPAWN))
@pytest.mark.parametrize("shell", _powershells())
def test_powershell_hooks_hand_back_exit_2(
    rendered: Rig, shell: str, harness: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A guard deny's exit 2 must reach the harness as 2, not PowerShell's 1.

    Claude Code blocks only on exit 2, so before the fix every guard and gate
    deny on a box without Git Bash let the tool call through (#425 review).
    """
    if shell == "<none>":
        pytest.skip("no PowerShell (pwsh or powershell.exe) on this box")
    monkeypatch.setattr(provision, "git_bash_path", lambda: None)
    for command in _powershell_hook_commands(rendered)[harness]:
        assert command.endswith(provision.POWERSHELL_EXIT_SUFFIX), command
        for code in (2, 0):
            result = _run(_PS_SPAWN[harness](shell, command), rendered, exit_code=code)
            assert result.returncode == code, (harness, command, result.stdout, result.stderr)


@windows_only
def test_claude_hooks_reach_omind_intact_under_git_bash(
    rendered: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Claude Code's default Windows shell (Git Bash), with Git Bash present."""
    # The conftest pins a placeholder bash.exe; look for the real one.
    monkeypatch.delenv("CLAUDE_CODE_GIT_BASH_PATH", raising=False)
    bash = provision.git_bash_path()
    if bash is None:
        pytest.skip("no Git Bash on this Windows box")
    for command in _claude_commands(rendered):
        assert command.startswith('"'), command  # the bash form, not PowerShell's
        _assert_arrives(_run([bash, "-c", command], rendered), command)
