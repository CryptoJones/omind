# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Aaron K. Clark
"""Tests for the harness-agnostic OMI-compliance guard decision engine."""

from __future__ import annotations

import importlib.resources
import io
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from omind import guard, paths

#: The omi-guard.sh adapter is a POSIX bash+jq deployment artifact (Claude Code on
#: Linux/macOS). Its subprocess tests only make sense where a real bash + jq run it —
#: NOT on Windows, where Git Bash's CRLF/path quirks make the same script exit 1 and
#: where the hook isn't the deployed form anyway.
_HOOK_TESTABLE = (
    sys.platform != "win32"
    and shutil.which("bash") is not None
    and shutil.which("jq") is not None
)


def _satisfy_repo_preconditions(session: str) -> None:
    guard.record_consult(session, kind="read", target=guard.GIT_RULES_NOTE, relevant=True)
    repo = guard._repo_root_for_action({"tool": "Bash", "command": "git status"})
    assert repo is not None
    guard._record_git_freshness(session, repo, "git fetch --all --prune")


def test_omi_consult_is_allowed_and_sets_the_per_turn_sentinel() -> None:
    guard.clear_gate("s1")
    assert guard.decide({"is_omi_consult": True, "session": "s1"}).allow
    assert guard.consulted_this_turn("s1")
    guard.clear_gate("s1")


def test_hard_block_fires_even_when_consulted() -> None:
    guard.mark_consulted("s2")  # gate is satisfied, yet a hard rule still wins
    verdict = guard.decide({"tool": "Bash", "command": "gh repo delete x/y", "session": "s2"})
    assert not verdict.allow
    assert "hard" in verdict.reason
    guard.clear_gate("s2")


def test_gate_blocks_until_consulted_then_re_arms() -> None:
    guard.clear_gate("s3")
    assert not guard.decide({"command": "ls", "session": "s3"}).allow  # nothing consulted
    guard.decide({"is_omi_consult": True, "session": "s3"})  # consult
    assert guard.decide({"command": "ls", "session": "s3"}).allow  # cleared for the turn
    guard.clear_gate("s3")  # turn-start reset
    assert not guard.decide({"command": "ls", "session": "s3"}).allow  # re-armed


def test_full_destructive_set_is_blocked() -> None:
    guard.mark_consulted("s4")
    for cmd in (
        "gh auth setup-git",
        "gh repo delete x/y",
        "gh api -X DELETE repos/x/y",
    ):
        assert not guard.decide({"command": cmd, "session": "s4"}).allow, cmd
    guard.clear_gate("s4")


def test_codeberg_push_is_allowed_after_consult() -> None:
    _satisfy_repo_preconditions("s5")
    cmd = "git push git@codeberg.org:CryptoJones/omind.git main"
    assert guard.decide({"command": cmd, "session": "s5"}).allow
    guard.clear_gate("s5")


def test_raw_sudo_blocked_but_fleet_sudo_and_opt_in_allowed() -> None:
    guard.mark_consulted("sSudo")
    # raw sudo is a hard block that names the fleet-sudo rule
    verdict = guard.decide({"command": "sudo systemctl reload nginx", "session": "sSudo"})
    assert not verdict.allow
    assert verdict.rule_id == "sudo-use-fleet-sudo"
    # fleet-sudo is NOT caught by the sudo rule (the "-sudo" suffix is excluded)
    assert guard.decide(
        {"command": "fleet-sudo systemctl reload nginx", "session": "sSudo"}
    ).allow
    # a deliberate raw sudo opts in, like the Codeberg-mirror escape hatch
    assert guard.decide({"command": "OMI_SUDO_OK=1 sudo reboot", "session": "sSudo"}).allow
    guard.clear_gate("sSudo")


def test_escalation_keyword_only_matches_in_command_position() -> None:
    # #98/#108: the keyword must be the COMMAND being run, not a substring in an
    # argument, path, string, comment, or assignment value. These all USED to be
    # blocked and must now pass.
    guard.mark_consulted("sCmdPos")
    allowed = [
        'grep -rn "sudo" src/',  # grep argument
        "cat /var/log/sudo.log",  # path component
        "find . -name sudo.txt",  # filename
        'git commit -m "fix sudo handling"',  # commit message
        "pass show sudo/akclark",  # pass entry value (sudo guard only; see note)
        "ls /etc/sudoers.d/",  # directory name
        "FOO=sudo ./run.sh",  # env VALUE, not the command
        "apt install sudo",  # installing the package
        "man su",  # su as an argument
        "cat doas.conf",  # doas in a filename
        "tmux new -s run0",  # run0 as a session name
        "git log --grep su",  # su as a grep pattern
    ]
    for cmd in allowed:
        assert guard.decide({"command": cmd, "session": "sCmdPos"}).allow, cmd

    # ...but a real escalation in command position still blocks, including after
    # every shell separator and past a leading env-assignment.
    blocked = [
        "sudo -n true",
        "sudo apt; echo done",
        "echo x | sudo tee /etc/hosts",
        "cd /tmp && sudo reboot",
        "FOO=1 sudo apt update",
        "make build\nsudo make install",
        "$(sudo id)",
        "(sudo reboot)",
        "pkexec rm -rf /tmp/x",
        "doas reboot",
        'su -c "x" root',
        "echo x | su",
        "cd /x && pkexec y",
    ]
    for cmd in blocked:
        v = guard.decide({"command": cmd, "session": "sCmdPos"})
        assert not v.allow, cmd
        assert v.rule_id in {"sudo-use-fleet-sudo", "privesc-alternatives"}, cmd
    guard.clear_gate("sCmdPos")


def test_run_guard_check_and_reset_exit_codes() -> None:
    guard.clear_gate("s6")
    blocked = guard.run_guard("check", io.StringIO(json.dumps({"command": "ls", "session": "s6"})))
    assert blocked == 2
    ok = guard.run_guard(
        "check", io.StringIO(json.dumps({"is_omi_consult": True, "session": "s6"}))
    )
    assert ok == 0
    assert guard.run_guard("reset", io.StringIO(json.dumps({"session": "s6"}))) == 0
    assert not guard.consulted_this_turn("s6")


def test_repo_work_requires_git_rules_note_and_freshness_check() -> None:
    guard.clear_gate("repo")
    blocked = guard.decide({"tool": "Bash", "command": "pytest", "session": "repo"})
    assert not blocked.allow
    assert blocked.rule_id == "repo-work-read-git-rules"

    # After the rules-note consult, NON-commit repo work (a test run) is allowed —
    # freshness is only demanded before a commit.
    guard.record_consult("repo", kind="read", target=guard.GIT_RULES_NOTE, relevant=True)
    assert guard.decide({"tool": "Bash", "command": "pytest", "session": "repo"}).allow

    # A commit, however, still demands freshness.
    blocked = guard.decide({"tool": "Bash", "command": "git commit -am x", "session": "repo"})
    assert not blocked.allow
    assert blocked.rule_id == "repo-work-fresh-base"

    # A fetch chained with the commit is NOT a pure freshness command, so it
    # records nothing and the commit stays blocked.
    compound_cmd = "git fetch --all --prune && git commit -am x"
    compound = guard.decide({"tool": "Bash", "command": compound_cmd, "session": "repo"})
    assert not compound.allow
    assert compound.rule_id == "repo-work-fresh-base"

    # A standalone fetch establishes freshness for the separate next commit.
    fresh = guard.decide(
        {"tool": "Bash", "command": "git fetch --all --prune", "session": "repo"}
    )
    assert fresh.allow
    assert guard.decide({"tool": "Bash", "command": "git commit -am x", "session": "repo"}).allow
    guard.clear_gate("repo")


def _git_init(path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(path)], check=True)


def test_repo_has_remote_detects_configured_remotes(tmp_path: Path) -> None:
    repo = tmp_path / "r"
    repo.mkdir()
    _git_init(repo)
    # A freshly initialised repo has no remote.
    assert guard._repo_has_remote(repo) is False
    subprocess.run(
        ["git", "-C", str(repo), "remote", "add", "origin", "https://x.invalid/y.git"],
        check=True,
    )
    assert guard._repo_has_remote(repo) is True
    # Conservative on any doubt: a path that isn't a resolvable repo dir (no
    # readable `<repo>/.git/config`) is treated as HAVING a remote so freshness
    # is never wrongly waived for a real repo.
    assert guard._repo_has_remote(tmp_path / "does-not-exist") is True


def test_new_repo_without_a_remote_does_not_demand_freshness(tmp_path: Path) -> None:
    # A brand-new `git init` repo has no remote — `git fetch`/`git pull` are
    # impossible, so the freshness gate must not lock the agent out of its own
    # new repo (#149). The rules-note consult is still required.
    repo = tmp_path / "newrepo"
    repo.mkdir()
    _git_init(repo)
    session = "newrepo-noremote"
    guard.clear_gate(session)

    wfile = str(repo / "hello.py")
    blocked = guard.decide({"tool": "Write", "file_path": wfile, "session": session})
    assert not blocked.allow
    assert blocked.rule_id == "repo-work-read-git-rules"

    guard.record_consult(session, kind="read", target=guard.GIT_RULES_NOTE, relevant=True)
    allowed = guard.decide({"tool": "Write", "file_path": wfile, "session": session})
    assert allowed.allow, allowed.rule_id  # was repo-work-fresh-base before #149
    guard.clear_gate(session)


def test_non_repo_work_does_not_demand_freshness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cwd and target outside Git must never demand an impossible fetch."""
    monkeypatch.chdir(tmp_path)
    session = "not-a-repo"
    guard.clear_gate(session)
    guard.record_consult(session, kind="read", target="task memory", relevant=True)

    target = tmp_path / "notes.txt"
    assert guard._repo_root_for_action(
        {"tool": "Write", "file_path": str(target), "session": session}
    ) is None
    allowed = guard.decide(
        {"tool": "Write", "file_path": str(target), "session": session}
    )
    assert allowed.allow, allowed.rule_id
    guard.clear_gate(session)


def test_new_repo_with_a_remote_still_demands_freshness(tmp_path: Path) -> None:
    # A repo that HAS a remote has an upstream to be stale against, so a COMMIT
    # still demands freshness — the #149 waiver is scoped to no-remote. (A plain
    # edit no longer demands it; only the commit does — see
    # test_freshness_gate_applies_only_to_commits.)
    repo = tmp_path / "hasremote"
    repo.mkdir()
    _git_init(repo)
    subprocess.run(
        ["git", "-C", str(repo), "remote", "add", "origin", "https://x.invalid/y.git"],
        check=True,
    )
    session = "hasremote-fresh"
    guard.clear_gate(session)
    guard.record_consult(session, kind="read", target=guard.GIT_RULES_NOTE, relevant=True)

    commit = guard.decide(
        {"tool": "Bash", "command": f"git -C {repo} commit -am x", "session": session}
    )
    assert not commit.allow
    assert commit.rule_id == "repo-work-fresh-base"
    guard.clear_gate(session)


def test_freshness_gate_applies_only_to_commits(tmp_path: Path) -> None:
    """CJ, 2026-07-20: the freshness check is scoped to ``git commit`` only. After
    the rules-note consult, edits/tests/pushes/reads on a stale (never-fetched)
    repo are allowed; only a commit is blocked until a standalone fetch runs."""
    repo = tmp_path / "scoped"
    repo.mkdir()
    _git_init(repo)
    subprocess.run(
        ["git", "-C", str(repo), "remote", "add", "origin", "https://x.invalid/y.git"],
        check=True,
    )
    session = "commit-scope"
    guard.clear_gate(session)
    guard.record_consult(session, kind="read", target=guard.GIT_RULES_NOTE, relevant=True)

    # Non-commit repo work on a stale base: allowed (rules-note satisfied, no fetch).
    assert guard.decide(
        {"tool": "Edit", "file_path": str(repo / "x.py"), "session": session}
    ).allow
    assert guard.decide(
        {"tool": "Bash", "command": f"git -C {repo} push origin main", "session": session}
    ).allow
    assert guard.decide(
        {"tool": "Bash", "command": f"cd {repo} && pytest", "session": session}
    ).allow

    # The commit is the one action still gated on freshness.
    blocked = guard.decide(
        {"tool": "Bash", "command": f"git -C {repo} commit -am x", "session": session}
    )
    assert not blocked.allow
    assert blocked.rule_id == "repo-work-fresh-base"
    guard.clear_gate(session)


def test_global_config_mutation_requires_explicit_turn_authorization() -> None:
    guard.begin_turn("global", "Can you fix both?")
    blocked = guard.decide(
        {
            "tool": "Write",
            "file_path": str(Path.home() / ".codex" / "AGENTS.md"),
            "session": "global",
        }
    )
    assert not blocked.allow
    assert blocked.rule_id == "capability-question-explicit-auth"

    guard.begin_turn("global", "Please update the global Codex AGENTS bootstrap.")
    allowed = guard.decide(
        {
            "tool": "Write",
            "file_path": str(Path.home() / ".codex" / "AGENTS.md"),
            "session": "global",
        }
    )
    assert not allowed.allow
    assert allowed.rule_id not in {
        "capability-question-explicit-auth",
        "global-config-explicit-auth",
    }

    guard.begin_turn("global", "Send it.")
    send_it = guard.decide(
        {
            "tool": "Write",
            "file_path": str(Path.home() / ".codex" / "AGENTS.md"),
            "session": "global",
        }
    )
    assert not send_it.allow
    assert send_it.rule_id not in {
        "capability-question-explicit-auth",
        "global-config-explicit-auth",
    }
    guard.clear_gate("global")


def test_global_config_auth_can_come_from_action_prompt() -> None:
    hook_path = Path.home() / ".claude" / "hooks" / "omi-guard.sh"
    verdict = guard.decide(
        {
            "tool": "Bash",
            "command": f"chmod 600 {hook_path}",
            "prompt": "I give you explicit permission to make the change.",
            "session": "global-prompt",
        }
    )
    assert not verdict.allow
    assert verdict.rule_id == "omi-gate"
    guard.clear_gate("global-prompt")


def test_capability_question_blocks_side_effect_without_explicit_auth() -> None:
    blocked = guard.decide(
        {
            "tool": "Bash",
            "command": "gh issue create --title x",
            "prompt": "Can you make an issue for that?",
            "session": "capq",
        }
    )
    assert not blocked.allow
    assert blocked.rule_id == "capability-question-explicit-auth"

    allowed = guard.decide(
        {
            "tool": "Bash",
            "command": "gh issue create --title x",
            "prompt": "Can you make an issue for that? Send it.",
            "session": "capq",
        }
    )
    assert not allowed.allow
    assert allowed.rule_id == "omi-gate"
    guard.clear_gate("capq")


def test_global_config_read_only_shell_commands_are_not_mutations() -> None:
    hook_path = Path.home() / ".claude" / "hooks" / "omi-guard.sh"

    guard.begin_turn("global-read", "Can you inspect the hook?")
    guard.mark_consulted("global-read")
    allowed = guard.decide(
        {
            "tool": "Bash",
            "command": f"stat {hook_path}",
            "session": "global-read",
        }
    )
    assert allowed.allow

    blocked = guard.decide(
        {
            "tool": "Bash",
            "command": f"chmod +x {hook_path}",
            "session": "global-read",
        }
    )
    assert not blocked.allow
    assert blocked.rule_id == "capability-question-explicit-auth"
    guard.clear_gate("global-read")


def test_clear_gate_reaps_legacy_tmp_sentinels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(guard, "_LEGACY_SENTINEL_DIRS", (tmp_path,))
    legacy = tmp_path / "omi-gate-deadbeef"
    legacy.write_text("")
    unrelated = tmp_path / "keep.txt"
    unrelated.write_text("x")
    guard.clear_gate("sReap")
    assert not legacy.exists()  # stale prototype sentinel reaped
    assert unrelated.exists()  # unrelated files untouched


def test_sentinel_path_lives_in_state_dir() -> None:
    assert guard._sentinel_path("abc.def") == paths.state_dir() / "gate-abc.def"


def test_guard_and_reset_adapters_share_one_sentinel_path() -> None:
    """Regression for the /tmp-vs-state-dir drift: the guard and reset adapters
    must compute the same per-turn sentinel path, and the guard must never use
    the legacy /tmp path (only the reset reaps it)."""
    files = importlib.resources.files("omind")
    guard_sh = files.joinpath("omi-guard.sh").read_text(encoding="utf-8")
    reset_sh = files.joinpath("omi-gate-reset.sh").read_text(encoding="utf-8")
    state_expr = "${XDG_STATE_HOME:-$HOME/.local/state}/omind"
    assert state_expr in guard_sh and "gate-$sid" in guard_sh
    assert state_expr in reset_sh and "gate-$sid" in reset_sh
    assert "/tmp/omi-gate" not in guard_sh


def test_toolsearch_is_never_gated_and_does_not_satisfy_the_gate() -> None:
    """Regression: ToolSearch (the only way to load a deferred OMI MCP tool's
    schema) must pass the gate so a consult is possible, yet must NOT itself
    count as a consult — otherwise it would silently clear the gate."""
    guard.clear_gate("sTS")
    verdict = guard.decide({"tool": "ToolSearch", "session": "sTS"})
    assert verdict.allow  # allowed with nothing consulted — no deadlock
    assert not guard.consulted_this_turn("sTS")  # but it did NOT clear the gate
    guard.clear_gate("sTS")


def test_bash_adapters_exempt_toolsearch_from_the_gate() -> None:
    files = importlib.resources.files("omind")
    for name in ("omi-guard.sh", "omi-guard-hermes.sh"):
        sh = files.joinpath(name).read_text(encoding="utf-8")
        assert "ToolSearch)" in sh, f"{name} must exempt ToolSearch from the gate"


def test_turn_task_capture_roundtrip() -> None:
    guard.begin_turn("t1", "fix the codeberg release workflow")
    assert guard.turn_task("t1") == "fix the codeberg release workflow"
    assert guard.turn_task("never-set") == ""  # never raises on a missing turn file


def test_reset_clears_gate_and_captures_task() -> None:
    guard.mark_consulted("t2")
    assert guard.consulted_this_turn("t2")
    guard.run_guard(
        "reset", io.StringIO(json.dumps({"session_id": "t2", "prompt": "do the thing"}))
    )
    assert not guard.consulted_this_turn("t2")  # gate re-armed
    assert guard.turn_task("t2") == "do the thing"  # task captured for the verifier


def test_turn_preflight_recalls_relevant_memory_and_satisfies_soft_gate(
    tmp_path: Path,
) -> None:
    from omind import ai_usage
    from omind.store import NoteFields, OmiStore

    omi = tmp_path / "OMI"
    omi.mkdir()
    OmiStore(omi).create_note(
        NoteFields(
            title="Token Usage Strategy",
            summary="Keep OMI token usage bounded.",
            details="Use compact recall and avoid duplicate note representations.",
        )
    )
    event = {"session_id": "preflight-1", "prompt": "reduce OMI token usage"}
    context = guard.preflight_turn(event, omi)
    assert "[[Token Usage Strategy]]" in context
    assert "compact recall" in context
    assert guard.consulted_this_turn("preflight-1")
    usage = ai_usage.read_events(omi)
    assert usage[-1]["operation"] == "recall"

    repeated = guard.preflight_turn(event, omi)
    assert "already injected earlier this session" in repeated
    assert "Keep OMI token usage bounded." in repeated
    assert "compact recall" not in repeated


def test_turn_preflight_without_match_leaves_gate_armed(tmp_path: Path) -> None:
    omi = tmp_path / "OMI"
    omi.mkdir()
    context = guard.preflight_turn(
        {"session_id": "preflight-none", "prompt": "unmatched subject"},
        omi,
    )
    assert "search-vault" in context and "recall-note" in context
    assert not guard.consulted_this_turn("preflight-none")


def test_preflight_cli_emits_user_prompt_additional_context(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    omi = tmp_path / "OMI"
    omi.mkdir()
    rc = guard.run_guard(
        "preflight",
        io.StringIO(json.dumps({"session_id": "preflight-cli", "prompt": "unknown"})),
        omi_dir=omi,
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    assert "additionalContext" in payload["hookSpecificOutput"]


def test_reset_with_no_session_clears_every_gate() -> None:
    """A by-hand ``omind guard reset`` (no session id) clears ALL gates — the
    recovery path, since a human un-wedging the gate cannot know the live sid."""
    guard.mark_consulted("recoverA")
    guard.mark_consulted("recoverB")
    guard.bump_reclose("recoverA")
    assert guard.consulted_this_turn("recoverA") and guard.consulted_this_turn("recoverB")
    assert guard.run_guard("reset", io.StringIO("")) == 0  # empty payload, no session
    assert not guard.consulted_this_turn("recoverA")
    assert not guard.consulted_this_turn("recoverB")
    assert guard.reclose_count("recoverA") == 0  # counters reaped too


def test_reset_does_not_hang_on_an_interactive_tty() -> None:
    """``omind guard reset`` typed at a shell has no piped payload; reading the
    TTY would block forever, so ``_load`` short-circuits an interactive stdin."""

    class _Tty(io.StringIO):
        def isatty(self) -> bool:
            return True

    # If ``_load`` read this stream the content would parse as JSON; it must NOT
    # touch a TTY (that is the hang), and return ``{}`` instead.
    assert guard._load(_Tty('{"session": "ttysess"}')) == {}
    guard.mark_consulted("ttysess")
    assert guard.run_guard("reset", _Tty("")) == 0  # clears all gates, never hangs
    assert not guard.consulted_this_turn("ttysess")


def test_reclose_counter_survives_clear_gate_and_resets_each_turn() -> None:
    """The verifier's anti-wedge cap is per turn: the counter increments, SURVIVES
    ``clear_gate`` (which a re-close calls), and zeroes at turn start."""
    guard.begin_turn("rc", "some task")  # turn start zeroes the counter
    assert guard.reclose_count("rc") == 0
    assert guard.bump_reclose("rc") == 1
    guard.clear_gate("rc")  # a re-close must NOT reset the counter
    assert guard.reclose_count("rc") == 1
    assert guard.bump_reclose("rc") == 2
    guard.begin_turn("rc", "next turn")  # a new turn resets it
    assert guard.reclose_count("rc") == 0


def test_record_consult_accumulates_and_survives_a_bash_touch(tmp_path: Path) -> None:
    guard.record_consult("t3", kind="read", target="A.md", relevant=True)
    guard.record_consult("t3", kind="search", target="codeberg", relevant=None)
    recorded = guard.consults("t3")
    assert [c["target"] for c in recorded] == ["A.md", "codeberg"]
    assert recorded[0]["relevant"] is True
    # An empty file (as the bash adapter's `touch` leaves it) reads as no consults,
    # never a crash.
    guard._sentinel_path("t4").parent.mkdir(parents=True, exist_ok=True)
    guard._sentinel_path("t4").write_text("", encoding="utf-8")
    assert guard.consults("t4") == []
    assert guard.consulted_this_turn("t4")


def test_is_omi_consult_with_target_is_recorded() -> None:
    guard.clear_gate("t5")
    guard.decide(
        {
            "is_omi_consult": True,
            "session": "t5",
            "consult_target": "Note.md",
            "consult_kind": "read",
        }
    )
    assert guard.consults("t5")[0]["target"] == "Note.md"
    guard.clear_gate("t5")


# -- 2.41.0: observability + repair ------------------------------------------


def test_guard_policy_and_status(capsys: pytest.CaptureFixture[str]) -> None:
    assert guard.run_guard("policy") == 0
    out = capsys.readouterr().out
    assert "gh-repo-delete" in out and "seed" in out
    assert guard.run_guard("status") == 0
    status = capsys.readouterr().out
    assert "hermes" in status and "opencode" in status and "claude" in status


def test_guard_explain_allow_and_deny(capsys: pytest.CaptureFixture[str]) -> None:
    assert guard.run_guard("explain", command="ls -la") == 0
    assert "ALLOW" in capsys.readouterr().out
    assert guard.run_guard("explain", command="gh repo delete x/y") == 0
    out = capsys.readouterr().out
    assert "DENY" in out and "gh-repo-delete" in out
    assert guard.run_guard("explain", command="") == 1  # no command -> error


def test_guard_log(capsys: pytest.CaptureFixture[str]) -> None:
    from omind import compliance

    compliance.log_event(
        compliance.KIND_DECISION, rule_id="gh-repo-delete", command="x", outcome="deny"
    )
    assert guard.run_guard("log", limit=10) == 0
    out = capsys.readouterr().out
    assert "gh-repo-delete" in out and "deny" in out


def test_guard_repair_invokes_heal(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from omind import provision

    monkeypatch.setattr(provision, "heal_omi_guard", lambda **kw: True)
    assert guard.run_guard("repair", omi_dir=Path("/x/OMI")) == 0
    assert "repaired" in capsys.readouterr().out


def test_pause_skips_the_gate_but_keeps_hard_blocks() -> None:
    """`omind guard pause` opens the consult-gate for a window, but a hard
    destructive rule still denies — the pause check sits AFTER the hard blocks."""
    # Unconsulted action is gate-blocked normally...
    assert not guard.decide({"command": "ls", "session": "pz"}).allow
    guard.pause_gate(60)
    assert guard.gate_paused()
    # ...and allowed while paused, with no consult.
    assert guard.decide({"command": "ls", "session": "pz"}).allow
    # But a hard destructive command is STILL denied even while paused.
    blocked = guard.decide({"command": "gh repo delete acme/x", "session": "pz"})
    assert not blocked.allow and blocked.rule_id == "gh-repo-delete"


def test_pause_auto_expires_and_reaps_the_sentinel() -> None:
    guard.pause_gate(5, now=0.0)  # expiry at epoch 5
    assert guard.gate_paused(now=1.0)
    assert guard.pause_remaining(now=1.0) == 4
    assert not guard.gate_paused(now=100.0)  # window lapsed -> re-armed (fails safe)
    assert not guard._pause_path().exists()  # expired sentinel reaped


def test_resume_re_arms_immediately() -> None:
    guard.pause_gate(3600)
    assert guard.gate_paused()
    guard.resume_gate()
    assert not guard.gate_paused()
    assert not guard.decide({"command": "ls", "session": "pr"}).allow  # gate back on


def test_clear_all_gates_leaves_an_intentional_pause_intact() -> None:
    guard.pause_gate(3600)
    guard.clear_all_gates()  # the by-hand un-wedge must not kill a deliberate pause
    assert guard.gate_paused()


def test_parse_duration_units() -> None:
    assert guard._parse_duration("90s") == 90
    assert guard._parse_duration("30m") == 1800
    assert guard._parse_duration("2h") == 7200
    assert guard._parse_duration("45") == 45 * 60  # bare number = minutes
    assert guard._parse_duration("banana") is None
    assert guard._parse_duration("") is None


def test_run_pause_default_and_resume(capsys: pytest.CaptureFixture[str]) -> None:
    assert guard.run_guard("pause") == 0  # no --for -> default window
    assert guard.gate_paused()
    assert "PAUSED" in capsys.readouterr().out
    assert guard.run_guard("resume") == 0
    assert not guard.gate_paused()
    assert "re-armed" in capsys.readouterr().out


def test_run_pause_rejects_a_bad_duration(capsys: pytest.CaptureFixture[str]) -> None:
    assert guard.run_guard("pause", duration="banana") == 1
    assert not guard.gate_paused()  # nothing engaged on a bad value
    assert "bad --for" in capsys.readouterr().err


def test_pause_engagement_is_logged_for_audit() -> None:
    from omind import compliance

    guard.run_guard("pause", duration="15m")
    assert any(
        e.get("rule_id") == "gate-paused" and e.get("outcome") == "paused"
        for e in compliance.read_events()
    )


def test_opt_in_must_be_a_real_leading_assignment_not_a_substring() -> None:
    """#2: the opt-in token only bypasses a hard rule when it is a genuine leading
    env assignment — forging it in a comment or a string must NOT skip the deny."""
    _satisfy_repo_preconditions("optf")
    # forged in a trailing comment -> not a real assignment -> still denied
    assert not guard.decide({"command": "sudo reboot   # OMI_SUDO_OK=1", "session": "optf"}).allow
    # forged inside a string arg -> still denied
    assert not guard.decide(
        {"command": 'echo "OMI_SUDO_OK=1 to allow" && sudo reboot', "session": "optf"}
    ).allow
    # genuine leading assignment -> allowed (the deliberate opt-in)
    assert guard.decide({"command": "OMI_SUDO_OK=1 sudo reboot", "session": "optf"}).allow
    # genuine, after a separator -> allowed
    assert guard.decide(
        {
            "command": "cd /r && OMI_PUSH_GITHUB=1 git push https://x@github.com/o/r main",
            "session": "optf",
        }
    ).allow
    guard.clear_gate("optf")


def _render_hook(tmp_path: Path, omind_bin: str) -> Path:
    """Render the package omi-guard.sh with substituted paths to a runnable file."""
    src = importlib.resources.files("omind").joinpath("omi-guard.sh").read_text(encoding="utf-8")
    src = src.replace("__OMIND_BIN__", omind_bin).replace("__OMI_DIR__", str(tmp_path / "OMI"))
    hook = tmp_path / "omi-guard.sh"
    hook.write_text(src, encoding="utf-8")
    hook.chmod(0o755)
    return hook


def _run_hook(hook: Path, event: dict[str, object]) -> int:
    return subprocess.run(
        ["bash", str(hook)], input=json.dumps(event), capture_output=True, text=True
    ).returncode


_BASH_EVENT = {"tool_name": "Bash", "session_id": "h", "tool_input": {"command": "echo hi"}}


@pytest.mark.skipif(not _HOOK_TESTABLE, reason="omi-guard.sh is a POSIX bash+jq adapter")
def test_hook_fails_closed_when_omind_is_missing(tmp_path: Path) -> None:
    """#1: a Bash command must never run if the core can't evaluate its hard-rules."""
    hook = _render_hook(tmp_path, "/nonexistent/omind")
    assert _run_hook(hook, _BASH_EVENT) == 2  # BLOCK, not the old fall-through


@pytest.mark.skipif(not _HOOK_TESTABLE, reason="omi-guard.sh is a POSIX bash+jq adapter")
def test_hook_fails_closed_on_unexpected_core_exit(tmp_path: Path) -> None:
    fake = tmp_path / "fakeomind"
    fake.write_text("#!/usr/bin/env bash\nexit 99\n", encoding="utf-8")
    fake.chmod(0o755)
    hook = _render_hook(tmp_path, str(fake))
    assert _run_hook(hook, _BASH_EVENT) == 2  # 99 != 0/2 => policy not evaluated => BLOCK


@pytest.mark.skipif(not _HOOK_TESTABLE, reason="omi-guard.sh is a POSIX bash+jq adapter")
def test_hook_allows_when_core_allows(tmp_path: Path) -> None:
    fake = tmp_path / "fakeomind"
    fake.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    fake.chmod(0o755)
    hook = _render_hook(tmp_path, str(fake))
    assert _run_hook(hook, _BASH_EVENT) == 0  # a clean allow is still honoured


_NOJQ_TESTABLE = (
    sys.platform != "win32"
    and shutil.which("bash") is not None
    and shutil.which("cat") is not None
    and shutil.which("grep") is not None
)


def _bin_without_jq(tmp_path: Path) -> Path:
    """A PATH dir with the tools omi-guard.sh needs symlinked in — but NOT jq — so
    `command -v jq` fails and the hook must take the pure-Python fallback (#107)."""
    bindir = tmp_path / "nojqbin"
    bindir.mkdir()
    for tool in ("bash", "sh", "cat", "grep", "mkdir", "touch", "tr", "date", "env"):
        real = shutil.which(tool)
        if real and not (bindir / tool).exists():
            (bindir / tool).symlink_to(real)
    assert shutil.which("jq", path=str(bindir)) is None  # jq really is hidden
    return bindir


def _fake_omind(tmp_path: Path, exit_code: int) -> Path:
    fake = tmp_path / f"omind{exit_code}"
    fake.write_text(f"#!/usr/bin/env bash\nexit {exit_code}\n", encoding="utf-8")
    fake.chmod(0o755)
    return fake


def _fake_consult_omind(tmp_path: Path) -> Path:
    fake = tmp_path / "fake-consult-omind"
    fake.write_text(
        f"""#!{sys.executable}
import json
import os
import pathlib
import sys

data = json.loads(sys.stdin.read() or "{{}}")
if sys.argv[1:3] == ["guard", "check"] and data.get("is_omi_consult"):
    sid = "".join(ch for ch in str(data.get("session") or "nosid") if ch.isalnum() or ch in "._-")
    if os.environ.get("XDG_STATE_HOME"):
        base = pathlib.Path(os.environ["XDG_STATE_HOME"])
    else:
        base = pathlib.Path.home() / ".local" / "state"
    state = base / "omind"
    state.mkdir(parents=True, exist_ok=True)
    payload = {{
        "consults": [
            {{
                "kind": data.get("consult_kind", "consult"),
                "target": data.get("consult_target", ""),
                "relevant": None,
            }}
        ]
    }}
    (state / f"gate-{{sid or 'nosid'}}").write_text(
        json.dumps(payload),
        encoding="utf-8",
    )
sys.exit(0)
""",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    return fake


@pytest.mark.skipif(not _NOJQ_TESTABLE, reason="needs posix bash + coreutils")
def test_hook_routes_through_adapter_when_jq_missing(tmp_path: Path) -> None:
    """#107: without jq the hook must NOT wedge — it routes the raw event through
    `omind guard adapter` (pure Python). A Bash event returning 0 can ONLY happen
    via that route, since the no-core fallback fails CLOSED (2) for Bash."""
    bindir = _bin_without_jq(tmp_path)
    bash = shutil.which("bash")
    assert bash is not None
    for code in (0, 2):
        hook = _render_hook(tmp_path, str(_fake_omind(tmp_path, code)))
        rc = subprocess.run(
            [bash, str(hook)],
            input=json.dumps(_BASH_EVENT),
            capture_output=True,
            text=True,
            env={"PATH": str(bindir), "HOME": str(tmp_path)},
        ).returncode
        assert rc == code, f"adapter exit {code} should pass through, got {rc}"


@pytest.mark.skipif(not _NOJQ_TESTABLE, reason="needs posix bash + coreutils")
def test_hook_without_jq_and_without_omind_fails_closed_for_bash_only(tmp_path: Path) -> None:
    """No jq AND no working core: Bash fails CLOSED (2), non-Bash fails OPEN (0)."""
    bindir = _bin_without_jq(tmp_path)
    bash = shutil.which("bash")
    assert bash is not None
    hook = _render_hook(tmp_path, "/nonexistent/omind")

    def run(event: dict[str, object]) -> int:
        return subprocess.run(
            [bash, str(hook)],
            input=json.dumps(event),
            capture_output=True,
            text=True,
            env={"PATH": str(bindir), "HOME": str(tmp_path)},
        ).returncode

    assert run(_BASH_EVENT) == 2  # destructive command must not run unchecked
    edit_event = {"tool_name": "Edit", "session_id": "h", "tool_input": {"file_path": "/x"}}
    assert run(edit_event) == 0  # a non-Bash tool must not wedge the host


def _read_event(omi: Path, name: str, sid: str) -> dict[str, object]:
    return {
        "tool_name": "Read",
        "session_id": sid,
        "tool_input": {"file_path": str(omi / name)},
    }


@pytest.mark.skipif(not _HOOK_TESTABLE, reason="omi-guard.sh is a POSIX bash+jq adapter")
def test_hook_index_read_does_not_clear_the_gate_but_real_note_does(tmp_path: Path) -> None:
    """The index.md gate-dodge: a Read of the vault TOC / MEMORY.md / template
    under the OMI folder is ALLOWED but must NOT clear the per-turn gate, while a
    Read of a real content note still does."""
    hook = _render_hook(tmp_path, str(_fake_consult_omind(tmp_path)))
    omi = tmp_path / "OMI"  # matches __OMI_DIR__ substituted by _render_hook
    for scaffold in ("index.md", "MEMORY.md", "Memory Template.md"):
        guard.clear_gate("hidx")
        assert _run_hook(hook, _read_event(omi, scaffold, "hidx")) == 0  # allowed through
        assert not guard.consulted_this_turn("hidx"), f"{scaffold} wrongly cleared the gate"
    # a real content note under the OMI folder still clears the gate
    guard.clear_gate("hidx")
    assert _run_hook(hook, _read_event(omi, "RealNote.md", "hidx")) == 0
    assert guard.consulted_this_turn("hidx")
    guard.clear_gate("hidx")


def test_bash_adapters_exclude_the_index_from_the_gate_clear() -> None:
    """Both bash adapters must NOT clear the gate on a Read of the vault TOC /
    scaffolding (the index.md dodge); assert the basename exclusion is present and
    stays in sync with the canonical set."""
    files = importlib.resources.files("omind")
    expected = 'index.md|MEMORY.md|"Memory Template.md"'
    assert {Path(n).name for n in (paths.INDEX_FILENAME, paths.MEMORY_TEMPLATE_FILENAME)} | {
        "MEMORY.md"
    } == set(paths.NON_CONSULT_FILENAMES)
    for name in ("omi-guard.sh", "omi-guard-hermes.sh"):
        sh = files.joinpath(name).read_text(encoding="utf-8")
        assert expected in sh, f"{name} must exclude the index/scaffolding from the gate clear"


def test_widened_destructive_rules_close_red_team_gaps() -> None:
    """#B1: the bypasses the red-team found are now denied, while reads still pass."""
    guard.mark_consulted("b1")
    blocked = [
        "gh api repos/acme/widget -X DELETE",  # path-before-method reorder
        "curl -X DELETE https://api.github.com/repos/acme/widget",  # curl, not gh
        "pkexec rm -rf /tmp/x",
        "doas reboot",
        "su -c 'rm -rf /tmp/x' root",
    ]
    for cmd in blocked:
        assert not guard.decide({"command": cmd, "session": "b1"}).allow, cmd
    # a GitHub API read (no DELETE) is not a destructive rule -> allowed
    assert guard.decide({"command": "gh api repos/acme/widget/pulls", "session": "b1"}).allow
    # privesc still has the deliberate opt-in (a real leading assignment, #2)
    assert guard.decide(
        {"command": "OMI_SUDO_OK=1 pkexec systemctl restart x", "session": "b1"}
    ).allow
    guard.clear_gate("b1")


def test_freshness_accepts_dash_c_and_compound_read_forms() -> None:
    """#449: `git -C <repo> fetch` and `git fetch && git status` establish freshness."""
    guard.record_consult("fresh2", kind="read", target=guard.GIT_RULES_NOTE, relevant=True)
    repo = guard._repo_root_for_action({"tool": "Bash", "command": "git status"})
    assert repo is not None
    for cmd in (
        f"git -C {repo} fetch --all --prune",
        "git fetch --all --prune && git status -sb",
    ):
        guard.clear_gate("fresh2")
        guard.record_consult("fresh2", kind="read", target=guard.GIT_RULES_NOTE, relevant=True)
        v = guard.decide({"tool": "Bash", "command": cmd, "session": "fresh2"})
        assert v.allow, cmd
    # A fetch chained with a non-read command is NOT a pure freshness command,
    # so it must not establish freshness (a piggybacked write can't ride in).
    assert guard._is_freshness_command("git fetch --all --prune")
    assert guard._is_freshness_command("git -C /r fetch && git status")
    assert not guard._is_freshness_command("git fetch --all && rm -rf build")
    assert not guard._is_freshness_command("git fetch | tee /etc/x")
    guard.clear_gate("fresh2")


def test_freshness_message_recommends_standalone_fetch_then_separate_write() -> None:
    """Regression for the self-contradictory guidance: the old block message told
    the agent to chain ``git fetch … && git commit …``, which can NEVER satisfy the
    check — a command that also contains the write is not a pure freshness command,
    so it records nothing and the write stays blocked. The message must recommend a
    standalone fetch, then a separate write, and the behaviour it describes must
    actually hold."""
    msg = guard.GIT_FRESHNESS_MESSAGE
    assert re.search(r"separate", msg, re.IGNORECASE)
    assert re.search(r"own command", msg, re.IGNORECASE)
    # The worked examples are two SEPARATE command lines: the fetch example carries
    # no commit and the commit example carries no fetch (never chained as the remedy).
    example_lines = [ln.strip() for ln in msg.splitlines() if ln.strip().startswith("git ")]
    fetch_examples = [ln for ln in example_lines if "fetch" in ln]
    commit_examples = [ln for ln in example_lines if "commit" in ln]
    assert fetch_examples and commit_examples
    assert all("commit" not in ln for ln in fetch_examples)
    assert all("fetch" not in ln for ln in commit_examples)

    # The behaviour the message now describes: chaining the write does NOT establish
    # freshness, so it stays blocked…
    guard.clear_gate("freshmsg")
    guard.record_consult("freshmsg", kind="read", target=guard.GIT_RULES_NOTE, relevant=True)
    chained = guard.decide(
        {
            "tool": "Bash",
            "command": "git fetch --all --prune && git commit -am x",
            "session": "freshmsg",
        }
    )
    assert not chained.allow
    assert chained.rule_id == "repo-work-fresh-base"

    # …but a standalone fetch establishes freshness for the SEPARATE next write.
    guard.clear_gate("freshmsg")
    guard.record_consult("freshmsg", kind="read", target=guard.GIT_RULES_NOTE, relevant=True)
    assert guard.decide(
        {"tool": "Bash", "command": "git fetch --all --prune", "session": "freshmsg"}
    ).allow
    assert guard.decide(
        {"tool": "Bash", "command": "git commit -am x", "session": "freshmsg"}
    ).allow
    guard.clear_gate("freshmsg")


def test_stderr_redirect_is_not_a_side_effect_under_a_capability_question() -> None:
    """#498: `pytest 2>&1 | tail` must not be read as a file-writing side effect."""
    guard.mark_consulted("redir")
    v = guard.decide(
        {
            "tool": "Bash",
            "command": "pytest -q 2>&1 | tail",
            "prompt": "Could you check why the tests fail?",
            "session": "redir",
        }
    )
    # Not blocked as an unauthorized capability side-effect (it may still need the
    # repo note/freshness, but never `capability-question-explicit-auth`).
    assert v.rule_id != "capability-question-explicit-auth"
    guard.clear_gate("redir")


def test_project_local_dotclaude_is_not_a_global_config_mutation(tmp_path: Path) -> None:
    """#453: editing <repo>/.claude/settings.json is project config, not global."""
    project = tmp_path / "myrepo" / ".claude"
    project.mkdir(parents=True)
    assert not guard._is_global_config_path(str(project / "settings.json"))
    # The real home-anchored global still is.
    assert guard._is_global_config_path(str(Path.home() / ".claude" / "settings.json"))


def _mk_repo(tmp_path: Path, name: str) -> Path:
    """A minimal repo-shaped dir (`.git` present) — the root walk needs no real git."""
    repo = tmp_path / name
    (repo / ".git").mkdir(parents=True)
    return repo.resolve()


def test_dash_c_fetch_attributes_freshness_to_the_target_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#147: `git -C <B> fetch` from an A-rooted cwd freshens B — not the cwd repo."""
    repo_a = _mk_repo(tmp_path, "a")
    repo_b = _mk_repo(tmp_path, "b")
    monkeypatch.chdir(repo_a)
    guard.clear_gate("dashc")
    guard.record_consult("dashc", kind="read", target=guard.GIT_RULES_NOTE, relevant=True)
    fetch = guard.decide(
        {"tool": "Bash", "command": f"git -C {repo_b} fetch --all --prune", "session": "dashc"}
    )
    assert fetch.allow
    assert str(repo_b) in guard._fresh_repos("dashc")
    assert str(repo_a) not in guard._fresh_repos("dashc")
    # A commit in B (repo resolved from the -C path) now passes the freshness check
    # — freshness gates commits only, so a commit is the probe that exercises it...
    commit_b = guard.decide(
        {"tool": "Bash", "command": f"git -C {repo_b} commit -am x", "session": "dashc"}
    )
    assert commit_b.allow, commit_b.reason
    # ...while A — never fetched — is still stale.
    stale = guard.decide(
        {"tool": "Bash", "command": f"git -C {repo_a} commit -am x", "session": "dashc"}
    )
    assert not stale.allow
    assert stale.rule_id == "repo-work-fresh-base"
    guard.clear_gate("dashc")


def test_dash_c_parsing_edge_cases_fall_back_to_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#147: relative/repeated `-C` resolve like git's own; anything unparseable
    (or non-git `-C` like make/tar) attributes to the cwd repo, never crashes."""
    repo_a = _mk_repo(tmp_path, "a")
    repo_b = _mk_repo(tmp_path, "b")
    monkeypatch.chdir(repo_a)
    # A `-C` target that is itself no repo attributes to its ENCLOSING repo when
    # one exists (that is where git would run — e.g. a stray /tmp/.git above
    # pytest's tmp dir), and only falls back to the cwd repo when there is none.
    plain = tmp_path / "plain"
    plain.mkdir()
    enclosing = next((p for p in (plain, *plain.parents) if (p / ".git").exists()), None)
    for command, expected in [
        (f"git -C {repo_b} fetch", repo_b),  # absolute
        ("git -C ../b fetch", repo_b),  # relative to cwd
        (f"git -C {tmp_path} -C b fetch", repo_b),  # repeated -C chains cumulatively
        (f"git -c user.name=x -C {repo_b} fetch", repo_b),  # -c skipped, -C honored
        (f"git -C {plain} fetch", enclosing or repo_a),  # -C at a non-repo
        ('git -C "unclosed fetch', repo_a),  # unbalanced quote -> cwd, no crash
        (f"make -C {repo_b} test", repo_a),  # not git: -C untrusted
        (f"tar -C {repo_b} -xf x.tar", repo_a),
        ("git fetch --all --prune", repo_a),  # no -C -> cwd, as before
    ]:
        got = guard._repo_root_for_action({"tool": "Bash", "command": command})
        assert got == expected, command
    # Record and check sides resolve the SAME string for the same repo (#147) —
    # the marker is an exact string match, so this equality is load-bearing.
    fetch_side = guard._repo_root_for_action(
        {"tool": "Bash", "command": f"git -C {repo_b} fetch"}
    )
    commit_side = guard._repo_root_for_action(
        {"tool": "Bash", "command": f"git -C {repo_b} commit -m x"}
    )
    assert str(fetch_side) == str(commit_side) == str(repo_b)


def test_dash_c_git_writes_are_classified_and_checked_against_the_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#147: `git -C <B> commit` is repo work (the old verb regex required the
    verb right after `git`, so a `-C` form bypassed the checks entirely) and is
    checked against B's freshness, not the cwd's."""
    repo_a = _mk_repo(tmp_path, "a")
    repo_b = _mk_repo(tmp_path, "b")
    monkeypatch.chdir(repo_a)
    assert guard._is_repo_sensitive_action(
        {"tool": "Bash", "command": f"git -C {repo_b} commit -m x"}
    )
    assert guard._is_side_effect_action(
        {"tool": "Bash", "command": f"git -C {repo_b} push codeberg main"}
    )
    guard.clear_gate("dashcw")
    guard.record_consult("dashcw", kind="read", target=guard.GIT_RULES_NOTE, relevant=True)
    guard._record_git_freshness("dashcw", repo_a, "git fetch --all --prune")
    stale = guard.decide(
        {"tool": "Bash", "command": f"git -C {repo_b} commit -m x", "session": "dashcw"}
    )
    assert not stale.allow
    assert stale.rule_id == "repo-work-fresh-base"
    guard._record_git_freshness("dashcw", repo_b, f"git -C {repo_b} fetch")
    fresh = guard.decide(
        {"tool": "Bash", "command": f"git -C {repo_b} commit -m x", "session": "dashcw"}
    )
    assert fresh.allow, fresh.reason
    guard.clear_gate("dashcw")


def test_freshness_marker_holds_multiple_repos_and_reads_legacy_shape(tmp_path: Path) -> None:
    """#147: fetching B must not evict A's freshness within the turn; the
    pre-3.8.3 single-slot payload still reads (mid-upgrade session)."""
    repo_a = _mk_repo(tmp_path, "a")
    repo_b = _mk_repo(tmp_path, "b")
    guard._record_git_freshness("multi", repo_a, "git fetch --all --prune")
    guard._record_git_freshness("multi", repo_b, f"git -C {repo_b} fetch")
    assert guard._git_fresh_for_repo("multi", repo_a)
    assert guard._git_fresh_for_repo("multi", repo_b)
    guard._git_fresh_path("multi").write_text(
        json.dumps({"repo": str(repo_a), "command": "git fetch", "ts": 1}), encoding="utf-8"
    )
    assert guard._git_fresh_for_repo("multi", repo_a)
    assert not guard._git_fresh_for_repo("multi", repo_b)


def test_repo_block_records_the_demanded_note_and_turn_start_clears_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#148: the git-rules block names the note it demands (so the verifier can
    credit the obeying read as relevant); begin_turn clears the marker."""
    repo = _mk_repo(tmp_path, "r")
    monkeypatch.chdir(repo)
    guard.begin_turn("dmd0", "some task")
    guard.mark_consulted("dmd0")
    blocked = guard.decide({"tool": "Bash", "command": "pytest", "session": "dmd0"})
    assert blocked.rule_id == "repo-work-read-git-rules"
    assert guard.demanded_note("dmd0") == guard.GIT_RULES_NOTE
    guard.begin_turn("dmd0", "next turn")
    assert guard.demanded_note("dmd0") == ""


def test_bash_adapters_treat_vault_writes_as_ordinary_actions() -> None:
    """#148: create/edit/delete/restore-note must not be consult-marked in either
    bash adapter (they fall through to the generic gated delegation), and the
    turn reset must clear the demanded-note marker like the other per-turn files."""
    files = importlib.resources.files("omind")
    writes = (
        "mcp__omi__create-note | mcp__omi__edit-note | "
        "mcp__omi__delete-note | mcp__omi__restore-note"
    )
    for name in ("omi-guard.sh", "omi-guard-hermes.sh"):
        sh = files.joinpath(name).read_text(encoding="utf-8")
        assert writes in sh, f"{name} must not treat vault writes as consults"
    reset = files.joinpath("omi-gate-reset.sh").read_text(encoding="utf-8")
    assert "demanded-$sid.txt" in reset


@pytest.mark.skipif(not _HOOK_TESTABLE, reason="omi-guard.sh is a POSIX bash+jq adapter")
def test_hook_gates_vault_writes_but_not_reads(tmp_path: Path) -> None:
    """#148 end-to-end at the hook: a vault WRITE delegates to the core as an
    ordinary action (a core deny reaches the host), while a read-note consult
    stays the always-allowed clear-path."""
    hook = _render_hook(tmp_path, str(_fake_omind(tmp_path, 2)))
    write_event = {
        "tool_name": "mcp__omi__edit-note",
        "session_id": "h",
        "tool_input": {"name": "Some Note"},
    }
    read_event = {
        "tool_name": "mcp__omi__read-note",
        "session_id": "h",
        "tool_input": {"name": "Some Note"},
    }
    assert _run_hook(hook, write_event) == 2  # gated like any ordinary action
    assert _run_hook(hook, read_event) == 0  # the consult clear-path, unchanged


def test_inert_commands_skip_the_consult_gate_without_satisfying_it() -> None:
    """#147: a provably-inert inspection command runs unconsulted; nothing can
    piggyback on one, and it does NOT clear the gate for what follows."""
    guard.clear_gate("inert")
    for cmd in (
        "pwd",
        "whoami",
        "id",
        "id -u",
        "date",
        "date +%s",
        "uname -a",
        "hostname",
        "which git",
        "command -v jq",
        "git --version",
        "true",
        "false",
    ):
        assert guard.decide({"tool": "Bash", "command": cmd, "session": "inert"}).allow, cmd
    # The exemption does not set the sentinel: a real action still needs a consult.
    assert not guard.decide({"tool": "Bash", "command": "ls", "session": "inert"}).allow
    for cmd in (
        "pwd && rm -rf build",  # no passengers
        "pwd > /tmp/x",  # no redirects
        "which $(rm x)",  # no substitution
        "date -s 12:00",  # sets the clock: only read forms are inert
        "hostname evil",  # renames the host: bare form only
        "echo hi",  # arbitrary arguments: excluded by design
        "cat /etc/hostname",  # reads a file: stays gated
        "uname -a; curl example.com",  # no chains
    ):
        assert not guard.decide({"tool": "Bash", "command": cmd, "session": "inert"}).allow, cmd
    guard.clear_gate("inert")


def test_bad_learned_rule_does_not_brick_the_guard() -> None:
    """#668: a malformed regex reaching decide() must be skipped, not crash it."""
    from omind import policy

    # A rule object whose compiled() raises (bypassing the loader's validation).
    class _BadRule(policy.Rule):
        def compiled(self):  # type: ignore[override]
            raise __import__("re").error("boom")

    bad = _BadRule(id="bad", pattern="x", message="m", severity=policy.SEVERITY_HARD)
    import unittest.mock as mock

    guard.mark_consulted("brick")
    with mock.patch.object(policy, "load_policy", return_value=[bad]):
        # Must not raise; the bad rule is skipped and the action is decided.
        v = guard.decide({"tool": "Bash", "command": "echo hi", "session": "brick"})
    assert v.allow
    guard.clear_gate("brick")


def test_opt_in_env_prefix_must_be_at_command_position() -> None:
    """#517: `env TOKEN` forged inside a string must not satisfy the opt-in."""
    assert guard._opt_in_satisfied("OMI_SUDO_OK=1", "OMI_SUDO_OK=1 sudo x")
    assert guard._opt_in_satisfied("OMI_SUDO_OK=1", "env OMI_SUDO_OK=1 sudo x")
    assert not guard._opt_in_satisfied("OMI_SUDO_OK=1", 'echo "use env OMI_SUDO_OK=1" && sudo x')


def test_negated_verb_is_not_global_authorization() -> None:
    """#463: 'don't change anything' must not authorize a global-config mutation."""
    assert guard._has_global_auth("please update the global config")
    assert guard._has_global_auth("fix the hook please")  # expanded verb set
    assert not guard._has_global_auth("don't change anything yet")


def test_guard_status_flags_agent_writable_config(capsys: pytest.CaptureFixture[str]) -> None:
    """#B2: status surfaces the kill-shot surface when the guard's own config is
    writable by the agent (here, under the test's isolated HOME)."""
    from omind import provision

    hook = provision._omi_guard_dest()
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text("#!/bin/sh\n", encoding="utf-8")
    assert guard.run_guard("status") == 0
    out = capsys.readouterr().out
    assert "self-protection" in out and "AGENT-WRITABLE" in out
