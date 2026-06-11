# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Aaron K. Clark
"""Tests for omind.mesh: node identity, repo init, and the git wrapper.

These drive a real git binary against temp repos (skipped if git is absent;
both CI runners ship it). Replication tests over file:// remotes join this
file with the sync engine.
"""

from __future__ import annotations

import json
import os
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


# -- replication: two real repos as peers (file:// remotes) ----------------------


def _note_text(omi: Path, name: str) -> str:
    return (omi / name).read_text(encoding="utf-8")


def _tracked_notes(omi: Path) -> dict[str, str]:
    """filename -> bytes for every tracked note (the convergence comparator)."""
    files = git(omi, "ls-files").stdout.splitlines()
    return {f: _note_text(omi, f) for f in files if f.endswith(".md")}


@pytest.fixture
def pair(tmp_path: Path) -> tuple[Path, str, Path, str]:
    """Two initialized nodes, A and B, peered both ways; B cloned from A."""
    a = tmp_path / "A" / "OMI"
    a.mkdir(parents=True)
    OmiStore(a).create_note(
        NoteFields(title="Shared", summary="origin", details="the truth", tags=["base"])
    )
    cfg_a = mesh.mesh_init(a, log=quiet)

    b = tmp_path / "B" / "OMI"
    cfg_b = mesh.clone(str(a), b, log=quiet)
    assert cfg_b.node_id != cfg_a.node_id

    mesh.add_peer(a, "b", str(b))
    # b already has "origin" -> a from the clone.
    return a, cfg_a.node_id, b, cfg_b.node_id


def test_clone_seeds_a_working_node(pair: tuple[Path, str, Path, str]) -> None:
    a, _, b, _ = pair
    assert _note_text(b, "Shared.md") == _note_text(a, "Shared.md")
    driver = git(b, "config", "--get", "merge.omi.driver").stdout
    assert "merge-driver" in driver  # per-clone config, never travels
    assert mesh.peers(b) == {"origin": str(a)}


def test_clone_refuses_non_empty_target(tmp_path: Path, omi_dir: Path) -> None:
    mesh.mesh_init(omi_dir, log=quiet)
    target = tmp_path / "occupied"
    target.mkdir()
    (target / "junk").write_text("x")
    with pytest.raises(MeshError, match="non-empty"):
        mesh.clone(str(omi_dir), target, log=quiet)


def test_partitioned_edits_converge_without_data_loss(
    pair: tuple[Path, str, Path, str],
) -> None:
    a, id_a, b, id_b = pair
    store_a = OmiStore(a, node_id=id_a)
    store_b = OmiStore(b, node_id=id_b)

    # Partitioned: A rewrites the summary (LWW scalar); B adds a tag and
    # appends details (unions / line merge). Same note, both sides.
    fa = store_a.read_fields("Shared.md")
    fa.summary = "rewritten on A"
    store_a.update_note("Shared.md", fa)

    fb = store_b.read_fields("Shared.md")
    fb.tags = [*fb.tags, "from-b"]
    fb.details = "appended on B"
    store_b.update_note("Shared.md", fb)

    # Heal the partition: B pulls A's edit and publishes; A merges B's
    # publication; B picks up A's regeneration. Then both trees must agree.
    assert mesh.sync(b, id_b, log=quiet).ok
    assert mesh.sync(a, id_a, log=quiet).ok
    assert mesh.sync(b, id_b, log=quiet).ok

    assert _tracked_notes(a) == _tracked_notes(b)  # byte-identical convergence
    merged = OmiStore(a).read_fields("Shared.md")
    assert merged.summary == "rewritten on A"  # A's scalar survived
    assert "from-b" in merged.tags  # B's tag survived
    assert "appended on B" in merged.details  # B's details survived
    assert "merge-conflict" not in merged.tags


def test_concurrent_disable_and_edit_converge(pair: tuple[Path, str, Path, str]) -> None:
    a, id_a, b, id_b = pair
    OmiStore(a, node_id=id_a).disable_note("Shared.md")
    store_b = OmiStore(b, node_id=id_b)
    fb = store_b.read_fields("Shared.md")
    fb.summary = "edited while A disabled it"
    store_b.update_note("Shared.md", fb)

    assert mesh.sync(b, id_b, log=quiet).ok
    assert mesh.sync(a, id_a, log=quiet).ok
    assert mesh.sync(b, id_b, log=quiet).ok

    assert _tracked_notes(a) == _tracked_notes(b)
    merged = OmiStore(a).read_fields("Shared.md")
    # 3-way semantics: B's edit survives AND A's disable survives — B never
    # touched the disabled field, so there is nothing to arbitrate by rev.
    # Restore is an explicit act, never a side effect of editing.
    assert merged.summary == "edited while A disabled it"
    assert merged.disabled is True
    # The note file exists everywhere either way (disable never unlinks).
    assert (a / "Shared.md").is_file() and (b / "Shared.md").is_file()
    # And an explicit restore wins everywhere on the next sync round.
    OmiStore(a, node_id=id_a).restore_note("Shared.md")
    assert mesh.sync(a, id_a, log=quiet).ok
    assert mesh.sync(b, id_b, log=quiet).ok
    assert OmiStore(b).read_fields("Shared.md").disabled is False


def test_purge_propagates_via_tombstone(pair: tuple[Path, str, Path, str]) -> None:
    a, id_a, b, id_b = pair
    mesh.purge(a, "Shared.md", id_a, log=quiet)
    assert not (a / "Shared.md").exists()

    assert mesh.sync(b, id_b, log=quiet).ok  # b merges a's tombstone
    assert not (b / "Shared.md").exists()
    tomb = (b / mesh.TOMBSTONES_FILENAME).read_text(encoding="utf-8")
    assert "Shared.md" in tomb
    assert mesh.sync(a, id_a, log=quiet).ok
    assert _tracked_notes(a) == _tracked_notes(b)


def test_unreachable_peer_is_skipped_not_fatal(pair: tuple[Path, str, Path, str]) -> None:
    a, id_a, _b, _ = pair
    mesh.add_peer(a, "ghost", str(Path(a).parent / "no-such-repo"))
    report = mesh.sync(a, id_a, log=quiet)
    ghost = next(p for p in report.peers if p.name == "ghost")
    assert ghost.error
    assert not ghost.fetched
    others = [p for p in report.peers if p.name != "ghost"]
    assert others and all(p.pushed for p in others)  # the live peer still synced
    assert report.ok is False  # but the report says something needs attention


def test_index_regenerates_and_never_conflicts(pair: tuple[Path, str, Path, str]) -> None:
    a, id_a, b, id_b = pair
    OmiStore(a, node_id=id_a).create_note(NoteFields(title="Only A", summary="sa"))
    OmiStore(b, node_id=id_b).create_note(NoteFields(title="Only B", summary="sb"))

    assert mesh.sync(b, id_b, log=quiet).ok
    assert mesh.sync(a, id_a, log=quiet).ok
    assert mesh.sync(b, id_b, log=quiet).ok

    assert _tracked_notes(a) == _tracked_notes(b)
    index = (a / "index.md").read_text(encoding="utf-8")
    assert "[[Only A]]" in index and "[[Only B]]" in index
    assert "<<<<<<<" not in index


def test_true_conflict_converges_with_markers_and_tag(
    pair: tuple[Path, str, Path, str],
) -> None:
    a, id_a, b, id_b = pair
    sa = OmiStore(a, node_id=id_a)
    sb = OmiStore(b, node_id=id_b)
    fa = sa.read_fields("Shared.md")
    fa.details = "A's version of the truth"
    sa.update_note("Shared.md", fa)
    fb = sb.read_fields("Shared.md")
    fb.details = "B's version of the truth"
    sb.update_note("Shared.md", fb)

    # B's first sync publishes B's edit (A's edit is not committed yet on A,
    # so there is nothing to collide with). The collision happens on A's
    # sync, which commits A's edit and merges B's pushed ref.
    assert mesh.sync(b, id_b, log=quiet).ok
    r2 = mesh.sync(a, id_a, log=quiet)
    assert r2.conflicts == ["Shared.md"]  # loud in the report
    assert mesh.sync(b, id_b, log=quiet).ok

    assert _tracked_notes(a) == _tracked_notes(b)  # markers and all
    merged = OmiStore(a).read_fields("Shared.md")
    assert "merge-conflict" in merged.tags
    assert "A's version of the truth" in merged.details
    assert "B's version of the truth" in merged.details


def test_sync_state_recorded_for_doctor(pair: tuple[Path, str, Path, str]) -> None:
    a, id_a, _b, _ = pair
    mesh.sync(a, id_a, log=quiet)
    state = mesh.read_sync_state(a)
    assert state is not None
    assert state["ok"] is True
    assert state["last_sync"]
    assert [p["name"] for p in state["peers"]] == ["b"]


def test_peers_with_space_in_url(pair: tuple[Path, str, Path, str]) -> None:
    a, _, _b, _ = pair
    url = "ssh://pluto/home/user/Documents/Obsidian Vault/OMI"
    mesh.add_peer(a, "spaced", url)
    assert mesh.peers(a)["spaced"] == url


def test_remove_peer(pair: tuple[Path, str, Path, str]) -> None:
    a, _, _b, _ = pair
    assert "b" in mesh.peers(a)
    mesh.remove_peer(a, "b")
    assert "b" not in mesh.peers(a)
    with pytest.raises(MeshError):
        mesh.add_peer(a, "bad name!", "url")


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_mesh_init_hardens_permissions(omi_dir: Path) -> None:
    """A traversable OMI folder leaks the whole memory history to any local
    user via a file:// fetch — init locks it to owner-only."""
    omi_dir.chmod(0o755)
    mesh.mesh_init(omi_dir, log=quiet)
    assert (omi_dir.stat().st_mode & 0o777) == 0o700
    assert ((omi_dir / ".git").stat().st_mode & 0o777) == 0o700


# -- daemon trigger + loop ---------------------------------------------------------


def test_should_sync_interval_elapsed() -> None:
    cfg = NodeConfig(node_id="n", interval_seconds=300, debounce_seconds=10)
    assert mesh._should_sync(now=1000.0, last_sync=700.0, signal_mtime=None, cfg=cfg)
    assert not mesh._should_sync(now=1000.0, last_sync=701.0, signal_mtime=None, cfg=cfg)


def test_should_sync_debounced_signal() -> None:
    cfg = NodeConfig(node_id="n", interval_seconds=300, debounce_seconds=10)
    # Signal newer than last sync, debounce satisfied -> sync.
    assert mesh._should_sync(now=1000.0, last_sync=900.0, signal_mtime=985.0, cfg=cfg)
    # Still inside the debounce window -> wait (writes batch).
    assert not mesh._should_sync(now=1000.0, last_sync=900.0, signal_mtime=995.0, cfg=cfg)
    # Signal older than the last sync -> already handled.
    assert not mesh._should_sync(now=1000.0, last_sync=990.0, signal_mtime=985.0, cfg=cfg)


def test_daemon_first_tick_syncs_and_exits(pair: tuple[Path, str, Path, str]) -> None:
    a, id_a, _b, _ = pair
    cfg = mesh.load_node_config(a)
    assert cfg is not None
    OmiStore(a, node_id=id_a).create_note(NoteFields(title="Daemon Note", summary="s"))
    rc = mesh.run_daemon(a, cfg, log=quiet, _max_tick_seconds=3.0)
    assert rc == 0
    # The first tick committed and pushed local work.
    assert "Daemon Note.md" in git(a, "ls-files").stdout.splitlines()
    assert mesh.read_sync_state(a) is not None


def test_install_service_requires_init(tmp_path: Path) -> None:
    omi = tmp_path / "Vault" / "OMI"
    omi.mkdir(parents=True)
    with pytest.raises(MeshError, match="mesh init"):
        mesh.install_service(tmp_path / "Vault", "OMI", log=quiet)
