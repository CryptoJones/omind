# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Aaron K. Clark
"""Tests for the shared subprocess runner.

These spawn real (tiny) subprocesses via sys.executable — no fakes — because
the whole point of run_command is its behavior at the process boundary:
capture, timeout, and error mapping.
"""

from __future__ import annotations

import os
import sys

import pytest

from omind.proc import run_command


class DomainError(Exception):
    pass


def _py(code: str) -> list[str]:
    return [sys.executable, "-c", code]


def test_success_captures_stdout() -> None:
    result = run_command(_py("print('hello')"), error=DomainError)
    assert result.returncode == 0
    assert result.stdout.strip() == "hello"


def test_missing_binary_maps_to_domain_error() -> None:
    with pytest.raises(DomainError, match="command not found: omind-no-such-binary"):
        run_command(["omind-no-such-binary"], error=DomainError)


def test_nonzero_exit_maps_to_domain_error_with_stderr_detail() -> None:
    cmd = _py("import sys; sys.stderr.write('boom'); sys.exit(2)")
    with pytest.raises(DomainError, match="command failed") as excinfo:
        run_command(cmd, error=DomainError)
    assert "boom" in str(excinfo.value)


def test_check_false_returns_completed_process_on_failure() -> None:
    result = run_command(_py("import sys; sys.exit(3)"), error=DomainError, check=False)
    assert result.returncode == 3


def test_timeout_maps_to_domain_error() -> None:
    cmd = _py("import time; time.sleep(30)")
    with pytest.raises(DomainError, match="command timed out after 0.5s"):
        run_command(cmd, error=DomainError, timeout=0.5)


def test_env_is_passed_through() -> None:
    cmd = _py("import os; print(os.environ['OMIND_PROC_TEST'])")
    env = {**os.environ, "OMIND_PROC_TEST": "marker"}
    result = run_command(cmd, error=DomainError, env=env)
    assert result.stdout.strip() == "marker"
