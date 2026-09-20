# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Aaron K. Clark
"""Check the running omind against the latest GitHub release, and self-update.

The MCP server (``omind node``) runs from a pinned install (a ``uv tool`` wheel
today), so a tagged release on the forge does NOT reach the running server on its
own — it stays on whatever was installed until someone reinstalls. This module
closes that gap with a *check + notify*, plus an explicit ``omind self-update``:

  * :func:`check_for_update` — cached once/day in ``state_dir``, fail-open, never
    raises. Compares ``omind.__version__`` to the latest version on GitHub.
  * ``omind doctor`` surfaces it as a line; ``omind node`` prints a one-line
    stderr nudge on start (cached, never blocks the server, never touches stdout
    — that is the MCP protocol channel).
  * :func:`self_update` — the explicit updater (``omind self-update``): detects
    how omind is installed and reinstalls the latest tag from the public GitHub
    repo.

Design choice: **notify, do not silently auto-apply.** omind backs the OMI
memory for every agent on the box; a bad release auto-deployed everywhere is the
failure we refuse to risk. Set ``OMIND_NO_UPDATE_CHECK=1`` to disable the
network check entirely (offline/privacy).
"""

from __future__ import annotations

import contextlib
import importlib.util
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from omind import __version__
from omind.paths import state_dir

#: The public mirror whose releases/tags are the version source of truth for the
#: check. Codeberg is the canonical push target; GitHub is the public read API
#: (no auth for public repos), and the channel the user asked the check to use.
GITHUB_REPO = "CryptoJones/omind"
_RELEASES_API = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
_TAGS_API = f"https://api.github.com/repos/{GITHUB_REPO}/tags?per_page=100"
_CHECK_INTERVAL = 86_400  # seconds: check at most once a day
_CACHE_NAME = "update-check.json"
_DISABLE_ENV = "OMIND_NO_UPDATE_CHECK"
_HTTP_TIMEOUT = 2.0
_HEADERS = {"User-Agent": f"omind/{__version__}", "Accept": "application/vnd.github+json"}


def _parse(version: str) -> tuple[int, ...] | None:
    """``"v2.37.0"`` / ``"2.37.0"`` -> ``(2, 37, 0)``; None if not X.Y.Z."""
    m = re.match(r"v?(\d+)\.(\d+)\.(\d+)", version.strip())
    return tuple(int(g) for g in m.groups()) if m else None


@dataclass(frozen=True)
class UpdateStatus:
    """The installed version vs. the latest known on GitHub."""

    current: str
    latest: str | None  # None = unknown (offline, rate-limited, disabled)

    @property
    def available(self) -> bool:
        cur, lat = _parse(self.current), _parse(self.latest or "")
        return cur is not None and lat is not None and lat > cur


def _get_json(url: str, timeout: float) -> object:
    request = urllib.request.Request(url, headers=_HEADERS)  # fixed https host
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _is_release_tag(name: str) -> bool:
    """A clean ``vX.Y.Z`` release tag — excludes pre-release / suffixed tags."""
    return re.fullmatch(r"v?\d+\.\d+\.\d+", name.strip()) is not None


def _fetch_latest(timeout: float) -> str | None:
    """Highest release version on GitHub, or None on any failure.

    Takes the MAX across the published Release marker AND the git tags: a pushed
    tag does not create a Release object, so on a repo whose Releases lag its tags
    (or has none), the newest *tag* is the real latest. Preferring
    ``releases/latest`` alone would report a stale version. Suffixed/pre-release
    tags are ignored. Each source is independently fail-open.
    """
    versions: list[str] = []
    try:
        data = _get_json(_RELEASES_API, timeout)
        if isinstance(data, dict) and _is_release_tag(str(data.get("tag_name") or "")):
            versions.append(str(data["tag_name"]).strip().lstrip("vV"))
    except (urllib.error.URLError, OSError, ValueError):
        pass
    try:
        tags = _get_json(_TAGS_API, timeout)
        if isinstance(tags, list):
            versions += [
                str(t["name"]).strip().lstrip("vV")
                for t in tags
                if isinstance(t, dict) and _is_release_tag(str(t.get("name", "")))
            ]
    except (urllib.error.URLError, OSError, ValueError):
        pass
    if not versions:
        return None
    return max(versions, key=lambda v: _parse(v) or ())


def _cache_path() -> Path:
    return state_dir() / _CACHE_NAME


def _read_cache(path: Path) -> tuple[bool, str | None]:
    """``(is_fresh, latest)``. A fresh cache short-circuits the network — even a
    fresh *failure* (latest=None) is honored, so a persistent outage is not
    re-hammered every call."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        fresh = time.time() - float(data["checked_at"]) < _CHECK_INTERVAL
        latest = data.get("latest")
        return fresh, (str(latest) if latest else None)
    except (OSError, ValueError, KeyError, TypeError):
        return False, None


def _write_cache(path: Path, latest: str | None) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"checked_at": time.time(), "latest": latest}), encoding="utf-8"
        )
    except OSError:
        pass


def check_for_update(*, force: bool = False, timeout: float = _HTTP_TIMEOUT) -> UpdateStatus:
    """Latest-version check, cached once a day in ``state_dir``. Fail-open.

    ``OMIND_NO_UPDATE_CHECK`` (or any network failure) yields ``latest=None``,
    treated as "unknown / up to date". ``force=True`` bypasses the cache.
    """
    current = __version__
    # The env var disables the PASSIVE nudge (privacy); an explicit
    # `omind self-update` (force=True) must still be able to check, or the
    # documented opt-out silently breaks self-update with a misleading "offline".
    if os.environ.get(_DISABLE_ENV) and not force:
        return UpdateStatus(current, None)
    if not force:
        fresh, latest = _read_cache(_cache_path())
        if fresh:
            return UpdateStatus(current, latest)
    latest = _fetch_latest(timeout)
    _write_cache(_cache_path(), latest)
    return UpdateStatus(current, latest)


def update_nudge() -> str | None:
    """One-line "newer version available" message for doctor/node, or None.

    Fully defensive: any failure yields None so it can never break a caller (the
    MCP server start path must not be wedged by a version check)."""
    try:
        status = check_for_update()
    except Exception:
        return None
    if not status.available:
        return None
    return (
        f"omind {status.latest} is available (you're on {status.current}) — "
        "run `omind self-update` to upgrade."
    )


@dataclass(frozen=True)
class InstallInfo:
    method: str  # "uv-tool" | "pip" | "editable" | "unknown"
    detail: str = ""


def detect_install() -> InstallInfo:
    """How the running omind is installed — picks the right reinstall command."""
    import omind

    loc = str(getattr(omind, "__file__", "") or "")
    posix = loc.replace("\\", "/")
    # The receipt catches a relocated tool dir ($UV_TOOL_DIR), which the path
    # heuristic alone filed under "pip" — and pip cannot update a uv tool env.
    if "uv/tools/omind" in posix or tool_env_dir(InstallInfo("uv-tool", loc)) is not None:
        return InstallInfo("uv-tool", loc)
    repo = Path(loc).resolve().parent.parent.parent  # …/src/omind/__init__.py -> repo
    if loc and (repo / "pyproject.toml").is_file() and (repo / ".git").exists():
        return InstallInfo("editable", str(repo))
    if "site-packages" in posix:
        return InstallInfo("pip", loc)
    return InstallInfo("unknown", loc)


_RECEIPT_NAME = "uv-receipt.toml"


def tool_env_dir(install: InstallInfo) -> Path | None:
    """The `uv tool` environment omind runs from, or None when it can't be found.

    Found by walking up from the package to the directory holding uv's receipt,
    not assumed: uv keeps tools under ``%APPDATA%\\uv\\tools`` on Windows and
    wherever ``$UV_TOOL_DIR`` points anywhere else."""
    if install.method != "uv-tool" or not install.detail:
        return None
    try:
        for parent in Path(install.detail).resolve().parents:
            if (parent / _RECEIPT_NAME).is_file():
                return parent
    except OSError:
        pass
    return None


def installed_extras(env: Path | None = None) -> list[str]:
    """Extras the current `uv tool` install was created with (``[]`` if none).

    ``uv tool install --force --from <ref> omind`` installs the BARE package, so
    any extra the user chose is silently dropped on update. That quietly disabled
    semantic relevance on a box running ``omind[embed]``: search fell back to the
    keyword path with no error, only a doctor warning nobody was watching for. uv
    records the original request in its receipt, so read the extras back and
    reinstate them. Fail-open — no receipt, no extras, behaviour unchanged.

    ``env`` is the located tool environment (:func:`tool_env_dir`); the XDG path
    is only the fallback, because it does not exist on Windows — where the extras
    were therefore dropped on every update.
    """
    receipt = Path.home() / ".local" / "share" / "uv" / "tools" / "omind" / _RECEIPT_NAME
    if env is not None and (env / _RECEIPT_NAME).is_file():
        receipt = env / _RECEIPT_NAME
    try:
        # tomlkit, not tomllib: the latter is 3.11+ and this project floors at
        # 3.10. tomlkit is already a runtime dependency.
        import tomlkit

        data = tomlkit.parse(receipt.read_text(encoding="utf-8"))
    except (OSError, ValueError, ModuleNotFoundError):
        return []
    tool = data.get("tool")
    if not isinstance(tool, dict):
        return []
    requirements = tool.get("requirements")
    if not isinstance(requirements, list):
        return []
    for entry in requirements:
        if isinstance(entry, dict) and entry.get("name") == "omind":
            extras = entry.get("extras")
            if isinstance(extras, list):
                return [str(e) for e in extras if isinstance(e, str)]
    return []


def _resolve_tag_sha(version: str, timeout: float = 60.0) -> str | None:
    """The commit a release tag points at, via ``git ls-remote`` over HTTPS.

    Installs pin the SHA, not the mutable tag: a tag can be moved or forged,
    and a tag-pinned ref let a moved tag swap the code every fleet machine
    installs (2026-08-27 review). Prefers the peeled ``^{}`` commit of an
    annotated tag. ``None`` when resolution fails — the caller falls back to
    the tag ref rather than refusing updates offline."""
    try:
        result = subprocess.run(
            ["git", "ls-remote", f"https://github.com/{GITHUB_REPO}", f"refs/tags/v{version}"],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    peeled: str | None = None
    plain: str | None = None
    for line in result.stdout.splitlines():
        sha, _, ref = line.partition("\t")
        ref = ref.strip()
        if ref == f"refs/tags/v{version}^{{}}":
            peeled = sha.strip() or None
        elif ref == f"refs/tags/v{version}":
            plain = sha.strip() or None
    return peeled or plain


def update_command(install: InstallInfo, version: str) -> list[str] | None:
    """The argv that installs ``version``, or None when it can't be automated.

    The ref is pinned to the resolved commit SHA when ``git ls-remote`` can
    resolve it (see :func:`_resolve_tag_sha`); on resolution failure it falls
    back to the tag ref so an offline-but-cached install path still works."""
    ref = f"git+https://github.com/{GITHUB_REPO}@v{version}"
    sha = _resolve_tag_sha(version)
    if sha:
        ref = f"git+https://github.com/{GITHUB_REPO}@{sha}"
    if install.method == "uv-tool":
        extras = installed_extras(tool_env_dir(install))
        # The PEP 508 `omind[embed] @ git+…` form, so uv keeps the extras it was
        # originally installed with instead of silently downgrading to bare.
        spec = f"omind[{','.join(extras)}] @ {ref}" if extras else ref
        return ["uv", "tool", "install", "--force", "--from", spec, "omind"]
    if install.method == "pip":
        return [sys.executable, "-m", "pip", "install", "--upgrade", "--force-reinstall", ref]
    return None  # editable -> git pull; unknown -> manual


def _is_windows() -> bool:
    return os.name == "nt"


def _shell_join(cmd: list[str]) -> str:
    """``cmd`` as one copy-pasteable line — the extras spec contains spaces."""
    return subprocess.list2cmdline(cmd) if _is_windows() else shlex.join(cmd)


def _env_processes(env: Path) -> list[str]:
    """Windows: ``"pid 1234 (omind.exe node …)"`` for every OTHER process running
    out of ``env``. Informational and fail-open — ``[]`` when the query fails."""
    script = (
        "Get-CimInstance Win32_Process | Select-Object ProcessId,ParentProcessId,"
        "ExecutablePath,CommandLine | ConvertTo-Json -Compress"
    )
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        rows = json.loads(result.stdout or "[]")
    except (OSError, subprocess.SubprocessError, ValueError):
        return []
    if isinstance(rows, dict):
        rows = [rows]
    if not isinstance(rows, list):
        return []
    rows = [r for r in rows if isinstance(r, dict)]
    parents = {r.get("ProcessId"): r.get("ParentProcessId") for r in rows}
    # This process and the launcher chain above it are the caller, not a blocker.
    mine: set[object] = set()
    pid: object = os.getpid()
    while pid is not None and pid not in mine:
        mine.add(pid)
        pid = parents.get(pid)
    prefix = os.path.normcase(str(env)) + os.sep
    found: list[str] = []
    for row in rows:
        exe = os.path.normcase(str(row.get("ExecutablePath") or ""))
        if exe.startswith(prefix) and row.get("ProcessId") not in mine:
            # The tail: the head is the same long interpreter path every time,
            # the end is what says `node` / `serve` / `hook`.
            what = str(row.get("CommandLine") or exe).strip()
            what = what if len(what) <= 80 else "…" + what[-79:]
            found.append(f"pid {row.get('ProcessId')} ({what})")
    return found


def _last_line(text: str | None) -> str:
    lines = (text or "").strip().splitlines()
    return lines[-1].strip() if lines else ""


def _canary_install(cmd: list[str], version: str, timeout: float = 600.0) -> str | None:
    """Install the target into a THROWAWAY tool dir and run it. None = it works;
    otherwise why it doesn't.

    `uv tool install --force` deletes the live environment before it builds the
    replacement, so a release that cannot resolve, build, or import on this
    machine — or a network that drops mid-download — leaves no omind at all.
    Proving the install somewhere disposable first turns every one of those into
    a refusal with the old version still in place. It also warms uv's cache, so
    the real install that follows is mostly offline."""
    spec = cmd[cmd.index("--from") + 1]
    with tempfile.TemporaryDirectory(prefix="omind-canary-", ignore_cleanup_errors=True) as tmp:
        bin_dir = Path(tmp) / "bin"
        env = {
            **os.environ,
            "UV_TOOL_DIR": str(Path(tmp) / "tools"),
            "UV_TOOL_BIN_DIR": str(bin_dir),
        }
        try:
            built = subprocess.run(
                ["uv", "tool", "install", "--quiet", "--from", spec, "omind"],
                env=env,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
            if built.returncode != 0:
                why = _last_line(built.stderr) or f"uv exit {built.returncode}"
                return f"it does not install ({why})"
            exe = bin_dir / ("omind.exe" if _is_windows() else "omind")
            ran = subprocess.run(
                [str(exe), "--version"], capture_output=True, text=True, timeout=120, check=False
            )
        except subprocess.TimeoutExpired:
            return "the trial install timed out (network stall?)"
        except (OSError, subprocess.SubprocessError) as exc:
            return f"the trial install could not run ({exc})"
        if ran.returncode != 0 or version not in (ran.stdout or ""):
            why = _last_line(ran.stderr or ran.stdout) or f"exit {ran.returncode}"
            return f"it installs but does not start as omind {version} ({why})"
    return None


def preflight(install: InstallInfo, cmd: list[str], version: str) -> list[str]:
    """Why this machine must NOT run ``cmd`` right now (``[]`` = go).

    Every check runs BEFORE anything is touched, because the installers this
    drives are not transactional: a refusal costs nothing, while a failure
    half-way costs the install. Cheap checks first; the trial install last."""
    problems: list[str] = []
    if shutil.which("git") is None:
        problems.append("`git` is not on PATH — the release is installed from a git ref.")
    if install.method == "pip":
        if importlib.util.find_spec("pip") is None:
            problems.append(f"`pip` is not available in {sys.executable}.")
        return problems
    if shutil.which("uv") is None:
        problems.append("`uv` is not on PATH — this is a `uv tool` install.")
    if _is_windows():
        # Windows will not delete a running executable, and uv only finds that out
        # after it has already removed the rest of the environment. This process
        # runs from that environment, so an in-place update can never succeed
        # here: 9.4.0 -> 9.7.5 left `Scripts\python.exe` and nothing else.
        manual = _shell_join(cmd)
        env = tool_env_dir(install)
        others = _env_processes(env) if env is not None else []
        problems.append(
            "on Windows omind cannot replace the environment it is running from "
            "(uv deletes it first, then fails on the locked python.exe). Update from "
            "outside omind instead:\n"
            "    1. close every agent session / MCP server / `omind serve` using omind"
            + (
                "\n       still running: " + "; ".join(others)
                if others
                else ""
            )
            + f"\n    2. {manual}\n    3. omind setup"
        )
        return problems
    if problems:
        return problems
    failure = _canary_install(cmd, version)
    if failure is not None:
        problems.append(f"omind {version} was tried in a throwaway environment and {failure}.")
    return problems


def _install_works(version: str) -> bool:
    """Does the install on disk start and report ``version``? A fresh interpreter,
    for the reason :func:`_post_update_heal` gives."""
    try:
        result = subprocess.run(
            [sys.executable, "-m", "omind", "--version"],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0 and version in (result.stdout or "")


def _refused(
    install: InstallInfo, cmd: list[str], version: str, *, what: str, log: Callable[[str], object]
) -> bool:
    """Run :func:`preflight`; report and return True when the install must not run."""
    problems = preflight(install, cmd, version)
    if problems:
        log(f"refusing to {what} — nothing was changed:")
        for problem in problems:
            log(f"  - {problem}")
    return bool(problems)


def _run_install(cmd: list[str], version: str, *, what: str, log: Callable[[str], object]) -> int:
    """Run ``cmd``, then confirm the result actually starts."""
    try:
        # A watchdog timeout so a hung `uv tool install git+…` (a stalled clone,
        # a dead network) can't wedge the update pass forever when run from
        # fleet automation.
        result = subprocess.run(cmd, check=False, timeout=600)  # streams to terminal
    except subprocess.TimeoutExpired:
        log(f"{what} timed out after 600s (network stall?) — try again.")
        result = None
    except (OSError, subprocess.SubprocessError) as exc:
        log(f"{what} failed to launch: {exc}")
        result = None
    if result is not None and result.returncode == 0 and _install_works(version):
        return 0
    if result is not None and result.returncode != 0:
        log(f"{what} command exited {result.returncode}.")
    elif result is not None:
        log(f"{what} command succeeded, but the installed omind does not start.")
    # Say which it is: an installer that failed cleanly left the old version
    # working; one that failed half-way left nothing, and the next hook or MCP
    # start would be the first anyone heard of it.
    if not _install_works(__version__):
        log("the omind install is now BROKEN. Once the cause above is dealt with,")
        log("repair it from a plain terminal (no agent sessions open) with:")
        log(f"    {_shell_join(cmd)}")
    return (result.returncode if result is not None else 0) or 1


#: Shared opt-out with the `omind node` startup self-heal — one switch for
#: "I manage my own hooks", not two.
_NO_AUTOHEAL_ENV = "OMIND_NO_AUTOHEAL"


def _post_update_heal(*, log: Callable[[str], object] = print) -> None:
    """Run the post-update heal in a SUBPROCESS of the just-installed code.

    Never inline. By the time we reach here `uv tool install --force` has already
    swapped the package on disk, so this interpreter is a chimera: modules
    imported at startup are the OLD release, while anything imported from here on
    is read fresh off disk and is the NEW one. Importing `provision` therefore
    handed new code a stale `omind.filelock` out of `sys.modules`, and updating
    8.10.1 -> 9.1.1 died on `module 'omind.filelock' has no attribute
    'exclusive'` — a function the outgoing release simply did not have (issue
    #315). Every
    release pairing mines a fresh version of that, and no import order defuses
    it; a clean interpreter is the only fix.

    `sys.executable` is the venv python uv just rebuilt in place, so `-m omind`
    there loads the new package end to end. Fail-open as before: a successful
    update must never be reported as a failure because a follow-up chore didn't
    work. (A rollback to a release predating `--heal` gets the "run `omind setup`
    by hand" warning rather than a heal — visible, and it names the repair.)
    """
    if os.environ.get(_NO_AUTOHEAL_ENV):
        return
    cmd = [sys.executable, "-m", "omind", "self-update", "--heal"]
    try:
        result = subprocess.run(
            cmd, check=False, timeout=600, capture_output=True, text=True
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log(f"warning: re-provision failed to launch ({exc}); run `omind setup` by hand.")
        return
    for line in (result.stdout or "").splitlines():
        if line.strip():
            log(line)
    if result.returncode != 0:
        detail = (result.stderr or "").strip().splitlines()
        why = detail[-1] if detail else f"exit {result.returncode}"
        log(f"warning: re-provision failed ({why}); run `omind setup` by hand.")


def run_post_update_heal(*, log: Callable[[str], object] = print) -> None:
    """Re-provision the wiring and pay any index migration, after an update.

    The heal itself. Always reached through `omind self-update --heal` in a fresh
    interpreter running the installed code, never inline in the process that did
    the installing — see :func:`_post_update_heal`.

    Both steps are fail-open: a successful update must never be reported as a
    failure because a follow-up chore didn't work.
    """
    from omind.provision import Provisioner, SetupConfig, default_vault_path

    vault = default_vault_path()
    # Hook scripts, the MCP entry, and the skill are all rewritten by the new
    # binary — otherwise a release that changes any of them lands only on boxes
    # where someone remembered to re-run `omind setup` by hand.
    try:
        actions = Provisioner(config=SetupConfig(vault=vault), log=lambda _m: None).run()
        if actions:
            log(f"re-provisioned wiring ({len(actions)} change(s)).")
    except Exception as exc:  # noqa: BLE001 — never fail a good update
        log(f"warning: re-provision failed ({exc}); run `omind setup` by hand.")
    # Not a rebuild: opening the index runs the existing SCHEMA_VERSION/model
    # check, which wipes and repopulates only when the format actually changed.
    # Doing it here pays that cost in the update the user is already waiting on,
    # instead of surprising the next search with it.
    try:
        from omind import searchindex

        index = searchindex.shared(vault / "OMI")
        done = index.refresh() if index is not None else None
        if done is not None and (done.reindexed or done.removed):
            log(
                f"search index refreshed: {done.reindexed} note(s) reindexed, "
                f"{done.removed} removed ({done.seconds:.1f}s)."
            )
    except Exception:  # noqa: BLE001 — an index chore must never break an update
        pass


def self_update(
    *,
    check_only: bool = False,
    force: bool = False,
    rollback: bool = False,
    heal_only: bool = False,
    log: Callable[[str], object] = print,
) -> int:
    """``omind self-update``: report, then (unless ``--check``) reinstall the latest
    tag. ``--rollback`` reinstalls the version that was current before the last
    update (2026-08-27 review — a broken release used to strand every machine
    until a manual downgrade). ``--heal`` runs only the post-update heal: it is
    how :func:`_post_update_heal` re-enters a clean interpreter, and so it must
    never spawn one itself, or the recursion has no floor."""
    if heal_only:
        run_post_update_heal(log=log)
        return 0
    if rollback:
        return rollback_update(log=log)
    # A user-invoked update gets a generous network timeout, not the 2s nudge
    # budget (which times out on a slow-but-working link and falsely reports
    # "could not reach GitHub").
    status = check_for_update(force=True, timeout=15.0)
    log(f"installed: omind {status.current}")
    if status.latest is None:
        log("could not reach GitHub (offline, rate-limited, or no releases yet).")
        return 1
    log(f"latest:    omind {status.latest}")
    if not status.available and not force:
        log("already up to date.")
        return 0
    if check_only:
        log("update available — run `omind self-update` (without --check) to apply it.")
        return 0
    install = detect_install()
    cmd = update_command(install, status.latest)
    if cmd is None:
        if install.method == "editable":
            log(f"editable checkout at {install.detail} — update it with `git pull`.")
        else:
            log(
                f"cannot auto-update a {install.method!r} install "
                f"({install.detail}); reinstall by hand."
            )
        return 1
    if _refused(install, cmd, status.latest, what="update", log=log):
        return 1
    log(f"updating: {_shell_join(cmd)}")
    _record_rollback(status.current, status.latest, cmd)
    code = _run_install(cmd, status.latest, what="update", log=log)
    if code == 0:
        log(f"updated to {status.latest}.")
        _post_update_heal(log=log)
        log("Restart the MCP server / agent session to load it.")
    return code


_ROLLBACK_NAME = "last-install.json"


def _rollback_path() -> Path:
    return state_dir() / _ROLLBACK_NAME


def _record_rollback(previous: str, target: str, cmd: list[str]) -> None:
    """Remember what was installed before this update, so a broken release can
    be rolled back with `omind self-update --rollback` instead of stranding
    every machine until a manual downgrade (2026-08-27 review)."""
    with contextlib.suppress(OSError):
        _rollback_path().write_text(
            json.dumps({"from": previous, "to": target, "at": time.time()}),
            encoding="utf-8",
        )


def rollback_update(*, log: Callable[[str], object] = print) -> int:
    """Reinstall the version that was current before the last self-update."""
    try:
        data = json.loads(_rollback_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        log("no rollback record: `omind self-update` has not upgraded from here before.")
        return 1
    previous = str(data.get("from") or "")
    if not previous:
        log("rollback record is empty.")
        return 1
    install = detect_install()
    cmd = update_command(install, previous)
    if cmd is None:
        if install.method == "editable":
            log(
                f"editable checkout at {install.detail} — roll back with "
                f"`git checkout v{previous}`."
            )
        else:
            log(f"cannot auto-install {previous} on a {install.method!r} install.")
        return 1
    if _refused(install, cmd, previous, what="roll back", log=log):
        return 1
    log(f"rolling back to omind {previous}: {_shell_join(cmd)}")
    code = _run_install(cmd, previous, what="rollback", log=log)
    if code == 0:
        _post_update_heal(log=log)
        log(f"rolled back to {previous}. Restart the MCP server / agent session to load it.")
    return code
