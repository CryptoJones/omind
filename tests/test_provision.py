# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Aaron K. Clark
"""Tests for omind.provision: dry-run, idempotency, prereqs, bad layouts."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from omind import provision, seeds
from omind.provision import Provisioner, ProvisionError, SetupConfig, default_vault_path

# Captured before the autouse isolate_settings fixture patches the module attribute,
# so the path-resolution tests can exercise the real function.
_real_claude_settings_path = provision.claude_settings_path


@pytest.fixture(autouse=True)
def clear_claude_config_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the dev machine's CLAUDE_CONFIG_DIR from leaking into path resolution."""
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)


@pytest.fixture
def fake_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(provision.shutil, "which", lambda name: f"/usr/bin/{name}")


@pytest.fixture
def fake_subprocess(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, "obsidian: Connected", "")

    monkeypatch.setattr(provision.subprocess, "run", fake_run)
    return calls


@pytest.fixture
def isolate_claude(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    cfg = tmp_path / "claude.json"
    monkeypatch.setattr(provision, "claude_config_path", lambda: cfg)
    return cfg


@pytest.fixture(autouse=True)
def isolate_server_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Keep the managed install dir + EOF guard inside tmp, never real ~/.claude."""
    home = tmp_path / "mcp-servers"
    monkeypatch.setattr(provision, "mcp_servers_dir", lambda: home)
    return home


@pytest.fixture(autouse=True)
def isolate_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Never touch the real ~/.claude/settings.json when (un)installing hooks."""
    settings = tmp_path / "settings.json"
    monkeypatch.setattr(provision, "claude_settings_path", lambda: settings)
    return settings


def _config(tmp_path: Path, **kw: object) -> SetupConfig:
    return SetupConfig(vault=tmp_path / "vault", **kw)  # type: ignore[arg-type]


def _quiet(_: str) -> None:
    pass


def _write_server_config(cfg: Path, omi_path: str) -> None:
    """Write a registered server in the current leak-free node form."""
    server = {
        "command": "node",
        "args": [
            "--require",
            str(provision.eof_guard_path()),
            str(provision.obsidian_mcp_entry()),
            omi_path,
        ],
    }
    cfg.write_text(json.dumps({"mcpServers": {"obsidian": server}}))


def _write_legacy_server_config(cfg: Path, omi_path: str) -> None:
    """Write the old, leak-prone ``npx -y obsidian-mcp`` registration."""
    server = {"command": "npx", "args": ["-y", "obsidian-mcp", omi_path]}
    cfg.write_text(json.dumps({"mcpServers": {"obsidian": server}}))


def _provision_files(config: SetupConfig) -> None:
    obs = config.omi_dir / ".obsidian"
    obs.mkdir(parents=True)
    (obs / "app.json").write_text("{}")
    (config.omi_dir / seeds.MEMORY_TEMPLATE_FILENAME).write_text("x")
    (config.omi_dir / seeds.INDEX_FILENAME).write_text("x")
    guard = provision.eof_guard_path()
    guard.parent.mkdir(parents=True, exist_ok=True)
    guard.write_text(seeds.EOF_GUARD_JS)


def _install_hooks(config: SetupConfig) -> None:
    """Write the auto-memory hooks into the isolated settings.json."""
    Provisioner(config, log=_quiet).ensure_hooks_installed()


def test_default_vault_path_shape() -> None:
    path = default_vault_path()
    assert path.name == "Obsidian Vault"
    assert path.parent.name == "Documents"


def test_dry_run_creates_nothing(
    tmp_path: Path, fake_tools: None, isolate_claude: Path
) -> None:
    config = _config(tmp_path, dry_run=True)
    actions = Provisioner(config, log=_quiet).run()
    assert not config.omi_dir.exists()
    assert any("write" in a for a in actions)
    assert all(a.startswith("[dry-run]") or True for a in actions)  # smoke


def test_real_run_creates_files_and_registers(
    tmp_path: Path, fake_tools: None, fake_subprocess: list[list[str]], isolate_claude: Path
) -> None:
    config = _config(tmp_path)
    Provisioner(config, log=_quiet).run()
    obs = config.omi_dir / ".obsidian"
    assert (obs / "app.json").is_file()
    assert (obs / "core-plugins.json").is_file()
    assert (config.omi_dir / seeds.MEMORY_TEMPLATE_FILENAME).is_file()
    assert (config.omi_dir / seeds.INDEX_FILENAME).is_file()
    assert provision.eof_guard_path().is_file()
    assert any(c[:2] == ["npm", "install"] for c in fake_subprocess)
    assert fake_subprocess[-2][:6] == ["claude", "mcp", "add", "-s", "user", "obsidian"]
    add_cmd = fake_subprocess[-2]
    assert "node" in add_cmd and "--require" in add_cmd
    assert "npx" not in add_cmd
    assert fake_subprocess[-1][:3] == ["claude", "mcp", "get"]


def test_no_clobber_of_existing_seed(
    tmp_path: Path, fake_tools: None, fake_subprocess: list[list[str]], isolate_claude: Path
) -> None:
    config = _config(tmp_path)
    config.omi_dir.mkdir(parents=True)
    template = config.omi_dir / seeds.MEMORY_TEMPLATE_FILENAME
    template.write_text("DO NOT TOUCH")
    Provisioner(config, log=_quiet).run()
    assert template.read_text() == "DO NOT TOUCH"


def test_idempotent_registration_when_path_matches(
    tmp_path: Path, fake_tools: None, fake_subprocess: list[list[str]], isolate_claude: Path
) -> None:
    config = _config(tmp_path)
    _write_server_config(isolate_claude, str(config.omi_dir))
    Provisioner(config, log=_quiet).run()
    assert not any(c[:3] == ["claude", "mcp", "add"] for c in fake_subprocess)
    assert not any(c[:3] == ["claude", "mcp", "remove"] for c in fake_subprocess)


def test_changed_path_triggers_reregistration(
    tmp_path: Path, fake_tools: None, fake_subprocess: list[list[str]], isolate_claude: Path
) -> None:
    config = _config(tmp_path)
    _write_server_config(isolate_claude, "/old/path")
    Provisioner(config, log=_quiet).run()
    assert any(c[:3] == ["claude", "mcp", "remove"] for c in fake_subprocess)
    assert any(c[:3] == ["claude", "mcp", "add"] for c in fake_subprocess)


def test_migrates_legacy_npx_registration(
    tmp_path: Path, fake_tools: None, fake_subprocess: list[list[str]], isolate_claude: Path
) -> None:
    config = _config(tmp_path)
    # Old leak-prone form, already pointing at the right folder: must still migrate.
    _write_legacy_server_config(isolate_claude, str(config.omi_dir))
    Provisioner(config, log=_quiet).run()
    assert any(c[:3] == ["claude", "mcp", "remove"] for c in fake_subprocess)
    add_cmd = next(c for c in fake_subprocess if c[:3] == ["claude", "mcp", "add"])
    assert "node" in add_cmd and "--require" in add_cmd
    assert "npx" not in add_cmd


def test_obsidian_dir_is_a_file_errors(
    tmp_path: Path, fake_tools: None, fake_subprocess: list[list[str]], isolate_claude: Path
) -> None:
    config = _config(tmp_path)
    config.omi_dir.mkdir(parents=True)
    (config.omi_dir / ".obsidian").write_text("oops, a file")
    with pytest.raises(ProvisionError, match="not a directory"):
        Provisioner(config, log=_quiet).run()


def test_missing_prereq_errors(
    tmp_path: Path, isolate_claude: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        provision.shutil, "which", lambda name: None if name == "npm" else f"/usr/bin/{name}"
    )
    config = _config(tmp_path)
    with pytest.raises(ProvisionError, match="npm"):
        Provisioner(config, log=_quiet).run()


def test_idempotent_files_on_rerun(
    tmp_path: Path, fake_tools: None, fake_subprocess: list[list[str]], isolate_claude: Path
) -> None:
    config = _config(tmp_path)
    Provisioner(config, log=_quiet).run()
    Provisioner(config, log=_quiet).run()  # must not raise
    template = config.omi_dir / seeds.MEMORY_TEMPLATE_FILENAME
    assert template.read_text() == seeds.MEMORY_TEMPLATE


def test_doctor_healthy_when_provisioned(
    tmp_path: Path, fake_tools: None, isolate_claude: Path
) -> None:
    config = _config(tmp_path)
    _provision_files(config)
    _write_server_config(isolate_claude, str(config.omi_dir))
    _install_hooks(config)
    results = {r.key: r for r in provision.diagnose(config)}
    assert results["omi_dir"].level == "ok"
    assert results["obsidian_config"].level == "ok"
    assert results["seeds"].level == "ok"
    assert results["mcp_registration"].level == "ok"
    assert results["hooks"].level == "ok"
    assert provision.run_doctor(config, log=_quiet) == 0


def test_doctor_flags_missing_setup(
    tmp_path: Path, isolate_claude: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(provision.shutil, "which", lambda name: None)
    config = _config(tmp_path)
    levels = {r.key: r.level for r in provision.diagnose(config)}
    assert levels["omi_dir"] == "fail"
    assert levels["obsidian_config"] == "fail"
    assert levels["mcp_registration"] == "fail"
    assert provision.run_doctor(config, log=_quiet) == 1


def test_doctor_warns_on_path_mismatch(
    tmp_path: Path, fake_tools: None, isolate_claude: Path
) -> None:
    config = _config(tmp_path)
    _provision_files(config)
    _write_server_config(isolate_claude, "/some/other/path")
    _install_hooks(config)
    results = {r.key: r for r in provision.diagnose(config)}
    assert results["mcp_registration"].level == "warn"
    assert provision.run_doctor(config, log=_quiet) == 0  # warnings don't fail


def test_doctor_warns_on_legacy_npx_form(
    tmp_path: Path, fake_tools: None, isolate_claude: Path
) -> None:
    config = _config(tmp_path)
    _provision_files(config)
    _write_legacy_server_config(isolate_claude, str(config.omi_dir))
    _install_hooks(config)
    results = {r.key: r for r in provision.diagnose(config)}
    # right path, wrong (leak-prone) command form -> warn, not ok
    assert results["mcp_registration"].level == "warn"
    assert "npx" in results["mcp_registration"].message
    assert provision.run_doctor(config, log=_quiet) == 0


def test_doctor_warns_on_missing_eof_guard(
    tmp_path: Path, fake_tools: None, isolate_claude: Path
) -> None:
    config = _config(tmp_path)
    _provision_files(config)
    _write_server_config(isolate_claude, str(config.omi_dir))
    provision.eof_guard_path().unlink()  # simulate a pre-migration setup
    results = {r.key: r for r in provision.diagnose(config)}
    assert results["eof_guard"].level == "warn"


def test_claude_config_path_is_home_dotclaude_json(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Regression: the config holding mcpServers is ~/.claude.json, not ~/.claude/.

    Pointing at ~/.claude/.claude.json (which never exists) made
    registered_server() always return None — a false 'not registered' in doctor
    and a spurious 'already exists' on setup re-run.
    """
    monkeypatch.setattr(provision.Path, "home", classmethod(lambda cls: tmp_path))
    (tmp_path / ".claude.json").write_text("{}", encoding="utf-8")
    assert provision.claude_config_path() == tmp_path / ".claude.json"


def test_claude_config_path_falls_back_to_legacy_when_only_legacy_exists(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """If only the old ~/.claude/.claude.json exists, keep using it."""
    monkeypatch.setattr(provision.Path, "home", classmethod(lambda cls: tmp_path))
    legacy = tmp_path / ".claude" / ".claude.json"
    legacy.parent.mkdir(parents=True)
    legacy.write_text("{}", encoding="utf-8")
    assert provision.claude_config_path() == legacy


def test_claude_config_path_honors_claude_config_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Regression: CLAUDE_CONFIG_DIR relocates the CLI config wholesale.

    With the env var set, the CLI reads/writes $CLAUDE_CONFIG_DIR/.claude.json
    even when a stale ~/.claude.json exists — reading the stale file made
    doctor report a false 'not registered' and setup abort on 'already exists'.
    """
    monkeypatch.setattr(provision.Path, "home", classmethod(lambda cls: tmp_path))
    (tmp_path / ".claude.json").write_text("{}", encoding="utf-8")  # stale decoy
    config_dir = tmp_path / "custom-claude"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))
    assert provision.claude_config_path() == config_dir / ".claude.json"


def test_claude_settings_path_default_and_claude_config_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """settings.json follows the config dir: ~/.claude by default, env var wins."""
    monkeypatch.setattr(provision.Path, "home", classmethod(lambda cls: tmp_path))
    assert _real_claude_settings_path() == tmp_path / ".claude" / "settings.json"
    config_dir = tmp_path / "custom-claude"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))
    assert _real_claude_settings_path() == config_dir / "settings.json"


def test_doctor_finds_server_via_canonical_config_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fake_tools: None
) -> None:
    """End-to-end: with a real ~/.claude.json, doctor sees the registration.

    This is the bug we hit live — doctor reported 'not registered' although
    `claude mcp get` showed the server connected at user scope.
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(provision.Path, "home", classmethod(lambda cls: home))
    config = _config(tmp_path)
    _provision_files(config)
    _write_server_config(home / ".claude.json", str(config.omi_dir))
    results = {r.key: r for r in provision.diagnose(config)}
    assert results["mcp_registration"].level == "ok"


# -- auto-memory hooks (settings.json) --------------------------------------


def _omind_entries(settings: Path, event: str) -> list[dict[str, object]]:
    data = json.loads(settings.read_text(encoding="utf-8"))
    return [e for e in data["hooks"][event] if provision._entry_has_omind_marker(e)]


def test_setup_installs_hooks_idempotently(tmp_path: Path, isolate_settings: Path) -> None:
    config = _config(tmp_path)
    _install_hooks(config)
    _install_hooks(config)  # second run must not duplicate
    for event in provision.HANDLED_EVENTS:
        entries = _omind_entries(isolate_settings, event)
        assert len(entries) == 1
        cmd = provision._entry_command_text(entries[0])
        assert f"hook {event}" in cmd  # the `hook <event>` subcommand is present
        assert provision.HOOK_MARKER in cmd  # detectable marker
        assert str(config.vault) in cmd


def test_setup_preserves_existing_settings_keys(tmp_path: Path, isolate_settings: Path) -> None:
    isolate_settings.write_text(
        json.dumps({"theme": "dark", "skipDangerousModePermissionPrompt": True})
    )
    _install_hooks(_config(tmp_path))
    data = json.loads(isolate_settings.read_text(encoding="utf-8"))
    assert data["theme"] == "dark"
    assert data["skipDangerousModePermissionPrompt"] is True
    assert "hooks" in data


def test_setup_preserves_user_hooks(tmp_path: Path, isolate_settings: Path) -> None:
    user_entry = {"hooks": [{"type": "command", "command": "echo mine"}]}
    isolate_settings.write_text(json.dumps({"hooks": {"PostToolUse": [user_entry]}}))
    _install_hooks(_config(tmp_path))
    entries = json.loads(isolate_settings.read_text(encoding="utf-8"))["hooks"]["PostToolUse"]
    assert user_entry in entries  # untouched
    assert any(provision._entry_has_omind_marker(e) for e in entries)
    assert len(entries) == 2


def test_hook_path_drift_triggers_update(tmp_path: Path, isolate_settings: Path) -> None:
    stale_cmd = 'omind hook PostToolUse --vault "/old/vault" --folder OMI'
    stale = {
        "matcher": "*",
        "hooks": [{"type": "command", "command": stale_cmd}],
    }
    isolate_settings.write_text(json.dumps({"hooks": {"PostToolUse": [stale]}}))
    config = _config(tmp_path)
    _install_hooks(config)
    entries = _omind_entries(isolate_settings, "PostToolUse")
    assert len(entries) == 1
    text = provision._entry_command_text(entries[0])
    assert str(config.vault) in text
    assert "/old/vault" not in text


def test_corrupt_settings_json_errors(tmp_path: Path, isolate_settings: Path) -> None:
    isolate_settings.write_text("{ not valid json")
    with pytest.raises(ProvisionError):
        _install_hooks(_config(tmp_path))


def test_hooks_dry_run_writes_nothing(tmp_path: Path, isolate_settings: Path) -> None:
    Provisioner(_config(tmp_path, dry_run=True), log=_quiet).ensure_hooks_installed()
    assert not isolate_settings.exists()


def test_doctor_ok_when_hooks_installed(
    tmp_path: Path, fake_tools: None, isolate_claude: Path
) -> None:
    config = _config(tmp_path)
    _provision_files(config)
    _write_server_config(isolate_claude, str(config.omi_dir))
    _install_hooks(config)
    results = {r.key: r for r in provision.diagnose(config)}
    assert results["hooks"].level == "ok"


def test_doctor_fail_when_hooks_absent(
    tmp_path: Path, fake_tools: None, isolate_claude: Path
) -> None:
    config = _config(tmp_path)
    _provision_files(config)
    _write_server_config(isolate_claude, str(config.omi_dir))
    results = {r.key: r for r in provision.diagnose(config)}
    assert results["hooks"].level == "fail"
    assert provision.run_doctor(config, log=_quiet) == 1


def test_doctor_warns_on_hook_path_mismatch(
    tmp_path: Path, fake_tools: None, isolate_claude: Path, isolate_settings: Path
) -> None:
    config = _config(tmp_path)
    _provision_files(config)
    _write_server_config(isolate_claude, str(config.omi_dir))
    isolate_settings.write_text(
        json.dumps(
            {
                "hooks": {
                    event: [
                        {
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": (
                                        f'omind hook {event} --vault "/elsewhere" --folder OMI'
                                    ),
                                }
                            ]
                        }
                    ]
                    for event in provision.HANDLED_EVENTS
                }
            }
        )
    )
    results = {r.key: r for r in provision.diagnose(config)}
    assert results["hooks"].level == "warn"
    assert provision.run_doctor(config, log=_quiet) == 0


# -- hook failure breadcrumbs in doctor ----------------------------------------


def test_diagnose_hook_failures_ok_when_no_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    result = provision._diagnose_hook_failures()
    assert result.level == "ok"


def test_diagnose_hook_failures_warns_on_recent_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omind import hooks

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    log = hooks.failure_log_path()
    log.parent.mkdir(parents=True)
    log.write_text("2026-06-10T12:00:00 append_entry(/x): OSError()\n", encoding="utf-8")
    result = provision._diagnose_hook_failures()
    assert result.level == "warn"
    assert str(log) in result.message


def test_diagnose_hook_failures_ok_when_entries_are_stale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os
    import time

    from omind import hooks

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    log = hooks.failure_log_path()
    log.parent.mkdir(parents=True)
    log.write_text("old failure\n", encoding="utf-8")
    stale = time.time() - 8 * 86400
    os.utime(log, (stale, stale))
    result = provision._diagnose_hook_failures()
    assert result.level == "ok"
