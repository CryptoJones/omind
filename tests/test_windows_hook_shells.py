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
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from omind import agents, provision
from omind.provision import SetupConfig

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="spawns real Windows shells")

_STUB_MAIN = "import json, sys\nprint(json.dumps(sys.argv[1:]))\n"

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
        self.python = root / "venv" / "Scripts" / "python.exe"
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
    go = shutil.which("go")
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
    """Render hooks with the rig's spaced interpreter pinned."""
    monkeypatch.setattr(provision, "canonical_omind_argv", lambda: [str(rig.python), "-m", "omind"])
    return rig


def _config(rig: Rig, agent: str) -> SetupConfig:
    return SetupConfig(vault=rig.vault, folder="OMI", agent=agent)  # type: ignore[arg-type]


def _quiet(_msg: str) -> None:
    pass


def _intended_argv(command: str) -> list[str]:
    """What *command* means after ``-m omind``: its tail split on whitespace,
    honouring the single and double quotes the renderers use, no escapes."""
    tail = command.split(" -m omind ", 1)[1]
    lex = shlex.shlex(tail, posix=True)
    lex.whitespace_split = True
    lex.escape = ""
    return list(lex)


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


def _run(argv: list[str] | str, rig: Rig) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv, capture_output=True, text=True, env=rig.env, timeout=60, check=False
    )


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


@pytest.mark.parametrize("spawn", list(_CMD_SPAWNERS), ids=list(_CMD_SPAWNERS))
@pytest.mark.parametrize("harness", ["agy", "poolside"])
def test_cmd_hooks_reach_omind_intact(rendered: Rig, harness: str, spawn: str) -> None:
    commands = _agy_commands(rendered) if harness == "agy" else _pool_commands(rendered)
    assert commands
    for command in commands:
        assert '"' not in command, f"8.3 short names were expected here: {command}"
        _assert_arrives(_CMD_SPAWNERS[spawn](command, rendered), command)


def _powershells() -> list[str]:
    found = [p for p in ("powershell.exe", "pwsh") if shutil.which(p)]
    return found or ["powershell.exe"]


def _claude_commands(rig: Rig) -> list[str]:
    claude = provision.Provisioner(_config(rig, "claude"), log=_quiet)
    return [claude._hook_command(e) for e in provision.HANDLED_EVENTS]


@pytest.mark.parametrize("shell", _powershells())
def test_powershell_hooks_reach_omind_intact(
    rendered: Rig, shell: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Gemini, Codex, and Claude Code on a box without Git Bash."""
    monkeypatch.setattr(provision, "git_bash_path", lambda: None)
    gemini = agents.GeminiProvisioner(_config(rendered, "gemini"), log=_quiet)
    codex = agents.CodexProvisioner(_config(rendered, "codex"), log=_quiet)
    commands = [
        gemini._guard_hook_group()["hooks"][0]["command"],
        codex._guard_hook_group()["hooks"][0]["command"],
        codex._omind_hook_command("SessionStart"),
        *_claude_commands(rendered),
    ]
    for command in commands:
        result = _run([shell, "-NoProfile", "-NonInteractive", "-Command", command], rendered)
        _assert_arrives(result, command)


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
