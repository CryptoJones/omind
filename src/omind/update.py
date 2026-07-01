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

import json
import os
import re
import subprocess
import sys
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
    if "uv/tools/omind" in posix:
        return InstallInfo("uv-tool", loc)
    repo = Path(loc).resolve().parent.parent.parent  # …/src/omind/__init__.py -> repo
    if loc and (repo / "pyproject.toml").is_file() and (repo / ".git").exists():
        return InstallInfo("editable", str(repo))
    if "site-packages" in posix:
        return InstallInfo("pip", loc)
    return InstallInfo("unknown", loc)


def update_command(install: InstallInfo, version: str) -> list[str] | None:
    """The argv that installs ``version``, or None when it can't be automated."""
    ref = f"git+https://github.com/{GITHUB_REPO}@v{version}"
    if install.method == "uv-tool":
        return ["uv", "tool", "install", "--force", "--from", ref, "omind"]
    if install.method == "pip":
        return [sys.executable, "-m", "pip", "install", "--upgrade", "--force-reinstall", ref]
    return None  # editable -> git pull; unknown -> manual


def self_update(
    *, check_only: bool = False, force: bool = False, log: Callable[[str], object] = print
) -> int:
    """``omind self-update``: report, then (unless ``--check``) reinstall the latest tag."""
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
    log(f"updating: {' '.join(cmd)}")
    try:
        # A watchdog timeout so a hung `uv tool install git+…` (a stalled clone,
        # a dead network) can't wedge the update pass forever when run from
        # fleet automation.
        result = subprocess.run(cmd, check=False, timeout=600)  # streams to terminal
    except subprocess.TimeoutExpired:
        log("update timed out after 600s (network stall?) — try again.")
        return 1
    except (OSError, subprocess.SubprocessError) as exc:
        log(f"update failed to launch: {exc}")
        return 1
    if result.returncode == 0:
        log(f"updated to {status.latest}. Restart the MCP server / agent session to load it.")
        return 0
    log(f"update command exited {result.returncode}.")
    return result.returncode
