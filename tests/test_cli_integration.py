# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Aaron K. Clark
"""Integration tests for the CLI subcommand flows.

test_cli.py covers the parser and the store-backed subcommands (reindex,
note, rollup); the modules behind setup/doctor/backup/export have their own
component suites. What was untested is the *wiring* in cli.py itself — arg
plumbing, exit-code mapping, and the serve/uvicorn handoff. These tests call
``main()`` end to end, faking only the process boundary (subprocess, uvicorn)
and isolating every config path the real code would touch.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI

from omind import backup, provision
from omind.cli import main


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    (tmp_path / "vault" / "OMI").mkdir(parents=True)
    return tmp_path / "vault"


@pytest.fixture
def isolate_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Seal off every host config location the subcommands might touch."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setattr(provision, "claude_config_path", lambda: tmp_path / ".claude.json")
    monkeypatch.setattr(
        provision, "claude_settings_path", lambda: tmp_path / ".claude" / "settings.json"
    )
    monkeypatch.setattr(provision, "mcp_servers_dir", lambda: tmp_path / "mcp-servers")
    return tmp_path


@pytest.fixture
def fake_subprocess(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(backup.subprocess, "run", fake_run)
    return calls


def _note(vault: Path, title: str, body: str) -> int:
    return main(
        [
            "note",
            "--title", title,
            "--summary", f"summary of {title}",
            "--details", body,
            "--vault", str(vault),
        ]
    )


# -- serve --------------------------------------------------------------------


def test_serve_passes_app_host_and_port_to_uvicorn(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, Any] = {}

    def fake_uvicorn_run(app: object, **kwargs: object) -> None:
        captured["app"] = app
        captured.update(kwargs)

    monkeypatch.setattr("uvicorn.run", fake_uvicorn_run)
    rc = main(["serve", "--vault", str(vault), "--host", "127.0.0.2", "--port", "9999"])
    assert rc == 0
    assert isinstance(captured["app"], FastAPI)
    assert captured["host"] == "127.0.0.2"
    assert captured["port"] == 9999


def test_serve_reload_hands_omi_dir_to_the_factory_via_env(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omind.web.app import get_app

    captured: dict[str, Any] = {}

    def fake_uvicorn_run(app: object, **kwargs: object) -> None:
        captured["app"] = app
        captured.update(kwargs)

    monkeypatch.setattr("uvicorn.run", fake_uvicorn_run)
    # Pre-set via monkeypatch so the value _run_serve writes is restored after.
    monkeypatch.setenv("OMIND_OMI_DIR", "sentinel")
    rc = main(["serve", "--vault", str(vault), "--reload"])
    assert rc == 0
    assert captured["app"] == "omind.web.app:get_app"
    assert captured["factory"] is True
    assert captured["reload"] is True
    # The env handoff is the only channel to the reload child; the factory
    # must come up on the exact folder serve printed.
    assert os.environ["OMIND_OMI_DIR"] == str(vault / "OMI")
    assert isinstance(get_app(), FastAPI)


def test_get_app_factory_refuses_to_guess_without_the_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from omind.web.app import get_app

    monkeypatch.delenv("OMIND_OMI_DIR", raising=False)
    with pytest.raises(RuntimeError, match="OMIND_OMI_DIR"):
        get_app()


# -- export / import ----------------------------------------------------------


def test_export_import_round_trip(vault: Path, tmp_path: Path) -> None:
    assert _note(vault, "Round Trip", "original body") == 0
    out = tmp_path / "bundle.json"
    assert main(["export", "--vault", str(vault), "--out", str(out)]) == 0
    assert out.is_file()

    dest = tmp_path / "dest"
    (dest / "OMI").mkdir(parents=True)
    assert main(["import", str(out), "--vault", str(dest)]) == 0
    imported = dest / "OMI" / "Round Trip.md"
    assert imported.is_file()
    assert "original body" in imported.read_text(encoding="utf-8")


def test_import_conflict_is_soft_failure_until_forced(vault: Path, tmp_path: Path) -> None:
    assert _note(vault, "Conflicted", "source body") == 0
    out = tmp_path / "bundle.json"
    assert main(["export", "--vault", str(vault), "--out", str(out)]) == 0

    dest = tmp_path / "dest"
    (dest / "OMI").mkdir(parents=True)
    assert main(["import", str(out), "--vault", str(dest)]) == 0
    target = dest / "OMI" / "Conflicted.md"
    target.write_text("# Conflicted\n\nlocal divergence\n", encoding="utf-8")

    # Differing content without --force: keep the on-disk copy, exit 1.
    assert main(["import", str(out), "--vault", str(dest)]) == 1
    assert "local divergence" in target.read_text(encoding="utf-8")

    assert main(["import", str(out), "--vault", str(dest), "--force"]) == 0
    assert "source body" in target.read_text(encoding="utf-8")


def test_import_missing_bundle_exits_1_with_error(
    vault: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = main(["import", "/no/such/bundle.json", "--vault", str(vault)])
    assert rc == 1
    assert "error:" in capsys.readouterr().err


# -- doctor -------------------------------------------------------------------


def test_doctor_reports_problems_with_exit_1_on_a_bare_machine(
    vault: Path,
    isolate_config: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(provision.shutil, "which", lambda name: None)
    rc = main(["doctor", "--vault", str(vault)])
    out = capsys.readouterr().out
    assert rc == 1
    assert "problem(s)" in out
    # The agent-independent backup check must ride along with the agent checks.
    assert "backup" in out.lower()


# -- backup -------------------------------------------------------------------


def test_backup_init_creates_password_file_and_repo(
    vault: Path,
    isolate_config: Path,
    fake_subprocess: list[list[str]],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(backup.shutil, "which", lambda name: f"/usr/bin/{name}")
    rc = main(["backup", "init", "--repo", str(tmp_path / "repo")])
    assert rc == 0
    passfile = tmp_path / "xdg" / "omind" / "backup.pass"
    assert passfile.is_file()
    assert passfile.stat().st_mode & 0o777 == 0o600
    assert ["restic", "init"] in fake_subprocess


def test_backup_run_without_init_exits_1_with_error(
    vault: Path,
    isolate_config: Path,
    fake_subprocess: list[list[str]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = main(["backup", "run", "--vault", str(vault)])
    assert rc == 1
    assert "error:" in capsys.readouterr().err


def test_backup_run_after_init_snapshots_and_records_success(
    vault: Path,
    isolate_config: Path,
    fake_subprocess: list[list[str]],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(backup.shutil, "which", lambda name: f"/usr/bin/{name}")
    assert main(["backup", "init", "--repo", str(tmp_path / "repo")]) == 0
    rc = main(["backup", "run", "--vault", str(vault)])
    assert rc == 0
    assert any(c[:2] == ["restic", "backup"] for c in fake_subprocess)
    assert any(c[:2] == ["restic", "forget"] for c in fake_subprocess)
    state = json.loads(
        (tmp_path / "xdg" / "omind" / "backup.json").read_text(encoding="utf-8")
    )
    assert state["consecutive_failures"] == 0
    assert state["last_success"]


# -- setup --------------------------------------------------------------------


def test_setup_dry_run_plans_without_touching_anything(
    isolate_config: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(provision.shutil, "which", lambda name: f"/usr/bin/{name}")
    target = tmp_path / "never-created"
    rc = main(["setup", "--dry-run", "--vault", str(target)])
    assert rc == 0
    assert "[dry-run]" in capsys.readouterr().out
    assert not target.exists()


def test_setup_missing_tools_exits_1_with_error(
    isolate_config: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(provision.shutil, "which", lambda name: None)
    rc = main(["setup", "--vault", str(tmp_path / "vault")])
    assert rc == 1
    assert "missing required tool" in capsys.readouterr().err


# -- quickstart ---------------------------------------------------------------


def test_quickstart_is_personalized_to_the_vault(
    vault: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = main(["quickstart", "--vault", str(vault)])
    assert rc == 0
    assert str(vault / "OMI") in capsys.readouterr().out
