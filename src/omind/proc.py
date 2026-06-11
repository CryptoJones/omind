# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Aaron K. Clark
"""Shared subprocess plumbing for every external command omind spawns.

``claude``, ``npm``, ``restic``, ``rsync``, ``systemctl``, … all go through
:func:`run_command`:

  * output is always captured, so a failing tool can never leak repository
    details (or worse) into a terminal or systemd journal line we don't
    control;
  * on Windows, ``cmd[0]`` is resolved via :func:`shutil.which`, because
    ``CreateProcess`` won't resolve ``npm.cmd``-style shims from a bare name;
  * every call has a timeout — a hung npm install, or a restic stalled on a
    dead SFTP link, must fail loudly instead of wedging an unattended timer
    forever;
  * the usual failure modes (missing binary, non-zero exit, timeout) are
    re-raised as the caller's domain exception.

Tests fake ``subprocess.run`` (the module attribute) and never spawn anything.
"""

from __future__ import annotations

import os
import shutil
import subprocess

DEFAULT_TIMEOUT = 600.0
"""Seconds before a spawned command is killed; generous for slow npm installs."""


def run_command(
    cmd: list[str],
    *,
    error: type[Exception],
    check: bool = True,
    env: dict[str, str] | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run *cmd* with output captured, mapping the usual failures to *error*.

    *input_text* becomes the child's stdin (mesh add-seed streams a hook
    script to a remote `cat` this way — no temp file on either side).
    """
    if os.name == "nt":
        # CreateProcess won't resolve npm.cmd / claude.cmd from a bare
        # name; shutil.which finds the shim with its extension.
        resolved = shutil.which(cmd[0])
        if resolved:
            cmd = [resolved, *cmd[1:]]
    try:
        return subprocess.run(
            cmd,
            check=check,
            capture_output=True,
            text=True,
            env=env,
            timeout=timeout,
            input=input_text,
        )
    except FileNotFoundError as exc:
        raise error(f"command not found: {cmd[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise error(f"command timed out after {timeout:g}s: {' '.join(cmd)}") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip()
        raise error(f"command failed: {' '.join(cmd)}\n{detail}") from exc
