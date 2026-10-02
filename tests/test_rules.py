# SPDX-License-Identifier: Apache-2.0
"""Tests for omind.rules: parsing, matching, fail-open visibility, caching,
and the guard wiring (#240)."""

from __future__ import annotations

import functools
import shlex
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
        ["git", "-C", str(repo), "remote", "add", "origin", "https://github.com/o/some-repo.git"],
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


def test_deny_on_force_push_refspec(tmp_path: Path, repo: Path, monkeypatch) -> None:
    """`git push origin +main` is the RISKIEST variant of the public-main push —
    the leading '+' of the force refspec used to defeat the branch condition and
    the deny silently skipped it (2026-08-27 review)."""
    omi = tmp_path / "OMI"
    _note_with_rule(omi)
    monkeypatch.setattr(rules, "_repo_visibility", lambda r, **k: "public")
    monkeypatch.setattr(rules, "_repo_branch", lambda r: "main")
    for command in (
        "git push origin +main",
        "git push origin +refs/heads/main",
        "git push origin HEAD:+main",
    ):
        hit = rules.evaluate(_action(command), omi, repo)
        assert hit is not None and hit.outcome == "deny", command


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


def test_guard_check_action_denies_via_note_rule(tmp_path: Path, repo: Path, monkeypatch) -> None:
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


@pytest.fixture
def two_repos(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path, Path]:
    """(omi, public, private): two repos on main, told apart only by visibility,
    and an OMI vault holding the seed-shaped `*git push*` rule."""
    omi = tmp_path / "OMI"
    _note_with_rule(omi)
    public, private = tmp_path / "public", tmp_path / "private"
    for r in (public, private):
        (r / ".git").mkdir(parents=True)
        (r / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    public, private = public.resolve(), private.resolve()
    monkeypatch.setattr(
        rules, "_repo_visibility", lambda r, **k: "public" if r == public else "private"
    )
    monkeypatch.setattr(rules, "_repo_branch", lambda r: "main")
    monkeypatch.setattr(rules, "_repo_name", lambda r: r.name)
    return omi, public, private


def _denied(
    omi: Path, command: str, where: Path, monkeypatch: pytest.MonkeyPatch, **extra: str
) -> bool:
    """Whether the note rules deny ``command`` with the process cwd at ``where``."""
    monkeypatch.chdir(where)
    v = guard._note_rules_verdict({"tool": "Bash", "command": command, **extra}, omi)
    return v is not None and not v.allow


#: The pre-#394 deny set. Every row was denied by the guard on origin/main
#: before this fix (b832d60, checked by running this table against it), run
#: with the process cwd in the repo named second. None of them is a remote
#: payload, so every one must still be denied.
_PRE_394_DENIED: tuple[tuple[str, str], ...] = (
    ("git push", "public"),
    ("git push origin main", "public"),
    ("git push -q -u origin main", "public"),
    ("git push origin +main", "public"),
    ("git push origin HEAD:main", "public"),
    ("git push origin main 2>&1 | tail -3", "public"),
    ("git add -A && git commit -m x && git push origin main", "public"),
    ("git status; git push origin main", "public"),
    ("git fetch || git push origin main", "public"),
    ("sudo git push origin main", "public"),
    ("env X=1 git push origin main", "public"),
    ("bash -c 'git push'", "public"),
    ("sh -c 'git push'", "public"),
    ("eval 'git push'", "public"),
    ("sudo sh -c 'git push'", "public"),
    ("timeout 60 bash -c 'git push'", "public"),
    ("(cd {private} && git status) && git push origin main", "public"),
    ("cd {public} && git push origin main", "public"),
    ("ssh h uptime; git push origin main", "public"),
    ("git commit -m 'git push later' && git push", "public"),
    # The body of an executor the guard does not unwrap fails closed:
    ("su -c 'git push'", "public"),
    ("git push origin main # ssh host 'x'", "public"),
)

#: Denied now, and NOT before #394's review fixes: the push reaches public main
#: through a cd, a -C, a wrapper or a second push the old resolver missed.
_NEWLY_DENIED: tuple[tuple[str, str], ...] = (
    ("bash -c 'git push origin main'", "public"),
    ('sh -c "git push origin main"', "public"),
    ("eval 'git push origin main'", "public"),
    ("sudo sh -c 'git push origin main'", "public"),
    ("timeout 60 bash -c 'git push origin main'", "public"),
    ("bash -c 'cd {public} && git push origin main'", "private"),
    ("bash -lc 'cd {public}; git push origin main'", "private"),
    ("git status && cd {public} && git push origin main", "private"),
    ("cd {private} && git status && cd {public} && git push origin main", "private"),
    ("git -C {private} commit -m x && git -C {public} push origin main", "private"),
    ("git push origin feature && git push origin main", "public"),
    ("ssh h 'git push origin feature' && git push origin main", "public"),
    ("su -c 'git push origin main'", "public"),  # body of an executor not unwrapped
    ("(cd {private} && git status) && git push origin main", "public"),
    ("cd {public} && git push origin main", "private"),
    ("(cd {public}; git push origin main)", "private"),
    ("git -C {public} push origin main", "private"),
    ("git -c x=y push origin main", "public"),
    ("git -C {public} -c x=y push origin main", "private"),
    ("git --no-pager push origin main", "public"),
    ("sudo git -C {public} push origin main", "private"),
    ("env X=1 git -C {public} push origin main", "private"),
    ("pushd {public} && git push origin main", "private"),
    ("pushd {private} && popd && git push origin main", "public"),
    ("cd {public} && eval 'git push origin main'", "private"),
)

#: Denied before #394 and allowed now, each for a stated reason.
_NOW_ALLOWED: tuple[tuple[str, str], ...] = (
    # Quoted data handed to a program that does not execute it (#413 review 3).
    ("echo 'git push'", "public"),
    ('echo "run git push origin main after review"', "public"),
    ("git commit -m 'docs: explain git push'", "public"),
    # The push runs on another host: the local repo says nothing about it.
    ("ssh localhost 'cd {private} && git commit -m x && git push origin main'", "public"),
    ("ssh -p 22 host 'git push origin main'", "public"),
    ("ssh host git push origin main", "public"),
    ("bash -c \"ssh host 'git push origin main'\"", "public"),
    # The push provably targets the PRIVATE repo, not the public cwd (#394).
    ("cd {private} && git push origin main", "public"),
    ("pushd {private} && git push origin main", "public"),
)


@pytest.mark.parametrize(("command", "where"), _PRE_394_DENIED)
def test_pre_394_deny_set_is_still_denied(
    two_repos: tuple[Path, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    command: str,
    where: str,
) -> None:
    """#394 review: enforcement may only loosen for a genuinely remote payload
    or a provably different target. Everything the old guard denied stays."""
    omi, public, private = two_repos
    cmd = command.format(public=public, private=private)
    assert _denied(omi, cmd, public if where == "public" else private, monkeypatch), cmd


@pytest.mark.parametrize(("command", "where"), _NEWLY_DENIED)
def test_every_git_is_judged_against_its_own_repo(
    two_repos: tuple[Path, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    command: str,
    where: str,
) -> None:
    """#394 review items 1-5: local `sh/bash -c` and `eval` bodies are code;
    each git is resolved at its own cd/-C, after subshells close and behind
    wrappers; every push's refspec counts; the seed matches past git's global
    options (#414)."""
    omi, public, private = two_repos
    cmd = command.format(public=public, private=private)
    assert _denied(omi, cmd, public if where == "public" else private, monkeypatch), cmd


@pytest.mark.parametrize(("command", "where"), _NOW_ALLOWED)
def test_remote_or_private_target_is_not_judged_against_the_cwd(
    two_repos: tuple[Path, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    command: str,
    where: str,
) -> None:
    """#394 repro: with the cwd in a PUBLIC repo, a push inside an ssh payload
    to another host's private repo was denied as a direct push to public main."""
    omi, public, private = two_repos
    cmd = command.format(public=public, private=private)
    assert not _denied(omi, cmd, public if where == "public" else private, monkeypatch), cmd


#: #413 review round 3, item 2: quoted arguments of programs that do not run
#: them are data. Each was allowed on main before #394 or by its quoting, and
#: must stay allowed on a PUBLIC main: grepping a repo that documents its git
#: workflow is common.
_QUOTED_DATA: tuple[str, ...] = (
    "grep 'git push' README.md",
    "rg 'git push' src",
    'grep -rn "git push origin main" docs/',
    'git commit -m "docs: explain git push origin main"',
    "sed -i '' 's/git push origin main/git push origin feat/' doc.md",
    "printf '%s\\n' 'git push' >> CHANGELOG.md",
    "gh issue comment 1 --body 'never git push to main'",
    "echo 'git push origin main'",
    "python3 -c \"print('git push origin main')\"",
    "git log --oneline --grep='git push'",
    'gh pr create --title x --body "Then git push origin main once merged"',
    "gh pr create --body \"$(cat <<'EOF'\nSteps: git push origin main\nEOF\n)\"",
    "cat > notes.md <<'EOF'\nUse git push origin main\nEOF",
)


@pytest.mark.parametrize("command", _QUOTED_DATA)
def test_quoted_data_is_not_a_push(
    two_repos: tuple[Path, Path, Path], monkeypatch: pytest.MonkeyPatch, command: str
) -> None:
    """#413 review 3: a repo-scoped rule judges only a command the shell runs.
    `git push` inside a grep pattern, a commit message or a `--body` is data;
    it used to fall back to the HEAD branch (main) and deny."""
    omi, public, _private = two_repos
    assert not _denied(omi, command, public, monkeypatch), command


#: Executors whose argument IS code the guard does not unwrap: they fail closed.
_OPAQUE_EXECUTORS: tuple[str, ...] = (
    "su -c 'git push origin main'",
    "su - root -c 'git push origin main'",
    "fish -c 'git push origin main'",
    "python3 -c \"import os; os.system('git push origin main')\"",
    "python3 -c \"import subprocess; subprocess.run('git push origin main', shell=True)\"",
    "perl -e 'system(\"git push origin main\")'",
    "ssh -o PermitLocalCommand=yes -o LocalCommand='git push origin main' host true",
    "ssh -o ProxyCommand=\"sh -c 'git push origin main'\" host",
    "bash -c '\"$@\"' _ git push origin main",  # positional args the body runs
    "bash -c bash -c 'git push origin main'",
    "eval " * 6 + "git push origin main",  # deeper than the unwrap limit
    functools.reduce(
        lambda body, _: "bash -c " + shlex.quote(body), range(6), "git push origin main"
    ),
)


@pytest.mark.parametrize("command", _OPAQUE_EXECUTORS)
def test_bodies_of_unwrapped_executors_fail_closed(
    two_repos: tuple[Path, Path, Path], monkeypatch: pytest.MonkeyPatch, command: str
) -> None:
    """#413 review 3: quoted text is data only for a program that does not
    execute it. `su -c`, a python `-c` that calls os.system/subprocess, and a
    shell nested past the unwrap limit run their body: judged as before."""
    omi, public, _private = two_repos
    assert _denied(omi, command, public, monkeypatch), command


#: #413 review 3, item 3: a cd/pushd with a redirect or an option still moves.
_CD_FORMS: tuple[str, ...] = (
    "pushd {public} >/dev/null && git push origin main",
    "pushd {public} > /dev/null; git push origin main",
    "cd {public} >/dev/null && git push origin main",
    "cd {public} 2>/dev/null && git push origin main",
    "cd {public} &>/dev/null && git push origin main",
    "cd {public} 2>&1 && git push origin main",
    "bash -c 'cd {public} >/dev/null && git push origin main'",
    "cd -P {public} && git push origin main",
    "cd -L {public} && git push origin main",
    "cd -- {public} && git push origin main",
    "cd -P -- {public} 2>/dev/null && git push origin main",
)


@pytest.mark.parametrize("command", _CD_FORMS)
def test_cd_with_redirect_or_option_still_moves(
    two_repos: tuple[Path, Path, Path], monkeypatch: pytest.MonkeyPatch, command: str
) -> None:
    """`pushd X >/dev/null` is how pushd is normally written. The operand was
    read only from a two-token `cd X`, so these left the cwd unknown and the
    push was judged at the PRIVATE start directory."""
    omi, public, private = two_repos
    cmd = command.format(public=public)
    assert _denied(omi, cmd, private, monkeypatch), cmd


def test_popd_and_cd_with_redirects_move_both_ways(
    two_repos: tuple[Path, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    omi, public, private = two_repos
    cmd = f"pushd {private} >/dev/null && popd >/dev/null && git push origin main"
    assert _denied(omi, cmd, public, monkeypatch), cmd
    cmd = f"cd {public} >/dev/null && cd -P -- {private} && git push origin main"
    assert not _denied(omi, cmd, public, monkeypatch), cmd


def test_nul_byte_in_cd_falls_back_to_the_cwd(
    two_repos: tuple[Path, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """#413 review 3, item 4: `resolve()` raises ValueError on an embedded NUL,
    which escaped every rule. An unresolvable directory falls back to the cwd."""
    omi, public, _private = two_repos
    assert _denied(omi, "cd /tm\x00p && git push origin main", public, monkeypatch)
    action = {"tool": "Bash", "command": "cd /tm\x00p && ls"}
    assert guard._repo_root_for_action(action) == public


def test_git_global_options_do_not_backtrack_exponentially(
    two_repos: tuple[Path, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """#413 review 3, item 1: `--git-dir=X` matched both the named-option and the
    generic `--long[=v]` branch, so a run of them with no push after took 8.6 s
    at n=24, doubling per option."""
    omi, public, _private = two_repos
    for command in (
        "git " + "--git-dir=a " * 40 + "log # git push",
        "git " + "--work-tree a " * 40 + "pus",
        "git " + "--namespace=a --no-pager " * 40 + "log # git push",
    ):
        start = time.perf_counter()
        rules._PUSH_ARGS_RE.search(command)
        rules._canonical_git(command)
        _denied(omi, command, public, monkeypatch)
        assert time.perf_counter() - start < 1.0, command
    assert rules._pushed_branches("git --git-dir=a --work-tree b push origin main") == ["main"]


def test_private_targets_stay_allowed(
    two_repos: tuple[Path, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    omi, public, private = two_repos
    for command, cwd in (
        ("git push origin main", private),
        (f"git -C {private} push origin main", public),
        (f"cd {public} && git status && cd {private} && git push origin main", public),
        (f"git -C {public} status && git push origin main", private),
        (f"bash -c 'cd {private} && git push origin main'", public),
        ("git push origin feature", public),
    ):
        assert not _denied(omi, command, cwd, monkeypatch), command


def test_event_cwd_decides_the_repo_end_to_end(
    two_repos: tuple[Path, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """#394: the hook event's cwd (the agent's shell) wins over the hook
    process's own cwd, in both directions."""
    omi, public, private = two_repos
    assert _denied(omi, "git push origin main", private, monkeypatch, cwd=str(public))
    assert not _denied(omi, "git push origin main", public, monkeypatch, cwd=str(private))
    assert _denied(omi, "cd ../public && git push", private, monkeypatch, cwd=str(private))


def test_seed_matches_push_after_git_global_options(
    tmp_path: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#414: the seed's `*git push*` used to miss `git -C <dir> push` and
    `git -c k=v push`. A push as git's subcommand matches after any global
    option; `git log --grep push` still does not."""
    omi = tmp_path / "OMI"
    _note_with_rule(omi)
    monkeypatch.setattr(rules, "_repo_visibility", lambda r, **k: "public")
    monkeypatch.setattr(rules, "_repo_branch", lambda r: "main")
    for command in (
        f"git -C {repo} push origin main",
        "git -c x=y push",
        "git -c user.name='A B' push origin main",
        f'git -C "{repo}" -c x=y push origin main',
        f"git --git-dir={repo}/.git --work-tree {repo} push origin main",
        "git --no-pager -P push",
        # #431 review: an escaped quote outside quotes is not a quoted run.
        "git -c user.name=O\\'Brien push origin main",
        'git -c a=\\"b push origin main',
    ):
        hit = rules.evaluate(_action(command), omi, repo)
        assert hit is not None and hit.outcome == "deny", command
    for command in (
        "git log --grep push",
        "git -C x log --grep push",
        "git --no-pager log -S push",
    ):
        assert rules.evaluate(_action(command), omi, repo) is None, command
    seed = {r.id: r for r in rules.SEED_NOTE_RULES}["no-direct-push-public-main"]
    hit = rules.evaluate(_action("git -C x push origin main"), omi, repo, rules=[seed])
    assert hit is not None and hit.outcome == "deny"
    assert rules.evaluate(_action("git log --grep push"), omi, repo, rules=[seed]) is None


def test_rules_without_repo_conditions_still_see_ssh_payloads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only REPO-scoped rules skip an ssh payload; a plain rule keeps matching
    the raw command, because a remote side effect is still a side effect."""
    omi = tmp_path / "OMI"
    omi.mkdir()
    (omi / "R.md").write_text(
        "```omind-rule\nid: no-remote-reboot\ntool: Bash\nmatch: '*reboot*'\n"
        "action: deny\nmessage: 'no'\n```\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    v = guard._note_rules_verdict({"tool": "Bash", "command": "ssh host 'sudo reboot'"}, omi)
    assert v is not None and not v.allow


def test_unknown_visibility_on_any_matching_git_is_reported(
    two_repos: tuple[Path, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Per-git judging keeps the fail-open contract: an undeterminable
    visibility is logged, never denied, unless another push hits."""
    omi, public, private = two_repos
    monkeypatch.setattr(
        rules,
        "_repo_visibility",
        lambda r, **k: "public" if r == public else "unknown",
    )
    view = rules.CommandView(
        local_text="x",
        sites=(
            rules.CommandSite("git push origin main", private),
            rules.CommandSite("git push origin feature", public),
        ),
    )
    hit = rules.evaluate(_action("x"), omi, private, view=view)
    assert hit is not None and hit.outcome == "unknown-visibility"
    view = rules.CommandView(
        local_text="x",
        sites=(
            rules.CommandSite("git push origin main", private),
            rules.CommandSite("git push origin main", public),
        ),
    )
    hit = rules.evaluate(_action("x"), omi, private, view=view)
    assert hit is not None and hit.outcome == "deny"


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


def test_export_rules_emits_markdown_for_an_always_loaded_file(tmp_path: Path) -> None:
    # #321 fix 7: an invariant behind probabilistic retrieval is a coin flip.
    omi = tmp_path / "OMI"
    omi.mkdir()
    text = rules.export_rules(omi)
    assert text.startswith("<!-- generated by `omind rules export`")
    assert "## Hard rules (omind)" in text
    assert "**NEVER `*git push*`**" in text
    assert "public repos" in text and "on main/master" in text
    assert "Go Big Red" in text  # docs carry the signature line


def test_no_github_remote_is_private_not_a_failure(tmp_path: Path, monkeypatch) -> None:
    """A repo with no GitHub remote (the OMI mesh vault pushes only to pluto/seed)
    is classified ``private`` and records NO hook failure: ``gh`` cannot classify a
    non-GitHub repo and that is expected, not an error (fixes the omind-doctor
    ``rules_visibility: gh visibility lookup failed`` breadcrumb)."""
    repo = tmp_path / "local-repo"
    repo.mkdir()

    def fake_run(argv, *a, **k):  # type: ignore[no-untyped-def]
        if argv and argv[0] == "gh":
            return subprocess.CompletedProcess(argv, 1, "", "no known GitHub host")
        if argv[:1] == ["git"] and "remote" in argv:
            return subprocess.CompletedProcess(
                argv, 0, "seed\tssh://akclark@pluto.local/home/akclark/omi-mesh.git (fetch)\n", ""
            )
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(rules.subprocess, "run", fake_run)
    breadcrumbs: list = []
    monkeypatch.setattr(rules, "_breadcrumb", lambda *a, **k: breadcrumbs.append(a))
    monkeypatch.setattr(rules, "_visibility_cache_path", lambda: tmp_path / "vis.json")

    assert rules._repo_visibility(repo) == "private"
    assert breadcrumbs == []  # not logged as a failure


def test_github_remote_but_gh_fails_is_unknown_and_breadcrumbed(
    tmp_path: Path, monkeypatch
) -> None:
    """A repo that DOES have a GitHub remote but whose ``gh`` lookup fails is a real
    failure: UNKNOWN + breadcrumb (unchanged fail-open behaviour)."""
    repo = tmp_path / "gh-repo"
    repo.mkdir()

    def fake_run(argv, *a, **k):  # type: ignore[no-untyped-def]
        if argv and argv[0] == "gh":
            return subprocess.CompletedProcess(argv, 1, "", "auth error")
        if argv[:1] == ["git"] and "remote" in argv:
            return subprocess.CompletedProcess(
                argv, 0, "origin\thttps://github.com/o/r.git (fetch)\n", ""
            )
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(rules.subprocess, "run", fake_run)
    breadcrumbs: list = []
    monkeypatch.setattr(rules, "_breadcrumb", lambda *a, **k: breadcrumbs.append(a))
    monkeypatch.setattr(rules, "_visibility_cache_path", lambda: tmp_path / "vis.json")

    assert rules._repo_visibility(repo) == rules._VISIBILITY_UNKNOWN
    assert len(breadcrumbs) == 1


def test_github_host_match_is_not_a_substring_test() -> None:
    """CodeQL py/incomplete-url-substring-sanitization: ``"github.com" in url``
    also matches hosts that merely CONTAIN it. Misclassifying one of those as
    GitHub decides repo visibility, which decides whether the public-repo
    branch+PR deny fires at all — so the host is parsed, not searched."""
    for url in (
        "https://github.com/CryptoJones/omind.git",
        "git@github.com:CryptoJones/omind.git",
        "https://user:tok@github.com/CryptoJones/omind.git",
        "ssh://git@ssh.github.com:443/CryptoJones/omind.git",
        "HTTPS://GitHub.com/CryptoJones/omind",
    ):
        assert rules._is_github_host(url), url

    for url in (
        "https://github.com.evil.example/CryptoJones/omind.git",
        "https://not-github.com/CryptoJones/omind.git",
        "git@codeberg.org:akclark/omind.git",
        "ssh://hermes/srv/git/omi.git",
        "/srv/git/local.git",
    ):
        assert not rules._is_github_host(url), url


def test_remote_urls_pulled_out_of_git_remote_v() -> None:
    out = (
        "origin\thttps://github.com/CryptoJones/omind.git (fetch)\n"
        "origin\thttps://github.com/CryptoJones/omind.git (push)\n"
        "seed\tssh://hermes/srv/git/omi.git (fetch)\n"
    )
    assert rules._remote_urls(out) == [
        "https://github.com/CryptoJones/omind.git",
        "https://github.com/CryptoJones/omind.git",
        "ssh://hermes/srv/git/omi.git",
    ]
    assert rules._remote_urls("") == []


# --- repo_has_commits (empty-repo exemption) --------------------------------
#
# An initial commit into an empty repo has nothing to open a PR against, so the
# branch+PR deny must be narrowable to repos that actually have history.


def _note_with_has_commits(omi: Path, value: str) -> None:
    omi.mkdir(parents=True, exist_ok=True)
    (omi / "Guard Rules.md").write_text(
        "```omind-rule\n"
        "id: no-direct-push-public-main\n"
        "tool: Bash\n"
        'match: "*git push*"\n'
        "when:\n"
        "  repo_visibility: public\n"
        "  branch: [main, master]\n"
        f"  repo_has_commits: {value}\n"
        "action: deny\n"
        'message: "Public repo: branch + PR required."\n'
        "```\n",
        encoding="utf-8",
    )


def test_has_commits_parsed_as_bool(tmp_path: Path) -> None:
    omi = tmp_path / "OMI"
    _note_with_has_commits(omi, "true")
    rule = rules.load_rules(omi)[0]
    assert rule.when_has_commits is True
    assert rule.conditioned_on_has_commits()

    _note_with_has_commits(omi, "false")
    rules._file_cache.clear()
    assert rules.load_rules(omi)[0].when_has_commits is False


def test_has_commits_absent_means_unconditioned(tmp_path: Path) -> None:
    omi = tmp_path / "OMI"
    _note_with_rule(omi)
    rule = rules.load_rules(omi)[0]
    assert rule.when_has_commits is None
    assert not rule.conditioned_on_has_commits()


def test_has_commits_rejects_non_boolean(tmp_path: Path) -> None:
    """A quoted "true" is a string, not a bool. Guessing at it would silently
    flip a deny, so the block is skipped and reported instead."""
    omi = tmp_path / "OMI"
    _note_with_has_commits(omi, '"yes please"')
    everything = rules.load_rules(omi, include_invalid=True)
    assert any("repo_has_commits" in r.invalid for r in everything)
    # The bad block does not replace the seed rule -- the guard stays armed
    # rather than silently vanishing, which is the failure mode that makes a
    # typo'd exception so dangerous.
    live = rules.load_rules(omi)
    assert [r.id for r in live] == ["no-direct-push-public-main"]
    assert live[0].note == "(seed)"
    assert live[0].when_has_commits is None


def test_deny_fires_when_remote_has_commits(tmp_path: Path, repo: Path, monkeypatch) -> None:
    omi = tmp_path / "OMI"
    _note_with_has_commits(omi, "true")
    monkeypatch.setattr(rules, "_repo_visibility", lambda r, **k: "public")
    monkeypatch.setattr(rules, "_repo_branch", lambda r: "main")
    monkeypatch.setattr(rules, "_remote_has_commits", lambda r: True)
    hit = rules.evaluate(_action("git push origin main"), omi, repo)
    assert hit is not None and hit.outcome == "deny"


def test_deny_skipped_when_remote_is_empty(tmp_path: Path, repo: Path, monkeypatch) -> None:
    """The whole point: an empty remote is exempt from branch+PR."""
    omi = tmp_path / "OMI"
    _note_with_has_commits(omi, "true")
    monkeypatch.setattr(rules, "_repo_visibility", lambda r, **k: "public")
    monkeypatch.setattr(rules, "_repo_branch", lambda r: "main")
    monkeypatch.setattr(rules, "_remote_has_commits", lambda r: False)
    assert rules.evaluate(_action("git push origin main"), omi, repo) is None


def test_unknown_remote_fails_safe_and_still_denies(
    tmp_path: Path, repo: Path, monkeypatch
) -> None:
    """Opposite of visibility's fail-open. An unreachable remote must never
    hand out the empty-repo exemption, so unknown keeps the deny."""
    omi = tmp_path / "OMI"
    _note_with_has_commits(omi, "true")
    monkeypatch.setattr(rules, "_repo_visibility", lambda r, **k: "public")
    monkeypatch.setattr(rules, "_repo_branch", lambda r: "main")
    monkeypatch.setattr(rules, "_remote_has_commits", lambda r: None)
    hit = rules.evaluate(_action("git push origin main"), omi, repo)
    assert hit is not None and hit.outcome == "deny"


def test_remote_has_commits_probe_reads_the_remote(tmp_path: Path) -> None:
    """Empty remote -> False; after a commit is pushed -> True. Uses real git
    against a local bare remote, so the probe itself is exercised."""
    bare = tmp_path / "bare.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    work = tmp_path / "work"
    work.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(work)], check=True)
    subprocess.run(["git", "-C", str(work), "remote", "add", "origin", str(bare)], check=True)
    # Local commit exists, remote is still empty -- the exact case that makes a
    # local-only probe useless.
    (work / "f.txt").write_text("x", encoding="utf-8")
    subprocess.run(["git", "-C", str(work), "add", "-A"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(work),
            "-c",
            "user.email=t@t",
            "-c",
            "user.name=t",
            "commit",
            "-qm",
            "initial",
        ],
        check=True,
    )
    assert rules._remote_has_commits(work) is False

    subprocess.run(["git", "-C", str(work), "push", "-q", "origin", "main"], check=True)
    assert rules._remote_has_commits(work) is True


def test_remote_has_commits_unknown_without_origin(tmp_path: Path) -> None:
    solo = tmp_path / "solo"
    solo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(solo)], check=True)
    assert rules._remote_has_commits(solo) is None


def test_format_rules_shows_has_commits(tmp_path: Path) -> None:
    omi = tmp_path / "OMI"
    _note_with_has_commits(omi, "true")
    assert "has_commits=true" in rules.format_rules(omi)


def test_pushed_branches_quoted_and_spaced_dash_c() -> None:
    # #345: _pushed_branches must recognise push commands with quoted/spaced -C / -c options
    assert rules._pushed_branches('git -C "/repo with spaces" push origin main') == ["main"]
    assert rules._pushed_branches("git -C '/repo with spaces' push origin feat") == ["feat"]
    assert rules._pushed_branches('git -C "       " push origin main') == ["main"]
    assert rules._pushed_branches(
        'git -C "/path" -c user.name="Aaron Clark" push origin feat:main'
    ) == ["main"]
    assert rules._pushed_branches("git push origin --tags") == ["(tags)"]
    # A push with no refspec is resolved by where it lands (#423).
    assert rules._pushed_branches('git -C "/repo with spaces" push') == ["(default-push):"]
    assert rules._pushed_branches("git push -u origin") == ["(default-push):origin"]
    assert rules._pushed_branches("git status") is None


def _real_repo(path: Path, branch: str) -> Path:
    """A real git repo with one commit, checked out on ``branch`` (#423): the
    HEAD tests below resolve the branch through git itself, not a stub."""
    git = ["git", "-C", str(path), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    subprocess.run([*git, "commit", "-q", "--allow-empty", "-m", "init"], check=True)
    if branch != "main":
        subprocess.run([*git, "checkout", "-q", "-b", branch], check=True)
    return path.resolve()


@pytest.fixture
def head_repos(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path, Path]:
    """(omi, on_main, on_feature): the seed-shaped `*git push*` rule's vault, and
    two REAL public repos, the first on main and the second on a feature branch."""
    omi = tmp_path / "OMI"
    _note_with_rule(omi)
    on_main = _real_repo(tmp_path / "on-main", "main")
    on_feature = _real_repo(tmp_path / "on-feature", "feature/x")
    monkeypatch.setattr(rules, "_repo_visibility", lambda r, **k: "public")
    monkeypatch.setattr(rules, "_repo_name", lambda r: r.name)
    return omi, on_main, on_feature


#: #423: `HEAD` / `@` as the refspec name the checked-out branch.
_HEAD_PUSHES: tuple[str, ...] = (
    "git push origin HEAD",
    "git push -u origin HEAD",
    "git push --set-upstream origin HEAD",
    "git push origin @",
    "git push -u origin @",
    "git push origin +HEAD",
    # Lowercase: on case-insensitive APFS / NTFS git resolves `head` to
    # .git/HEAD (checked with git 2.54 on macOS), so it is matched everywhere.
    "git push origin head",
    "git push origin +head",
    "git push",  # bare push, no upstream: the checked-out branch, as before
    "git push origin",
    "git push -u origin",
)


@pytest.mark.parametrize("command", _HEAD_PUSHES)
def test_head_push_from_public_main_is_denied(
    head_repos: tuple[Path, Path, Path], monkeypatch: pytest.MonkeyPatch, command: str
) -> None:
    omi, on_main, _on_feature = head_repos
    assert _denied(omi, command, on_main, monkeypatch), command


@pytest.mark.parametrize("command", _HEAD_PUSHES)
def test_head_push_from_feature_branch_is_allowed(
    head_repos: tuple[Path, Path, Path], monkeypatch: pytest.MonkeyPatch, command: str
) -> None:
    omi, _on_main, on_feature = head_repos
    assert not _denied(omi, command, on_feature, monkeypatch), command


def test_head_resolves_against_the_target_repo(
    head_repos: tuple[Path, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """#423: HEAD is the branch of the repo the push runs in, not of the cwd."""
    omi, on_main, on_feature = head_repos
    to_main = f"git -C {on_main.as_posix()} push -u origin HEAD"
    to_feature = f"git -C {on_feature.as_posix()} push -u origin HEAD"
    assert _denied(omi, to_main, on_feature, monkeypatch)
    assert not _denied(omi, to_feature, on_main, monkeypatch)
    assert _denied(omi, f"cd {on_main.as_posix()} && git push origin @", on_feature, monkeypatch)


def test_head_source_refspec_judges_its_destination(
    head_repos: tuple[Path, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """#423: `src:dst` is judged by dst, whatever HEAD is."""
    omi, on_main, on_feature = head_repos
    assert _denied(omi, "git push origin HEAD:main", on_feature, monkeypatch)
    assert _denied(omi, "git push origin @:refs/heads/main", on_feature, monkeypatch)
    assert not _denied(omi, "git push origin HEAD:feature/y", on_main, monkeypatch)
    assert rules._pushed_branches("git push -u origin HEAD") == ["(current)"]
    assert rules._pushed_branches("git push origin @:main") == ["main"]


def test_head_as_destination_stays_literal(
    head_repos: tuple[Path, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """#423 review: `src:HEAD` pushes a remote ref literally named HEAD, so it is
    not judged as the local checkout (a false positive on a public main)."""
    omi, on_main, on_feature = head_repos
    for where in (on_main, on_feature):
        assert not _denied(omi, "git push origin feature:HEAD", where, monkeypatch)
        assert not _denied(omi, "git push origin main:HEAD", where, monkeypatch)
        assert not _denied(omi, "git push origin main:@", where, monkeypatch)
    assert rules._pushed_branches("git push origin main:HEAD") == ["HEAD"]


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


@pytest.fixture
def tracking_main(head_repos: tuple[Path, Path, Path], tmp_path: Path) -> tuple[Path, Path]:
    """(omi, repo): the public `feature/x` repo with a REAL bare remote
    `origin`, tracking that remote's `main` (#423 review)."""
    omi, _on_main, on_feature = head_repos
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    _git(on_feature, "remote", "add", "origin", remote.as_posix())
    _git(on_feature, "push", "-q", "origin", "feature/x:main")
    _git(on_feature, "branch", "-q", "--set-upstream-to", "origin/main")
    return omi, on_feature


@pytest.mark.parametrize("command", ["git push", "git push origin", "git push -u origin"])
def test_bare_push_to_upstream_main_is_denied(
    tracking_main: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch, command: str
) -> None:
    """#423 review: on `feature/x` tracking `origin/main` with
    `push.default=upstream`, a bare push lands on main: judged by `@{push}`."""
    omi, repo = tracking_main
    _git(repo, "config", "push.default", "upstream")
    assert _denied(omi, command, repo, monkeypatch), command
    # The same repo pushing to its own name lands on feature/x: allowed.
    _git(repo, "config", "push.default", "current")
    assert not _denied(omi, command, repo, monkeypatch), command


def test_bare_push_with_head_push_refspec_is_denied(
    tracking_main: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """#423 review: `remote.origin.push HEAD:refs/heads/main` sends a bare push
    to main. git's `@{push}` cannot resolve a HEAD-sourced refspec, so the
    configured refspec is read too."""
    omi, repo = tracking_main
    _git(repo, "config", "remote.origin.push", "HEAD:refs/heads/main")
    assert _denied(omi, "git push", repo, monkeypatch)
    assert _denied(omi, "git push origin", repo, monkeypatch)


def test_head_push_from_detached_head_stays_quiet(
    head_repos: tuple[Path, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """AGENTS.md invariant 2: a detached HEAD names no branch, so the branch
    condition cannot hold and the rule stays quiet on purpose."""
    omi, on_main, _on_feature = head_repos
    _git(on_main, "checkout", "-q", "--detach")
    assert rules._repo_branch(on_main) == "HEAD"
    for command in ("git push origin HEAD", "git push", "git push origin"):
        assert not _denied(omi, command, on_main, monkeypatch), command


def test_branch_lookup_failure_stays_quiet(
    head_repos: tuple[Path, Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AGENTS.md invariant 2: when git cannot name the branch (not a repo, or
    git missing) the lookup fails open and the rule stays quiet on purpose."""
    omi, on_main, _on_feature = head_repos
    not_a_repo = tmp_path / "plain"
    not_a_repo.mkdir()
    for command in ("git push origin HEAD", "git push", "git push origin"):
        assert rules.evaluate(_action(command), omi, not_a_repo) is None, command
    assert rules._default_push_branches(not_a_repo, "") == [""]

    def no_git(*args: object, **kwargs: object) -> object:
        raise FileNotFoundError("git")

    monkeypatch.setattr(rules.subprocess, "run", no_git)
    assert rules._repo_branch(on_main) == ""
    assert rules._default_push_branches(on_main, "origin") == [""]
    for command in ("git push origin HEAD", "git push", "git push origin"):
        assert rules.evaluate(_action(command), omi, on_main) is None, command
