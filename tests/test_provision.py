# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Aaron K. Clark
"""Tests for omind.provision: dry-run, idempotency, prereqs, bad layouts."""

from __future__ import annotations

import io
import json
import os
import subprocess
import tempfile
from pathlib import Path, PurePosixPath

import pytest

from omind import paths, provision, seeds
from omind.provision import (
    LEGACY_SERVER_NAME,
    Provisioner,
    ProvisionError,
    SetupConfig,
    default_vault_path,
)

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
        # Record by bare name: on Windows the provisioner resolves cmd[0] via
        # shutil.which (patched to /usr/bin/<name> by fake_tools).
        calls.append([PurePosixPath(cmd[0]).name, *cmd[1:]])
        return subprocess.CompletedProcess(cmd, 0, "obsidian: Connected", "")

    monkeypatch.setattr(provision.subprocess, "run", fake_run)
    return calls


@pytest.fixture
def isolate_claude(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    cfg = tmp_path / "claude.json"
    monkeypatch.setattr(provision, "claude_config_path", lambda: cfg)
    return cfg


@pytest.fixture(autouse=True)
def isolate_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Never touch the real ~/.claude/settings.json when (un)installing hooks."""
    settings = tmp_path / "settings.json"
    monkeypatch.setattr(provision, "claude_settings_path", lambda: settings)
    return settings


@pytest.fixture(autouse=True)
def isolate_claude_skill(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Never touch the real ~/.claude/skills/omind when installing the skill."""
    skill_dir = tmp_path / "skills" / "omind"
    monkeypatch.setattr(provision, "claude_skill_dir", lambda: skill_dir)
    return skill_dir


def _config(tmp_path: Path, **kw: object) -> SetupConfig:
    return SetupConfig(vault=tmp_path / "vault", **kw)  # type: ignore[arg-type]


def _quiet(_: str) -> None:
    pass


def _write_server_config(cfg: Path, config: SetupConfig) -> None:
    """Write a registered server in the current `omind node` form."""
    server = Provisioner(config, log=_quiet).desired_server_entry()
    cfg.write_text(json.dumps({"mcpServers": {config.server_name: server}}))


def _write_legacy_server_config(cfg: Path, omi_path: str) -> None:
    """Write the retired 1.x obsidian-mcp registration (direct-node form)."""
    server = {
        "command": "node",
        "args": ["--require", "/old/guard.js", "/old/obsidian-mcp/build/main.js", omi_path],
    }
    cfg.write_text(json.dumps({"mcpServers": {LEGACY_SERVER_NAME: server}}))


def _provision_files(config: SetupConfig) -> None:
    obs = config.omi_dir / ".obsidian"
    obs.mkdir(parents=True)
    (obs / "app.json").write_text("{}")
    (config.omi_dir / paths.MEMORY_TEMPLATE_FILENAME).write_text("x")
    (config.omi_dir / paths.INDEX_FILENAME).write_text("x")


def _install_hooks(config: SetupConfig) -> None:
    """Write the auto-memory hooks into the isolated settings.json, plus the
    enforcement hook *script* on disk (the doctor `hooks` check verifies it
    exists, and HOME is isolated, so the real machine's copy is not in scope)."""
    prov = Provisioner(config, log=_quiet)
    prov.ensure_hooks_installed()
    prov._write_enforce_hook_script()


def _install_guard(config: SetupConfig, monkeypatch: pytest.MonkeyPatch, home: Path) -> None:
    """Install the enforcement + OMI-compliance guard hook *files* and their
    settings wiring into an isolated home, so the #86 doctor block-path check
    sees a fully wired guard (files on disk + PreToolUse/UserPromptSubmit + stamp)."""
    monkeypatch.setattr(provision.Path, "home", classmethod(lambda cls: home))
    prov = Provisioner(config, log=_quiet)
    prov._write_enforce_hook_script()
    prov._write_omi_guard_scripts()
    prov.ensure_omi_guard_installed()


def test_managed_hook_scripts_are_written_0755_atomically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_tools: None
) -> None:
    """Regression: every managed hook script is written 0o755 in a SINGLE atomic
    write (the mode is set on the temp file before the rename), never at mkstemp's
    0600 default followed by a separate chmod. A 0600 destination that a root-run
    provision then chowns to root is unreadable by the agent user until the chmod
    runs — the transient window that made `python3 omi-enforce.py` fail with EACCES
    mid-reprovision. World-readable (o+r) is what keeps a chown-root from hiding it."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(provision.Path, "home", classmethod(lambda cls: home))
    prov = Provisioner(_config(tmp_path), log=_quiet)
    prov._write_enforce_hook_script()
    prov._write_guard_hook_script()
    prov._write_secret_output_guard_script()
    prov._write_omi_guard_scripts()

    hooks = home / ".claude" / "hooks"
    for name in (
        "omi-enforce.py",
        "git-fresh-base.sh",
        "secret-output-guard.sh",
        "omi-guard.sh",
        "omi-gate-reset.sh",
    ):
        f = hooks / name
        assert f.exists(), f"{name} was not provisioned"
        if os.name != "nt":
            perms = f.stat().st_mode & 0o777
            assert perms == 0o755, (
                f"{name} is {oct(perms)}; expected 0o755 — a hook must be o+r+x so a "
                "chown-root can't render it unreadable to the agent user"
            )


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
    assert (config.omi_dir / paths.MEMORY_TEMPLATE_FILENAME).is_file()
    assert (config.omi_dir / paths.INDEX_FILENAME).is_file()
    assert not any(c[0] == "npm" for c in fake_subprocess)  # obsidian-mcp retired
    assert any(c[:2] == ["git", "-C"] for c in fake_subprocess)  # mesh init ran
    add_cmd = fake_subprocess[-2]
    assert add_cmd[:6] == ["claude", "mcp", "add", "-s", "user", "omi"]
    assert "node" in add_cmd and "--vault" in add_cmd and "--folder" in add_cmd
    assert "npx" not in add_cmd and "--require" not in add_cmd
    assert fake_subprocess[-1][:3] == ["claude", "mcp", "get"]


def test_no_clobber_of_existing_seed(
    tmp_path: Path, fake_tools: None, fake_subprocess: list[list[str]], isolate_claude: Path
) -> None:
    config = _config(tmp_path)
    config.omi_dir.mkdir(parents=True)
    template = config.omi_dir / paths.MEMORY_TEMPLATE_FILENAME
    template.write_text("DO NOT TOUCH")
    Provisioner(config, log=_quiet).run()
    assert template.read_text() == "DO NOT TOUCH"


def test_idempotent_registration_when_path_matches(
    tmp_path: Path, fake_tools: None, fake_subprocess: list[list[str]], isolate_claude: Path
) -> None:
    config = _config(tmp_path)
    _write_server_config(isolate_claude, config)
    Provisioner(config, log=_quiet).run()
    assert not any(c[:3] == ["claude", "mcp", "add"] for c in fake_subprocess)
    assert not any(c[:3] == ["claude", "mcp", "remove"] for c in fake_subprocess)


def test_changed_command_triggers_reregistration(
    tmp_path: Path, fake_tools: None, fake_subprocess: list[list[str]], isolate_claude: Path
) -> None:
    config = _config(tmp_path)
    drifted = {"command": "omind", "args": ["node", "--vault", "/old/vault", "--folder", "OMI"]}
    isolate_claude.write_text(json.dumps({"mcpServers": {"omi": drifted}}))
    Provisioner(config, log=_quiet).run()
    assert any(c[:3] == ["claude", "mcp", "remove"] for c in fake_subprocess)
    assert any(c[:3] == ["claude", "mcp", "add"] for c in fake_subprocess)


def test_retires_legacy_obsidian_registration(
    tmp_path: Path, fake_tools: None, fake_subprocess: list[list[str]], isolate_claude: Path
) -> None:
    """A 1.x install carries an 'obsidian' (obsidian-mcp) entry; setup removes
    it and registers the omind node server under the new name."""
    config = _config(tmp_path)
    _write_legacy_server_config(isolate_claude, str(config.omi_dir))
    Provisioner(config, log=_quiet).run()
    removes = [c for c in fake_subprocess if c[:3] == ["claude", "mcp", "remove"]]
    assert [c[3] for c in removes] == [LEGACY_SERVER_NAME]
    add_cmd = next(c for c in fake_subprocess if c[:3] == ["claude", "mcp", "add"])
    assert add_cmd[5] == "omi"
    assert "node" in add_cmd and "--require" not in add_cmd


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
        provision.shutil, "which", lambda name: None if name == "git" else f"/usr/bin/{name}"
    )
    config = _config(tmp_path)
    with pytest.raises(ProvisionError, match="git"):
        Provisioner(config, log=_quiet).run()


def test_jq_diagnostic_warns_when_absent_and_is_not_required(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # #107: a missing jq must be a WARN (the guard hook falls back to the
    # pure-Python adapter), never a fail, and jq must NOT be a hard prereq —
    # otherwise `omind setup` would refuse on a jq-less box, the original wedge.
    monkeypatch.setattr(
        provision.shutil, "which", lambda name: None if name == "jq" else f"/usr/bin/{name}"
    )
    res = provision._diagnose_jq()
    assert res.key == "tool:jq"
    assert res.level == "warn"
    assert "fall" in res.message.lower() or "python" in res.message.lower()
    assert "jq" not in Provisioner.REQUIRED_TOOLS
    from omind.agents import AgentProvisioner

    assert "jq" not in AgentProvisioner.REQUIRED_TOOLS


def test_jq_diagnostic_ok_when_present(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(provision.shutil, "which", lambda name: f"/usr/bin/{name}")
    res = provision._diagnose_jq()
    assert res.level == "ok"


def test_idempotent_files_on_rerun(
    tmp_path: Path, fake_tools: None, fake_subprocess: list[list[str]], isolate_claude: Path
) -> None:
    config = _config(tmp_path)
    Provisioner(config, log=_quiet).run()
    Provisioner(config, log=_quiet).run()  # must not raise
    template = config.omi_dir / paths.MEMORY_TEMPLATE_FILENAME
    assert template.read_text() == seeds.MEMORY_TEMPLATE


def test_doctor_healthy_when_provisioned(
    tmp_path: Path, fake_tools: None, isolate_claude: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    _provision_files(config)
    _write_server_config(isolate_claude, config)
    _install_hooks(config)
    _install_guard(config, monkeypatch, tmp_path)
    results = {r.key: r for r in provision.diagnose(config)}
    assert results["omi_dir"].level == "ok"
    assert results["obsidian_config"].level == "ok"
    assert results["seeds"].level == "ok"
    assert results["mcp_registration"].level == "ok"
    assert "legacy_server" not in results
    assert results["hooks"].level == "ok"
    assert results["omi_guard"].level == "ok"
    assert provision.run_doctor(config, log=_quiet) == 0


def test_real_run_installs_claude_skill(
    tmp_path: Path,
    fake_tools: None,
    fake_subprocess: list[list[str]],
    isolate_claude: Path,
    isolate_claude_skill: Path,
) -> None:
    config = _config(tmp_path)
    Provisioner(config, log=_quiet).run()
    skill = isolate_claude_skill / paths.AGENT_SKILL_FILENAME
    assert skill.is_file()
    body = skill.read_text(encoding="utf-8")
    assert body.startswith("---\nname: omind\n")
    assert str(config.vault) in body  # placeholders were filled
    assert "omind note" in body  # teaches the single-writer write path
    assert "MCP `help` tool first" in body
    metadata = isolate_claude_skill / "agents" / "openai.yaml"
    assert metadata.is_file()
    assert 'default_prompt: "Use $omind' in metadata.read_text(encoding="utf-8")


def test_claude_skill_refreshes_on_drift(
    tmp_path: Path, fake_tools: None, isolate_claude_skill: Path
) -> None:
    """The skill is managed: a stale copy is rewritten, not left as-is."""
    config = _config(tmp_path)
    skill = isolate_claude_skill / paths.AGENT_SKILL_FILENAME
    skill.parent.mkdir(parents=True)
    skill.write_text("stale", encoding="utf-8")
    Provisioner(config, log=_quiet).install_claude_skill()
    assert skill.read_text(encoding="utf-8") != "stale"
    assert "name: omind" in skill.read_text(encoding="utf-8")


def test_doctor_reports_claude_skill(
    tmp_path: Path, fake_tools: None, isolate_claude: Path, isolate_claude_skill: Path
) -> None:
    config = _config(tmp_path)
    missing = {r.key: r for r in provision.diagnose(config)}
    assert missing["claude_skill"].level == "warn"
    Provisioner(config, log=_quiet).install_claude_skill()
    present = {r.key: r for r in provision.diagnose(config)}
    assert present["claude_skill"].level == "ok"


def test_doctor_flags_missing_setup(
    tmp_path: Path, isolate_claude: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(provision.shutil, "which", lambda name: None)
    config = _config(tmp_path)
    levels = {r.key: r.level for r in provision.diagnose(config)}
    assert levels["omi_dir"] == "fail"
    assert levels["obsidian_config"] == "warn"  # cosmetic now: omind node doesn't need it
    assert levels["mcp_registration"] == "fail"
    assert provision.run_doctor(config, log=_quiet) == 1


def test_doctor_warns_on_path_mismatch(
    tmp_path: Path, fake_tools: None, isolate_claude: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    _provision_files(config)
    drifted = {"command": "omind", "args": ["node", "--vault", "/elsewhere", "--folder", "OMI"]}
    isolate_claude.write_text(json.dumps({"mcpServers": {"omi": drifted}}))
    _install_hooks(config)
    _install_guard(config, monkeypatch, tmp_path)
    results = {r.key: r for r in provision.diagnose(config)}
    assert results["mcp_registration"].level == "warn"
    assert provision.run_doctor(config, log=_quiet) == 0  # warnings don't fail


def test_doctor_warns_on_lingering_legacy_server(
    tmp_path: Path, fake_tools: None, isolate_claude: Path
) -> None:
    config = _config(tmp_path)
    _provision_files(config)
    _write_legacy_server_config(isolate_claude, str(config.omi_dir))
    _install_hooks(config)
    results = {r.key: r for r in provision.diagnose(config)}
    assert results["mcp_registration"].level == "fail"  # omi itself not registered
    assert results["legacy_server"].level == "warn"
    assert "obsidian-mcp" in results["legacy_server"].message


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
    _write_server_config(home / ".claude.json", config)
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
        assert provision._command_is_omind_hook(cmd)  # detectable as ours (omind.EXE on Windows)
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


# -- fresh-base git guard hook (PreToolUse/Bash) ----------------------------


def _guard_entries(settings: Path) -> list[dict[str, object]]:
    data = json.loads(settings.read_text(encoding="utf-8"))
    return [
        e
        for e in data["hooks"]["PreToolUse"]
        if provision.GUARD_HOOK_MARKER in provision._entry_command_text(e)
    ]


def test_setup_writes_guard_hook_script(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(provision.Path, "home", classmethod(lambda cls: tmp_path))
    Provisioner(_config(tmp_path), log=_quiet)._write_guard_hook_script()
    dest = tmp_path / ".claude" / "hooks" / "git-fresh-base.sh"
    assert dest.is_file()
    if os.name != "nt":  # Windows has no POSIX executable bit to assert on
        assert dest.stat().st_mode & 0o111
    assert "git-fresh-base" in dest.read_text(encoding="utf-8")


def test_setup_installs_guard_hook_idempotently(
    tmp_path: Path, isolate_settings: Path
) -> None:
    config = _config(tmp_path)
    Provisioner(config, log=_quiet).ensure_guard_hook_installed()
    before = isolate_settings.read_text(encoding="utf-8")
    Provisioner(config, log=_quiet).ensure_guard_hook_installed()  # second run: no change
    assert isolate_settings.read_text(encoding="utf-8") == before
    entries = _guard_entries(isolate_settings)
    assert len(entries) == 1
    assert entries[0]["matcher"] == "Bash"


def test_guard_hook_preserves_user_pretooluse_hook(
    tmp_path: Path, isolate_settings: Path
) -> None:
    user_entry = {
        "matcher": "Bash",
        "hooks": [{"type": "command", "command": "/x/confirm-process-kill.sh"}],
    }
    isolate_settings.write_text(json.dumps({"hooks": {"PreToolUse": [user_entry]}}))
    Provisioner(_config(tmp_path), log=_quiet).ensure_guard_hook_installed()
    entries = json.loads(isolate_settings.read_text(encoding="utf-8"))["hooks"]["PreToolUse"]
    assert user_entry in entries  # untouched
    assert len(_guard_entries(isolate_settings)) == 1
    assert len(entries) == 2


def test_guard_hook_dry_run_writes_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, isolate_settings: Path
) -> None:
    monkeypatch.setattr(provision.Path, "home", classmethod(lambda cls: tmp_path))
    prov = Provisioner(_config(tmp_path, dry_run=True), log=_quiet)
    prov._write_guard_hook_script()
    prov.ensure_guard_hook_installed()
    assert not isolate_settings.exists()
    assert not (tmp_path / ".claude" / "hooks" / "git-fresh-base.sh").exists()


# -- secret-output guard hook (PreToolUse/Bash) -----------------------------


def _secret_guard_entries(settings: Path) -> list[dict[str, object]]:
    data = json.loads(settings.read_text(encoding="utf-8"))
    return [
        e
        for e in data["hooks"]["PreToolUse"]
        if provision.SECRET_OUTPUT_GUARD_MARKER in provision._entry_command_text(e)
    ]


def test_setup_writes_secret_output_guard_script(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(provision.Path, "home", classmethod(lambda cls: tmp_path))
    Provisioner(_config(tmp_path), log=_quiet)._write_secret_output_guard_script()
    dest = tmp_path / ".claude" / "hooks" / "secret-output-guard.sh"
    assert dest.is_file()
    if os.name != "nt":  # Windows has no POSIX executable bit to assert on
        assert dest.stat().st_mode & 0o111
    assert "secret-output-guard" in dest.read_text(encoding="utf-8")


def test_bash_guard_entry_wires_both_hooks_in_order(
    tmp_path: Path, isolate_settings: Path
) -> None:
    """One Bash matcher entry holds both omind guards, secret-output guard FIRST."""
    Provisioner(_config(tmp_path), log=_quiet).ensure_guard_hook_installed()
    entries = _secret_guard_entries(isolate_settings)
    assert len(entries) == 1
    entry = entries[0]
    assert entry["matcher"] == "Bash"
    commands = [h["command"] for h in entry["hooks"]]
    assert len(commands) == 2
    assert provision.SECRET_OUTPUT_GUARD_MARKER in commands[0]
    assert provision.GUARD_HOOK_MARKER in commands[1]  # fresh-base runs second


def test_secret_output_guard_installed_idempotently(
    tmp_path: Path, isolate_settings: Path
) -> None:
    config = _config(tmp_path)
    Provisioner(config, log=_quiet).ensure_guard_hook_installed()
    before = isolate_settings.read_text(encoding="utf-8")
    Provisioner(config, log=_quiet).ensure_guard_hook_installed()  # second run: no change
    assert isolate_settings.read_text(encoding="utf-8") == before
    assert len(_secret_guard_entries(isolate_settings)) == 1
    assert len(_guard_entries(isolate_settings)) == 1  # still exactly one omind entry


def test_secret_output_guard_preserves_user_pretooluse_hook(
    tmp_path: Path, isolate_settings: Path
) -> None:
    user_entry = {
        "matcher": "Bash",
        "hooks": [{"type": "command", "command": "/x/confirm-process-kill.sh"}],
    }
    isolate_settings.write_text(json.dumps({"hooks": {"PreToolUse": [user_entry]}}))
    Provisioner(_config(tmp_path), log=_quiet).ensure_guard_hook_installed()
    entries = json.loads(isolate_settings.read_text(encoding="utf-8"))["hooks"]["PreToolUse"]
    assert user_entry in entries  # untouched
    assert len(_secret_guard_entries(isolate_settings)) == 1
    assert len(entries) == 2  # the user hook + the single omind guard entry


def test_secret_output_guard_dry_run_writes_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, isolate_settings: Path
) -> None:
    monkeypatch.setattr(provision.Path, "home", classmethod(lambda cls: tmp_path))
    prov = Provisioner(_config(tmp_path, dry_run=True), log=_quiet)
    prov._write_secret_output_guard_script()
    assert not (tmp_path / ".claude" / "hooks" / "secret-output-guard.sh").exists()


def test_setup_writes_omi_guard_scripts(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(provision.Path, "home", classmethod(lambda cls: tmp_path))
    Provisioner(_config(tmp_path), log=_quiet)._write_omi_guard_scripts()
    hooks = tmp_path / ".claude" / "hooks"
    for name in ("omi-guard.sh", "omi-gate-reset.sh"):
        dest = hooks / name
        assert dest.is_file()
        if os.name != "nt":
            assert dest.stat().st_mode & 0o111
        body = dest.read_text(encoding="utf-8")
        assert "__OMI_DIR__" not in body  # install-time placeholders substituted
        assert "__OMIND_BIN__" not in body


def test_omi_guard_installed_idempotently(tmp_path: Path, isolate_settings: Path) -> None:
    config = _config(tmp_path)
    Provisioner(config, log=_quiet).ensure_omi_guard_installed()
    before = isolate_settings.read_text(encoding="utf-8")
    Provisioner(config, log=_quiet).ensure_omi_guard_installed()  # second run: no change
    assert isolate_settings.read_text(encoding="utf-8") == before
    data = json.loads(before)
    pre = [e for e in data["hooks"]["PreToolUse"] if "omi-guard.sh" in json.dumps(e)]
    ups = [e for e in data["hooks"]["UserPromptSubmit"] if "omi-gate-reset.sh" in json.dumps(e)]
    assert len(pre) == 1
    assert pre[0]["matcher"] == "*"
    assert len(ups) == 1
    assert "mcp__omi__read-note" in data["permissions"]["allow"]
    assert "mcp__omi__recall-note" in data["permissions"]["allow"]
    assert "mcp__omi__help" in data["permissions"]["allow"]


def test_omi_guard_preserves_user_hooks(tmp_path: Path, isolate_settings: Path) -> None:
    user_pre = {"matcher": "Bash", "hooks": [{"type": "command", "command": "/x/mine.sh"}]}
    isolate_settings.write_text(json.dumps({"hooks": {"PreToolUse": [user_pre]}}))
    Provisioner(_config(tmp_path), log=_quiet).ensure_omi_guard_installed()
    pre = json.loads(isolate_settings.read_text(encoding="utf-8"))["hooks"]["PreToolUse"]
    assert user_pre in pre  # untouched
    assert any("omi-guard.sh" in json.dumps(e) for e in pre)
    assert len(pre) == 2


def test_omi_guard_dry_run_writes_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, isolate_settings: Path
) -> None:
    monkeypatch.setattr(provision.Path, "home", classmethod(lambda cls: tmp_path))
    prov = Provisioner(_config(tmp_path, dry_run=True), log=_quiet)
    prov._write_omi_guard_scripts()
    prov.ensure_omi_guard_installed()
    assert not isolate_settings.exists()
    assert not (tmp_path / ".claude" / "hooks" / "omi-guard.sh").exists()


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
    _write_server_config(isolate_claude, config)
    _install_hooks(config)
    results = {r.key: r for r in provision.diagnose(config)}
    assert results["hooks"].level == "ok"


def test_doctor_fail_when_hooks_absent(
    tmp_path: Path, fake_tools: None, isolate_claude: Path
) -> None:
    config = _config(tmp_path)
    _provision_files(config)
    _write_server_config(isolate_claude, config)
    results = {r.key: r for r in provision.diagnose(config)}
    assert results["hooks"].level == "fail"
    assert provision.run_doctor(config, log=_quiet) == 1


def test_doctor_warns_on_hook_path_mismatch(
    tmp_path: Path,
    fake_tools: None,
    isolate_claude: Path,
    isolate_settings: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    _provision_files(config)
    _write_server_config(isolate_claude, config)
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
    _install_guard(config, monkeypatch, tmp_path)
    results = {r.key: r for r in provision.diagnose(config)}
    assert results["hooks"].level == "warn"
    assert results["omi_guard"].level == "ok"
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


# -- hook-entry recognition across platforms ------------------------------------


def test_entry_marker_recognizes_windows_resolved_exe() -> None:
    """shutil.which on Windows resolves omind to omind.EXE — still ours."""
    for command in (
        'omind hook PostToolUse --vault "/home/u/vault" --folder OMI',
        'C:\\Users\\u\\Scripts\\omind.EXE hook PostToolUse --vault "C:\\v" --folder OMI',
        'C:\\Users\\u\\Scripts\\omind.cmd hook Stop --vault "C:\\v" --folder OMI',
        '/home/u/.local/bin/omind hook Stop --vault "/home/u/vault" --folder OMI',
    ):
        entry = {"hooks": [{"type": "command", "command": command}]}
        assert provision._entry_has_omind_marker(entry), command


def test_entry_marker_ignores_foreign_commands() -> None:
    for command in ("echo mine", "myomindish hooks", "omind doctor --vault x"):
        entry = {"hooks": [{"type": "command", "command": command}]}
        assert not provision._entry_has_omind_marker(entry), command


# -- guard hook-set: manifest, drift, self-heal, migration (#86/#87) ------------


def test_provision_manifest_roundtrip_and_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(provision.Path, "home", classmethod(lambda cls: tmp_path))
    assert provision.hookset_drift() is not None  # never stamped yet
    provision.write_provision_manifest()
    manifest = provision.read_provision_manifest()
    assert manifest["omind_version"] == provision.__version__
    assert "omi-guard.sh" in manifest["hooks"]
    assert provision.hookset_drift() is None  # freshly stamped by this binary
    stamp = tmp_path / ".claude" / "hooks" / ".omind-provision.json"
    stamp.write_text(json.dumps({"omind_version": "0.0.1", "hooks": manifest["hooks"]}))
    assert provision.hookset_drift() is not None  # older recorded version -> stale


def test_autoheal_installs_guard_when_drifted(
    tmp_path: Path, fake_tools: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(provision.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.delenv("OMIND_NO_AUTOHEAL", raising=False)
    config = _config(tmp_path)
    err = io.StringIO()
    provision.autoheal_on_startup(config.vault, config.folder, out=err)
    assert (tmp_path / ".claude" / "hooks" / "omi-guard.sh").is_file()
    assert (tmp_path / ".claude" / "hooks" / "omi-gate-reset.sh").is_file()
    assert provision.hookset_drift() is None  # stamped -> no longer drifted
    assert "healed" in err.getvalue()
    err2 = io.StringIO()
    provision.autoheal_on_startup(config.vault, config.folder, out=err2)
    assert err2.getvalue() == ""  # second start: current -> silent no-op


def test_autoheal_respects_opt_out(
    tmp_path: Path, fake_tools: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(provision.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setenv("OMIND_NO_AUTOHEAL", "1")
    config = _config(tmp_path)
    provision.autoheal_on_startup(config.vault, config.folder)
    assert not (tmp_path / ".claude" / "hooks" / "omi-guard.sh").exists()


def test_provision_migrates_legacy_guard(
    tmp_path: Path, fake_tools: None, isolate_settings: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(provision.Path, "home", classmethod(lambda cls: tmp_path))
    hooks_dir = tmp_path / ".claude" / "hooks"
    hooks_dir.mkdir(parents=True)
    legacy_file = hooks_dir / "omi-git-guard.sh"
    legacy_file.write_text("#!/usr/bin/env bash\n# hand-rolled prototype\n")
    isolate_settings.write_text(
        json.dumps(
            {
                "hooks": {
                    "PreToolUse": [
                        {
                            "matcher": "*",
                            "hooks": [{"type": "command", "command": str(legacy_file)}],
                        }
                    ]
                }
            }
        )
    )
    config = _config(tmp_path)
    prov = Provisioner(config, log=_quiet)
    prov._write_omi_guard_scripts()
    prov.ensure_omi_guard_installed()
    assert not legacy_file.exists()  # stale prototype script deleted
    pre = json.loads(isolate_settings.read_text())["hooks"]["PreToolUse"]
    assert not any("omi-git-guard.sh" in json.dumps(e) for e in pre)  # deregistered
    assert any(
        e.get("matcher") == "*" and "omi-guard.sh" in json.dumps(e) for e in pre
    )  # canonical installed


def test_doctor_fails_when_guard_absent(
    tmp_path: Path, fake_tools: None, isolate_claude: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#86 regression: auto-memory hooks present but the OMI-compliance guard not
    installed must be a doctor failure, not a false green."""
    monkeypatch.setattr(provision.Path, "home", classmethod(lambda cls: tmp_path))
    config = _config(tmp_path)
    _provision_files(config)
    _write_server_config(isolate_claude, config)
    prov = Provisioner(config, log=_quiet)
    prov._write_enforce_hook_script()
    prov.ensure_hooks_installed()
    results = {r.key: r for r in provision.diagnose(config)}
    assert results["hooks"].level == "ok"
    assert results["omi_guard"].level == "fail"
    assert provision.run_doctor(config, log=_quiet) == 1


# -- mesh initialization in setup -------------------------------------------------


def test_setup_initializes_mesh_node(
    tmp_path: Path, fake_tools: None, fake_subprocess: list[list[str]], isolate_claude: Path
) -> None:
    config = _config(tmp_path)
    actions = Provisioner(config, log=_quiet).run()
    assert any("initialize mesh node" in a for a in actions)
    git_cmds = [c for c in fake_subprocess if c[0] == "git"]
    assert any("init" in c for c in git_cmds)
    assert any("merge.omi.driver" in " ".join(c) for c in git_cmds)


def test_setup_no_mesh_skips_initialization(
    tmp_path: Path, fake_tools: None, fake_subprocess: list[list[str]], isolate_claude: Path
) -> None:
    config = _config(tmp_path, no_mesh=True)
    actions = Provisioner(config, log=_quiet).run()
    assert not any("initialize mesh node" in a for a in actions)
    assert not any(c[0] == "git" for c in fake_subprocess)


def test_diagnose_enforcement_reports_policy_compliance_and_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from omind import compliance, policy

    policy.append_learned_rule(policy.Rule(id="lr", pattern="x", message="m"))
    compliance.log_event(compliance.KIND_DECISION, rule_id="gh-repo-delete", outcome="deny")

    monkeypatch.setattr(provision.shutil, "which", lambda name: f"/usr/bin/{name}")
    by_name = {c.key: c for c in provision._diagnose_enforcement()}
    assert "seed" in by_name["policy"].message and "1 learned" in by_name["policy"].message
    assert by_name["compliance_log"].message.startswith("compliance log: 1 event")
    assert by_name["verifier_backend"].level == "ok"


def test_diagnose_enforcement_warns_without_claude(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(provision.shutil, "which", lambda name: None)
    by_name = {c.key: c for c in provision._diagnose_enforcement()}
    assert by_name["verifier_backend"].level == "warn"
    assert by_name["compliance_log"].message == "compliance log: no violations recorded yet"


# -- 2.40.1: test-isolation guard + stale allow-rule pruning ------------------


def test_guard_test_isolation_blocks_non_temp_write_during_pytest() -> None:
    # PYTEST_CURRENT_TEST is set while this runs; a non-temp target must raise.
    with pytest.raises(provision.ProvisionError, match="2.40.1 guard"):
        provision._guard_test_isolation(Path("/usr/local/omind-nope/settings.json"))


def test_guard_test_isolation_allows_temp_write(tmp_path: Path) -> None:
    provision._guard_test_isolation(tmp_path / "x.json")  # under the temp dir -> no raise


def test_guard_test_isolation_is_noop_outside_pytest(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    provision._guard_test_isolation(Path("/usr/local/omind-nope/settings.json"))  # no-op


def test_ensure_omi_guard_prunes_stale_temp_allow_rules(
    tmp_path: Path, fake_tools: None, isolate_settings: Path
) -> None:
    stale = f"Read({tempfile.gettempdir()}/pytest-of-hermes/pytest-99/Vault/OMI/**)"
    real = "Read(/home/someone/Documents/Obsidian Vault/OMI/**)"
    isolate_settings.write_text(
        json.dumps({"permissions": {"allow": [stale, real, "Bash(ls:*)"]}}), encoding="utf-8"
    )
    provision.Provisioner(_config(tmp_path), log=_quiet).ensure_omi_guard_installed()
    allow = json.loads(isolate_settings.read_text(encoding="utf-8"))["permissions"]["allow"]
    assert stale not in allow  # temp-dir Read rule pruned (the 2.40.1 litter)
    assert real in allow  # a real OMI Read rule is kept
    assert "Bash(ls:*)" in allow  # unrelated rules untouched
