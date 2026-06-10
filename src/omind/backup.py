# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Aaron K. Clark
"""Encrypted, unattended backup of the OMI folder, wrapping restic.

The OMI vault is a single copy of long-term memory on one disk — and it holds
sensitive inventory, so it must never be pushed to a forge. ``omind backup``
keeps an encrypted off-machine copy instead:

  * ``init``          — generate the repository password (kept only in
    ``~/.config/omind/backup.pass``, mode 0600) and initialize the restic repo.
  * ``run``           — ``restic backup`` + ``restic forget --prune`` with a
    7 daily / 4 weekly / 6 monthly retention. Three consecutive failures write
    a ``BACKUP FAILING`` note into the vault through the single-writer path so
    the problem surfaces in session priming; the next success removes it.
  * ``verify``        — ``restic check``, then restore the latest snapshot's
    ``index.md`` sentinel and diff it against the live file.
  * ``install-timer`` — a daily systemd *user* timer running ``backup run``.

When restic is absent, ``run`` degrades to an rsync ``--link-dest`` dated
snapshot copy and ``omind doctor`` reports the degradation as a warning.

All subprocess calls go through :func:`_run` (the :mod:`omind.provision`
pattern) so tests can fake ``subprocess.run`` and never touch a real restic,
rsync, or systemctl. The password is never printed or logged.
"""

from __future__ import annotations

import contextlib
import json
import os
import secrets
import shutil
import subprocess
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from omind.notes import upsert_note
from omind.paths import INDEX_FILENAME
from omind.provision import CheckResult, Logger, SetupConfig
from omind.store import NoteError, NoteFields, NoteNotFoundError, OmiStore

PASS_FILENAME = "backup.pass"
CONFIG_FILENAME = "backup.json"

#: Retention policy applied by ``restic forget --prune`` after every backup.
KEEP_DAILY, KEEP_WEEKLY, KEEP_MONTHLY = 7, 4, 6

#: Consecutive ``backup run`` failures before the alert note is written.
FAILURE_NOTE_THRESHOLD = 3

#: A last success older than this many hours turns the doctor check into a warn.
FRESH_HOURS = 48

FAILING_NOTE_TITLE = "BACKUP FAILING"
FAILING_NOTE_FILENAME = f"{FAILING_NOTE_TITLE}.md"

SERVICE_UNIT_NAME = "omind-backup.service"
TIMER_UNIT_NAME = "omind-backup.timer"

#: Restic repository schemes rsync cannot reach; the degraded path refuses them.
_RSYNC_UNSUPPORTED_SCHEMES = (
    "sftp://", "s3:", "b2:", "azure:", "gs:", "rest:", "rclone:", "swift:",
)


class BackupError(Exception):
    """A backup step failed (missing config, restic/rsync error, ...)."""


# -- locations ----------------------------------------------------------------


def xdg_config_home() -> Path:
    """The base config directory, honoring ``XDG_CONFIG_HOME``."""
    env = os.environ.get("XDG_CONFIG_HOME")
    return Path(env).expanduser() if env else Path.home() / ".config"


def config_dir() -> Path:
    """omind's own config directory (holds the backup password and repo spec)."""
    return xdg_config_home() / "omind"


def password_path() -> Path:
    """The restic repository password file. NEVER print or commit its content."""
    return config_dir() / PASS_FILENAME


def config_path() -> Path:
    """The backup state file: repo spec, failure counter, last-success stamp."""
    return config_dir() / CONFIG_FILENAME


def systemd_user_dir() -> Path:
    """Where systemd *user* units live, honoring ``XDG_CONFIG_HOME``."""
    return xdg_config_home() / "systemd" / "user"


# -- config -------------------------------------------------------------------


@dataclass
class BackupConfig:
    """The persisted backup state (``backup.json``)."""

    repo: str
    consecutive_failures: int = 0
    last_success: str | None = None  # ISO-8601, UTC
    last_snapshot: str | None = None  # rsync fallback: previous --link-dest dir


def load_config() -> BackupConfig | None:
    """Read ``backup.json``; ``None`` when no backup has been configured."""
    path = config_path()
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise BackupError(
            f"{path} is not valid JSON ({exc}); fix or remove it and re-run "
            "`omind backup init`."
        ) from exc
    if not isinstance(data, dict) or not isinstance(data.get("repo"), str) or not data["repo"]:
        return None
    return BackupConfig(
        repo=data["repo"],
        consecutive_failures=int(data.get("consecutive_failures") or 0),
        last_success=data.get("last_success") or None,
        last_snapshot=data.get("last_snapshot") or None,
    )


def save_config(config: BackupConfig) -> None:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(config), indent=2) + "\n", encoding="utf-8")


# -- subprocess plumbing --------------------------------------------------------


def _run(
    cmd: list[str],
    *,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a command, mapping the usual failures to :class:`BackupError`.

    Output is always captured so a restic error can never leak repository
    details (or worse) into a systemd journal line we don't control.
    """
    if os.name == "nt":
        # CreateProcess won't resolve restic.cmd-style shims from a bare
        # name; shutil.which finds the executable with its extension.
        resolved = shutil.which(cmd[0])
        if resolved:
            cmd = [resolved, *cmd[1:]]
    try:
        return subprocess.run(
            cmd,
            check=True,
            capture_output=True,
            text=True,
            env=env,
        )
    except FileNotFoundError as exc:
        raise BackupError(f"command not found: {cmd[0]}") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip()
        raise BackupError(f"command failed: {' '.join(cmd)}\n{detail}") from exc


def restic_available() -> bool:
    return shutil.which("restic") is not None


def _restic_env(repo: str) -> dict[str, str]:
    """The environment restic needs: repo spec + password *file* (never inline)."""
    passfile = password_path()
    if not passfile.is_file():
        raise BackupError(
            f"password file missing: {passfile} (run `omind backup init` first)"
        )
    env = dict(os.environ)
    env["RESTIC_REPOSITORY"] = repo
    env["RESTIC_PASSWORD_FILE"] = str(passfile)
    return env


# -- init -----------------------------------------------------------------------


def init_backup(repo: str, log: Logger = print) -> None:
    """Generate the password file (0600, once) and initialize the restic repo.

    Refuses to overwrite an existing password file: losing it means losing
    every snapshot encrypted with it.
    """
    repo = repo.strip()
    if not repo:
        raise BackupError("a repository spec is required (e.g. sftp:host:/path or a local path)")
    passfile = password_path()
    passfile.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(passfile, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise BackupError(
            f"refusing to overwrite existing password file: {passfile} "
            "(it decrypts every existing snapshot)"
        ) from None
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(secrets.token_urlsafe(32) + "\n")
    os.chmod(passfile, 0o600)
    log(f"  wrote password file {passfile} (0600) — copy it somewhere safe, never commit it")

    if restic_available():
        _run(["restic", "init"], env=_restic_env(repo))
        log(f"  initialized encrypted restic repository: {repo}")
    else:
        log(
            "  WARNING: restic not found — `omind backup run` will degrade to "
            "unencrypted rsync snapshots until restic is installed"
        )
    save_config(BackupConfig(repo=repo))
    log(f"  recorded repository spec in {config_path()}")


# -- run --------------------------------------------------------------------------


def run_backup(omi_dir: Path, log: Logger = print) -> None:
    """Back up the OMI folder, then apply retention. Raises on failure.

    Failures are counted in ``backup.json``; at :data:`FAILURE_NOTE_THRESHOLD`
    consecutive failures a ``BACKUP FAILING`` note is upserted into the vault
    through the single-writer path so it surfaces in session priming. The next
    success clears the counter and deletes the note.
    """
    config = _require_config()
    if not omi_dir.is_dir():
        raise BackupError(f"OMI folder missing: {omi_dir} (run `omind setup`)")
    log(f"omind backup run -> {config.repo}")
    try:
        if restic_available():
            _restic_backup(config, omi_dir, log)
        else:
            log("  restic not found — degrading to an rsync --link-dest snapshot copy")
            _rsync_backup(config, omi_dir, log)
    except BackupError as exc:
        _record_failure(config, omi_dir, str(exc), log)
        raise
    _record_success(config, omi_dir, log)


def _require_config() -> BackupConfig:
    config = load_config()
    if config is None:
        raise BackupError("no backup configured — run `omind backup init --repo <dest>` first")
    return config


def _restic_backup(config: BackupConfig, omi_dir: Path, log: Logger) -> None:
    env = _restic_env(config.repo)
    _run(["restic", "backup", str(omi_dir)], env=env)
    log(f"  backed up {omi_dir}")
    _run(
        [
            "restic", "forget", "--prune",
            "--keep-daily", str(KEEP_DAILY),
            "--keep-weekly", str(KEEP_WEEKLY),
            "--keep-monthly", str(KEEP_MONTHLY),
        ],
        env=env,
    )
    log(
        f"  applied retention: {KEEP_DAILY} daily / {KEEP_WEEKLY} weekly / "
        f"{KEEP_MONTHLY} monthly"
    )


def _rsync_destination(repo: str) -> str:
    """Map the restic repo spec to an rsync destination, or refuse.

    ``sftp:host:/path`` becomes rsync's native ``host:/path``; a local path
    passes through. Object-store schemes have no rsync equivalent.
    """
    if repo.lower().startswith(_RSYNC_UNSUPPORTED_SCHEMES):
        raise BackupError(
            f"restic is not installed and the repository spec {repo!r} is not "
            "reachable by rsync — install restic to back up to it"
        )
    if repo.lower().startswith("sftp:"):
        return repo[len("sftp:"):]
    return repo


def _rsync_backup(config: BackupConfig, omi_dir: Path, log: Logger) -> None:
    dest = _rsync_destination(config.repo).rstrip("/")
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M%SZ")
    cmd = ["rsync", "-a", "--delete"]
    if config.last_snapshot:
        # Hard-link unchanged files against the previous snapshot (sibling dir).
        cmd.append(f"--link-dest=../{config.last_snapshot}")
    cmd.extend([f"{omi_dir}/", f"{dest}/{stamp}/"])
    _run(cmd)
    config.last_snapshot = stamp
    log(f"  rsync snapshot written: {dest}/{stamp}/ (unencrypted — install restic)")


def _record_success(config: BackupConfig, omi_dir: Path, log: Logger) -> None:
    config.consecutive_failures = 0
    config.last_success = datetime.now(timezone.utc).isoformat()
    save_config(config)
    with contextlib.suppress(NoteNotFoundError):
        OmiStore(omi_dir).delete_note(FAILING_NOTE_FILENAME)
        log(f"  backup healthy again — removed the '{FAILING_NOTE_TITLE}' note")
    log("  backup succeeded")


def _record_failure(config: BackupConfig, omi_dir: Path, error: str, log: Logger) -> None:
    config.consecutive_failures += 1
    save_config(config)
    log(f"  backup failed ({config.consecutive_failures} consecutive)")
    if config.consecutive_failures < FAILURE_NOTE_THRESHOLD:
        return
    fields = NoteFields(
        title=FAILING_NOTE_TITLE,
        summary=(
            f"{config.consecutive_failures} consecutive `omind backup run` failures — "
            "long-term memory is NOT being backed up."
        ),
        details=(
            "Last error:\n\n```\n"
            + error
            + "\n```\n\nDiagnose with `omind doctor`; the next successful "
            "`omind backup run` removes this note automatically."
        ),
        tags=["backup", "alert"],
    )
    try:
        upsert_note(omi_dir, fields)  # the single-writer path (lock + atomic write)
        log(f"  wrote '{FAILING_NOTE_TITLE}' note into the vault")
    except (NoteError, OSError) as exc:
        # Never let the alert mask the underlying backup error.
        log(f"  NOTE: could not write the '{FAILING_NOTE_TITLE}' note: {exc}")


# -- verify -----------------------------------------------------------------------


def verify_backup(omi_dir: Path, log: Logger = print) -> list[CheckResult]:
    """``restic check``, then diff the latest snapshot's ``index.md`` sentinel.

    Drift between the restored sentinel and the live file is a warning (the
    snapshot is merely older than the vault); byte-identical is ok.
    """
    config = _require_config()
    if not restic_available():
        raise BackupError(
            "restic not found — `omind backup verify` needs restic to check and "
            "restore snapshots"
        )
    env = _restic_env(config.repo)
    log(f"omind backup verify -> {config.repo}")
    results: list[CheckResult] = []
    _run(["restic", "check"], env=env)
    results.append(CheckResult("backup_check", "ok", "restic check passed"))

    sentinel = omi_dir / INDEX_FILENAME
    with tempfile.TemporaryDirectory(prefix="omind-backup-verify-") as tmp:
        _run(
            ["restic", "restore", "latest", "--target", tmp, "--include", str(sentinel)],
            env=env,
        )
        restored = Path(tmp) / sentinel.relative_to(sentinel.anchor)
        results.append(_diff_sentinel(sentinel, restored))
    return results


def _diff_sentinel(sentinel: Path, restored: Path) -> CheckResult:
    if not restored.is_file():
        return CheckResult(
            "backup_sentinel", "warn", f"latest snapshot does not contain {sentinel}"
        )
    if not sentinel.is_file():
        return CheckResult(
            "backup_sentinel", "warn", f"live sentinel missing: {sentinel} (run `omind setup`)"
        )
    if restored.read_bytes() == sentinel.read_bytes():
        return CheckResult(
            "backup_sentinel", "ok", f"sentinel {sentinel.name} is byte-identical in the backup"
        )
    return CheckResult(
        "backup_sentinel",
        "warn",
        f"sentinel {sentinel.name} drifts from the latest snapshot — the vault has "
        "changed since the last `omind backup run`",
    )


# -- systemd user timer -------------------------------------------------------------


def install_timer(config: SetupConfig, log: Logger = print) -> None:
    """Install + enable a daily systemd *user* timer running ``omind backup run``.

    ``Type=oneshot`` with no dependents: a failing backup can never block
    login, the session, or anything else — failures only surface through the
    counter / ``BACKUP FAILING`` note / ``omind doctor``.
    """
    _require_config()  # don't install a timer that can only ever fail
    unit_dir = systemd_user_dir()
    unit_dir.mkdir(parents=True, exist_ok=True)
    omind_exe = shutil.which("omind") or "omind"
    service = (
        "[Unit]\n"
        "Description=omind encrypted vault backup\n"
        "\n"
        "[Service]\n"
        "Type=oneshot\n"
        f'ExecStart={omind_exe} backup run --vault "{config.vault}" --folder {config.folder}\n'
    )
    timer = (
        "[Unit]\n"
        "Description=Daily omind encrypted vault backup\n"
        "\n"
        "[Timer]\n"
        "OnCalendar=daily\n"
        "Persistent=true\n"
        "RandomizedDelaySec=15m\n"
        "\n"
        "[Install]\n"
        "WantedBy=timers.target\n"
    )
    (unit_dir / SERVICE_UNIT_NAME).write_text(service, encoding="utf-8")
    (unit_dir / TIMER_UNIT_NAME).write_text(timer, encoding="utf-8")
    log(f"  wrote {unit_dir / SERVICE_UNIT_NAME} and {unit_dir / TIMER_UNIT_NAME}")
    _run(["systemctl", "--user", "daemon-reload"])
    _run(["systemctl", "--user", "enable", "--now", TIMER_UNIT_NAME])
    log(f"  enabled {TIMER_UNIT_NAME} (daily)")


# -- doctor -------------------------------------------------------------------------


def _hours_since(iso: str) -> float | None:
    try:
        then = datetime.fromisoformat(iso)
    except ValueError:
        return None
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - then).total_seconds() / 3600.0


def diagnose_backup(config: SetupConfig) -> list[CheckResult]:
    """The backup doctor checks (pure read; agent-independent).

    Not configured → warn; configured → ok when the last success is fresher
    than :data:`FRESH_HOURS`, warn when staler, fail at
    :data:`FAILURE_NOTE_THRESHOLD`+ consecutive failures. A configured backup
    on a machine without restic adds a degradation warning.
    """
    del config  # backup state is per-user, not per-vault/agent
    try:
        backup_cfg = load_config()
    except BackupError as exc:
        return [CheckResult("backup", "fail", str(exc))]
    if backup_cfg is None:
        return [
            CheckResult(
                "backup",
                "warn",
                "no backup configured — the vault is a single copy on one disk "
                "(run `omind backup init --repo <dest>`)",
            )
        ]

    results: list[CheckResult] = []
    if backup_cfg.consecutive_failures >= FAILURE_NOTE_THRESHOLD:
        results.append(
            CheckResult(
                "backup",
                "fail",
                f"backup FAILING: {backup_cfg.consecutive_failures} consecutive failures "
                f"(repo {backup_cfg.repo}) — see the '{FAILING_NOTE_TITLE}' note",
            )
        )
    elif backup_cfg.last_success is None:
        results.append(
            CheckResult(
                "backup",
                "warn",
                f"backup configured ({backup_cfg.repo}) but no successful run yet — "
                "run `omind backup run`",
            )
        )
    else:
        age = _hours_since(backup_cfg.last_success)
        if age is not None and age < FRESH_HOURS:
            results.append(
                CheckResult(
                    "backup", "ok", f"last backup succeeded {age:.0f}h ago -> {backup_cfg.repo}"
                )
            )
        else:
            shown = f"{age:.0f}h ago" if age is not None else "at an unparsable time"
            results.append(
                CheckResult(
                    "backup",
                    "warn",
                    f"last successful backup was {shown} (>{FRESH_HOURS}h) — "
                    "check the timer / run `omind backup run`",
                )
            )
    if not restic_available():
        results.append(
            CheckResult(
                "backup_tool",
                "warn",
                "restic not found — backups degrade to unencrypted rsync snapshots",
            )
        )
    return results
