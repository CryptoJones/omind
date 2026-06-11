# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Aaron K. Clark
"""Tests for omind.mesh: node identity, repo init, and the git wrapper.

These drive a real git binary against temp repos (skipped if git is absent;
both CI runners ship it). Replication tests over file:// remotes join this
file with the sync engine.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from omind import mesh
from omind.mesh import (
    GITATTRIBUTES,
    MeshError,
    NodeConfig,
    git,
    load_node_config,
    mesh_init,
    new_node_id,
    node_config_path,
    save_node_config,
)
from omind.store import NoteFields, OmiStore

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")


@pytest.fixture(autouse=True)
def _isolate_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))


@pytest.fixture
def omi_dir(tmp_path: Path) -> Path:
    omi = tmp_path / "OMI"
    omi.mkdir()
    OmiStore(omi).create_note(NoteFields(title="Seed Note", summary="first"))
    return omi


def quiet(_msg: str) -> None:
    pass


def test_new_node_id_shape() -> None:
    node_id = new_node_id()
    host, _, suffix = node_id.rpartition("-")
    assert host
    assert len(suffix) == 6
    assert int(suffix, 16) >= 0
    assert new_node_id() != node_id  # random suffix


def test_node_config_round_trip(tmp_path: Path) -> None:
    omi = tmp_path / "OMI"
    assert load_node_config(omi) is None
    cfg = NodeConfig(node_id="box-abc123", interval_seconds=60, debounce_seconds=5)
    save_node_config(omi, cfg)
    assert load_node_config(omi) == cfg
    # Two folders coexist in the one config file.
    other = tmp_path / "Other"
    save_node_config(other, NodeConfig(node_id="box-def456"))
    assert load_node_config(omi) == cfg
    data = json.loads(node_config_path().read_text(encoding="utf-8"))
    assert len(data["nodes"]) == 2


def test_mesh_init_creates_repo_driver_and_identity(omi_dir: Path) -> None:
    cfg = mesh_init(omi_dir, log=quiet)

    assert (omi_dir / ".git").is_dir()
    assert load_node_config(omi_dir) == cfg

    driver = git(omi_dir, "config", "--get", "merge.omi.driver").stdout.strip()
    assert "-m omind merge-driver %O %A %B %P" in driver
    assert git(omi_dir, "config", "--get", "core.autocrlf").stdout.strip() == "false"
    assert git(omi_dir, "config", "--get", "user.name").stdout.strip() == cfg.node_id

    assert (omi_dir / ".gitattributes").read_text(encoding="utf-8") == GITATTRIBUTES
    assert ".omi.lock" in (omi_dir / ".gitignore").read_text(encoding="utf-8")

    branch = git(omi_dir, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    assert branch == "main"
    files = git(omi_dir, "ls-files").stdout.splitlines()
    assert "Seed Note.md" in files
    assert ".gitattributes" in files
    assert ".omi.lock" not in files
    status = git(omi_dir, "status", "--porcelain").stdout.strip()
    assert status == ""


def test_mesh_init_is_idempotent(omi_dir: Path) -> None:
    first = mesh_init(omi_dir, log=quiet)
    head = git(omi_dir, "rev-parse", "HEAD").stdout.strip()
    second = mesh_init(omi_dir, log=quiet)
    assert second.node_id == first.node_id  # identity never regenerated
    assert git(omi_dir, "rev-parse", "HEAD").stdout.strip() == head  # no churn commit


def test_mesh_init_commits_new_work_on_rerun(omi_dir: Path) -> None:
    mesh_init(omi_dir, log=quiet)
    OmiStore(omi_dir).create_note(NoteFields(title="Later Note", summary="s"))
    mesh_init(omi_dir, log=quiet)
    assert "Later Note.md" in git(omi_dir, "ls-files").stdout.splitlines()


def test_mesh_init_missing_folder_raises(tmp_path: Path) -> None:
    with pytest.raises(MeshError, match="not found"):
        mesh_init(tmp_path / "Nowhere", log=quiet)


def test_git_wrapper_maps_failures(omi_dir: Path) -> None:
    mesh_init(omi_dir, log=quiet)
    with pytest.raises(MeshError, match="command failed"):
        git(omi_dir, "rev-parse", "--verify", "no-such-ref")
    assert git(omi_dir, "rev-parse", "--verify", "no-such-ref", check=False).returncode != 0


def test_store_delete_now_archives_after_init(omi_dir: Path) -> None:
    """mesh init flips the store into merge-safe deletion via the .git probe."""
    mesh_init(omi_dir, log=quiet)
    store = OmiStore(omi_dir)
    store.delete_note("Seed Note.md")
    assert (omi_dir / "Seed Note.md").is_file()
    assert store.read_fields("Seed Note.md").disabled is True


def test_unreadable_config_raises(tmp_path: Path) -> None:
    node_config_path().parent.mkdir(parents=True, exist_ok=True)
    node_config_path().write_text("{not json", encoding="utf-8")
    with pytest.raises(MeshError, match="unreadable node config"):
        mesh.load_node_config(tmp_path / "OMI")
