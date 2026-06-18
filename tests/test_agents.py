# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Aaron K. Clark
"""Tests for omind.agents: Hermes Agent and OpenClaw provisioning."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from omind import agents, paths, provision, seeds
from omind.agents import (
    HermesProvisioner,
    OpenClawProvisioner,
    diagnose_for,
    diagnose_hermes,
    diagnose_openclaw,
    run_setup_for,
)
from omind.provision import ProvisionError, SetupConfig


@pytest.fixture(autouse=True)
def fake_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(provision.shutil, "which", lambda name: f"/usr/bin/{name}")
    # agents._omind_hook_command resolves omind via agents.shutil — patch it too
    # so the hook/bootstrap wiring is deterministic regardless of the test host.
    monkeypatch.setattr(agents.shutil, "which", lambda name: f"/usr/bin/{name}")


@pytest.fixture(autouse=True)
def no_subprocess(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **kwargs: object) -> object:
        calls.append(list(cmd))
        import subprocess

        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(provision.subprocess, "run", fake_run)
    return calls


@pytest.fixture
def hermes_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    root = tmp_path / "hermes-home"
    root.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(root))
    return root


@pytest.fixture
def openclaw_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    root = tmp_path / "openclaw-home"
    root.mkdir()
    monkeypatch.setattr(agents, "openclaw_root", lambda: root)
    return root


def _config(tmp_path: Path, agent: str, **kw: object) -> SetupConfig:
    return SetupConfig(vault=tmp_path / "vault", agent=agent, **kw)  # type: ignore[arg-type]


def _quiet(_: str) -> None:
    pass


# -- agent location helpers ----------------------------------------------------


def test_hermes_root_honors_hermes_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "custom"))
    assert agents.hermes_root() == tmp_path / "custom"
    monkeypatch.delenv("HERMES_HOME")
    monkeypatch.setattr(agents.Path, "home", classmethod(lambda cls: tmp_path))
    assert agents.hermes_root() == tmp_path / ".hermes"


def test_openclaw_root_prefers_existing_legacy_dirs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(agents.Path, "home", classmethod(lambda cls: tmp_path))
    assert agents.openclaw_root() == tmp_path / ".openclaw"  # default when none exist
    (tmp_path / ".moltbot").mkdir()
    assert agents.openclaw_root() == tmp_path / ".moltbot"
    (tmp_path / ".openclaw").mkdir()
    assert agents.openclaw_root() == tmp_path / ".openclaw"  # current name wins


def test_openclaw_config_path_prefers_existing_legacy_names(
    openclaw_home: Path,
) -> None:
    assert agents.openclaw_config_path() == openclaw_home / "openclaw.json"
    (openclaw_home / "moltbot.json").write_text("{}", encoding="utf-8")
    assert agents.openclaw_config_path() == openclaw_home / "moltbot.json"
    (openclaw_home / "openclaw.json").write_text("{}", encoding="utf-8")
    assert agents.openclaw_config_path() == openclaw_home / "openclaw.json"


# -- Hermes provisioning ---------------------------------------------------------


def test_hermes_setup_registers_server_and_skill(
    tmp_path: Path, hermes_home: Path
) -> None:
    config = _config(tmp_path, "hermes")
    (hermes_home / "config.yaml").write_text(
        yaml.safe_dump({"model": {"provider": "openrouter"}, "toolsets": ["hermes-cli"]}),
        encoding="utf-8",
    )
    run_setup_for(config, log=_quiet)

    data = yaml.safe_load((hermes_home / "config.yaml").read_text(encoding="utf-8"))
    server = data["mcp_servers"]["omi"]
    assert server["command"] == "/usr/bin/omind"
    assert server["args"] == ["node", "--vault", str(config.vault), "--folder", "OMI"]
    # untouched pre-existing keys
    assert data["model"] == {"provider": "openrouter"}
    assert data["toolsets"] == ["hermes-cli"]

    skill = agents.hermes_skill_dir() / paths.AGENT_SKILL_FILENAME
    text = skill.read_text(encoding="utf-8")
    assert str(config.omi_dir) in text
    assert "omind note" in text


def test_hermes_setup_without_config_file_creates_minimal_one(
    tmp_path: Path, hermes_home: Path
) -> None:
    run_setup_for(_config(tmp_path, "hermes"), log=_quiet)
    data = yaml.safe_load((hermes_home / "config.yaml").read_text(encoding="utf-8"))
    assert "omi" in data["mcp_servers"]


def test_hermes_setup_is_idempotent(tmp_path: Path, hermes_home: Path) -> None:
    config = _config(tmp_path, "hermes")
    run_setup_for(config, log=_quiet)
    first = (hermes_home / "config.yaml").read_text(encoding="utf-8")
    actions = run_setup_for(config, log=_quiet)
    assert (hermes_home / "config.yaml").read_text(encoding="utf-8") == first
    assert not any("register MCP server" in a for a in actions)


def test_hermes_setup_retires_legacy_and_registers_omi(
    tmp_path: Path, hermes_home: Path
) -> None:
    config = _config(tmp_path, "hermes")
    (hermes_home / "config.yaml").write_text(
        yaml.safe_dump(
            {"mcp_servers": {"obsidian": {"command": "npx", "args": ["obsidian-mcp", "/old"]}}}
        ),
        encoding="utf-8",
    )
    run_setup_for(config, log=_quiet)
    data = yaml.safe_load((hermes_home / "config.yaml").read_text(encoding="utf-8"))
    assert "obsidian" not in data["mcp_servers"]  # retired 1.x entry dropped
    assert data["mcp_servers"]["omi"]["args"][0] == "node"


def test_hermes_setup_preserves_other_mcp_servers(
    tmp_path: Path, hermes_home: Path
) -> None:
    (hermes_home / "config.yaml").write_text(
        yaml.safe_dump({"mcp_servers": {"github": {"command": "npx", "args": ["gh-mcp"]}}}),
        encoding="utf-8",
    )
    run_setup_for(_config(tmp_path, "hermes"), log=_quiet)
    data = yaml.safe_load((hermes_home / "config.yaml").read_text(encoding="utf-8"))
    assert data["mcp_servers"]["github"] == {"command": "npx", "args": ["gh-mcp"]}
    assert "omi" in data["mcp_servers"]


def test_hermes_setup_refuses_corrupt_yaml(tmp_path: Path, hermes_home: Path) -> None:
    (hermes_home / "config.yaml").write_text("model: [unclosed", encoding="utf-8")
    with pytest.raises(ProvisionError, match="not valid YAML"):
        run_setup_for(_config(tmp_path, "hermes"), log=_quiet)


def test_hermes_setup_fails_without_hermes_install(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "nope"))
    with pytest.raises(ProvisionError, match="Hermes Agent not found"):
        run_setup_for(_config(tmp_path, "hermes"), log=_quiet)


def test_hermes_dry_run_changes_nothing(tmp_path: Path, hermes_home: Path) -> None:
    config = _config(tmp_path, "hermes", dry_run=True)
    actions = run_setup_for(config, log=_quiet)
    assert not (hermes_home / "config.yaml").exists()
    assert not config.omi_dir.exists()
    assert all(a.startswith("[dry-run] would ") for a in actions)


def test_hermes_skill_not_clobbered_without_force(
    tmp_path: Path, hermes_home: Path
) -> None:
    config = _config(tmp_path, "hermes")
    skill = agents.hermes_skill_dir() / paths.AGENT_SKILL_FILENAME
    skill.parent.mkdir(parents=True)
    skill.write_text("user-customized", encoding="utf-8")
    run_setup_for(config, log=_quiet)
    assert skill.read_text(encoding="utf-8") == "user-customized"
    run_setup_for(_config(tmp_path, "hermes", force=True), log=_quiet)
    assert "omind note" in skill.read_text(encoding="utf-8")


# -- OpenClaw provisioning --------------------------------------------------------


def test_openclaw_setup_registers_server_and_skill(
    tmp_path: Path, openclaw_home: Path
) -> None:
    config = _config(tmp_path, "openclaw")
    (openclaw_home / "openclaw.json").write_text(
        json.dumps({"agents": {"defaults": {"workspace": "~/clawd"}}}), encoding="utf-8"
    )
    run_setup_for(config, log=_quiet)

    data = json.loads((openclaw_home / "openclaw.json").read_text(encoding="utf-8"))
    server = data["mcp"]["servers"]["omi"]
    assert server["command"] == "/usr/bin/omind"
    assert server["args"] == ["node", "--vault", str(config.vault), "--folder", "OMI"]
    assert data["agents"] == {"defaults": {"workspace": "~/clawd"}}  # untouched

    skill = agents.openclaw_skill_dir() / paths.AGENT_SKILL_FILENAME
    assert "omind note" in skill.read_text(encoding="utf-8")


def test_openclaw_setup_merges_into_legacy_config_name(
    tmp_path: Path, openclaw_home: Path
) -> None:
    (openclaw_home / "moltbot.json").write_text(
        json.dumps({"mcp": {"servers": {"other": {"url": "https://x"}}}}), encoding="utf-8"
    )
    run_setup_for(_config(tmp_path, "openclaw"), log=_quiet)
    data = json.loads((openclaw_home / "moltbot.json").read_text(encoding="utf-8"))
    assert "omi" in data["mcp"]["servers"]
    assert data["mcp"]["servers"]["other"] == {"url": "https://x"}
    assert not (openclaw_home / "openclaw.json").exists()


def test_openclaw_setup_is_idempotent(tmp_path: Path, openclaw_home: Path) -> None:
    config = _config(tmp_path, "openclaw")
    run_setup_for(config, log=_quiet)
    first = (openclaw_home / "openclaw.json").read_text(encoding="utf-8")
    actions = run_setup_for(config, log=_quiet)
    assert (openclaw_home / "openclaw.json").read_text(encoding="utf-8") == first
    assert not any("register MCP server" in a for a in actions)


def test_openclaw_setup_refuses_corrupt_json(tmp_path: Path, openclaw_home: Path) -> None:
    (openclaw_home / "openclaw.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(ProvisionError, match="not valid JSON"):
        run_setup_for(_config(tmp_path, "openclaw"), log=_quiet)


def test_openclaw_setup_fails_without_openclaw_install(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(agents, "openclaw_root", lambda: tmp_path / "nope")
    with pytest.raises(ProvisionError, match="OpenClaw not found"):
        run_setup_for(_config(tmp_path, "openclaw"), log=_quiet)


# -- session priming --------------------------------------------------------------


def test_hermes_setup_installs_priming_hook_and_allowlist(
    tmp_path: Path, hermes_home: Path
) -> None:
    config = _config(tmp_path, "hermes")
    run_setup_for(config, log=_quiet)

    data = yaml.safe_load((hermes_home / "config.yaml").read_text(encoding="utf-8"))
    entries = data["hooks"]["pre_llm_call"]
    assert len(entries) == 1
    cmd = entries[0]["command"]
    assert cmd.startswith("/usr/bin/omind hook pre_llm_call")
    assert f'--vault "{config.vault}"' in cmd
    assert entries[0]["timeout"] == 15

    allow = json.loads(
        (hermes_home / "shell-hooks-allowlist.json").read_text(encoding="utf-8")
    )
    assert {"event": "pre_llm_call", "command": cmd} in allow["approvals"]


def test_hermes_priming_preserves_user_hooks(tmp_path: Path, hermes_home: Path) -> None:
    user_hook = {"command": "/usr/bin/my-own-hook", "timeout": 5}
    (hermes_home / "config.yaml").write_text(
        yaml.safe_dump({"hooks": {"pre_llm_call": [user_hook]}}), encoding="utf-8"
    )
    run_setup_for(_config(tmp_path, "hermes"), log=_quiet)
    entries = yaml.safe_load((hermes_home / "config.yaml").read_text(encoding="utf-8"))[
        "hooks"
    ]["pre_llm_call"]
    assert user_hook in entries  # untouched
    assert any("omind hook pre_llm_call" in e["command"] for e in entries)


def test_hermes_priming_is_idempotent(tmp_path: Path, hermes_home: Path) -> None:
    config = _config(tmp_path, "hermes")
    run_setup_for(config, log=_quiet)
    run_setup_for(config, log=_quiet)
    entries = yaml.safe_load((hermes_home / "config.yaml").read_text(encoding="utf-8"))[
        "hooks"
    ]["pre_llm_call"]
    assert sum("omind hook" in e["command"] for e in entries) == 1
    allow = json.loads(
        (hermes_home / "shell-hooks-allowlist.json").read_text(encoding="utf-8")
    )
    assert len(allow["approvals"]) == 1


def test_hermes_priming_tolerates_corrupt_allowlist(
    tmp_path: Path, hermes_home: Path
) -> None:
    (hermes_home / "shell-hooks-allowlist.json").write_text("{bad", encoding="utf-8")
    run_setup_for(_config(tmp_path, "hermes"), log=_quiet)  # must not raise
    # an unparseable allowlist is left exactly as-is, never clobbered
    assert (hermes_home / "shell-hooks-allowlist.json").read_text(
        encoding="utf-8"
    ) == "{bad"


def test_openclaw_setup_installs_bootstrap_priming(
    tmp_path: Path, openclaw_home: Path
) -> None:
    config = _config(tmp_path, "openclaw")
    run_setup_for(config, log=_quiet)

    bootstrap = agents.openclaw_bootstrap_path()
    assert bootstrap.name == "MEMORY.md"  # a basename OpenClaw auto-loads
    text = bootstrap.read_text(encoding="utf-8")
    assert str(config.omi_dir) in text
    assert "omind note" in text

    data = json.loads((openclaw_home / "openclaw.json").read_text(encoding="utf-8"))
    extra = data["hooks"]["internal"]["entries"]["bootstrap-extra-files"]
    assert extra["enabled"] is True
    assert str(bootstrap) in extra["paths"]


def test_openclaw_bootstrap_preserves_existing_paths(
    tmp_path: Path, openclaw_home: Path
) -> None:
    (openclaw_home / "openclaw.json").write_text(
        json.dumps(
            {
                "hooks": {
                    "internal": {
                        "entries": {
                            "bootstrap-extra-files": {
                                "enabled": True,
                                "paths": ["packages/*/AGENTS.md"],
                            }
                        }
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    run_setup_for(_config(tmp_path, "openclaw"), log=_quiet)
    extra = json.loads((openclaw_home / "openclaw.json").read_text(encoding="utf-8"))[
        "hooks"
    ]["internal"]["entries"]["bootstrap-extra-files"]
    assert "packages/*/AGENTS.md" in extra["paths"]  # user path kept
    assert str(agents.openclaw_bootstrap_path()) in extra["paths"]


# -- doctor -----------------------------------------------------------------------


def test_diagnose_hermes_all_ok_after_setup(tmp_path: Path, hermes_home: Path) -> None:
    config = _config(tmp_path, "hermes")
    run_setup_for(config, log=_quiet)
    results = {r.key: r for r in diagnose_hermes(config)}
    assert results["hermes_root"].level == "ok"
    assert results["hermes_mcp_registration"].level == "ok"
    assert results["hermes_skill"].level == "ok"
    assert "tool:claude" not in results  # hermes wiring does not need the claude CLI


def test_diagnose_hermes_reports_missing_wiring(tmp_path: Path, hermes_home: Path) -> None:
    config = _config(tmp_path, "hermes")
    results = {r.key: r for r in diagnose_hermes(config)}
    assert results["hermes_mcp_registration"].level == "fail"
    assert results["hermes_skill"].level == "warn"


def test_diagnose_hermes_warns_on_drifted_entry(tmp_path: Path, hermes_home: Path) -> None:
    config = _config(tmp_path, "hermes")
    run_setup_for(config, log=_quiet)
    cfg_path = hermes_home / "config.yaml"
    data = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    data["mcp_servers"]["omi"]["args"][-1] = "/somewhere/else"
    cfg_path.write_text(yaml.safe_dump(data), encoding="utf-8")
    results = {r.key: r for r in diagnose_hermes(config)}
    assert results["hermes_mcp_registration"].level == "warn"


def test_diagnose_openclaw_reports_missing_install(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(agents, "openclaw_root", lambda: tmp_path / "nope")
    results = {r.key: r for r in diagnose_openclaw(_config(tmp_path, "openclaw"))}
    assert results["openclaw_root"].level == "fail"


def test_diagnose_openclaw_all_ok_after_setup(tmp_path: Path, openclaw_home: Path) -> None:
    config = _config(tmp_path, "openclaw")
    run_setup_for(config, log=_quiet)
    results = {r.key: r for r in diagnose_openclaw(config)}
    assert results["openclaw_root"].level == "ok"
    assert results["openclaw_mcp_registration"].level == "ok"
    assert results["openclaw_skill"].level == "ok"


def test_diagnose_for_dispatches_on_agent(tmp_path: Path, hermes_home: Path) -> None:
    config = _config(tmp_path, "hermes")
    keys = {r.key for r in diagnose_for(config)}
    assert "hermes_root" in keys


# -- claude path unchanged ----------------------------------------------------------


def test_run_setup_for_claude_uses_base_provisioner() -> None:
    assert agents.PROVISIONERS["claude"] is provision.Provisioner
    assert agents.DIAGNOSERS["claude"] is provision.diagnose


# -- skill template -------------------------------------------------------------------


def test_skill_template_renders_clean_yaml_frontmatter(tmp_path: Path) -> None:
    config = _config(tmp_path, "hermes")
    content = seeds.AGENT_SKILL_TEMPLATE.format(
        vault=config.vault, folder=config.folder, omi_dir=config.omi_dir
    )
    body = content.split("---\n")[1]
    meta = yaml.safe_load(body)
    assert meta["name"] == "omind-omi-memory"
    assert "single-insight" in meta["description"]


# -- provisioner overrides stay subprocess-free ---------------------------------------


def test_agent_provisioners_never_call_claude(
    tmp_path: Path,
    hermes_home: Path,
    openclaw_home: Path,
    no_subprocess: list[list[str]],
) -> None:
    run_setup_for(_config(tmp_path, "hermes"), log=_quiet)
    run_setup_for(_config(tmp_path, "openclaw"), log=_quiet)
    assert not any(cmd and cmd[0] == "claude" for cmd in no_subprocess)


def test_hermes_provisioner_done_message_names_hermes(tmp_path: Path) -> None:
    assert "Hermes" in HermesProvisioner.DONE_MESSAGE
    assert "OpenClaw" in OpenClawProvisioner.DONE_MESSAGE
