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


def _config(tmp_path: Path, **kw: object) -> SetupConfig:
    return SetupConfig(vault=tmp_path / "vault", **kw)  # type: ignore[arg-type]


def _quiet(_: str) -> None:
    pass


def _write_server_config(cfg: Path, omi_path: str) -> None:
    server = {"command": "npx", "args": ["-y", "obsidian-mcp", omi_path]}
    cfg.write_text(json.dumps({"mcpServers": {"obsidian": server}}))


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
    assert fake_subprocess[-2][:6] == ["claude", "mcp", "add", "-s", "user", "obsidian"]
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
        provision.shutil, "which", lambda name: None if name == "npx" else f"/usr/bin/{name}"
    )
    config = _config(tmp_path)
    with pytest.raises(ProvisionError, match="npx"):
        Provisioner(config, log=_quiet).run()


def test_idempotent_files_on_rerun(
    tmp_path: Path, fake_tools: None, fake_subprocess: list[list[str]], isolate_claude: Path
) -> None:
    config = _config(tmp_path)
    Provisioner(config, log=_quiet).run()
    Provisioner(config, log=_quiet).run()  # must not raise
    template = config.omi_dir / seeds.MEMORY_TEMPLATE_FILENAME
    assert template.read_text() == seeds.MEMORY_TEMPLATE
