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


def test_allows_git_credential_helper_echoing_pass(tmp_path: Path) -> None:
    # The git one-shot credential helper feeds the secret to git's credential
    # protocol on stdin (not the transcript), so the echo "$(pass …)" inside a
    # credential.helper definition is not a leak.
    cmd = (
        "git -c credential.helper='!f(){ echo username=x; "
        "echo \"password=$(pass codeberg/api-token)\"; }; f' push -u origin br"
    )
    assert _run(_hook(tmp_path), cmd) == 0


def test_blocks_literal_token_even_inside_credential_helper(tmp_path: Path) -> None:
    # The credential-helper exemption must not let a literal token slip through:
    # the literal-token check runs before the exemption.
    cmd = "git -c credential.helper=x commit -m ghp_AbCdEf0123456789AbCdEf0123456789AbCd"
    assert _run(_hook(tmp_path), cmd) == 2


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


def test_blocks_pass_show_with_stderr_redirect_piped(tmp_path: Path) -> None:
    """CRITICAL: `2>/dev/null` redirects stderr; stdout still leaks to the transcript."""
    assert _run(_hook(tmp_path), "pass show github/token 2>/dev/null | head") == 2
    assert _run(_hook(tmp_path), "pass show github/token 2>/dev/null") == 2


def test_word_boundary_avoids_bypass_false_positive(tmp_path: Path) -> None:
    """`pass` inside another word / a grep pattern must not false-block."""
    assert _run(_hook(tmp_path), "curl --noproxy '' https://bypass.example/x") == 0
    assert _run(_hook(tmp_path), 'grep -r "pass tests/unit" .') == 0
    assert _run(_hook(tmp_path), 'git commit -m "make the pass show up"') == 0


def test_forged_override_in_string_does_not_bypass(tmp_path: Path) -> None:
    """OMI_SECRET_OK=1 inside a quoted string must not disable the guard."""
    assert _run(_hook(tmp_path), 'echo "set OMI_SECRET_OK=1 first" && pass show x | head') == 2


def test_allows_multiline_captured_read(tmp_path: Path) -> None:
    """A multi-line `TOK=$(\\n pass show x \\n)` capture is safe, not a leak."""
    assert _run(_hook(tmp_path), "TOK=$(\n  pass show github/token\n)\necho done") == 0
