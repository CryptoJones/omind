# SPDX-License-Identifier: Apache-2.0
"""Tests for omind.rules: parsing, matching, fail-open visibility, caching,
and the guard wiring (#240)."""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

import pytest

from omind import guard, rules


def _note_with_rule(omi: Path, name: str = "Guard Rules.md", **overrides: str) -> Path:
    block = {
        "id": "no-direct-push-public-main",
        "tool": "Bash",
        "match": '"*git push*"',
        "when": "\n  repo_visibility: public\n  branch: [main, master]",
        "except_repos": "[allowed-repo]",
        "action": "deny",
        "message": '"Public repo: branch + PR required."',
    }
    block.update(overrides)
    omi.mkdir(parents=True, exist_ok=True)
    path = omi / name
    path.write_text(
        "# Guard Rules\n\n```omind-rule\n"
        f"id: {block['id']}\n"
        f"tool: {block['tool']}\n"
        f"match: {block['match']}\n"
        f"when:{block['when']}\n"
        f"except_repos: {block['except_repos']}\n"
        f"action: {block['action']}\n"
        f"message: {block['message']}\n"
        "```\n",
        encoding="utf-8",
    )
    return path


def test_parse_valid_invalid_and_multiple_blocks(tmp_path: Path) -> None:
    omi = tmp_path / "OMI"
    omi.mkdir()
    (omi / "Multi.md").write_text(
        "```omind-rule\nid: a\ntool: Bash\nmatch: '*rm -rf*'\naction: warn\n"
        "message: careful\n```\n"
        "```omind-rule\nid: b\ntool: '*'\nmatch: '*curl*'\naction: deny\nmessage: 'no'\n```\n"
        "```omind-rule\ntool: Bash\nmatch: '*x*'\naction: deny\nmessage: m\n```\n"  # no id
        "```omind-rule\n[not: yaml\n```\n",  # parse error
        encoding="utf-8",
    )
    loaded = {r.id: r for r in rules.load_rules(omi)}
    assert "a" in loaded and loaded["a"].action == "warn"
    assert "b" in loaded and loaded["b"].tool == "*"
    everything = rules.load_rules(omi, include_invalid=True)
    assert sum(1 for r in everything if r.invalid) == 2  # both bad blocks skipped


def test_note_rule_replaces_seed_rule_by_id(tmp_path: Path) -> None:
    omi = tmp_path / "OMI"
    _note_with_rule(omi)
    loaded = {r.id: r for r in rules.load_rules(omi)}
    rule = loaded["no-direct-push-public-main"]
    assert rule.except_repos == ("allowed-repo",)  # note version, not the seed
    assert rule.note == "Guard Rules.md"


def test_cache_invalidates_on_note_edit(tmp_path: Path) -> None:
    omi = tmp_path / "OMI"
    path = _note_with_rule(omi)
    first = {r.id for r in rules.load_rules(omi)}
    assert "no-direct-push-public-main" in first
    time.sleep(0.01)
    path.write_text(
        "```omind-rule\nid: replacement\ntool: Bash\nmatch: '*x*'\naction: warn\n"
        "message: hi\n```\n",
        encoding="utf-8",
    )
    second = {r.id for r in rules.load_rules(omi)}
    assert "replacement" in second


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "remote", "add", "origin",
         "https://github.com/o/some-repo.git"],
        check=True,
    )
    return repo


def _action(command: str) -> dict:
    return {"tool": "Bash", "command": command, "session": "rules-test"}


def test_deny_on_public_main_push(tmp_path: Path, repo: Path, monkeypatch) -> None:
    omi = tmp_path / "OMI"
    _note_with_rule(omi)
    monkeypatch.setattr(rules, "_repo_visibility", lambda r, **k: "public")
    monkeypatch.setattr(rules, "_repo_branch", lambda r: "main")
    hit = rules.evaluate(_action("git push origin main"), omi, repo)
    assert hit is not None and hit.outcome == "deny"
    # Same command in an excepted repo: allowed.
    monkeypatch.setattr(rules, "_repo_name", lambda r: "allowed-repo")
    assert rules.evaluate(_action("git push origin main"), omi, repo) is None


def test_no_fire_on_private_or_feature_branch(tmp_path: Path, repo: Path, monkeypatch) -> None:
    omi = tmp_path / "OMI"
    _note_with_rule(omi)
    monkeypatch.setattr(rules, "_repo_visibility", lambda r, **k: "private")
    monkeypatch.setattr(rules, "_repo_branch", lambda r: "main")
    assert rules.evaluate(_action("git push origin main"), omi, repo) is None
    monkeypatch.setattr(rules, "_repo_visibility", lambda r, **k: "public")
    monkeypatch.setattr(rules, "_repo_branch", lambda r: "feature/x")
    # Bare push falls back to the checked-out branch; explicit `origin main`
    # would (correctly) deny regardless of checkout — covered below.
    assert rules.evaluate(_action("git push"), omi, repo) is None


def test_unknown_visibility_fails_open(tmp_path: Path, repo: Path, monkeypatch) -> None:
    omi = tmp_path / "OMI"
    _note_with_rule(omi)
    monkeypatch.setattr(rules, "_repo_visibility", lambda r, **k: "unknown")
    monkeypatch.setattr(rules, "_repo_branch", lambda r: "main")
    hit = rules.evaluate(_action("git push origin main"), omi, repo)
    assert hit is not None and hit.outcome == "unknown-visibility"  # logged, never denied


def test_repo_scoped_rule_needs_a_repo(tmp_path: Path) -> None:
    omi = tmp_path / "OMI"
    _note_with_rule(omi)
    assert rules.evaluate(_action("git push origin main"), omi, None) is None


def test_guard_check_action_denies_via_note_rule(
    tmp_path: Path, repo: Path, monkeypatch
) -> None:
    omi = tmp_path / "OMI"
    _note_with_rule(omi)
    monkeypatch.setattr(rules, "_repo_visibility", lambda r, **k: "public")
    monkeypatch.setattr(rules, "_repo_branch", lambda r: "main")
    monkeypatch.setattr(guard, "_repo_root_for_action", lambda a: repo)
    guard.begin_turn("rules-guard", "push it")
    verdict = guard.check_action(_action("git push origin main"), omi_dir=omi)
    assert not verdict.allow
    assert verdict.rule_id == "note-rule:no-direct-push-public-main"
    assert "branch + PR required" in verdict.reason


def test_pushed_refspec_wins_over_checked_out_branch(
    tmp_path: Path, repo: Path, monkeypatch
) -> None:
    """#240 v1 false positives: a tag push or feature-branch push issued while
    main is checked out must not match a main/master branch condition."""
    omi = tmp_path / "OMI"
    _note_with_rule(omi)
    monkeypatch.setattr(rules, "_repo_visibility", lambda r, **k: "public")
    monkeypatch.setattr(rules, "_repo_branch", lambda r: "main")
    for command in (
        "git push -q origin v8.6.1",  # tag push
        "git push --tags",
        "git push -q -u origin security/cryptography-50",  # feature branch
        "git checkout -q -b f && git push -q -u origin f",
        "git push origin HEAD:refs/heads/feature-x",
    ):
        assert rules.evaluate(_action(command), omi, repo) is None, command
    for command in (
        "git push origin main",
        "git push -q -u origin main",
        "git push",  # bare: falls back to the checked-out branch (main)
        "git push origin HEAD:main",
    ):
        hit = rules.evaluate(_action(command), omi, repo)
        assert hit is not None and hit.outcome == "deny", command


def test_format_rules_lists_seeds_and_invalids(tmp_path: Path) -> None:
    omi = tmp_path / "OMI"
    omi.mkdir()
    (omi / "Bad.md").write_text(
        "```omind-rule\ntool: Bash\nmatch: '*x*'\naction: deny\nmessage: m\n```\n",
        encoding="utf-8",
    )
    text = rules.format_rules(omi)
    assert "no-direct-push-public-main" in text  # seed present on a fresh vault
    assert "[skipped] Bad.md" in text
