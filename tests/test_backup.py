# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Aaron K. Clark
"""Tests for omind.backup: init, run, retention, failure note, verify, doctor.

Every restic/rsync/systemctl call is faked (the test machine has none of them
installed); the fakes follow the ``fake_subprocess`` pattern from
tests/test_provision.py and additionally capture the environment so the tests
can assert the password file is referenced — and the password itself never is.
"""

from __future__ import annotations

import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from omind import backup
from omind.backup import BackupConfig, BackupError, diagnose_backup
from omind.cli import build_parser, main
from omind.provision import SetupConfig

REPO = "sftp:pluto:/backups/omi"


@pytest.fixture(autouse=True)
def isolate_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Keep backup.pass/backup.json (and systemd units) inside tmp via XDG."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    return tmp_path / "xdg" / "omind"


@pytest.fixture
def fake_subprocess(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(backup.subprocess, "run", fake_run)
    return calls


@pytest.fixture
def fake_envs(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, str]]:
    """Like fake_subprocess, but records the env passed to each call."""
    envs: list[dict[str, str]] = []

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        env = kwargs.get("env")
        envs.append(dict(env) if isinstance(env, dict) else {})
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(backup.subprocess, "run", fake_run)
    return envs


@pytest.fixture
def restic_present(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(backup.shutil, "which", lambda name: f"/usr/bin/{name}")


@pytest.fixture
def restic_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        backup.shutil, "which", lambda name: None if name == "restic" else f"/usr/bin/{name}"
    )


def _quiet(_: str) -> None:
    pass


def _configure(repo: str = REPO, **kw: object) -> BackupConfig:
    """Write a ready-made backup.pass + backup.json, bypassing `init`."""
    backup.config_dir().mkdir(parents=True, exist_ok=True)
    backup.password_path().write_text("test-password\n", encoding="utf-8")
    config = BackupConfig(repo=repo, **kw)  # type: ignore[arg-type]
    backup.save_config(config)
    return config


def _omi(tmp_path: Path) -> Path:
    omi = tmp_path / "vault" / "OMI"
    omi.mkdir(parents=True, exist_ok=True)
    (omi / "index.md").write_text("# OMI\n", encoding="utf-8")
    return omi


# -- locations ---------------------------------------------------------------


def test_config_dir_honors_xdg_config_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "elsewhere"))
    assert backup.config_dir() == tmp_path / "elsewhere" / "omind"
    monkeypatch.delenv("XDG_CONFIG_HOME")
    assert backup.config_dir() == Path.home() / ".config" / "omind"
    assert backup.password_path().name == "backup.pass"
    assert backup.config_path().name == "backup.json"


# -- init ----------------------------------------------------------------------


def test_init_creates_password_file_0600_and_repo(
    restic_present: None, fake_subprocess: list[list[str]]
) -> None:
    logged: list[str] = []
    backup.init_backup(REPO, log=logged.append)
    passfile = backup.password_path()
    assert passfile.is_file()
    assert passfile.stat().st_mode & 0o777 == 0o600
    secret = passfile.read_text(encoding="utf-8").strip()
    assert len(secret) >= 32
    # The password is never printed, logged, or put on a command line.
    assert all(secret not in line for line in logged)
    assert all(secret not in arg for cmd in fake_subprocess for arg in cmd)
    assert ["restic", "init"] in fake_subprocess
    config = backup.load_config()
    assert config is not None and config.repo == REPO


def test_init_passes_repo_and_password_file_via_env(
    restic_present: None, fake_envs: list[dict[str, str]]
) -> None:
    backup.init_backup(REPO, log=_quiet)
    assert fake_envs[0]["RESTIC_REPOSITORY"] == REPO
    assert fake_envs[0]["RESTIC_PASSWORD_FILE"] == str(backup.password_path())


def test_init_refuses_to_overwrite_password_file(
    restic_present: None, fake_subprocess: list[list[str]]
) -> None:
    backup.init_backup(REPO, log=_quiet)
    secret = backup.password_path().read_text(encoding="utf-8")
    with pytest.raises(BackupError, match="refusing to overwrite"):
        backup.init_backup("/elsewhere", log=_quiet)
    assert backup.password_path().read_text(encoding="utf-8") == secret


def test_init_without_restic_warns_but_configures(
    restic_absent: None, fake_subprocess: list[list[str]]
) -> None:
    logged: list[str] = []
    backup.init_backup(REPO, log=logged.append)
    assert fake_subprocess == []  # no restic init attempted
    assert any("restic not found" in line for line in logged)
    config = backup.load_config()
    assert config is not None and config.repo == REPO


# -- run -------------------------------------------------------------------------


def test_run_invokes_restic_backup_then_retention(
    restic_present: None, fake_subprocess: list[list[str]], tmp_path: Path
) -> None:
    _configure()
    omi = _omi(tmp_path)
    backup.run_backup(omi, log=_quiet)
    assert fake_subprocess[0] == ["restic", "backup", str(omi)]
    forget = fake_subprocess[1]
    assert forget[:3] == ["restic", "forget", "--prune"]
    for flag, count in (("--keep-daily", "7"), ("--keep-weekly", "4"), ("--keep-monthly", "6")):
        idx = forget.index(flag)
        assert forget[idx + 1] == count
    config = backup.load_config()
    assert config is not None
    assert config.consecutive_failures == 0
    assert config.last_success is not None


def test_run_without_config_errors(restic_present: None, tmp_path: Path) -> None:
    with pytest.raises(BackupError, match="no backup configured"):
        backup.run_backup(_omi(tmp_path), log=_quiet)


def test_three_failures_write_the_failing_note_and_success_removes_it(
    restic_present: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure()
    omi = _omi(tmp_path)
    note = omi / "BACKUP FAILING.md"

    def failing_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.CalledProcessError(1, cmd, output="", stderr="repo unreachable")

    monkeypatch.setattr(backup.subprocess, "run", failing_run)
    for attempt in range(1, 4):
        with pytest.raises(BackupError, match="repo unreachable"):
            backup.run_backup(omi, log=_quiet)
        config = backup.load_config()
        assert config is not None and config.consecutive_failures == attempt
        assert note.is_file() == (attempt >= 3)  # note only at the threshold

    text = note.read_text(encoding="utf-8")
    assert "# BACKUP FAILING" in text
    assert "repo unreachable" in text
    assert "[[BACKUP FAILING]]" in (omi / "index.md").read_text(encoding="utf-8")

    def ok_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(backup.subprocess, "run", ok_run)
    backup.run_backup(omi, log=_quiet)
    assert not note.exists()  # deleted through OmiStore.delete_note
    assert "[[BACKUP FAILING]]" not in (omi / "index.md").read_text(encoding="utf-8")
    config = backup.load_config()
    assert config is not None and config.consecutive_failures == 0


def test_rsync_fallback_when_restic_missing(
    restic_absent: None, fake_subprocess: list[list[str]], tmp_path: Path
) -> None:
    _configure()  # sftp:pluto:/backups/omi -> rsync's pluto:/backups/omi
    omi = _omi(tmp_path)
    backup.run_backup(omi, log=_quiet)
    first = fake_subprocess[0]
    assert first[:3] == ["rsync", "-a", "--delete"]
    assert first[-2] == f"{omi}/"
    assert first[-1].startswith("pluto:/backups/omi/") and first[-1].endswith("/")
    assert not any(arg.startswith("--link-dest") for arg in first)

    config = backup.load_config()
    assert config is not None and config.last_snapshot is not None
    previous = config.last_snapshot

    backup.run_backup(omi, log=_quiet)  # second run hard-links against the first
    second = fake_subprocess[1]
    assert f"--link-dest=../{previous}" in second


def test_rsync_fallback_refuses_object_store_repos(
    restic_absent: None, fake_subprocess: list[list[str]], tmp_path: Path
) -> None:
    _configure(repo="s3:s3.amazonaws.com/bucket/omi")
    with pytest.raises(BackupError, match="not\\s+reachable by rsync"):
        backup.run_backup(_omi(tmp_path), log=_quiet)
    assert all(cmd[0] != "rsync" for cmd in fake_subprocess)


# -- verify ----------------------------------------------------------------------


def _fake_restore(
    monkeypatch: pytest.MonkeyPatch, content: bytes
) -> list[list[str]]:
    """Fake subprocess.run whose `restic restore` materializes the sentinel."""
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(list(cmd))
        if cmd[:2] == ["restic", "restore"]:
            target = Path(cmd[cmd.index("--target") + 1])
            include = Path(cmd[cmd.index("--include") + 1])
            restored = target / include.relative_to(include.anchor)
            restored.parent.mkdir(parents=True, exist_ok=True)
            restored.write_bytes(content)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(backup.subprocess, "run", fake_run)
    return calls


def test_verify_ok_when_sentinel_byte_identical(
    restic_present: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure()
    omi = _omi(tmp_path)
    calls = _fake_restore(monkeypatch, (omi / "index.md").read_bytes())
    results = {r.key: r for r in backup.verify_backup(omi, log=_quiet)}
    assert calls[0] == ["restic", "check"]
    assert calls[1][:2] == ["restic", "restore"]
    assert results["backup_check"].level == "ok"
    assert results["backup_sentinel"].level == "ok"


def test_verify_warns_on_sentinel_drift(
    restic_present: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure()
    omi = _omi(tmp_path)
    _fake_restore(monkeypatch, b"# OMI (older snapshot)\n")
    results = {r.key: r for r in backup.verify_backup(omi, log=_quiet)}
    assert results["backup_sentinel"].level == "warn"
    assert "drift" in results["backup_sentinel"].message


def test_verify_requires_restic(restic_absent: None, tmp_path: Path) -> None:
    _configure()
    with pytest.raises(BackupError, match="restic not found"):
        backup.verify_backup(_omi(tmp_path), log=_quiet)


# -- install-timer -----------------------------------------------------------------


def test_install_timer_writes_units_and_enables(
    restic_present: None, fake_subprocess: list[list[str]], tmp_path: Path
) -> None:
    _configure()
    config = SetupConfig(vault=tmp_path / "vault")
    backup.install_timer(config, log=_quiet)
    service = (backup.systemd_user_dir() / "omind-backup.service").read_text(encoding="utf-8")
    timer = (backup.systemd_user_dir() / "omind-backup.timer").read_text(encoding="utf-8")
    assert "Type=oneshot" in service  # fail-safe: never blocks anything
    assert "backup run" in service and str(config.vault) in service
    assert "OnCalendar=daily" in timer
    assert ["systemctl", "--user", "daemon-reload"] in fake_subprocess
    assert ["systemctl", "--user", "enable", "--now", "omind-backup.timer"] in fake_subprocess


def test_install_timer_requires_configured_backup(
    restic_present: None, fake_subprocess: list[list[str]], tmp_path: Path
) -> None:
    with pytest.raises(BackupError, match="no backup configured"):
        backup.install_timer(SetupConfig(vault=tmp_path / "vault"), log=_quiet)
    assert fake_subprocess == []


# -- doctor ------------------------------------------------------------------------


def _doctor_config(tmp_path: Path) -> SetupConfig:
    return SetupConfig(vault=tmp_path / "vault")


def test_doctor_warns_when_not_configured(restic_present: None, tmp_path: Path) -> None:
    results = {r.key: r for r in diagnose_backup(_doctor_config(tmp_path))}
    assert results["backup"].level == "warn"
    assert "no backup configured" in results["backup"].message


def test_doctor_ok_when_last_success_fresh(restic_present: None, tmp_path: Path) -> None:
    _configure(last_success=datetime.now(timezone.utc).isoformat())
    results = {r.key: r for r in diagnose_backup(_doctor_config(tmp_path))}
    assert results["backup"].level == "ok"
    assert "backup_tool" not in results  # restic present: no degradation warning


def test_doctor_warns_when_last_success_stale(restic_present: None, tmp_path: Path) -> None:
    stale = (datetime.now(timezone.utc) - timedelta(hours=72)).isoformat()
    _configure(last_success=stale)
    results = {r.key: r for r in diagnose_backup(_doctor_config(tmp_path))}
    assert results["backup"].level == "warn"


def test_doctor_warns_when_configured_but_never_run(
    restic_present: None, tmp_path: Path
) -> None:
    _configure()
    results = {r.key: r for r in diagnose_backup(_doctor_config(tmp_path))}
    assert results["backup"].level == "warn"


def test_doctor_fails_at_three_consecutive_failures(
    restic_present: None, tmp_path: Path
) -> None:
    _configure(
        consecutive_failures=3,
        last_success=datetime.now(timezone.utc).isoformat(),  # freshness can't mask it
    )
    results = {r.key: r for r in diagnose_backup(_doctor_config(tmp_path))}
    assert results["backup"].level == "fail"


def test_doctor_warns_on_restic_degradation(restic_absent: None, tmp_path: Path) -> None:
    _configure(last_success=datetime.now(timezone.utc).isoformat())
    results = {r.key: r for r in diagnose_backup(_doctor_config(tmp_path))}
    assert results["backup"].level == "ok"
    assert results["backup_tool"].level == "warn"
    assert "rsync" in results["backup_tool"].message


def test_doctor_fails_on_corrupt_backup_json(restic_present: None) -> None:
    backup.config_dir().mkdir(parents=True, exist_ok=True)
    backup.config_path().write_text("{ not json", encoding="utf-8")
    results = {r.key: r for r in diagnose_backup(SetupConfig(vault=Path("/tmp/v")))}
    assert results["backup"].level == "fail"


# -- CLI wiring --------------------------------------------------------------------


def test_backup_subcommands_parse() -> None:
    args = build_parser().parse_args(["backup", "init", "--repo", REPO])
    assert args.command == "backup" and args.backup_command == "init" and args.repo == REPO
    args = build_parser().parse_args(["backup", "run", "--folder", "OMI"])
    assert args.backup_command == "run" and args.folder == "OMI"
    args = build_parser().parse_args(["backup", "verify"])
    assert args.backup_command == "verify"
    args = build_parser().parse_args(["backup", "install-timer"])
    assert args.backup_command == "install-timer"


def test_backup_requires_a_subcommand() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["backup"])


def test_cli_backup_run_reports_errors(
    restic_present: None, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = main(["backup", "run", "--vault", str(tmp_path), "--folder", "OMI"])
    assert rc == 1
    assert "no backup configured" in capsys.readouterr().err


def test_cli_backup_run_end_to_end(
    restic_present: None, fake_subprocess: list[list[str]], tmp_path: Path
) -> None:
    _configure()
    omi = _omi(tmp_path / "v")
    rc = main(["backup", "run", "--vault", str(tmp_path / "v" / "vault"), "--folder", "OMI"])
    assert rc == 0
    assert fake_subprocess[0] == ["restic", "backup", str(omi)]


def test_doctor_includes_backup_check(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    main(["doctor", "--vault", str(tmp_path / "vault"), "--folder", "OMI"])
    assert "no backup configured" in capsys.readouterr().out
