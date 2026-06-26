# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Aaron K. Clark
"""Behavioral tests for the packaged secret-output-guard.sh PreToolUse(Bash) hook.

The hook blocks (exit 2) Bash commands whose credential VALUE would reach
stdout/the transcript, while allowing safe forms (captured into a var, redirected
off the transcript, fed into a request header). It ships verbatim — no install-time
substitution — so we run the package-data copy directly under a real bash + jq.
"""

from __future__ import annotations

import importlib.resources
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

#: secret-output-guard.sh is a POSIX bash+jq deployment artifact (Claude Code on
#: Linux/macOS). Its subprocess tests only make sense where a real bash + jq run it.
_HOOK_TESTABLE = (
    sys.platform != "win32"
    and shutil.which("bash") is not None
    and shutil.which("jq") is not None
)

pytestmark = pytest.mark.skipif(
    not _HOOK_TESTABLE, reason="secret-output-guard.sh is a POSIX bash+jq adapter"
)


def _hook(tmp_path: Path) -> Path:
    src = (
        importlib.resources.files("omind")
        .joinpath("secret-output-guard.sh")
        .read_text(encoding="utf-8")
    )
    hook = tmp_path / "secret-output-guard.sh"
    hook.write_text(src, encoding="utf-8")
    hook.chmod(0o755)
    return hook


def _run(hook: Path, command: str) -> int:
    event = {"tool_name": "Bash", "session_id": "s", "tool_input": {"command": command}}
    return subprocess.run(
        ["bash", str(hook)], input=json.dumps(event), capture_output=True, text=True
    ).returncode


def test_blocks_pass_show_piped_to_head(tmp_path: Path) -> None:
    assert _run(_hook(tmp_path), "pass show github/token | head") == 2


def test_allows_pass_show_captured_into_var(tmp_path: Path) -> None:
    assert _run(_hook(tmp_path), "TOK=$(pass show github/token)") == 0


def test_blocks_gh_auth_token(tmp_path: Path) -> None:
    assert _run(_hook(tmp_path), "gh auth token") == 2


def test_blocks_literal_token_in_command(tmp_path: Path) -> None:
    assert _run(_hook(tmp_path), "echo ghp_" + "A" * 36) == 2


def test_allows_pass_show_redirected_to_devnull(tmp_path: Path) -> None:
    assert _run(_hook(tmp_path), "pass show github/token >/dev/null") == 0


def test_allows_token_in_curl_header(tmp_path: Path) -> None:
    cmd = 'curl -H "Authorization: token $(pass show github/token)" https://api.github.com'
    assert _run(_hook(tmp_path), cmd) == 0


def test_audited_override_allows(tmp_path: Path) -> None:
    assert _run(_hook(tmp_path), "OMI_SECRET_OK=1 pass show github/token | head") == 0
