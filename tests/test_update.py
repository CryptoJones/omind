# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Aaron K. Clark
"""Tests for omind.update: version compare, cached check, nudge, self-update."""

from __future__ import annotations

import json
import sys
import urllib.error
from pathlib import Path

import pytest

from omind import update
from omind.update import (
    InstallInfo,
    UpdateStatus,
    _parse,
    check_for_update,
    self_update,
    update_command,
    update_nudge,
)


@pytest.fixture(autouse=True)
def isolate_cache_and_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(update, "state_dir", lambda: tmp_path)
    monkeypatch.delenv("OMIND_NO_UPDATE_CHECK", raising=False)


def _fixed_status(current: str, latest: str | None):
    def _cfu(*, force: bool = False, timeout: float = 2.0) -> UpdateStatus:
        return UpdateStatus(current, latest)

    return _cfu


def test_parse_versions() -> None:
    assert _parse("v2.37.0") == (2, 37, 0)
    assert _parse("2.37.0") == (2, 37, 0)
    assert _parse("2.37.0rc1") == (2, 37, 0)  # pre-release suffix ignored
    assert _parse("nightly") is None


@pytest.mark.parametrize(
    ("current", "latest", "available"),
    [
        ("2.36.0", "2.37.0", True),
        ("2.37.0", "2.37.0", False),
        ("2.37.0", "2.36.0", False),
        ("2.37.0", None, False),
        ("2.9.0", "2.10.0", True),  # numeric compare, not lexical
    ],
)
def test_status_available(current: str, latest: str | None, available: bool) -> None:
    assert UpdateStatus(current, latest).available is available


def test_fetch_takes_highest_across_release_and_tags(monkeypatch: pytest.MonkeyPatch) -> None:
    # A published Release lags the tags (the real-world case): the newest tag wins.
    def fake_get(url: str, timeout: float) -> object:
        if "releases/latest" in url:
            return {"tag_name": "v2.34.0"}  # stale published Release
        return [{"name": "v2.35.0"}, {"name": "v2.37.0"}, {"name": "not-a-version"}]

    monkeypatch.setattr(update, "_get_json", fake_get)
    assert update._fetch_latest(1.0) == "2.37.0"


def test_fetch_falls_back_to_tags_when_no_release(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = []

    def fake_get(url: str, timeout: float) -> object:
        seen.append(url)
        if "releases/latest" in url:
            raise urllib.error.URLError("404 - no releases")
        return [{"name": "v2.36.0"}, {"name": "v2.37.0"}]

    monkeypatch.setattr(update, "_get_json", fake_get)
    assert update._fetch_latest(1.0) == "2.37.0"
    assert any("/tags" in u for u in seen)


def test_fetch_uses_release_when_newer(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_get(url: str, t: float) -> object:
        if "releases/latest" in url:
            return {"tag_name": "v2.40.0"}
        return [{"name": "v2.37.0"}]

    monkeypatch.setattr(update, "_get_json", fake_get)
    assert update._fetch_latest(1.0) == "2.40.0"


def test_fetch_returns_none_on_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(url: str, t: float) -> object:
        raise OSError("offline")

    monkeypatch.setattr(update, "_get_json", boom)
    assert update._fetch_latest(1.0) is None


def test_check_caches_and_disable_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(update, "__version__", "2.36.0")
    hits = []
    monkeypatch.setattr(update, "_fetch_latest", lambda t: (hits.append(1), "2.37.0")[1])

    first = check_for_update()
    assert first.available and first.latest == "2.37.0"
    check_for_update()  # served from the day-cache — no new fetch
    assert len(hits) == 1
    check_for_update(force=True)  # force bypasses the cache
    assert len(hits) == 2

    monkeypatch.setenv("OMIND_NO_UPDATE_CHECK", "1")
    assert check_for_update().latest is None  # disabled → no network, unknown


def test_stale_cache_refetches(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    (tmp_path / "update-check.json").write_text(
        json.dumps({"checked_at": 0, "latest": "2.30.0"})  # epoch 0 = ancient
    )
    monkeypatch.setattr(update, "_fetch_latest", lambda t: "2.37.0")
    assert check_for_update().latest == "2.37.0"


def test_nudge(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(update, "check_for_update", _fixed_status("2.36.0", "2.37.0"))
    assert "2.37.0" in (update_nudge() or "")
    monkeypatch.setattr(update, "check_for_update", _fixed_status("2.37.0", "2.37.0"))
    assert update_nudge() is None

    def boom() -> UpdateStatus:
        raise RuntimeError("boom")

    monkeypatch.setattr(update, "check_for_update", boom)
    assert update_nudge() is None  # any failure is swallowed


def test_update_command_by_install(monkeypatch: pytest.MonkeyPatch) -> None:
    # SHA-pinned when ls-remote resolves the tag (a mutable tag must never be
    # the install pin); falls back to the tag ref when resolution fails.
    monkeypatch.setattr(update, "_resolve_tag_sha", lambda v, timeout=60.0: "abc123")
    ref = "git+https://github.com/CryptoJones/omind@abc123"
    uv = update_command(InstallInfo("uv-tool", "x"), "2.37.0")
    assert uv is not None and uv[:3] == ["uv", "tool", "install"] and ref in uv
    pip = update_command(InstallInfo("pip", "x"), "2.37.0")
    assert pip is not None and pip[1:3] == ["-m", "pip"] and ref in pip
    assert update_command(InstallInfo("editable", "/repo"), "2.37.0") is None


def test_update_command_falls_back_to_the_tag_when_the_sha_cannot_be_resolved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(update, "_resolve_tag_sha", lambda v, timeout=60.0: None)
    ref = "git+https://github.com/CryptoJones/omind@v2.37.0"
    uv = update_command(InstallInfo("uv-tool", "x"), "2.37.0")
    assert uv is not None and ref in uv


def test_self_update_check_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(update, "check_for_update", _fixed_status("2.36.0", "2.37.0"))
    out: list[str] = []
    assert self_update(check_only=True, log=out.append) == 0
    assert any("update available" in line for line in out)


class _Proc:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


def _is_canary(cmd: list[str], kwargs: dict[str, object]) -> bool:
    env = kwargs.get("env")
    return cmd[:3] == ["uv", "tool", "install"] and isinstance(env, dict) and "UV_TOOL_DIR" in env


def _updatable(monkeypatch: pytest.MonkeyPatch, *, windows: bool = False) -> None:
    """A uv-tool install with 2.37.0 available and every prerequisite present."""
    monkeypatch.setattr(update, "check_for_update", _fixed_status("2.36.0", "2.37.0"))
    monkeypatch.setattr(update, "detect_install", lambda: InstallInfo("uv-tool", "x"))
    monkeypatch.setattr(update, "_resolve_tag_sha", lambda v, timeout=60.0: "abc123")
    monkeypatch.setattr(update, "_is_windows", lambda: windows)
    monkeypatch.setattr(update.shutil, "which", lambda name: f"/usr/bin/{name}")


def test_self_update_runs_installer(monkeypatch: pytest.MonkeyPatch) -> None:
    _updatable(monkeypatch)
    ran: list[tuple[list[str], bool]] = []

    def fake_run(cmd: list[str], *args: object, **kwargs: object) -> _Proc:
        # monkeypatching update.subprocess patches the SHARED subprocess module,
        # so the trial install, the version probes, and the post-update heal all
        # land here too — collect all.
        ran.append((list(cmd), _is_canary(cmd, kwargs)))
        return _Proc(stdout="omind 2.37.0\n")

    monkeypatch.setattr(update.subprocess, "run", fake_run)
    assert self_update(log=lambda _m: None) == 0
    installs = [(cmd, canary) for cmd, canary in ran if cmd[:3] == ["uv", "tool", "install"]]
    # The throwaway install comes FIRST and never carries --force; only then is
    # the live environment replaced.
    assert [canary for _cmd, canary in installs] == [True, False]
    assert "--force" not in installs[0][0] and "--force" in installs[1][0]


def test_self_update_refuses_in_place_on_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    """The 9.4.0 -> 9.7.5 incident: on Windows this process runs from the tool
    environment, `uv tool install --force` deletes that environment and only then
    fails on the locked python.exe, and omind is gone. Refuse; touch nothing."""
    _updatable(monkeypatch, windows=True)
    monkeypatch.setattr(update, "tool_env_dir", lambda _i: Path("C:/uv/tools/omind"))
    monkeypatch.setattr(update, "_env_processes", lambda _env: ["pid 42 (omind.exe node)"])

    def no_install(cmd: list[str], *_a: object, **_k: object) -> _Proc:
        raise AssertionError(f"nothing may run on a refused update: {cmd}")

    monkeypatch.setattr(update.subprocess, "run", no_install)
    out: list[str] = []
    assert self_update(log=out.append) == 1
    text = "\n".join(out)
    assert "refusing to update" in text and "nothing was changed" in text
    # The way out is spelled out, including who is still holding the files.
    assert "uv tool install --force --from" in text and "pid 42" in text
    # A refused update is not an update: `--rollback` must not learn from it.
    assert not update._rollback_path().exists()


@pytest.mark.parametrize(
    ("install_rc", "version_out", "expected"),
    [
        (1, "", "does not install"),
        (0, "", "does not start"),  # builds, then dies on import
        (0, "omind 2.36.0\n", "does not run"),  # some OTHER omind answered
    ],
)
def test_self_update_refuses_a_release_that_fails_its_trial(
    monkeypatch: pytest.MonkeyPatch, install_rc: int, version_out: str, expected: str
) -> None:
    """A release that cannot install or start here is found out in a throwaway
    environment — not after the working one has been deleted."""
    _updatable(monkeypatch)

    def fake_run(cmd: list[str], *args: object, **kwargs: object) -> _Proc:
        if _is_canary(cmd, kwargs):
            return _Proc(returncode=install_rc, stderr="error: no wheel for this platform\n")
        if "--force" in cmd:
            raise AssertionError("the live install must not be touched after a failed trial")
        return _Proc(stdout=version_out)

    monkeypatch.setattr(update.subprocess, "run", fake_run)
    out: list[str] = []
    assert self_update(log=out.append) == 1
    assert any(expected in line for line in out)
    assert not update._rollback_path().exists()


@pytest.mark.parametrize("missing", ["uv", "git"])
def test_self_update_refuses_without_its_tools(
    monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    _updatable(monkeypatch)
    monkeypatch.setattr(
        update.shutil, "which", lambda name: None if name == missing else f"/usr/bin/{name}"
    )

    def no_install(cmd: list[str], *_a: object, **_k: object) -> _Proc:
        raise AssertionError(f"nothing may run without {missing}: {cmd}")

    monkeypatch.setattr(update.subprocess, "run", no_install)
    out: list[str] = []
    assert self_update(log=out.append) == 1
    assert any(f"`{missing}` is not on PATH" in line for line in out)


def test_self_update_reports_an_install_it_broke(monkeypatch: pytest.MonkeyPatch) -> None:
    """If the installer still dies half-way, say the install is broken and how to
    repair it — the old code printed an exit status and left a dead shim for the
    next hook to trip over."""
    _updatable(monkeypatch)
    replaced: list[bool] = []

    def fake_run(cmd: list[str], *args: object, **kwargs: object) -> _Proc:
        if "--force" in cmd:
            replaced.append(True)
            return _Proc(returncode=2)
        # Healthy until the live install is replaced; nothing answers afterwards.
        return _Proc(returncode=1) if replaced else _Proc(stdout="omind 2.37.0\n")

    monkeypatch.setattr(update.subprocess, "run", fake_run)
    out: list[str] = []
    assert self_update(log=out.append) == 2
    text = "\n".join(out)
    assert "BROKEN" in text and "uv tool install --force" in text
    assert "updated to" not in text


def test_rollback_is_preflighted_too(monkeypatch: pytest.MonkeyPatch) -> None:
    _updatable(monkeypatch, windows=True)
    monkeypatch.setattr(update, "_env_processes", lambda _env: [])
    update._record_rollback("2.35.0", "2.36.0", [])

    def no_install(cmd: list[str], *_a: object, **_k: object) -> _Proc:
        raise AssertionError(f"nothing may run on a refused rollback: {cmd}")

    monkeypatch.setattr(update.subprocess, "run", no_install)
    out: list[str] = []
    assert self_update(rollback=True, log=out.append) == 1
    assert any("refusing to roll back" in line for line in out)


def test_env_processes_ignores_its_own_launcher_chain(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """omind.exe -> Scripts/python.exe -> this process are all the CALLER; only an
    unrelated process in the environment (an `omind node`) is someone to close."""
    exe = str(tmp_path / "Scripts" / "python.exe")
    me = update.os.getpid()
    rows = [
        {"ProcessId": me, "ParentProcessId": 11, "ExecutablePath": exe, "CommandLine": "self"},
        {"ProcessId": 11, "ParentProcessId": 1, "ExecutablePath": exe, "CommandLine": "shim"},
        {"ProcessId": 42, "ParentProcessId": 1, "ExecutablePath": exe, "CommandLine": "omind node"},
        {"ProcessId": 43, "ParentProcessId": 1, "ExecutablePath": "C:/o.exe", "CommandLine": "x"},
    ]
    monkeypatch.setattr(update.subprocess, "run", lambda *a, **k: _Proc(stdout=json.dumps(rows)))
    assert update._env_processes(tmp_path) == ["pid 42 (omind node)"]
    # Informational only: a failed query must not turn into a crash.
    monkeypatch.setattr(update.subprocess, "run", lambda *a, **k: _Proc(stdout="not json"))
    assert update._env_processes(tmp_path) == []


def test_extras_are_read_from_the_located_tool_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """uv keeps tools under %APPDATA% on Windows (and $UV_TOOL_DIR anywhere), so
    the hard-coded XDG receipt path found nothing there and every update silently
    dropped `omind[embed]` — the very thing `installed_extras` exists to stop."""
    env = tmp_path / "AppData" / "Roaming" / "uv" / "tools" / "omind"
    package = env / "Lib" / "site-packages" / "omind"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (env / "uv-receipt.toml").write_text(
        '[tool]\nrequirements = [{ name = "omind", extras = ["embed"] }]\n', encoding="utf-8"
    )
    monkeypatch.setattr(update.Path, "home", classmethod(lambda _cls: tmp_path / "nowhere"))
    monkeypatch.setattr(update, "_resolve_tag_sha", lambda v, timeout=60.0: None)
    install = InstallInfo("uv-tool", str(package / "__init__.py"))
    assert update.tool_env_dir(install) == env.resolve()
    cmd = update_command(install, "9.9.9")
    assert cmd is not None and cmd[cmd.index("--from") + 1].startswith("omind[embed] @ git+")


def test_post_update_heal_re_enters_a_clean_interpreter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The heal must run the NEW code in a NEW process, never inline.

    `uv tool install --force` swaps the package under the running interpreter,
    so after it, modules imported at startup are the outgoing release while
    anything imported afterwards is the incoming one. Importing `provision`
    inline handed 9.1.1's code 8.10.1's `omind.filelock` out of `sys.modules`
    and the update ended on `module 'omind.filelock' has no attribute
    'exclusive'`. Assert we shell out instead of importing.
    """
    ran: list[list[str]] = []

    class _Result:
        returncode = 0
        stdout = "re-provisioned wiring (2 change(s)).\n"
        stderr = ""

    def fake_run(cmd: list[str], *args: object, **kwargs: object) -> _Result:
        ran.append(list(cmd))
        return _Result()

    monkeypatch.setattr(update.subprocess, "run", fake_run)

    def exploding_import(*_a: object, **_k: object) -> None:
        raise AssertionError("the heal must not import provision in this process")

    monkeypatch.setattr(update, "run_post_update_heal", exploding_import)
    out: list[str] = []
    update._post_update_heal(log=out.append)

    assert ran == [[sys.executable, "-m", "omind", "self-update", "--heal"]]
    # The child's report is the operator's report — forwarded, not swallowed.
    assert "re-provisioned wiring (2 change(s))." in out


def test_post_update_heal_reports_a_failing_child(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail-open, but never fail-silent: name the repair."""

    class _Result:
        returncode = 1
        stdout = ""
        stderr = "Traceback…\nProvisionError: settings.json is IMMUTABLE\n"

    monkeypatch.setattr(update.subprocess, "run", lambda *a, **k: _Result())
    out: list[str] = []
    update._post_update_heal(log=out.append)
    assert any("IMMUTABLE" in line and "omind setup" in line for line in out)


def test_heal_only_does_not_spawn_itself(monkeypatch: pytest.MonkeyPatch) -> None:
    """`--heal` is the bottom of the recursion: it heals, it does not re-enter."""

    def no_subprocess(*_a: object, **_k: object) -> None:
        raise AssertionError("`--heal` must not spawn another interpreter")

    monkeypatch.setattr(update.subprocess, "run", no_subprocess)
    healed: list[bool] = []
    monkeypatch.setattr(update, "run_post_update_heal", lambda **_k: healed.append(True))
    assert self_update(heal_only=True, log=lambda _m: None) == 0
    assert healed == [True]


def test_autoheal_opt_out_skips_the_subprocess(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMIND_NO_AUTOHEAL", "1")

    def no_subprocess(*_a: object, **_k: object) -> None:
        raise AssertionError("OMIND_NO_AUTOHEAL must skip the heal entirely")

    monkeypatch.setattr(update.subprocess, "run", no_subprocess)
    update._post_update_heal(log=lambda _m: None)


def test_self_update_up_to_date(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(update, "check_for_update", _fixed_status("2.37.0", "2.37.0"))
    out: list[str] = []
    assert self_update(log=out.append) == 0
    assert any("up to date" in line for line in out)


def test_update_command_preserves_installed_extras(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`uv tool install --from <git ref> omind` installs the BARE package, so an
    update silently dropped `omind[embed]` and disabled semantic relevance with no
    error — only a doctor warning nobody was watching for."""
    receipt = tmp_path / ".local" / "share" / "uv" / "tools" / "omind" / "uv-receipt.toml"
    receipt.parent.mkdir(parents=True)
    receipt.write_text(
        '[tool]\nrequirements = [{ name = "omind", extras = ["embed"], '
        'git = "https://github.com/CryptoJones/omind?rev=v8.2.1" }]\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(update.Path, "home", classmethod(lambda _cls: tmp_path))
    assert update.installed_extras() == ["embed"]
    cmd = update.update_command(update.InstallInfo("uv-tool", "x"), "8.2.2")
    assert cmd is not None
    assert cmd[cmd.index("--from") + 1].startswith("omind[embed] @ git+")


def test_update_command_without_extras_is_unchanged(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No receipt / no extras must leave the bare-ref behaviour exactly as it was."""
    monkeypatch.setattr(update.Path, "home", classmethod(lambda _cls: tmp_path))
    assert update.installed_extras() == []
    cmd = update.update_command(update.InstallInfo("uv-tool", "x"), "8.2.2")
    assert cmd is not None
    assert cmd[cmd.index("--from") + 1].startswith("git+https://")
