# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Aaron K. Clark
"""Tests for omind.quickstart: rendered snippets must match what setup installs."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from omind.cli import main
from omind.provision import Provisioner, SetupConfig
from omind.quickstart import build_quickstart


@pytest.fixture
def config(tmp_path: Path) -> SetupConfig:
    return SetupConfig(vault=tmp_path / "Vault", folder="OMI")


def _fenced(text: str, lang: str) -> list[str]:
    return re.findall(rf"```{lang}\n(.*?)\n```", text, flags=re.DOTALL)


def test_mentions_paths_and_fallback_command(config: SetupConfig) -> None:
    out = build_quickstart(config)
    assert str(config.omi_dir) in out
    assert f'omind setup --vault "{config.vault}" --folder OMI' in out
    assert "omind doctor" in out


def test_hooks_json_matches_provisioner(config: SetupConfig) -> None:
    out = build_quickstart(config)
    blocks = _fenced(out, "json")
    assert len(blocks) == 1
    data = json.loads(blocks[0])
    expected = Provisioner(config=config, log=lambda _m: None)._omind_hook_entries()
    assert data == {"hooks": expected}


def test_register_command_is_omind_node_form(config: SetupConfig) -> None:
    out = build_quickstart(config)
    assert f"claude mcp add -s user {config.server_name} -- " in out
    assert " node --vault " in out
    # The vault argument is quoted (paths contain spaces).
    assert f'"{config.vault}"' in out
    assert "npx" not in out and "obsidian-mcp" not in out


def test_mesh_init_step_present(config: SetupConfig) -> None:
    out = build_quickstart(config)
    assert f'omind mesh init --vault "{config.vault}" --folder OMI' in out
    assert "omind mesh add-peer" in out


def test_scaffold_includes_all_obsidian_config_files(config: SetupConfig) -> None:
    out = build_quickstart(config)
    for filename in ("app.json", "core-plugins.json", "appearance.json"):
        assert str(config.omi_dir / ".obsidian" / filename) in out


def test_respects_custom_folder_and_server_name(tmp_path: Path) -> None:
    config = SetupConfig(vault=tmp_path / "V", folder="Brain", server_name="memory")
    out = build_quickstart(config)
    assert str(tmp_path / "V" / "Brain") in out
    assert "claude mcp add -s user memory" in out
    assert "claude mcp remove memory -s user" in out


def test_cli_quickstart_prints_and_exits_zero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = main(["quickstart", "--vault", str(tmp_path / "Vault")])
    assert rc == 0
    out = capsys.readouterr().out
    assert "omind quickstart — manual wiring for" in out
    assert json.loads(_fenced(out, "json")[0])["hooks"]
