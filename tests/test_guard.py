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
import time
from collections.abc import Callable
from pathlib import Path

import pytest
from conftest import hard_time_limit as _hard_time_limit

from omind import compliance, guard, paths, policy

#: The omi-guard.sh adapter is a POSIX bash+jq deployment artifact (Claude Code on
#: Linux/macOS). Its subprocess tests only make sense where a real bash + jq run it —
#: NOT on Windows, where Git Bash's CRLF/path quirks make the same script exit 1 and
#: where the hook isn't the deployed form anyway.
_HOOK_TESTABLE = (
    sys.platform != "win32" and shutil.which("bash") is not None and shutil.which("jq") is not None
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
    assert guard.decide({"command": "fleet-sudo systemctl reload nginx", "session": "sSudo"}).allow
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
    fresh = guard.decide({"tool": "Bash", "command": "git fetch --all --prune", "session": "repo"})
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
    assert (
        guard._repo_root_for_action({"tool": "Write", "file_path": str(target), "session": session})
        is None
    )
    allowed = guard.decide({"tool": "Write", "file_path": str(target), "session": session})
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
    assert guard.decide({"tool": "Edit", "file_path": str(repo / "x.py"), "session": session}).allow
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


def _token_note(omi: Path) -> None:
    from omind.store import NoteFields, OmiStore

    OmiStore(omi).create_note(
        NoteFields(
            title="Token Usage Strategy",
            summary="Keep OMI token usage bounded.",
            details="Use compact recall and avoid duplicate note representations.",
        )
    )


def test_turn_preflight_names_the_match_without_injecting_it(tmp_path: Path) -> None:
    # #321: the default is a pull hint — the title, and an invitation to fetch
    # the body. The note text itself must NOT land in the context window.
    from omind import ai_usage

    omi = tmp_path / "OMI"
    omi.mkdir()
    _token_note(omi)
    event = {"session_id": "preflight-1", "prompt": "reduce OMI token usage"}
    context = guard.preflight_turn(event, omi)
    assert "[[Token Usage Strategy.md]]" in context
    assert "compact recall" not in context  # body stayed in the vault
    assert "recall-note" in context
    assert "Silence is not an override" not in context  # framing softened
    assert len(context) <= guard.PREFLIGHT_HINT_CHARS
    assert guard.consulted_this_turn("preflight-1")
    usage = ai_usage.read_events(omi)
    assert usage[-1]["operation"] == "recall"


def test_turn_preflight_inject_mode_recalls_full_memory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omind import ai_usage

    monkeypatch.setenv(guard.PREFLIGHT_MODE_ENV, "inject")
    omi = tmp_path / "OMI"
    omi.mkdir()
    _token_note(omi)
    event = {"session_id": "preflight-1", "prompt": "reduce OMI token usage"}
    context = guard.preflight_turn(event, omi)
    # #416: named by the stored filename recall-note opens as-is.
    assert "[[Token Usage Strategy.md]]" in context
    assert "compact recall" in context
    assert guard.consulted_this_turn("preflight-1")
    usage = ai_usage.read_events(omi)
    assert usage[-1]["operation"] == "recall"

    repeated = guard.preflight_turn(event, omi)
    assert "already injected earlier this session" in repeated
    assert "Keep OMI token usage bounded." in repeated
    assert "compact recall" not in repeated


def test_turn_preflight_off_mode_says_nothing_and_clears(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(guard.PREFLIGHT_MODE_ENV, "off")
    omi = tmp_path / "OMI"
    omi.mkdir()
    _token_note(omi)
    context = guard.preflight_turn(
        {"session_id": "preflight-off", "prompt": "reduce OMI token usage"}, omi
    )
    assert "[[" not in context
    assert guard.PREFLIGHT_MODE_ENV in context
    assert guard.consulted_this_turn("preflight-off")


def test_turn_preflight_skips_a_note_that_announces_its_own_correction(
    tmp_path: Path,
) -> None:
    # #321 fix 3: shipping a retraction stamped "the memory governs" is a
    # confabulation generator. Name it, do not inject it.
    from omind.store import NoteFields, OmiStore

    omi = tmp_path / "OMI"
    omi.mkdir()
    OmiStore(omi).create_note(
        NoteFields(
            title="Token Usage Strategy",
            summary="Keep OMI token usage bounded.",
            details="SUPERSEDED 2026-09-09 — the budget below is no longer true.",
        )
    )
    context = guard.preflight_turn(
        {"session_id": "preflight-stale", "prompt": "reduce OMI token usage"}, omi
    )
    assert "supersession" in context
    assert "no longer true" not in context
    assert guard.consulted_this_turn("preflight-stale")
    events = compliance.read_events()
    assert events[-1]["rule_id"] == guard.GATE_STALE_NOTE_RULE
    assert events[-1]["outcome"] == "auto-clear"


def test_turn_preflight_tapers_when_the_session_budget_is_spent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # #321 fix 6: budget the session, not just the turn.
    from omind import ai_usage

    monkeypatch.setenv(guard.PREFLIGHT_MODE_ENV, "inject")
    omi = tmp_path / "OMI"
    omi.mkdir()
    _token_note(omi)
    ai_usage.record_context(
        omi, "recall", guard.SESSION_INJECTION_BUDGET_CHARS, session_id="preflight-fat"
    )
    context = guard.preflight_turn(
        {"session_id": "preflight-fat", "prompt": "reduce OMI token usage"}, omi
    )
    assert "over budget" in context
    assert "compact recall" not in context
    assert guard.consulted_this_turn("preflight-fat")
    events = compliance.read_events()
    assert events[-1]["rule_id"] == guard.GATE_BUDGET_RULE


def test_turn_preflight_still_injects_hard_rule_notes_in_full(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The carve-out: a note compiling an omind-rule block is enforcement, not
    # recall. Hint mode, a spent budget and a stale marker must not silence it.
    from omind import ai_usage
    from omind.store import NoteFields, OmiStore

    omi = tmp_path / "OMI"
    omi.mkdir()
    OmiStore(omi).create_note(
        NoteFields(
            title="Token Usage Strategy",
            summary="Keep OMI token usage bounded.",
            details=(
                "SUPERSEDED in part, but the rule still binds.\n\n"
                "```omind-rule\n"
                "id: no-token-bonfire\n"
                "tool: Bash\n"
                "match: \"*token bonfire*\"\n"
                "action: deny\n"
                "message: \"no\"\n"
                "```"
            ),
        )
    )
    ai_usage.record_context(
        omi, "recall", guard.SESSION_INJECTION_BUDGET_CHARS, session_id="preflight-rule"
    )
    context = guard.preflight_turn(
        {"session_id": "preflight-rule", "prompt": "reduce OMI token usage"}, omi
    )
    assert "hard operational rule" in context
    assert "no-token-bonfire" in context


def test_turn_preflight_strips_action_items_from_an_injection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # #321 fix 4: an unchecked checkbox from another day is not this turn's job.
    from omind.store import NoteFields, OmiStore

    monkeypatch.setenv(guard.PREFLIGHT_MODE_ENV, "inject")
    omi = tmp_path / "OMI"
    omi.mkdir()
    OmiStore(omi).create_note(
        NoteFields(
            title="Token Usage Strategy",
            summary="Keep OMI token usage bounded.",
            details=(
                "Use compact recall.\n\n"
                "- [ ] Install ROCm on telesto\n"
                "- [x] Ship the compact-recall change\n"
            ),
        )
    )
    context = guard.preflight_turn(
        {"session_id": "preflight-todo", "prompt": "reduce OMI token usage"}, omi
    )
    assert "Install ROCm" not in context
    assert "Use compact recall." in context


def test_turn_preflight_without_match_auto_clears_gate(tmp_path: Path) -> None:
    omi = tmp_path / "OMI"
    omi.mkdir()
    context = guard.preflight_turn(
        {"session_id": "preflight-none", "prompt": "unmatched subject"},
        omi,
    )
    assert "found nothing relevant" in context
    assert guard.MISS_STRICT_ENV in context
    # Auto-cleared: the next tool call is not blocked demanding an arbitrary read.
    assert guard.consulted_this_turn("preflight-none")
    events = compliance.read_events()
    assert events[-1]["rule_id"] == guard.GATE_NO_MATCH_RULE
    assert events[-1]["outcome"] == "auto-clear"


def test_turn_preflight_without_match_stays_strict_when_opted_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(guard.MISS_STRICT_ENV, "1")
    omi = tmp_path / "OMI"
    omi.mkdir()
    context = guard.preflight_turn(
        {"session_id": "preflight-strict", "prompt": "unmatched subject"},
        omi,
    )
    assert "search-vault" in context and "recall-note" in context
    assert not guard.consulted_this_turn("preflight-strict")


def test_turn_preflight_with_empty_task_stays_strict(tmp_path: Path) -> None:
    # No captured task means the vault was never searched — a miss can't be
    # distinguished from "we didn't look," so this must not auto-clear.
    omi = tmp_path / "OMI"
    omi.mkdir()
    context = guard.preflight_turn({"session_id": "preflight-empty", "prompt": ""}, omi)
    assert "search-vault" in context and "recall-note" in context
    assert not guard.consulted_this_turn("preflight-empty")


def test_turn_preflight_weak_match_auto_clears_without_injecting(
    tmp_path: Path,
) -> None:
    # A single shared term (here "retry") ranks the note as the best candidate,
    # but one word is not evidence of relevance — no injection, gate cleared.
    from omind.store import NoteFields, OmiStore

    omi = tmp_path / "OMI"
    omi.mkdir()
    OmiStore(omi).create_note(
        NoteFields(
            title="Ghidra decompiler retry budget",
            summary="Retry the decompile with a doubled budget.",
            details="GUI-only behaviour; headless precheck cannot drive it.",
        )
    )
    context = guard.preflight_turn({"session_id": "preflight-weak", "prompt": "retry"}, omi)
    assert "weak memory match" in context
    assert "[[" not in context  # nothing injected
    assert guard.consulted_this_turn("preflight-weak")
    events = compliance.read_events()
    assert events[-1]["rule_id"] == guard.GATE_WEAK_MATCH_RULE
    assert events[-1]["outcome"] == "auto-clear"


def test_turn_preflight_weak_match_stays_strict_when_opted_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omind.store import NoteFields, OmiStore

    monkeypatch.setenv(guard.MISS_STRICT_ENV, "1")
    omi = tmp_path / "OMI"
    omi.mkdir()
    OmiStore(omi).create_note(
        NoteFields(title="Ghidra decompiler retry budget", summary="Retry logic.")
    )
    context = guard.preflight_turn({"session_id": "preflight-weak-strict", "prompt": "retry"}, omi)
    assert "search-vault" in context and "recall-note" in context
    assert not guard.consulted_this_turn("preflight-weak-strict")


def test_turn_preflight_weak_match_filter_disabled_by_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omind import retrieve
    from omind.store import NoteFields, OmiStore

    monkeypatch.setenv(retrieve.PREFLIGHT_MIN_TERMS_ENV, "0")
    omi = tmp_path / "OMI"
    omi.mkdir()
    OmiStore(omi).create_note(
        NoteFields(title="Ghidra decompiler retry budget", summary="Retry logic.")
    )
    context = guard.preflight_turn({"session_id": "preflight-weak-off", "prompt": "retry"}, omi)
    assert "[[Ghidra decompiler retry budget.md]]" in context  # legacy behavior


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
    """A minimal repo-shaped dir with the HEAD marker every real worktree has."""
    repo = tmp_path / name
    (repo / ".git").mkdir(parents=True)
    (repo / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
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
    windows_target = r"C:\Users\runneradmin\source\repo"
    assert str(guard._git_dash_c_path(f"git -C {windows_target} fetch")) == windows_target
    assert (
        str(guard._git_dash_c_path(f'git -C "{windows_target} with spaces" fetch'))
        == f"{windows_target} with spaces"
    )
    # A `-C` target that is itself no repo attributes to its ENCLOSING repo when
    # one exists (that is where git would run — e.g. a stray /tmp/.git above
    # pytest's tmp dir), and only falls back to the cwd repo when there is none.
    plain = tmp_path / "plain"
    plain.mkdir()
    enclosing = next(
        (
            p
            for p in (plain, *plain.parents)
            if (p / ".git").is_file() or (p / ".git" / "HEAD").is_file()
        ),
        None,
    )
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
    fetch_side = guard._repo_root_for_action({"tool": "Bash", "command": f"git -C {repo_b} fetch"})
    commit_side = guard._repo_root_for_action(
        {"tool": "Bash", "command": f"git -C {repo_b} commit -m x"}
    )
    assert str(fetch_side) == str(commit_side) == str(repo_b)


def test_repo_resolution_follows_cd_and_event_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#394: the repo a command acts on comes from `cd <dir> &&` / `(cd x; …)`
    and the hook event's cwd — never from a `cd` inside an ssh payload, and
    plain commands keep the cwd behaviour."""
    repo_a = _mk_repo(tmp_path, "a")
    repo_b = _mk_repo(tmp_path, "b")
    monkeypatch.chdir(repo_a)
    for command, expected in [
        (f"cd {repo_b} && git commit -m x", repo_b),
        ("cd ../b && git push origin main", repo_b),  # relative to cwd
        (f"cd {tmp_path} && cd b && git push", repo_b),  # chained cd
        (f"cd {tmp_path} && git -C b push", repo_b),  # cd then -C
        (f"(cd {repo_b}; git commit -m x)", repo_b),  # subshell
        (f"(cd {repo_b} && make) && git commit -m x", repo_a),  # subshell cd ends
        (f'cd "{repo_b}" && git status', repo_b),  # quoted dir
        (f"ssh host 'cd {repo_b} && git push origin main'", repo_a),  # remote payload
        (f"echo 'cd {repo_b} && git push'", repo_a),  # quoted data
        ("cd $HOME && git push", repo_a),  # unparseable -> cwd
        ("cd - && git push", repo_a),
        ("git push origin main", repo_a),  # plain command -> cwd
    ]:
        got = guard._repo_root_for_action({"tool": "Bash", "command": command})
        assert got == expected, command
    # The adapter-supplied event cwd wins over this process's cwd...
    got = guard._repo_root_for_action(
        {"tool": "Bash", "command": "git commit -m x", "cwd": str(repo_b)}
    )
    assert got == repo_b
    got = guard._repo_root_for_action(
        {"tool": "Bash", "command": "git -C ../a fetch", "cwd": str(repo_b)}
    )
    assert got == repo_a  # relative -C resolves against the event cwd
    # ...and a missing / bogus one falls back to it (fail open).
    for bogus in ("", str(tmp_path / "nope"), 42):
        got = guard._repo_root_for_action({"tool": "Bash", "command": "git push", "cwd": bogus})
        assert got == repo_a, bogus


def test_normalize_action_carries_event_cwd() -> None:
    """#394: the adapter passes the hook event's cwd through to the core."""
    from omind import adapters

    action = adapters.normalize_action(
        {"tool_name": "Bash", "tool_input": {"command": "git push"}, "cwd": "/w/t"}
    )
    assert action["cwd"] == "/w/t"


def _capturing_omind(tmp_path: Path) -> tuple[Path, Path]:
    """A fake omind that saves the JSON it is piped and allows."""
    capture = tmp_path / "captured.json"
    fake = tmp_path / "capture-omind"
    fake.write_text(f"#!/usr/bin/env bash\ncat > '{capture}'\nexit 0\n", encoding="utf-8")
    fake.chmod(0o755)
    return fake, capture


@pytest.mark.skipif(not _HOOK_TESTABLE, reason="omi-guard.sh is a POSIX bash+jq adapter")
def test_hook_forwards_the_event_cwd_to_the_core(tmp_path: Path) -> None:
    """#394 review: omi-guard.sh passes the event's `.cwd` (the agent's shell
    cwd) on the Bash path, so the core resolves the repo from it."""
    fake, capture = _capturing_omind(tmp_path)
    hook = _render_hook(tmp_path, str(fake))
    event = {**_BASH_EVENT, "cwd": "/w/tree"}
    assert _run_hook(hook, event) == 0
    assert json.loads(capture.read_text(encoding="utf-8"))["cwd"] == "/w/tree"


@pytest.mark.skipif(not _HOOK_TESTABLE, reason="omi-guard-hermes.sh is a POSIX bash+jq adapter")
def test_hermes_hook_forwards_the_event_cwd_to_the_core(tmp_path: Path) -> None:
    """Hermes' shell-hook payload carries `cwd`; its adapter forwards it too."""
    fake, capture = _capturing_omind(tmp_path)
    src = importlib.resources.files("omind").joinpath("omi-guard-hermes.sh")
    text = src.read_text(encoding="utf-8").replace("__OMIND_BIN__", str(fake))
    hook = tmp_path / "omi-guard-hermes.sh"
    hook.write_text(text.replace("__OMI_DIR__", str(tmp_path / "OMI")), encoding="utf-8")
    event = {
        "hook_event_name": "pre_tool_call",
        "tool_name": "terminal",
        "tool_input": {"command": "git status"},
        "session_id": "h",
        "cwd": "/w/tree",
    }
    subprocess.run(["bash", str(hook)], input=json.dumps(event), text=True, check=True)
    assert json.loads(capture.read_text(encoding="utf-8"))["cwd"] == "/w/tree"


def test_shell_sites_track_cd_pushd_popd_and_subshells(tmp_path: Path) -> None:
    """#394 review: each simple command carries the directory it runs in.
    `popd` undoes `pushd`; a subshell's move ends with it; a cd piped or
    backgrounded with a single `|`/`&` moves nothing; wrappers are skipped;
    an ssh remote command (quoted or not) is blanked from the local text."""

    # Real absolute directories: on Windows `/a` is drive-relative, not
    # absolute, so after an unknown `cd $X` it correctly resolves to None.
    a_dir, b_dir = tmp_path / "a", tmp_path / "b"
    a, b = a_dir.as_posix(), b_dir.as_posix()
    here = Path(".")

    def where(command: str) -> list[tuple[str, Path | None]]:
        sites, _cwd, _dirs, _local = guard._shell_sites(command)
        return [
            (s.program, None if s.cwd is None else Path(s.cwd)) for s in sites if s.program == "git"
        ]

    assert where(f"pushd {a} && git status && popd && git push") == [("git", a_dir), ("git", here)]
    assert where(f"pushd {a} && pushd {b} && popd && git push") == [("git", a_dir)]
    assert where("popd && git push") == [("git", here)]  # empty stack: popd fails, no move
    assert where(f"(cd {a} && git status) && git push") == [("git", a_dir), ("git", here)]
    assert where(f"echo $(cd {a} && git rev-parse HEAD) && git push") == [
        ("git", a_dir),
        ("git", here),
    ]
    assert where(f"cd {a} | true; git push") == [("git", here)]
    assert where(f"cd {a} & git push") == [("git", here)]
    assert where(f"cd {a} || exit 1; git push") == [("git", a_dir)]  # `||` read as `&&`
    assert where("cd $HOME && git push") == [("git", None)]
    assert where(f"cd {a} && cd $X && cd {b} && git push") == [("git", b_dir)]
    assert where(f"sudo -u bob git -C {a} push") == [("git", a_dir)]
    assert where("env X=1 timeout 60 git push") == [("git", here)]
    assert where(f"git --work-tree={a} --git-dir {b}/.git push") == [("git", a_dir)]
    assert where(f"bash -c 'cd {a} && git push' && git status") == [("git", a_dir), ("git", here)]
    assert where(f"eval 'cd {a}' && git push") == [("git", a_dir)]  # eval shares the shell
    assert where("ssh -p 22 host git push && git status") == [("git", here)]
    _sites, _cwd, _dirs, local = guard._shell_sites("ssh -i k h git push origin main; ls")
    assert local == "ssh -i k h" + " " * len(" git push origin main") + "; ls"
    _sites, _cwd, _dirs, local = guard._shell_sites("bash -c \"ssh h 'git push'\"")
    assert "git push" not in local


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
    # #392: the full/incomplete read record is per turn too.
    assert "incomplete-$sid.txt" in reset


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


def test_would_you_is_a_polite_imperative_not_a_capability_question() -> None:
    """ "Would you <verb> ...?" authorizes; "Can you ...?" still does not."""
    allowed = guard.decide(
        {
            "tool": "Bash",
            "command": "gh issue create --title x",
            "prompt": "Would you please add an issue for that?",
            "session": "wouldq",
        }
    )
    assert allowed.rule_id != "capability-question-explicit-auth"

    blocked = guard.decide(
        {
            "tool": "Bash",
            "command": "gh issue create --title x",
            "prompt": "Can you please add an issue for that?",
            "session": "wouldq",
        }
    )
    assert not blocked.allow
    assert blocked.rule_id == "capability-question-explicit-auth"
    guard.clear_gate("wouldq")


def test_will_you_is_a_polite_imperative_not_a_capability_question() -> None:
    """"Will you <verb> ...?" authorizes; "Can you ...?" still does not.

    Regression: `will` used to sit in the interrogatory set, so "will you please
    implement the fixes?" blocked every side effect in the turn.
    """
    allowed = guard.decide(
        {
            "tool": "Bash",
            "command": "gh issue create --title x",
            "prompt": "Will you please add an issue for that?",
            "session": "willq",
        }
    )
    assert allowed.rule_id != "capability-question-explicit-auth"

    blocked = guard.decide(
        {
            "tool": "Bash",
            "command": "gh issue create --title x",
            "prompt": "Can you please add an issue for that?",
            "session": "willq",
        }
    )
    assert not blocked.allow
    assert blocked.rule_id == "capability-question-explicit-auth"
    guard.clear_gate("willq")


def test_will_you_without_an_authorizing_verb_still_blocks_side_effects() -> None:
    """Dropping "will" from the interrogatory set must not blanket-authorize."""
    blocked = guard.decide(
        {
            "tool": "Bash",
            "command": "gh pr merge 1 --merge",
            "prompt": "Will you be around later?",
            "session": "willq2",
        }
    )
    assert not blocked.allow
    guard.clear_gate("willq2")


def test_would_you_without_an_authorizing_verb_still_blocks_side_effects() -> None:
    """Dropping "would" from the interrogatory set must not blanket-authorize:
    the verb-based check still has to find a non-negated authorizing verb."""
    blocked = guard.decide(
        {
            "tool": "Write",
            "file_path": str(Path.home() / ".codex" / "AGENTS.md"),
            "prompt": "Would you mind not touching the global bootstrap?",
            "session": "wouldneg",
        }
    )
    assert not blocked.allow
    guard.clear_gate("wouldneg")


def test_guard_pause_is_capped(capsys: pytest.CaptureFixture[str]) -> None:
    """A week-long pause is a disable with extra steps: it silently masks the
    enforcement check for the duration. One box was found paused for 185h."""
    assert guard.run_guard("pause", duration="185h") == 0
    out = capsys.readouterr().out
    assert "cap" in out
    remaining = guard.pause_remaining()
    assert 0 < remaining <= guard._MAX_PAUSE_SECONDS
    guard.resume_gate()


def test_guard_pause_under_the_cap_is_untouched(capsys: pytest.CaptureFixture[str]) -> None:
    assert guard.run_guard("pause", duration="30m") == 0
    assert 0 < guard.pause_remaining() <= 1800
    assert "cap" not in capsys.readouterr().out
    guard.resume_gate()


def test_capability_question_allows_ordinary_local_work() -> None:
    """A gate that stops legitimate work teaches people to route around it.

    Phrasing a request as "can you …" used to block `mkdir && cp` and a `sed -i`
    on a scratch file, because every Write/Edit and every `cp`/`touch`/redirect
    counted as a side effect. Local reversible work is the task itself.
    """
    for command in (
        "mkdir -p ~/x && cp a b",
        "sed -i 's/a/b/' scratch.html",
        "touch f",
        "git add -A",
        "git commit -m 'x'",
        "echo hi > f",
    ):
        verdict = guard.decide(
            {
                "tool": "Bash",
                "command": command,
                "prompt": "can you set that up for me?",
                "session": "capallow",
            }
        )
        assert verdict.rule_id != "capability-question-explicit-auth", command
    guard.clear_gate("capallow")


def test_capability_question_still_gates_the_irreversible() -> None:
    """Narrower must not mean toothless: outward, destructive, and permission
    changes still need an explicit go-ahead."""
    for command in (
        "git push origin main",
        "gh pr create -t x",
        "rm -rf /tmp/x",
        "chmod 777 /etc/x",
        "systemctl restart nginx",
        "docker compose down",
    ):
        verdict = guard.decide(
            {
                "tool": "Bash",
                "command": command,
                "prompt": "can you set that up for me?",
                "session": "capblock",
            }
        )
        assert not verdict.allow, command
        assert verdict.rule_id == "capability-question-explicit-auth", command
    guard.clear_gate("capblock")


# -- #239: a truncated read of the demanded note does not clear the gate ------


def test_truncated_demanded_read_keeps_git_rules_unconsulted() -> None:
    session = "trunc1"
    guard.begin_turn(session, "push the release")
    guard.record_demanded_note(session, guard.GIT_RULES_NOTE)
    guard.record_consult(session, kind="read", target=guard.GIT_RULES_NOTE, relevant=True)
    assert guard._has_consulted_git_rules(session)  # complete read counts
    guard.record_incomplete_consult(session, guard.GIT_RULES_NOTE)
    assert not guard._has_consulted_git_rules(session)  # truncated read does not
    guard.clear_incomplete_consult(session)
    assert guard._has_consulted_git_rules(session)  # full re-read clears it
    guard.begin_turn(session, "next turn")
    assert guard.incomplete_consult(session) == ""  # per-turn state resets


# -- #241: rule text adjacent to the action -----------------------------------


def test_repo_block_message_embeds_governing_excerpt(tmp_path: Path) -> None:
    from omind.store import NoteFields, OmiStore

    omi = tmp_path / "OMI"
    omi.mkdir()
    OmiStore(omi).create_note(
        NoteFields(
            title=guard.GIT_RULES_NOTE,
            summary="Branch plus PR on public repos; private goes straight to main.",
            details="EXCEPTION TABLE: repo-x pushes directly to master.",
        )
    )
    session = "adj1"
    guard.begin_turn(session, "push it")
    verdict = guard.check_action(
        {"tool": "Bash", "command": "git push origin main", "session": session},
        omi_dir=omi,
    )
    assert not verdict.allow
    assert "Governing memory (excerpt)" in verdict.reason
    assert "Branch plus PR on public repos" in verdict.reason
    assert "EXCEPTION TABLE" in verdict.reason
    assert verdict.reason.index("ACTION BLOCKED") < verdict.reason.index("Governing")
    assert len(verdict.reason) < 2_400  # demand + capped excerpt


def test_repo_block_message_survives_a_missing_note(tmp_path: Path) -> None:
    omi = tmp_path / "OMI"
    omi.mkdir()
    guard.begin_turn("adj2", "push it")
    verdict = guard.check_action(
        {"tool": "Bash", "command": "git push origin main", "session": "adj2"},
        omi_dir=omi,
    )
    assert not verdict.allow  # the demand still stands on its own
    assert "Governing memory" not in verdict.reason


def test_preflight_reinjects_full_excerpt_on_action_shaped_turns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omind.store import NoteFields, OmiStore

    monkeypatch.setenv(guard.PREFLIGHT_MODE_ENV, "inject")
    omi = tmp_path / "OMI"
    omi.mkdir()
    OmiStore(omi).create_note(
        NoteFields(
            title="Deploy Rules",
            summary="Deploys are gated.",
            details="Always deploy from a tagged release build.",
        )
    )
    event = {"session_id": "act-turn", "prompt": "deploy the release build"}
    first = guard.preflight_turn(event, omi)
    assert "tagged release" in first
    repeated = guard.preflight_turn(event, omi)
    # Action-shaped turn: full excerpt again, no summary-only downgrade (#241).
    assert "tagged release" in repeated
    assert "already injected earlier this session" not in repeated


def test_preflight_adds_second_title_summary_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omind import ai_usage
    from omind.store import NoteFields, OmiStore

    monkeypatch.setenv(guard.PREFLIGHT_MODE_ENV, "inject")
    omi = tmp_path / "OMI"
    omi.mkdir()
    store = OmiStore(omi)
    store.create_note(
        NoteFields(
            title="Token Budget Alpha",
            summary="primary token budget note",
            details="ALPHA-BODY token budget usage bounds",
        )
    )
    store.create_note(
        NoteFields(
            title="Token Budget Beta",
            summary="secondary token budget note",
            details="BETA-BODY token budget usage bounds",
        )
    )
    ai_usage.set_profile(omi, "full")
    context = guard.preflight_turn(
        {"session_id": "second-1", "prompt": "token budget usage bounds"}, omi
    )
    assert "Also possibly relevant: [[" in context
    assert "BETA-BODY" not in context or "ALPHA-BODY" not in context  # runner-up is summary-only
    ai_usage.set_profile(omi, "economy")
    guard.clear_gate("second-2")
    economy = guard.preflight_turn(
        {"session_id": "second-2", "prompt": "token budget usage bounds"}, omi
    )
    assert "Also possibly relevant" not in economy  # skipped on economy


def test_preflight_hint_names_both_candidates_without_bodies(tmp_path: Path) -> None:
    # The pull hint replaces the runner-up summary line with a second title:
    # two names cost ~60 chars, two bodies cost thousands (#321).
    from omind.store import NoteFields, OmiStore

    omi = tmp_path / "OMI"
    omi.mkdir()
    store = OmiStore(omi)
    store.create_note(
        NoteFields(
            title="Token Budget Alpha",
            summary="primary token budget note",
            details="ALPHA-BODY token budget usage bounds",
        )
    )
    store.create_note(
        NoteFields(
            title="Token Budget Beta",
            summary="secondary token budget note",
            details="BETA-BODY token budget usage bounds",
        )
    )
    context = guard.preflight_turn(
        {"session_id": "hint-2", "prompt": "token budget usage bounds"}, omi
    )
    assert "[[Token Budget Alpha.md]]" in context and "[[Token Budget Beta.md]]" in context
    assert "ALPHA-BODY" not in context and "BETA-BODY" not in context


# -- #311: Poolside preflight (snake_case reply, prompt from the trajectory) --


def test_preflight_cli_poolside_emits_snake_case_and_recovers_prompt(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    omi = tmp_path / "OMI"
    omi.mkdir()
    traj = tmp_path / "trajectory-standalone_x.ndjson"
    traj.write_text(
        json.dumps(
            {
                "type": "tool_call.inference.start",
                "tool_call_inference_start": {
                    "chat_completion_request": {
                        "messages": [
                            {"role": "user", "content": "<user_query>\nunknown\n</user_query>"}
                        ]
                    }
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    # pool exec's UserPromptSubmit payload carried no `prompt` (live, 1.0.16).
    rc = guard.run_guard(
        "preflight",
        io.StringIO(
            json.dumps(
                {
                    "hook_event_name": "UserPromptSubmit",
                    "session_id": "preflight-pool",
                    "trajectory_path": str(traj),
                }
            )
        ),
        omi_dir=omi,
        harness="poolside",
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["hook_specific_output"]["hook_event_name"] == "UserPromptSubmit"
    context = payload["hook_specific_output"]["additional_context"]
    # With the prompt recovered the vault was searched: an empty vault is a MISS
    # that auto-clears, not the strict "no task captured" branch.
    assert "found nothing relevant" in context
    assert guard.consulted_this_turn("preflight-pool")
    guard.clear_gate("preflight-pool")


def test_gate_exempts_poolside_control_tools_but_not_shell() -> None:
    """#313: pool ends a run through its `exit` tool and books work through
    `todo_action`; neither is an action on the world, so the consult gate must
    let them through WITHOUT marking the gate consulted — a real tool call in
    the same turn still has to consult first."""
    guard.clear_gate("s313")
    for tool in ("exit", "todo_action"):
        assert guard.decide({"tool": tool, "session": "s313", "command": ""}).allow
        assert not guard.consulted_this_turn("s313")  # exemption is not a consult
    # The exemption is by tool name only: pool's shell tool stays gated.
    shell = guard.check_action(
        {"tool": "shell", "command": "echo hi", "session": "s313", "is_omi_consult": False}
    )
    assert not shell.allow and "omi-gate" in shell.reason
    guard.clear_gate("s313")


def test_remote_and_quoted_git_verbs_are_not_local_repo_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#317: `_is_repo_sensitive_action` searched for a git verb after ANY
    separator, and the `&&` inside an ssh payload supplied one — so a commit on
    hermes was judged local repo work. The repo was then resolved from the local
    cwd, making the freshness fetch it demanded VACUOUS: it refreshed an
    unrelated repo and recorded a false attestation against the remote commit.

    CJ's exact shapes are pinned verbatim (the ssh payload that tripped the gate
    twice, and the heredoc whose script text merely contained a commit command).
    """
    repo = _mk_repo(tmp_path, "local")
    monkeypatch.chdir(repo)
    for command in (
        "ssh hermes 'cd /home/hermes/Source/repos/tts && git add -A && git commit -m wip'",
        'ssh hermes "cd /home/hermes/Source/repos/tts && git commit -m wip"',
        "docker exec box sh -c '(git commit -m x)'",
        "kubectl exec pod -- sh -c '(git commit -m x)'",
        "python3 <<'PY'\nsubprocess.run('cd /p && git commit -m x')\nPY",
        "gh issue create --body \"$(cat <<'MD'\nBroken by:\n(git commit -m x)\nMD\n)\"",
    ):
        action = {"tool": "Bash", "command": command}
        assert not guard._is_repo_sensitive_action(action), command
        assert not guard._is_commit_action(action), command

    # Local repo work is untouched — including through a SHELL heredoc, whose
    # body really is code this shell runs.
    for command in (
        "git commit -m real",
        "git add -A && git commit -m real",
        f"git -C {repo} commit -m real",
        "git add -A\ngit commit -m real",
        "(git commit -m real)",
        "bash <<'EOF'\ngit commit -m real\nEOF",
    ):
        action = {"tool": "Bash", "command": command}
        assert guard._is_repo_sensitive_action(action), command
        assert guard._is_commit_action(action), command


def test_side_effect_gate_keeps_raw_text() -> None:
    """The #317 masking is scoped to the LOCAL-repo classifiers on purpose.

    `_is_side_effect_action` asks "does this carry a consequence", not "is this
    local repo work" — a remote restart is a real side effect, merely a remote
    one — so masking there would be a fail-open rather than a fix. Pinned with
    the discriminating shape: a risky verb after a separator inside a NON-shell
    heredoc body, which the mask would hide and this gate must still see.
    """
    assert guard._is_side_effect_action({"tool": "Bash", "command": "cat <<'EOF'\nrm -rf /x\nEOF"})
    assert guard._is_side_effect_action({"tool": "Bash", "command": "echo hi && rm -rf /x"})


def test_git_dash_c_with_quoted_path_is_repo_work(tmp_path):
    """The quoted `git -C "<abs>" commit` form — the one GIT_FRESHNESS_MESSAGE teaches —
    reaches the classifier with its literal blanked (#317), which used to hide the verb.
    Found 2026-09-11 in the guard experiment: after one freshness block Opus 5 switched to
    this form and was never classified as repo work again for the rest of the session."""
    repo = tmp_path / "repo"
    repo.mkdir()
    for command in (
        f'git -C "{repo}" commit -m "audit 1" -- CHANGELOG.md',
        f"git -C '{repo}' push origin main",
        f'git -C "{repo}" -c user.name="Experiment Runner" commit -m x',
        f"git -C {repo} commit -m x",
    ):
        assert guard._is_repo_sensitive_action({"tool": "Bash", "command": command}), command
    for command in (
        f'git -C "{repo}" fetch --all --prune',
        f'git -C "{repo}" status --short',
        f'git -C "{repo}" log --oneline -3',
    ):
        assert not guard._is_repo_sensitive_action({"tool": "Bash", "command": command}), command


@pytest.mark.parametrize(
    "command",
    [
        # The #391 repro: `| sed` at command position plus grep's own `-iE`.
        "ioreg -p IOUSB -w0 | grep -oE '+-o [^<]+' | sed 's/+-o //'; "
        'system_profiler SPCardReaderDataType | grep -iE "media|vendor" | head',
        "git show HEAD:src/x.py | grep -i foo | sed -n 1,5p",
        "ls -i | sed s/a/b/",
        "sed -n 's/a/b/p' f | grep -i x",
        "sed -e 's/-i/x/' f",
        "sed -e s/a/-i/ f",
        "perl -MList::Util -e 'print 1' f",
        "perl -ne 'print if /-i/' f",
        "python3 -i",
        "python3 -c 'print(1)' | grep -i x",
        "echo 'sed -i s/a/b/ f'",
    ],
)
def test_foreign_dash_i_is_not_an_in_place_edit(command):
    """#391: a `-i` belonging to another program in the pipeline (grep -iE, ls -i)
    or sitting inside a quoted script must not make the command repo work."""
    assert not guard._is_repo_sensitive_action({"tool": "Bash", "command": command})


@pytest.mark.parametrize(
    "command",
    [
        "sed -i 's/a/b/' f",
        "sed -i '' 's/a/b/' f",  # BSD/macOS empty suffix
        "sed -i.bak 's/a/b/' f",
        "sed -Ei 's/a/b/' f",
        "sed -n -i 's/a/b/p' f",
        "sed --in-place 's/a/b/' f",
        "sed --in-place=.bak 's/a/b/' f",
        "grep -l x *.py | xargs echo && sed -i 's/a/b/' f",
        "cat f | /usr/bin/sed -i 's/a/b/' f",
        "LC_ALL=C sed -i 's/a/b/' f",
        "perl -pi -e 's/a/b/' f",
        "perl -i -pe 's/a/b/' f",
        "perl -i.bak -pe 's/a/b/' f",
        "ruby -pi -e 'x' f",
        "python3 -c \"from pathlib import Path; Path('x').write_text('y')\"",
        "python3 - <<'EOF'\nopen('x', 'w').write('y')\nEOF",
    ],
)
def test_real_in_place_edit_is_repo_work(command):
    """#391: narrowing the `-i` test must not let a real in-place edit (or a script
    writing files) slip out of the repo-work gate."""
    assert guard._is_repo_sensitive_action({"tool": "Bash", "command": command})


@pytest.mark.parametrize(
    "command",
    [
        # The marker belongs to grep's pattern, not to the python stage.
        'grep -rn "write_text" src | python3 -m json.tool',
        "python3 --version; grep -n \"open(p, 'w')\" x.py",
        # The bare word in a printed string is not a write call.
        "python3 -c 'print(\"write_text\")'",
        "python3 -c \"print(open('f').read())\"",
        "python3 -c \"open('x')\"",
        # A switch that takes the next word as its argument: `-i` is the script.
        "sed -e -i f",
        "sed -f -i.sed f",
        "sed --expression -i f",
        # A wrapper's own `-i` is not the editor's.
        "env -i sed 's/a/b/' f",
    ],
)
def test_script_write_marker_is_bound_to_the_interpreter_stage(command):
    """#391 review: a write marker and an interpreter elsewhere in the command, or a
    bare marker word, must not make a read-only command repo work."""
    assert not guard._is_repo_sensitive_action({"tool": "Bash", "command": command})


@pytest.mark.parametrize(
    "command",
    [
        # Line continuation used to put `-i` in its own stage.
        "sed \\\n  -i 's/a/b/' f",
        # Wrappers, keywords and find -exec in front of the editor.
        "grep -l x *.py | xargs sed -i 's/a/b/'",
        "grep -l x *.py | xargs -0 -I {} sed -i 's/a/b/' {}",
        "find . -name '*.py' -exec sed -i 's/a/b/' {} +",
        "find . -execdir perl -pi -e 's/a/b/' {} \\;",
        "env X=1 sed -i 's/a/b/' f",
        "sudo sed -i 's/a/b/' f",
        "sudo -u bob sed -i 's/a/b/' f",
        "command sed -i 's/a/b/' f",
        "time sed -i 's/a/b/' f",
        "nice -n 10 sed -i 's/a/b/' f",
        "nohup sed -i 's/a/b/' f",
        "timeout 5 sed -i 's/a/b/' f",
        "for f in *.py; do sed -i 's/a/b/' \"$f\"; done",
        "if true; then sed -i 's/a/b/' f; fi",
        "if false; then :; else sed -i 's/a/b/' f; fi",
        "{ sed -i 's/a/b/' f; }",
        "echo `sed -i 's/a/b/' f`",
        # BSD sed -I, a cluster after an arg-taking switch's argument, sed.exe.
        "sed -I '' 's/a/b/' f",
        "sed -e 's/a b/' -i f",
        "sed.exe -i 's/a/b/' f",
        "C:\\tools\\sed.exe -i s/a/b/ f",
        # Write-mode and destructive script calls.
        "python3 -c \"open('x', 'x').write('y')\"",
        "python3 -c \"open('x', 'r+').write('y')\"",
        "python3 -c \"open('x', mode='wb')\"",
        "python3 -c \"from pathlib import Path; Path('x').open('w')\"",
        "python3 -c \"from pathlib import Path; Path('x').unlink()\"",
        "python3 -c \"import os; os.remove('x')\"",
        "python3 -c \"import os; os.replace('a', 'b')\"",
        "python3 -c \"import shutil; shutil.rmtree('d')\"",
        "node -e \"require('fs').writeFileSync('x', 'y')\"",
        "cd src && python3 - <<'EOF'\nfrom pathlib import Path\nPath('x').write_text('y')\nEOF",
    ],
)
def test_wrapped_or_continued_in_place_edit_is_repo_work(command):
    """#391 review: editors behind a wrapper, keyword, `find -exec` or a line
    continuation, and script writes/deletes, stay in the repo-work gate."""
    assert guard._is_repo_sensitive_action({"tool": "Bash", "command": command})


@pytest.mark.parametrize(
    "command",
    [
        # Homebrew GNU sed, the usual dodge around BSD `-i ''` on darwin.
        "gsed -i 's/a/b/' f",
        "/opt/homebrew/bin/gsed -i 's/a/b/' f",
        "gsed --in-place 's/a/b/' f",
        # The escaped `\;` ending the first -exec is not a shell separator.
        "find . -exec echo {} \\; -exec sed -i 's/a/b/' {} \\;",
        "find . -name x -print -exec grep -l y {} \\; -exec perl -pi -e 's/a/b/' {} +",
        # A case arm's `)` ends the pattern; the editor runs after it.
        "case $1 in fix) sed -i 's/a/b/' f ;; esac",
        "case $1 in a|b) sed -i 's/a/b/' f ;; esac",
        # `poetry run` / `pipx run` / `uv run` exec the next word.
        "poetry run python -c \"open('x','w').write('y')\"",
        "poetry run sed -i 's/a/b/' f",
        "pipx run --spec foo python -c \"open('x','w')\"",
        "uv run python -c \"from pathlib import Path; Path('x').write_text('y')\"",
        # Destructive/moving script calls.
        "node -e \"require('fs').rmSync('x', {recursive: true})\"",
        "node -e \"require('fs').renameSync('a', 'b')\"",
        "python3 -c \"import os; os.rename('a', 'b')\"",
        "python3 -c \"import shutil; shutil.move('a', 'b')\"",
    ],
)
def test_issue_419_in_place_edits_are_repo_work(command):
    """#419: gsed, a second `find -exec` after `\\;`, case arms, `poetry`/`pipx run`
    and fs.rmSync/os.rename/shutil.move all edit files and must be repo work."""
    assert guard._is_repo_sensitive_action({"tool": "Bash", "command": command})


@pytest.mark.parametrize(
    "command",
    [
        "gsed -n 's/a/b/p' f",
        "gsed 's/a/b/' f | grep -i x",
        "find . -exec echo {} \\; -exec grep -i x {} \\;",
        "find . -exec ls -i {} \\; -print",
        # find's own `-iname` after the -exec ends is not sed's `-i`.
        "find . -exec sed -n p {} \\; -iname x",
        "case $1 in fix) sed -n 's/a/b/p' f ;; esac",
        "case $1 in a) grep -i x f ;; esac",
        "echo $(ls -i) foo",
        "poetry show",
        "poetry run python -c 'print(1)'",
        "pipx list",
        "pipx run cowsay -i hi",
        "node -e \"console.log(require('fs').readdirSync('.'))\"",
        "python3 -c \"import os; print(os.path.exists('a'))\"",
        "python3 -c \"import shutil; print(shutil.which('git'))\"",
        "grep -rn 'shutil.move(' src",
    ],
)
def test_issue_419_read_only_forms_stay_unflagged(command):
    """#419: the read-only forms of the same tools must not become repo work."""
    assert not guard._is_repo_sensitive_action({"tool": "Bash", "command": command})


@pytest.mark.parametrize(
    ("command", "program"),
    [
        # A wrapper's global options before `run` (#419 review).
        ("poetry -q run sed -i 's/a/b/' f", "sed"),
        ("poetry -C sub run sed -i 's/a/b/' f", "sed"),
        ("poetry --directory=sub run sed -i 's/a/b/' f", "sed"),
        ("uv --directory . run sed -i 's/a/b/' f", "sed"),
        ("uv --project sub --cache-dir /tmp/c run sed -i 's/a/b/' f", "sed"),
        # Value-taking `uv run` options must not leave their value as the program.
        ("uv run --extra dev python -c \"open('x','w')\"", "python"),
        ("uv run --group test python -c 1", "python"),
        ("uv run --env-file .env python -c 1", "python"),
        ("uv run --index https://x.invalid/simple python -c 1", "python"),
        ("uv run -w rich python -c 1", "python"),
        ("uv run --with rich python -c 1", "python"),
        ("uv run --python 3.12 python -c 1", "python"),
        ("uv run --package core python -c 1", "python"),
        ("uv run --project sub python -c 1", "python"),
        ("uv run --no-sync --extra dev -- sed -i 's/a/b/' f", "sed"),
        # More run-wrappers, same mechanism.
        ("pipenv run sed -i 's/a/b/' f", "sed"),
        ("pipenv --python 3.12 run python -c 1", "python"),
        ("pdm run sed -i 's/a/b/' f", "sed"),
        ("pdm run -p sub python -c 1", "python"),
        ("hatch run sed -i 's/a/b/' f", "sed"),
        ("hatch run dev:sed -i 's/a/b/' f", "sed"),
        ("hatch -e dev run python -c 1", "python"),
        ("conda run -n env sed -i 's/a/b/' f", "sed"),
        ("conda run --prefix /opt/env python -c 1", "python"),
        # Not running anything: the tool itself is the program.
        ("poetry -q show", "poetry"),
        ("uv --directory . sync", "uv"),
    ],
)
def test_issue_419_review_run_wrapper_resolves_program(command, program):
    """#419 review: global options before `run`, the full `uv run` option table,
    and pipenv/pdm/hatch/conda run all resolve to the program they exec."""
    stages = guard._program_stages(policy.shell_code_text(command), command)
    assert stages[0][0] == program


@pytest.mark.parametrize(
    "command",
    [
        "poetry -q run sed -i 's/a/b/' f",
        "poetry -C sub run sed -i 's/a/b/' f",
        "poetry -C sub run python3 -c \"open('x','w')\"",
        "pipenv run sed -i 's/a/b/' f",
        "pdm run sed -i 's/a/b/' f",
        "conda run -n env sed -i 's/a/b/' f",
        "conda run -n env python3 -c \"open('x','w')\"",
        # rm/rmdir/rename on a file-system receiver.
        "node -e \"require('fs').rename('a', 'b', () => {})\"",
        "node -e \"const fs = require('fs'); fs.rm('x', () => {})\"",
        "node -e \"const fs = require('fs'); fs.rmdir('d', () => {})\"",
        "node -e \"require('fs').promises.rm('x')\"",
        "node -e \"const {fsPromises} = x; fsPromises.rename('a', 'b')\"",
        "node -e \"require('fs').rmdirSync('d')\"",
        "python3 -c \"import os; os.rmdir('d')\"",
        "python3 -c \"from pathlib import Path; Path('a').rename('b')\"",
        "python3 -c \"from pathlib import Path; p = Path('a'); p.rename('b')\"",
        "python3 -c \"import pathlib; pathlib.Path('d').rmdir()\"",
        "ruby -e \"File.rename('a', 'b')\"",
        "ruby -e \"require 'fileutils'; FileUtils.rm('x')\"",
    ],
)
def test_issue_419_review_wrappers_and_fs_receivers_are_repo_work(command):
    """#419 review: wrapped writers and rm/rmdir/rename on file-system
    receivers are repo work."""
    assert guard._is_repo_sensitive_action({"tool": "Bash", "command": command})


@pytest.mark.parametrize(
    "command",
    [
        # rename/rm on a non-file-system receiver is not a file write.
        "python3 -c \"import pandas as pd; df = pd.DataFrame(); df.rename(columns={'a': 'b'})\"",
        "python3 -c \"import sqlite3; db = sqlite3.connect('x'); db.rename('table')\"",
        "node -e \"db.rename('table')\"",
        'node -e "const list = []; list.rm(0)"',
        "node -e \"tree.rmdir('n')\"",
        # A quoted `;` still ends the -exec: find's `-iname` is not sed's `-i`.
        "find . -exec sed -n p {} ';' -iname x",
        'find . -exec sed -n p {} ";" -iname x',
        # An escaped `|` is a word, not a pipe: one `echo` stage.
        "echo a\\|sed -i s/a/b/ f",
        # A run-wrapper that is not running anything.
        "poetry -q show",
        "conda run -n env python3 -c 'print(1)'",
    ],
)
def test_issue_419_review_read_only_forms_stay_unflagged(command):
    """#419 review: non-file `rename`/`rm`, a quoted find terminator, an escaped
    `|`, and wrappers not running a writer stay read-only."""
    assert not guard._is_repo_sensitive_action({"tool": "Bash", "command": command})


def test_issue_419_escaped_pipe_is_one_echo_stage():
    """#419 review: `a\\|sed` is one word, so the stage list is a single `echo`."""
    command = "echo a\\|sed -i s/a/b/ f"
    stages = guard._program_stages(policy.shell_code_text(command), command)
    assert [s[0] for s in stages] == ["echo"]


def test_record_freshness_outcome_retracts_for_dash_c_repo(tmp_path: Path) -> None:
    # #346: record_freshness_outcome must resolve -C repo from hook event and retract it
    repo = _mk_repo(tmp_path, "subrepo")
    guard.begin_turn("sess-f", "some task")
    guard._record_git_freshness("sess-f", repo, f'git -C "{repo}" fetch')
    assert guard._git_fresh_for_repo("sess-f", repo)

    failed_event = {
        "session_id": "sess-f",
        "tool_name": "Bash",
        "tool_input": {"command": f'git -C "{repo}" fetch'},
        "tool_response": {"exit_code": 1, "is_error": True},
    }
    guard.record_freshness_outcome(failed_event)
    # Freshness must be retracted from the -C repo, not from cwd
    assert not guard._git_fresh_for_repo("sess-f", repo)


# ---------------------------------------------------------------------------
# #296 — consult continuity: continuation prompts, retry carry, action budget
# ---------------------------------------------------------------------------


def _vault_with(tmp_path: Path, *notes: tuple[str, str, str]) -> Path:
    from omind.store import NoteFields, OmiStore

    omi = tmp_path / "OMI"
    omi.mkdir(exist_ok=True)
    store = OmiStore(omi)
    for title, summary, details in notes:
        store.create_note(NoteFields(title=title, summary=summary, details=details))
    return omi


def test_continuation_prompt_is_recognised_by_shape() -> None:
    assert guard.is_continuation_prompt("retry")
    assert guard.is_continuation_prompt("Yes please")
    assert guard.is_continuation_prompt("go ahead and do it")
    assert guard.is_continuation_prompt("<task-notification>agent done</task-notification>")
    assert not guard.is_continuation_prompt("")  # empty stays strict elsewhere
    assert not guard.is_continuation_prompt("is there a vpn turned on for this laptop?")
    assert not guard.is_continuation_prompt("reduce the OMI token usage in the preflight")


def test_continuation_prompt_is_resolved_against_the_prior_turns_task(tmp_path: Path) -> None:
    """The auto-clear hole: "go ahead" after a real task used to search the
    vault for "go ahead", miss, and open the gate with nothing injected."""
    omi = _vault_with(
        tmp_path,
        ("Token Usage Strategy", "Keep OMI token usage bounded.", "Use compact recall."),
    )
    sid = "cont-1"
    first = guard.preflight_turn({"session_id": sid, "prompt": "reduce OMI token usage"}, omi)
    assert "[[Token Usage Strategy.md]]" in first
    follow = guard.preflight_turn({"session_id": sid, "prompt": "go ahead"}, omi)
    assert "continuing the prior task" in follow
    assert "[[Token Usage Strategy.md]]" in follow
    assert guard.consulted_this_turn(sid)
    # The captured task the verifier scores against is the composite, and the
    # remembered substantive task survives a chain of continuations.
    assert "token usage" in guard.turn_task(sid).casefold()
    assert guard._read_last_turn(sid)["task"] == "reduce OMI token usage"
    events = compliance.read_events()
    assert events[-1]["rule_id"] == guard.GATE_PREFLIGHT_RULE
    # Default preflight is pull/hint (#321), so the continuation candidate is
    # named, not pushed — the logged outcome is "hint".
    assert events[-1]["outcome"] == "hint"
    assert "continuation=True" in events[-1]["detail"]


def test_continuation_with_nothing_prior_keeps_the_old_auto_clear(tmp_path: Path) -> None:
    omi = _vault_with(tmp_path, ("Ghidra decompiler retry budget", "Retry the decompile.", ""))
    context = guard.preflight_turn({"session_id": "cont-fresh", "prompt": "retry"}, omi)
    assert "weak memory match" in context
    assert compliance.read_events()[-1]["rule_id"] == guard.GATE_WEAK_MATCH_RULE


def test_identical_retry_inside_the_window_carries_the_gate_state(tmp_path: Path) -> None:
    """A burst of bare "retry" turns is Claude Code auto-retrying an API error;
    each one used to reset the gate, search for "retry", and auto-clear."""
    omi = _vault_with(tmp_path, ("Token Usage Strategy", "Keep OMI token usage bounded.", ""))
    sid = "carry-1"
    guard.preflight_turn({"session_id": sid, "prompt": "reduce OMI token usage"}, omi)
    guard.mark_consulted(sid)
    guard.count_action(sid, "pytest tests/")
    before = guard._read_sentinel(sid)
    first_retry = guard.preflight_turn({"session_id": sid, "prompt": "retry"}, omi)
    assert first_retry  # the first "retry" is judged like any continuation
    assert guard.consulted_this_turn(sid)
    second_retry = guard.preflight_turn({"session_id": sid, "prompt": "retry"}, omi)
    assert second_retry == ""  # carried: no reset, no re-judging, no injection
    assert guard.consulted_this_turn(sid)
    events = compliance.read_events()
    assert events[-1]["rule_id"] == guard.GATE_CARRY_RULE
    assert events[-1]["outcome"] == "carry"
    # A substantive prompt re-sent verbatim is a human re-asking, not a retry.
    again = guard.preflight_turn({"session_id": sid, "prompt": "reduce OMI token usage"}, omi)
    assert "[[Token Usage Strategy.md]]" in again
    assert before  # (sentinel existed before the retries)


def test_action_budget_rearms_the_gate_around_an_unseen_relevant_note(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    omi = _vault_with(
        tmp_path,
        (
            "telesto deploy runbook",
            "How to deploy the telesto services safely.",
            "Stop the telesto services before a deploy, then restart them.",
        ),
    )
    monkeypatch.setenv(guard.ACTION_BUDGET_ENV, "3")
    sid = "budget-1"
    # The turn's opening consult was about something else entirely.
    guard.preflight_turn({"session_id": sid, "prompt": "list the open pull requests"}, omi)
    guard.mark_consulted(sid)
    action = {"tool": "Edit", "file_path": "/srv/telesto/runbook/deploy.sh", "session": sid}
    for _ in range(3):
        assert guard.check_action(action, omi_dir=omi).allow
    assert guard.actions_since_consult(sid) == 3
    assert "deploy.sh" in " ".join(guard.action_trail(sid))
    # The 4th action: the work has drifted onto telesto deploys; an unseen
    # relevant note exists → the gate re-arms and demands exactly that note.
    verdict = guard.check_action(action, omi_dir=omi)
    assert not verdict.allow
    assert verdict.rule_id == guard.GATE_REARM_RULE
    assert "[[telesto deploy runbook]]" in verdict.reason
    assert "recall-note" in verdict.reason
    assert "Governing memory (excerpt)" in verdict.reason
    assert guard.demanded_note(sid) == "telesto deploy runbook.md"
    assert not guard.consulted_this_turn(sid)
    assert guard.rearm_count(sid) == 1
    events = compliance.read_events()
    assert events[-1]["rule_id"] == guard.GATE_REARM_RULE
    assert events[-1]["severity"] == "soft"  # a ceremony, never a hard deny
    # Consulting the demanded note clears it and restarts the budget.
    consult = {
        "is_omi_consult": True,
        "consult_target": "telesto deploy runbook.md",
        "consult_kind": "read",
        "session": sid,
    }
    assert guard.check_action(consult, omi_dir=omi).allow
    assert guard.check_action(action, omi_dir=omi).allow
    assert guard.actions_since_consult(sid) == 1
    # The same note is never re-demanded this session: budget exhausts to a
    # logged no-match auto-clear instead.
    for _ in range(3):  # actions 2, 3, then the budget hit: no candidate → reset
        assert guard.check_action(action, omi_dir=omi).allow
    assert compliance.read_events()[-1]["rule_id"] == guard.GATE_REARM_NO_MATCH_RULE
    assert guard.actions_since_consult(sid) == 1
    assert guard.check_action(action, omi_dir=omi).allow
    assert guard.actions_since_consult(sid) == 2


def test_action_budget_skips_notes_already_injected_this_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    omi = _vault_with(
        tmp_path, ("telesto deploy runbook", "Deploy the telesto services.", "Restart telesto.")
    )
    monkeypatch.setenv(guard.ACTION_BUDGET_ENV, "2")
    sid = "budget-seen"
    # Injected at turn start (the preflight found it) — it is already in context.
    context = guard.preflight_turn({"session_id": sid, "prompt": "deploy telesto services"}, omi)
    assert "[[telesto deploy runbook.md]]" in context
    action = {"tool": "Bash", "command": "systemctl restart telesto", "session": sid}
    for _ in range(4):
        assert guard.check_action(action, omi_dir=omi).allow
    assert guard.rearm_count(sid) == 0
    assert compliance.read_events()[-1]["rule_id"] == guard.GATE_REARM_NO_MATCH_RULE


def test_action_budget_is_capped_per_turn_and_reset_by_the_turn_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    omi = _vault_with(
        tmp_path,
        ("telesto deploy runbook", "Deploy the telesto services.", "Restart telesto."),
        ("telesto backup policy", "Back up telesto before any deploy.", "Snapshot telesto."),
    )
    monkeypatch.setenv(guard.ACTION_BUDGET_ENV, "1")
    monkeypatch.setenv(guard.MAX_REARM_ENV, "1")
    sid = "budget-cap"
    guard.begin_turn(sid, "restart the telesto services after the deploy")
    guard.mark_consulted(sid)
    action = {"tool": "Bash", "command": "systemctl restart telesto", "session": sid}
    assert guard.check_action(action, omi_dir=omi).allow
    blocked = guard.check_action(action, omi_dir=omi)
    assert not blocked.allow and blocked.rule_id == guard.GATE_REARM_RULE
    guard.mark_consulted(sid)  # (any consult re-opens; the cap is now reached)
    for _ in range(4):
        assert guard.check_action(action, omi_dir=omi).allow  # never re-gated again
    guard.begin_turn(sid, "next turn")
    assert guard.rearm_count(sid) == 0


def test_action_budget_ignores_consults_inert_commands_and_a_paused_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    omi = _vault_with(tmp_path, ("telesto deploy runbook", "Deploy telesto.", "Restart telesto."))
    monkeypatch.setenv(guard.ACTION_BUDGET_ENV, "1")
    sid = "budget-skip"
    guard.begin_turn(sid, "deploy telesto")
    guard.mark_consulted(sid)
    assert guard.check_action({"command": "pwd", "session": sid}, omi_dir=omi).allow
    assert guard.actions_since_consult(sid) == 0  # inert commands don't count
    assert guard.check_action({"command": "ls -la", "session": sid}, omi_dir=omi).allow
    assert guard.actions_since_consult(sid) == 1
    guard.pause_gate(60)
    try:
        assert guard.check_action({"command": "ls -la", "session": sid}, omi_dir=omi).allow
        assert guard.check_action({"command": "ls -la", "session": sid}, omi_dir=omi).allow
    finally:
        guard.resume_gate()
    assert guard.rearm_count(sid) == 0
    monkeypatch.setenv(guard.ACTION_BUDGET_ENV, "0")  # 0 disables the budget
    for _ in range(3):
        assert guard.check_action({"command": "ls -la", "session": sid}, omi_dir=omi).allow


def test_midturn_context_injects_the_candidate_and_resets_the_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    omi = _vault_with(
        tmp_path,
        (
            "telesto deploy runbook",
            "How to deploy the telesto services safely.",
            "Stop the telesto services before a deploy, then restart them.",
        ),
    )
    monkeypatch.setenv(guard.ACTION_BUDGET_ENV, "2")
    sid = "midturn-1"
    guard.begin_turn(sid, "restart the telesto services after the deploy")
    guard.mark_consulted(sid)
    event = {"session_id": sid, "tool_name": "Bash", "tool_input": {"command": "ls"}}
    assert guard.midturn_context(event, omi) == ""  # under budget: nothing
    for _ in range(2):
        guard.count_action(sid, "systemctl restart telesto")
    context = guard.midturn_context(event, omi)
    assert "OMI mid-turn recall" in context
    assert "[[telesto deploy runbook]]" in context
    assert "Stop the telesto services" in context
    assert guard.actions_since_consult(sid) == 0
    assert guard.consulted_this_turn(sid)
    assert guard.consults(sid)[-1]["kind"] == "midturn"
    assert compliance.read_events()[-1]["outcome"] == "inject"
    # Injected once, the note is "seen": the next budget hit finds nothing new
    # and the PreToolUse path never re-arms around it either.
    for _ in range(2):
        guard.count_action(sid, "systemctl restart telesto")
    assert guard.midturn_context(event, omi) == ""
    assert compliance.read_events()[-1]["rule_id"] == guard.GATE_REARM_NO_MATCH_RULE
    action = {"tool": "Bash", "command": "systemctl restart telesto", "session": sid}
    assert guard.check_action(action, omi_dir=omi).allow


# -- #358: the demanded git-rules note is missing, or its read failed ----------


def test_failed_read_of_git_rules_does_not_clear_the_gate() -> None:
    session = "gr358a"
    guard.begin_turn(session, "open a PR")
    # PreToolUse credits the consult before the read runs...
    guard.record_consult(session, kind="read", target=guard.GIT_RULES_NOTE)
    assert guard._has_consulted_git_rules(session)
    # ...and PostToolUse retracts it when the read came back not-found.
    guard.retract_consult(session, guard.GIT_RULES_NOTE)
    assert not guard._has_consulted_git_rules(session)
    blocked = guard.decide({"tool": "Bash", "command": "pytest", "session": session})
    assert blocked.rule_id == "repo-work-read-git-rules"
    # A later successful read is a separate record and still clears it.
    guard.record_consult(session, kind="read", target=guard.GIT_RULES_NOTE, relevant=True)
    assert guard._has_consulted_git_rules(session)
    guard.clear_gate(session)


def test_missing_git_rules_note_degrades_loudly_instead_of_demanding_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    omi = tmp_path / "OMI"
    omi.mkdir()
    session = "gr358b"
    guard.begin_turn(session, "run the tests")
    guard.mark_consulted(session)  # the ordinary consult gate is not under test
    action = {"tool": "Bash", "command": "pytest", "session": session}

    assert guard.demanded_note_missing(omi)
    assert guard.check_action(action, omi_dir=omi).allow
    assert "omind setup" in capsys.readouterr().err
    events = [e for e in compliance.read_events() if e.get("rule_id") == "demanded-note-missing"]
    assert len(events) == 1 and events[0]["outcome"] == "allowed"

    # Second repo action in the same turn: still allowed, not logged or warned again.
    assert guard.check_action(action, omi_dir=omi).allow
    assert capsys.readouterr().err == ""
    events = [e for e in compliance.read_events() if e.get("rule_id") == "demanded-note-missing"]
    assert len(events) == 1

    # Only the read demand is waived — a commit still needs a fresh base.
    commit = guard.check_action(
        {"tool": "Bash", "command": "git commit -am x", "session": session}, omi_dir=omi
    )
    assert commit.rule_id == "repo-work-fresh-base"
    guard.clear_gate(session)


def test_present_git_rules_note_is_still_demanded(tmp_path: Path) -> None:
    from omind.store import NoteFields, OmiStore

    omi = tmp_path / "OMI"
    omi.mkdir()
    OmiStore(omi).create_note(NoteFields(title=guard.GIT_RULES_NOTE, summary="rules"))
    assert not guard.demanded_note_missing(omi)
    # Doubt is not absence: an unreadable / nonexistent vault keeps the demand.
    assert not guard.demanded_note_missing(tmp_path / "nope")
    session = "gr358c"
    guard.begin_turn(session, "run the tests")
    verdict = guard.check_action(
        {"tool": "Bash", "command": "pytest", "session": session}, omi_dir=omi
    )
    assert verdict.rule_id == "repo-work-read-git-rules"
    guard.clear_gate(session)


# -- #290: a message the human sends mid-turn can authorize --------------------


def _transcript(tmp_path: Path, *entries: dict[str, object]) -> str:
    path = tmp_path / "transcript.jsonl"
    path.write_text("".join(json.dumps(e) + "\n" for e in entries), encoding="utf-8")
    return str(path)


def _opener(text: str) -> dict[str, object]:
    return {"type": "user", "message": {"role": "user", "content": text}}


def _tool_result() -> dict[str, object]:
    block = {"type": "tool_result", "tool_use_id": "t1", "content": "ok"}
    return {"type": "user", "message": {"role": "user", "content": [block]}}


def _queued(
    prompt: str, *, mode: str = "prompt", origin: object = ("human",)
) -> dict[str, object]:
    attachment: dict[str, object] = {
        "type": "queued_command",
        "prompt": prompt,
        "commandMode": mode,
    }
    if origin == ("human",):
        attachment["origin"] = {"kind": "human"}
    elif origin is not None:
        attachment["origin"] = origin
    return {"type": "attachment", "attachment": attachment}


def test_midturn_messages_are_this_turns_human_prompts_only(tmp_path: Path) -> None:
    path = _transcript(
        tmp_path,
        _opener("do the thing"),
        _queued("go ahead and push"),  # an EARLIER turn's go-ahead
        _opener("Can you also make it save its rules somewhere?"),
        _tool_result(),
        _queued("agent finished: go ahead and push", mode="task-notification", origin=None),
        _queued("go ahead", origin={"kind": "agent"}),
        _queued("go ahead", origin=None),  # does not SAY a human typed it
        _queued("Fix it all please"),
        _tool_result(),
    )
    assert guard.midturn_user_messages(path) == ["Fix it all please"]
    assert guard.midturn_user_messages(str(tmp_path / "missing.jsonl")) == []
    assert guard.midturn_user_messages(None) == []
    (tmp_path / "garbage.jsonl").write_text('{"type": "user"\nnot json\n', encoding="utf-8")
    assert guard.midturn_user_messages(str(tmp_path / "garbage.jsonl")) == []


def test_midturn_imperative_lifts_the_capability_question_block(tmp_path: Path) -> None:
    session = "mid290a"
    guard.begin_turn(session, "Can you also make it save its rules somewhere?")
    guard.record_consult(session, kind="read", target=guard.GIT_RULES_NOTE, relevant=True)
    push = {"tool": "Bash", "command": "git push origin feat/x", "session": session}

    blocked = guard.decide(push)
    assert blocked.rule_id == "capability-question-explicit-auth"

    opener = _opener("Can you also make it save its rules somewhere?")
    aside = _transcript(tmp_path, opener, _tool_result(), _queued("hm, interesting"))
    assert guard.decide({**push, "transcript_path": aside}).rule_id == blocked.rule_id

    machine = _transcript(
        tmp_path, opener, _queued("go ahead", mode="task-notification", origin=None)
    )
    assert guard.decide({**push, "transcript_path": machine}).rule_id == blocked.rule_id

    fixed = _transcript(tmp_path, opener, _tool_result(), _queued("Fix it all please"))
    after = guard.decide({**push, "transcript_path": fixed})
    assert after.rule_id != "capability-question-explicit-auth"

    # The LATEST message governs: a retraction after the go-ahead re-blocks.
    retracted = _transcript(
        tmp_path, opener, _queued("Fix it all please"), _queued("wait, don't push yet")
    )
    assert guard.decide({**push, "transcript_path": retracted}).rule_id == blocked.rule_id
    guard.clear_gate(session)


def test_midturn_authorization_never_lifts_a_destructive_hard_rule(tmp_path: Path) -> None:
    session = "mid290b"
    guard.begin_turn(session, "can you clean this up?")
    path = _transcript(tmp_path, _opener("can you clean this up?"), _queued("go ahead, do it"))
    verdict = guard.decide(
        {"tool": "Bash", "command": "gh repo delete me/x --yes", "session": session,
         "transcript_path": path}
    )
    assert not verdict.allow
    assert verdict.rule_id not in ("", "capability-question-explicit-auth")
    guard.clear_gate(session)


# -- #363 review: retraction is narrow, and a failed read re-arms the gate ------


def test_failed_read_re_arms_the_ordinary_consult_gate() -> None:
    """`recall-note` on a made-up name must not clear the gate — the same dodge
    as re-reading index.md."""
    session = "rv363a"
    guard.begin_turn(session, "do some work")
    guard.clear_gate(session)
    guard.record_consult(session, kind="read", target="No Such Note")  # PreToolUse credit
    assert guard.consulted_this_turn(session)
    guard.retract_consult(session, "No Such Note")
    assert not guard.consulted_this_turn(session)
    guard.clear_gate(session)


def test_failed_read_does_not_undo_an_earlier_successful_consult() -> None:
    session = "rv363b"
    guard.begin_turn(session, "do some work")
    guard.clear_gate(session)
    # A successful read: the PreToolUse credit, then the judged PostToolUse record.
    guard.record_consult(session, kind="read", target=guard.GIT_RULES_NOTE)
    guard.record_consult(session, kind="read", target=guard.GIT_RULES_NOTE, relevant=True)
    # A later attempt at the SAME note fails (a locked vault, a transient error).
    guard.record_consult(session, kind="read", target=guard.GIT_RULES_NOTE)
    guard.retract_consult(session, guard.GIT_RULES_NOTE)
    assert guard.consulted_this_turn(session)  # the real consult still stands
    assert guard._has_consulted_git_rules(session)
    assert sum(1 for c in guard.consults(session) if c.get("failed")) == 1
    # Retracting a read nothing credited is a no-op, not a gate clear.
    guard.retract_consult(session, "Some Other Note")
    assert guard.consulted_this_turn(session)
    guard.clear_gate(session)


def _pull(omi: Path, chars: int, session: str) -> None:
    from omind import ai_usage

    ai_usage.record_mcp_response(
        omi,
        {
            "tool_name": "mcp__omi__search-vault",
            "session_id": session,
            "tool_response": "x" * chars,
        },
    )


def test_agent_reads_do_not_spend_the_push_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # #387 acceptance: 70K of pull and 5K of push — the preflight still injects.
    from omind import ai_usage

    monkeypatch.delenv(ai_usage.SPLIT_BUDGET_ENV, raising=False)
    monkeypatch.setenv(guard.PREFLIGHT_MODE_ENV, "inject")
    omi = tmp_path / "OMI"
    omi.mkdir()
    _token_note(omi)
    _pull(omi, 70_000, "diligent")
    ai_usage.record_context(omi, "recall", 5_000, session_id="diligent")
    assert guard.session_context_chars(omi, "diligent") == 5_000
    context = guard.preflight_turn(
        {"session_id": "diligent", "prompt": "reduce OMI token usage"}, omi
    )
    assert "over budget" not in context
    assert "compact recall" in context


def test_split_budget_off_restores_the_combined_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omind import ai_usage

    monkeypatch.setenv(ai_usage.SPLIT_BUDGET_ENV, "0")
    monkeypatch.setenv(guard.PREFLIGHT_MODE_ENV, "inject")
    omi = tmp_path / "OMI"
    omi.mkdir()
    _token_note(omi)
    _pull(omi, 70_000, "old-way")
    ai_usage.record_context(omi, "recall", 5_000, session_id="old-way")
    assert guard.session_context_chars(omi, "old-way") > guard.SESSION_INJECTION_BUDGET_CHARS
    context = guard.preflight_turn(
        {"session_id": "old-way", "prompt": "reduce OMI token usage"}, omi
    )
    assert "over budget" in context
    assert "not counted" not in context


def test_over_budget_notice_reports_push_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The notice must name the push figure, not push + the agent's own reads.
    from omind import ai_usage

    monkeypatch.delenv(ai_usage.SPLIT_BUDGET_ENV, raising=False)
    monkeypatch.setenv(guard.PREFLIGHT_MODE_ENV, "inject")
    omi = tmp_path / "OMI"
    omi.mkdir()
    _token_note(omi)
    _pull(omi, 30_000, "both")
    ai_usage.record_context(omi, "recall", 61_000, session_id="both")
    context = guard.preflight_turn({"session_id": "both", "prompt": "reduce OMI token usage"}, omi)
    assert "over budget" in context
    assert "61,000 unrequested characters" in context
    assert "your own OMI reads are not counted" in context


# --- #420: the check action fails OPEN on an unexpected classifier exception ---


def _boom(*_args: object, **_kwargs: object) -> None:
    raise RuntimeError("classifier exploded")


def test_check_fails_open_when_a_classifier_raises(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(guard, "_repo_root_for_action", _boom)
    guard.clear_gate("s420")
    payload = {"tool": "Bash", "command": "ls", "session": "s420"}
    assert guard.run_guard("check", io.StringIO(json.dumps(payload))) == 0
    assert "classifier exploded" in capsys.readouterr().err
    event = compliance.read_events()[-1]
    assert event["rule_id"] == guard.GUARD_ERROR_RULE
    assert event["outcome"] == "fail-open"
    assert "RuntimeError: classifier exploded" in event["detail"]


def test_check_fail_open_still_honours_hard_policy_rules(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A crash in an unrelated classifier must not wave through a command a hard
    # policy rule plainly names.
    monkeypatch.setattr(guard, "_repo_root_for_action", _boom)
    verdict = guard.check_action({"tool": "Bash", "command": "sudo rm x", "session": "s420h"})
    assert not verdict.allow
    assert verdict.rule_id == "sudo-use-fleet-sudo"
    # The deny is logged under the hard rule's OWN id and severity (so it counts
    # in recidivism and doctor's top_rules), plus a separate internal-error event.
    events = [e for e in compliance.read_events() if e.get("session") == "s420h"]
    error = [e for e in events if e["rule_id"] == guard.GUARD_ERROR_RULE]
    deny = [e for e in events if e["rule_id"] == "sudo-use-fleet-sudo"]
    assert len(error) == 1
    assert error[0]["outcome"] == "error"
    assert error[0]["severity"] == "soft"
    assert len(deny) == 1
    assert deny[0]["outcome"] == "deny"
    assert deny[0]["severity"] == "hard"
    assert compliance.recidivism_counts()["sudo-use-fleet-sudo"] == 1


def test_check_keeps_a_decided_deny_when_a_later_step_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # decide() blocked on the consult gate; decorating the message then raised.
    from omind import retrieve

    monkeypatch.setattr(retrieve, "suggest_message", _boom)
    guard.clear_gate("s420g")
    omi = tmp_path / "OMI"
    omi.mkdir()
    verdict = guard.check_action({"tool": "Bash", "command": "ls", "session": "s420g"}, omi)
    assert not verdict.allow
    assert verdict.rule_id == "omi-gate"
    # Both the internal error and the deny that stood are on the record.
    events = [e for e in compliance.read_events() if e.get("session") == "s420g"]
    error = [e for e in events if e["rule_id"] == guard.GUARD_ERROR_RULE]
    deny = [e for e in events if e["rule_id"] == "omi-gate"]
    assert len(error) == 1
    assert error[0]["outcome"] == "error"
    assert "RuntimeError: classifier exploded" in error[0]["detail"]
    assert len(deny) == 1
    assert deny[0]["outcome"] == "deny"


def _no_home(*_args: object, **_kwargs: object) -> Path:
    # What Path.home() raises with no resolvable home (e.g. `docker run --user
    # 12345` with HOME / XDG_STATE_HOME unset).
    raise RuntimeError("Could not determine home directory.")


def test_check_fails_open_when_compliance_logging_also_raises(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The fail-open handler's own compliance write resolves the state dir too;
    # when that raises the same error, it must not escape (#420 review).
    monkeypatch.setattr(guard, "_repo_root_for_action", _boom)
    monkeypatch.setattr(paths, "state_dir", _no_home)
    payload = {"tool": "Bash", "command": "ls", "session": "s420n"}
    assert guard.run_guard("check", io.StringIO(json.dumps(payload))) == 0
    assert "internal error in guard check" in capsys.readouterr().err


def test_check_hard_deny_survives_compliance_logging_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(guard, "_repo_root_for_action", _boom)
    monkeypatch.setattr(compliance, "compliance_log_path", _no_home)
    verdict = guard.check_action({"tool": "Bash", "command": "sudo rm x", "session": "s420m"})
    assert not verdict.allow
    assert verdict.rule_id == "sudo-use-fleet-sudo"


def test_preflight_fails_open_when_compliance_logging_also_raises(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(guard, "preflight_turn", _boom)
    monkeypatch.setattr(compliance, "compliance_log_path", _no_home)
    payload = {"session_id": "s420q", "prompt": "do the thing"}
    assert guard.run_guard("preflight", io.StringIO(json.dumps(payload))) == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "internal error in guard preflight" in captured.err


def test_hard_policy_skips_a_raising_rule_and_keeps_the_rest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # One hard rule raising mid-match (not re.error) must not disable the hard
    # rules after it.
    from omind import policy

    class _Broken:
        id = "broken-rule"
        severity = policy.SEVERITY_HARD
        opt_in = ""

        def matches(self, _command: str) -> bool:
            raise TypeError("rule exploded")

    real = policy.load_policy()
    monkeypatch.setattr(policy, "load_policy", lambda: [_Broken(), *real])
    verdict = guard._hard_policy_verdict("sudo rm x")
    assert verdict is not None
    assert verdict.rule_id == "sudo-use-fleet-sudo"


def test_preflight_fails_open_when_it_raises(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(guard, "preflight_turn", _boom)
    payload = {"session_id": "s420p", "prompt": "do the thing"}
    assert guard.run_guard("preflight", io.StringIO(json.dumps(payload))) == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "classifier exploded" in captured.err
    assert compliance.read_events()[-1]["rule_id"] == guard.GUARD_ERROR_RULE


# --- #420 round 3: hard rules hold when the state dir cannot be resolved ---

#: Commands a SEED hard rule denies, with the rule id expected (None: any hard rule).
NO_STATE_DIR_HARD_COMMANDS = (
    ("sudo rm -rf /x", "sudo-use-fleet-sudo"),
    ("sudo ls", "sudo-use-fleet-sudo"),
    ("gh repo delete foo/bar --yes", None),
)


@pytest.mark.parametrize(("command", "rule_id"), NO_STATE_DIR_HARD_COMMANDS)
def test_check_hard_rules_hold_with_no_state_dir(
    monkeypatch: pytest.MonkeyPatch, command: str, rule_id: str | None
) -> None:
    # With no resolvable home, load_learned() raised out of load_policy() and
    # took the SEED rules (which live in code) down with it: every hard rule
    # failed open. The seed rules must still deny.
    monkeypatch.setattr(paths, "state_dir", _no_home)
    verdict = guard.check_action({"tool": "Bash", "command": command, "session": "s420nh"})
    assert not verdict.allow
    assert verdict.rule_id and not verdict.rule_id.startswith("omi-gate")
    if rule_id is not None:
        assert verdict.rule_id == rule_id


@pytest.mark.parametrize(("command", "_rule_id"), NO_STATE_DIR_HARD_COMMANDS)
def test_run_guard_check_blocks_hard_rules_with_no_state_dir(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
    _rule_id: str | None,
) -> None:
    monkeypatch.setattr(paths, "state_dir", _no_home)
    payload = {"tool": "Bash", "command": command, "session": "s420nr"}
    assert guard.run_guard("check", io.StringIO(json.dumps(payload))) == 2
    assert "BLOCKED by" in capsys.readouterr().err


def test_load_policy_keeps_seed_rules_when_the_state_dir_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from omind import policy

    monkeypatch.setattr(paths, "state_dir", _no_home)
    assert policy.load_learned() == []
    assert [r.id for r in policy.load_policy()] == [r.id for r in policy.SEED_RULES]


def test_hard_policy_verdict_falls_back_to_seed_rules_when_loading_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from omind import policy

    monkeypatch.setattr(policy, "load_policy", _no_home)
    verdict = guard._hard_policy_verdict("sudo rm -rf /x")
    assert verdict is not None
    assert verdict.rule_id == "sudo-use-fleet-sudo"


def test_run_guard_check_survives_a_failing_blocked_stderr_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _BrokenErr(io.StringIO):
        def write(self, _s: str) -> int:
            raise OSError("stderr closed")

    monkeypatch.setattr(sys, "stderr", _BrokenErr())
    payload = {"tool": "Bash", "command": "sudo ls", "session": "s420se"}
    assert guard.run_guard("check", io.StringIO(json.dumps(payload))) == 2


#: Every module-level regex built on guard's ``_GIT_GLOBAL_OPTS`` (#431). The
#: inline one in ``_is_repo_sensitive_action`` is exercised through the function.
_GIT_OPTS_REGEXES = (
    guard._GIT_FRESH_SUB_RE,
    guard._GIT_READONLY_SUB_RE,
    guard._GIT_COMMIT_RE,
    guard._SHELL_SIDE_EFFECT_RE,
    guard._RISKY_SIDE_EFFECT_RE,
)


def _timed(fn: Callable[[], object]) -> tuple[object, float]:
    """Run ``fn`` under :func:`_hard_time_limit`; return its result and elapsed time."""
    with _hard_time_limit():
        start = time.perf_counter()
        result = fn()
        return result, time.perf_counter() - start


@pytest.mark.parametrize(
    "opt",
    [
        "-c a=b ",
        '-C "  " ',
        "-c user.name='A B' ",
        "-C /abs/repo ",
        '-c k="  "x ',
        "-c user.name=O\\'Brien ",
        '-c a=\\"b ',
    ],
)
def test_git_global_options_do_not_backtrack_exponentially(opt: str) -> None:
    """#431: `\\S+(?:...)?\\S*` split each `-c k=v` value several ways, so a run of
    them with no matching verb after cost ~3^N (6.5 s at 16). A PreToolUse hook
    that times out does not block, so the soft gates were skipped. Each search
    runs under a hard bound, so a regression fails here rather than hanging."""
    command = "git " + opt * 40 + "bogus"
    for regex in _GIT_OPTS_REGEXES:
        found, elapsed = _timed(lambda regex=regex: regex.search(command))
        assert not found, regex.pattern
        assert elapsed < 1.0, regex.pattern
    action = {"tool": "Bash", "command": command}
    for check in (guard._is_repo_sensitive_action, guard._is_commit_action):
        found, elapsed = _timed(lambda check=check: check(action))
        assert not found, check.__name__
        assert elapsed < 1.0, check.__name__


@pytest.mark.parametrize("count", [16, 24, 40])
def test_decide_on_a_long_git_option_run_is_fast(count: int) -> None:
    """#431's headline case end to end: `git` + N x `-c a=b` + `commit -m x`
    through the whole ``decide()`` pipeline, under a second."""
    session = f"s431-{count}"
    guard.clear_gate(session)
    command = "git " + "-c a=b " * count + "commit -m x"
    action = {"tool": "Bash", "command": command, "session": session}
    verdict, elapsed = _timed(lambda: guard.decide(action))
    assert isinstance(verdict, guard.Verdict)
    assert elapsed < 1.0


@pytest.mark.parametrize("value", ["user.name=O\\'Brien", 'a=\\"b'])
def test_escaped_quote_in_a_git_option_value_still_classifies(value: str) -> None:
    """#431 review: ``shell_code_text`` leaves an escaped quote outside quotes
    as-is, and the one-shell-word pattern read it as an unclosed quoted run, so
    `git -c user.name=O\\'Brien commit` was neither a commit nor repo work and
    skipped the freshness gate (fail-open). The ``\\\\.`` branch consumes it."""
    commit = {"tool": "Bash", "command": f"git -c {value} commit -m fix"}
    assert guard._is_commit_action(commit)
    assert guard._is_repo_sensitive_action(commit)
    assert guard._is_repo_sensitive_action({"tool": "Bash", "command": f"git -c {value} merge x"})
    assert guard._GIT_FRESH_SUB_RE.search(f"git -c {value} fetch origin --prune")
    assert guard._GIT_READONLY_SUB_RE.search(f"git -c {value} status")
    assert guard._SHELL_SIDE_EFFECT_RE.search(f"git -c {value} add .")
    push = f"git -c {value} push origin main"
    assert guard._RISKY_SIDE_EFFECT_RE.search(push)
    assert guard._is_side_effect_action({"tool": "Bash", "command": push})


def test_git_global_options_still_match_after_the_431_fix() -> None:
    """#431: the non-backtracking value pattern keeps every form the old one took."""
    opts = '-C "  " -c user.name="  " -c core.x=1 -c k="  "x -C /abs/repo '
    assert guard._GIT_FRESH_SUB_RE.search(f"git {opts}fetch origin --prune")
    assert guard._GIT_READONLY_SUB_RE.search(f"git {opts}status")
    assert guard._GIT_COMMIT_RE.search(f"git {opts}commit -m x")
    assert guard._SHELL_SIDE_EFFECT_RE.search(f"git {opts}add .")
    assert guard._RISKY_SIDE_EFFECT_RE.search(f"git {opts}push")
    assert guard._is_repo_sensitive_action({"tool": "Bash", "command": f"git {opts}merge x"})
    assert not guard._GIT_FRESH_SUB_RE.search(f"git {opts}fetch | tee x")


# --- #430: hard rules see shell-wrapper bodies and env/nice/timeout wrappers ---

WRAPPED_HARD_COMMANDS = (
    ("bash -c 'sudo rm -rf /x'", "sudo-use-fleet-sudo"),
    ('sh -c "sudo rm -rf /x"', "sudo-use-fleet-sudo"),
    ("eval 'sudo rm -rf /x'", "sudo-use-fleet-sudo"),
    ("echo 'sudo rm -rf /x' | bash", "sudo-use-fleet-sudo"),
    ("printf 'ls\\nsudo id\\n' | sh", "sudo-use-fleet-sudo"),
    ("cat <<'EOF' | bash\nsudo id\nEOF", "sudo-use-fleet-sudo"),
    ("bash -lc 'eval \"sudo id\"'", "sudo-use-fleet-sudo"),
    ("bash -c 'gh repo delete o/r --yes'", "gh-repo-delete"),
    ("env sudo rm -rf /x", "sudo-use-fleet-sudo"),
    ("nice sudo rm -rf /x", "sudo-use-fleet-sudo"),
    ("timeout 5 sudo rm -rf /x", "sudo-use-fleet-sudo"),
    ("/usr/bin/env sudo rm -rf /x", "sudo-use-fleet-sudo"),
    ("timeout -k 2 5s sudo id", "sudo-use-fleet-sudo"),
    ("nice -n 5 sudo id", "sudo-use-fleet-sudo"),
    ("env -u FOO BAR=1 sudo id", "sudo-use-fleet-sudo"),
    ("sudo -u bob gh repo delete o/r", None),
    # One body's opt-in never covers a match elsewhere in the command.
    ("sudo id; bash -c 'OMI_SUDO_OK=1 sudo x'", "sudo-use-fleet-sudo"),
    ("fish -c 'sudo id'", "sudo-use-fleet-sudo"),
    ("watch 'sudo id'", "sudo-use-fleet-sudo"),
    # #430 review, item 1: an UNQUOTED payload piped into a shell.
    ("echo sudo rm -rf /x | bash", "sudo-use-fleet-sudo"),
    ("printf '%s %s' sudo id | sh", "sudo-use-fleet-sudo"),
    ("{ echo hi; echo sudo id; } | bash", "sudo-use-fleet-sudo"),
    ("(echo sudo id) | bash", "sudo-use-fleet-sudo"),
    ("echo sudo id |& bash", "sudo-use-fleet-sudo"),
    ("echo sudo id | tee log | bash", "sudo-use-fleet-sudo"),
    ("cat <<< 'sudo id' | bash", "sudo-use-fleet-sudo"),
    ("x=$(echo sudo id | bash)", "sudo-use-fleet-sudo"),
    ("echo 'gh repo delete o/r' | bash", "gh-repo-delete"),
    # Item 2: redirect targets and -o values are not script operands.
    ("echo 'sudo rm -rf /x' | bash > log", "sudo-use-fleet-sudo"),
    ("echo 'sudo rm -rf /x' | bash 2> err", "sudo-use-fleet-sudo"),
    ("echo 'sudo id' | bash -o pipefail", "sudo-use-fleet-sudo"),
    ("bash <<< 'sudo id'", "sudo-use-fleet-sudo"),
    ("bash <<<'sudo id'", "sudo-use-fleet-sudo"),
    ("bash -s <<< 'sudo id'", "sudo-use-fleet-sudo"),
    # Item 3: positional words a `-c` body runs.
    ("bash -c '\"$@\"' _ sudo rm -rf /x", "sudo-use-fleet-sudo"),
    ('sh -c \'"$0" "$@"\' sudo id', "sudo-use-fleet-sudo"),
    # Item 4: wrapper argument shapes.
    ("timeout 5 -- sudo id", "sudo-use-fleet-sudo"),
    ("timeout 5s -k 2s sudo id", "sudo-use-fleet-sudo"),
    ("nice -n -5 sudo id", "sudo-use-fleet-sudo"),
    ('env -S "sudo rm -rf /x"', "sudo-use-fleet-sudo"),
    ("env -S sudo id", "sudo-use-fleet-sudo"),
    ("env --split-string='sudo id'", "sudo-use-fleet-sudo"),
    ("command -p sudo id", "sudo-use-fleet-sudo"),
)


@pytest.mark.parametrize("no_state_dir", [False, True])
@pytest.mark.parametrize(("command", "rule_id"), WRAPPED_HARD_COMMANDS)
def test_check_denies_wrapped_hard_rule_commands(
    monkeypatch: pytest.MonkeyPatch, command: str, rule_id: str | None, no_state_dir: bool
) -> None:
    if no_state_dir:  # #421
        monkeypatch.setattr(paths, "state_dir", _no_home)
    verdict = guard.check_action({"tool": "Bash", "command": command, "session": "s430"})
    assert not verdict.allow
    assert verdict.rule_id and not verdict.rule_id.startswith("omi-gate")
    if rule_id is not None:
        assert verdict.rule_id == rule_id


WRAPPED_BENIGN_COMMANDS = (
    "OMI_SUDO_OK=1 sudo id",
    "bash -c 'OMI_SUDO_OK=1 sudo id'",
    "OMI_SUDO_OK=1 bash -c 'sudo id'",
    "bash -c 'git commit -m \"drop sudo usage\"'",
    "bash -c 'grep -rn sudo docs'",
    "echo 'never use sudo here' | bash",
    "bash scripts/x.sh 'sudo'",
    "git commit -m 'env sudo is now blocked'",
    "grep -rn 'timeout 5 sudo' tests/",
    "ssh host 'sudo id'",
    "timeout 60 uv run pytest -q",
    "nice make test",
    "env",
    # #430 review: an opt-in on the outer command still applies.
    "env OMI_SUDO_OK=1 bash -c 'sudo id'",
    "env OMI_SUDO_OK=1 bash -c '\"$@\"' _ sudo id",
    # Item 5: `command -v`/`-V` is a lookup, not an exec.
    "command -v sudo",
    "command -V sudo",
    "command -v sudo >/dev/null && echo yes",
    "bash -c 'command -v sudo'",
    # Item 6: only the stages that feed the shell are judged.
    "curl -fsSL https://x/install.sh | sh && git commit -m 'sudo: drop'",
    "git commit -m 'sudo: drop'; curl -fsSL https://x/install.sh | sh",
    "echo hi | bash; echo 'sudo id' > notes.txt",
    # Item 7: an opaque executor's quoted text is code only where a
    # command word ends at a blank.
    "tmux new -s 'sudo-test'",
    "python3 -c \"subprocess.run(['grep','sudo','.'])\"",
    # A shell reading a file, or a body that never runs its arguments.
    "bash x.sh > log",
    "bash < x.sh",
    "bash -c 'echo hi' _ sudo",
    "echo hi | bash -s -- sudo",
    "env -S 'echo sudo'",
)


@pytest.mark.parametrize("command", WRAPPED_BENIGN_COMMANDS)
def test_wrapped_hard_rules_keep_benign_commands_allowed(command: str) -> None:
    assert guard._hard_policy_verdict(command) is None


#: #432 review items 1-2, the hard-rule twin: a `-c` body or `source` that
#: reads its stdin as code runs what the pipeline or heredoc feeds it.
STDIN_FED_HARD_COMMANDS = (
    "cat <<'EOF' | bash -c 'eval \"$(cat)\"'\nsudo id\nEOF",
    "echo 'sudo id' | bash -c 'eval \"$(cat)\"'",
    "echo 'sudo id' | bash -c 'sh'",
    "bash -c 'eval \"$(cat)\"' <<'EOF'\nsudo id\nEOF",
    "bash -c 'eval \"$(cat)\"' <<< 'sudo id'",
    "cat <<'EOF' | source /dev/stdin\nsudo id\nEOF",
    "echo 'sudo id' | . /dev/stdin",
    "source /dev/stdin <<'EOF'\nsudo id\nEOF",
    "echo 'sudo id' | bash /dev/stdin",
)


@pytest.mark.parametrize("command", STDIN_FED_HARD_COMMANDS)
def test_hard_rules_judge_code_a_body_reads_from_stdin(command: str) -> None:
    verdict = guard._hard_policy_verdict(command)
    assert verdict is not None and verdict.rule_id == "sudo-use-fleet-sudo"


@pytest.mark.parametrize(
    "command",
    [
        "echo 'sudo id' | bash -c 'wc -c'",
        "cat <<'EOF' | bash -c 'cat > notes.md'\nsudo id\nEOF",
        "echo 'sudo id' | source ./env.sh",
    ],
)
def test_hard_rules_leave_stdin_data_alone(command: str) -> None:
    assert guard._hard_policy_verdict(command) is None


#: #444: a bare `-` stdin operand, a process substitution a shell or `source`
#: runs, a here-string `source /dev/stdin` reads, and the `setsid`/`stdbuf`
#: wrappers.
STDIN_OPERAND_HARD_COMMANDS = (
    "echo sudo id | bash -",
    "echo 'sudo id' | sh -",
    "bash - <<< 'sudo id'",
    "echo sudo id | bash /dev/stdin",
    "bash <(echo sudo id)",
    "sh <(printf 'sudo id')",
    "bash -x <(echo 'sudo id') arg",
    "bash < <(echo sudo id)",
    "source <(echo sudo id)",
    ". <(echo sudo id)",
    "source /dev/stdin < <(echo sudo id)",
    ". /dev/stdin <<< 'sudo id'",
    # The quoted `)` must not close the substitution: `sudo` here is only an
    # argument of `echo`, so this needs the paren scanner (#444 review).
    "bash <(echo ')' ; echo sudo id)",
    "setsid sudo id",
    "setsid -f sudo id",
    "stdbuf -oL sudo id",
    "stdbuf -o L sudo id",
    "/usr/bin/stdbuf -i 0 -e L sudo id",
    # #444 review: a long option is not `-s`.
    "bash --posix <(echo sudo id)",
    "bash --restricted <(echo sudo id)",
    # #444 review: the `<` of a positional `<(…)` is not a file redirect.
    "echo sudo id | bash -s <(:)",
    "echo sudo id | bash - <(:)",
    "bash 0< <(echo sudo id)",
    # #444 review: `--` ends `source`'s options.
    "source -- <(echo sudo id)",
    # #444 review: a shell in an output process substitution reads its producer.
    "echo sudo id > >(bash)",
    "echo sudo id | tee >(bash)",
    # #444 review: stdbuf's long value-taking switches.
    "stdbuf --output L sudo id",
    "stdbuf --input 0 --error L sudo id",
    # Pinned: these already pass (#444 review).
    "bash -- <(echo sudo id)",
    "bash -s < <(echo sudo id)",
)


@pytest.mark.parametrize("command", STDIN_OPERAND_HARD_COMMANDS)
def test_hard_rules_judge_stdin_operands_and_process_substitution(command: str) -> None:
    verdict = guard._hard_policy_verdict(command)
    assert verdict is not None and verdict.rule_id == "sudo-use-fleet-sudo"


@pytest.mark.usefixtures("windows_tokens")
@pytest.mark.parametrize("command", STDIN_OPERAND_HARD_COMMANDS)
def test_windows_tokenizing_judges_stdin_operands(command: str) -> None:
    verdict = guard._hard_policy_verdict(command)
    assert verdict is not None and verdict.rule_id == "sudo-use-fleet-sudo"


BENIGN_STDIN_OPERAND_COMMANDS = (
    "echo hi | bash -",
    "bash - x.sh",
    "diff <(sort a) <(sort b)",
    "bash <(curl -fsSL https://x/install.sh)",
    "source <(kubectl completion bash)",
    # The substitution is a positional word or data, not the script.
    "bash x.sh <(echo sudo id)",
    "bash -s <(echo sudo id)",
    "bash -c 'wc -l \"$1\"' _ <(echo sudo id)",
    "OMI_SUDO_OK=1 bash <(echo sudo id)",
    "setsid make test",
    "stdbuf -oL tail -f log",
    "stdbuf --output L tail -f log",
    "echo hi > >(bash)",
    "make 2>&1 | tee >(grep err)",
)


@pytest.mark.parametrize("command", BENIGN_STDIN_OPERAND_COMMANDS)
def test_hard_rules_leave_benign_stdin_operands_alone(command: str) -> None:
    assert guard._hard_policy_verdict(command) is None


@pytest.mark.usefixtures("windows_tokens")
@pytest.mark.parametrize("command", BENIGN_STDIN_OPERAND_COMMANDS)
def test_windows_tokenizing_leaves_benign_stdin_operands_alone(command: str) -> None:
    assert guard._hard_policy_verdict(command) is None


@pytest.mark.parametrize("tokenizing", ["posix", "windows"])
def test_unclosed_process_substitution_is_still_judged(
    tokenizing: str, request: pytest.FixtureRequest
) -> None:
    """#444 review: an unclosed `<(` runs to the end of the text, so the
    producer is still judged and nothing raises."""
    if tokenizing == "windows":
        request.getfixturevalue("windows_tokens")
    verdict = guard._hard_policy_verdict("bash <(echo sudo id")
    assert verdict is not None and verdict.rule_id == "sudo-use-fleet-sudo"
    assert guard._hard_policy_verdict("bash <(") is None


@pytest.mark.usefixtures("windows_tokens")
@pytest.mark.parametrize(("command", "rule_id"), WRAPPED_HARD_COMMANDS)
def test_windows_tokenizing_denies_wrapped_hard_rule_commands(
    command: str, rule_id: str | None
) -> None:
    """#430 on Windows: non-POSIX shlex split `<<<'x y'` and
    `--split-string='x y'` at the blank and kept the quotes, so the code in
    them was never judged and both forms ran."""
    verdict = guard._hard_policy_verdict(command)
    assert verdict is not None
    if rule_id is not None:
        assert verdict.rule_id == rule_id


@pytest.mark.usefixtures("windows_tokens")
@pytest.mark.parametrize("command", WRAPPED_BENIGN_COMMANDS)
def test_windows_tokenizing_keeps_benign_commands_allowed(command: str) -> None:
    assert guard._hard_policy_verdict(command) is None


@pytest.mark.usefixtures("windows_tokens")
@pytest.mark.parametrize(
    ("part", "tokens"),
    [
        ("bash <<<'x y'", ["bash", "<<<x y"]),
        ("env --split-string='x y'", ["env", "--split-string=x y"]),
        ('env -S"x y"', ["env", "-Sx y"]),
        ("bash -c 'x y'", ["bash", "-c", "x y"]),
        ('git -C "C:\\my repo" status', ["git", "-C", "C:\\my repo", "status"]),
        ("C:\\tools\\sed.exe -i s/a/b/ f", ["C:\\tools\\sed.exe", "-i", "s/a/b/", "f"]),
        ("echo '' #x", ["echo", "", "#x"]),
    ],
)
def test_windows_shell_tokens_join_mid_word_quotes_and_keep_backslashes(
    part: str, tokens: list[str]
) -> None:
    assert guard._shell_tokens(part) == tokens


@pytest.mark.parametrize(
    "command",
    [
        "timeout " * 400 + "x",
        "sudo -u " * 400 + "x",
        "nice -n " * 400 + "x",
        "nice -n -5 " * 400 + "x",
        "timeout 5 -k 1 " * 400 + "x",
        "command -v " * 400 + "x",
        "timeout 5 nice -n 1 env -u X sudo -u b " * 200 + "x",
        # The stdin and heredoc scans stay linear in the number of shells.
        "echo x | bash | " * 1000 + "cat",
        "a | " * 5000 + "bash",
        "cat <<E | bash\nx\nE\n" * 500,
        "bash -c '$@' _ x; " * 1000,
        "env -S x; " * 2000,
    ],
)
def test_wrapper_runs_cannot_backtrack_the_hard_rules(command: str) -> None:
    # Every wrapper word matches one way only, so long runs stay linear. A
    # hard SIGALRM bound (the #431 pattern) instead of a wall-clock assert.
    guard._shell_walk.cache_clear()
    with _hard_time_limit():
        guard._hard_policy_verdict(command)


def test_hard_rules_judge_only_the_command_when_the_walk_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(_command: str) -> object:
        raise RuntimeError("walk exploded")

    monkeypatch.setattr(guard, "_shell_walk", boom)
    assert guard._hard_policy_verdict("bash -c 'sudo id'") is None  # pre-#430 view
    verdict = guard._hard_policy_verdict("sudo id")
    assert verdict is not None and verdict.rule_id == "sudo-use-fleet-sudo"


@pytest.mark.parametrize("command", [c for c, _rule in WRAPPED_HARD_COMMANDS])
def test_guard_explain_agrees_with_check_on_wrapped_commands(
    command: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """#430 review: `explain` judged only the top-level text, so it printed
    ALLOW for `bash -c 'sudo id'` while `check` denied it."""
    assert guard._hard_policy_verdict(command) is not None
    assert guard.run_guard("explain", command=command) == 0
    out = capsys.readouterr().out
    assert f"DENY: {command}" in out


@pytest.mark.parametrize(
    "command", ["command -v sudo", "OMI_SUDO_OK=1 bash -c 'sudo id'", "tmux new -s 'sudo-test'"]
)
def test_guard_explain_agrees_with_check_on_benign_commands(
    command: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert guard._hard_policy_verdict(command) is None
    assert guard.run_guard("explain", command=command) == 0
    assert "DENY" not in capsys.readouterr().out


#: The audit's hard-rule probe table (``probe_classify``, #430): command and
#: whether a hard rule must deny it. Checked in so the claim is reproducible.
HARD_RULE_PROBES = (
    ("sudo rm -rf /x", True),
    ("ls; sudo rm -rf /x", True),
    ("$(sudo id)", True),
    ("FOO=1 sudo id", True),
    ("xargs sudo rm", True),
    ("if true; then sudo id; fi", True),
    ("OMI_SUDO_OK=1 sudo id", False),
    ("echo OMI_SUDO_OK=1 && sudo id", True),
    ("bash -c 'sudo rm -rf /x'", True),
    ('sh -c "sudo rm -rf /x"', True),
    ("eval 'sudo rm -rf /x'", True),
    ("echo 'sudo rm -rf /x' | bash", True),
    ("env sudo rm -rf /x", True),
    ("nice sudo rm -rf /x", True),
    ("timeout 5 sudo rm -rf /x", True),
    ("/usr/bin/env sudo rm -rf /x", True),
    ("gh repo delete o/r --yes", True),
    ("gh api -X DELETE repos/o/r", True),
    ("gh api repos/o/r -X DELETE", True),
    ("curl -X DELETE https://api.github.com/repos/o/r", True),
    ("bash -c 'gh repo delete o/r --yes'", True),
    ("grep -rn 'gh repo delete' docs", False),
    ("git commit -m 'never sudo'", False),
    ("fleet-sudo id", False),
    ("pkexec id", True),
    ("doas id", True),
    ("su -c id", True),
    ("ssh host sudo id", False),  # remote: deliberately not a local hard rule
    ("gh repo delete o/r --yes # OMI_SUDO_OK=1", True),
)


@pytest.mark.parametrize(("command", "denied"), HARD_RULE_PROBES)
def test_hard_rule_probe_table(command: str, denied: bool) -> None:
    assert (guard._hard_policy_verdict(command) is not None) is denied


@pytest.mark.parametrize(
    "command",
    [
        "timeout 5s -k 2s sed -i 's/a/b/' f",
        "timeout 5 -- sed -i 's/a/b/' f",
        "nice -n -5 sed -i 's/a/b/' f",
        "env -S sed -i 's/a/b/' f",
    ],
)
def test_stage_parser_reads_the_same_wrapper_shapes(command: str) -> None:
    """#430 review: the stage parser shares the wrapper table, so the shapes
    the hard rules now see also reach the editor behind them."""
    stages = guard._program_stages(policy.shell_code_text(command), command)
    assert [program for program, *_ in stages] == ["sed"]
    assert guard._is_repo_sensitive_action({"tool": "Bash", "command": command})


# --- #434: repo-work classifier gaps -----------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "bash -c 'sed -i s/a/b/ f'",
        'bash -c "git commit -m x"',
        "sh -c 'perl -pi -e s/a/b/ f'",
        "eval 'git add -A'",
        "bash -c \"sh -c 'sed -i s/a/b/ f'\"",
        "zsh -c 'python3 -m pytest -q'",
    ],
)
def test_shell_c_and_eval_bodies_are_classified_as_repo_work(command: str) -> None:
    """#434: a `-c`/`eval` body is blanked as a string literal in its caller,
    but this shell runs it, so the stage classifier judges it too."""
    assert guard._is_repo_sensitive_action({"tool": "Bash", "command": command})


def test_bash_c_commit_trips_the_freshness_gate() -> None:
    """#434: `bash -c "git commit …"` also skipped the freshness demand."""
    assert guard._is_commit_action({"tool": "Bash", "command": 'bash -c "git commit -m x"'})
    assert not guard._is_commit_action({"tool": "Bash", "command": "bash -c 'git status'"})


@pytest.mark.parametrize(
    "command",
    ["bash -c 'git status'", "bash -c 'ls >/dev/null 2>&1'", "eval 'echo hi'"],
)
def test_shell_c_bodies_that_only_read_stay_out_of_repo_work(command: str) -> None:
    assert not guard._is_repo_sensitive_action({"tool": "Bash", "command": command})


#: Interpreter spellings that classify identically (#434 review): one
#: python rule for all of them, run wrappers included.
_PYTHON_SPELLINGS = (
    "python",
    "python3",
    "python3.12",
    ".venv/bin/python3",
    "/usr/bin/python",
    "poetry run python",
    "uv run python3",
)


@pytest.mark.parametrize("interp", _PYTHON_SPELLINGS)
@pytest.mark.parametrize(
    ("args", "sensitive"),
    [
        # A test module, with or without interpreter flags before `-m`.
        ("-m pytest tests/", True),
        ("-m unittest discover", True),
        ("-m tox", True),
        ("-m nox", True),
        ("-X dev -m pytest", True),
        ("-B -m pytest -q", True),
        ("-mpytest", True),
        ("-Bm pytest", True),
        # A script-file operand.
        ("tests/test_x.py", True),
        ("run.py --flag", True),
        ("-u run.py", True),
        # Neither: a `-c` body, another module, stdin, a REPL, a version.
        ("-c 'print(1)'", False),
        ("-m json.tool", False),
        ("-m http.server", False),
        ("-m pip list", False),
        ("-mjson.tool", False),
        ("-", False),
        ("- < in.txt", False),
        ("< run.py", False),
        ("-i", False),
        ("--version", False),
        ("-X dev -c 'print(1)'", False),
    ],
)
def test_every_python_spelling_follows_one_rule(interp: str, args: str, sensitive: bool) -> None:
    """#434 review: `python`, `python3`, `python3.N`, a path-prefixed
    interpreter and `poetry run`/`uv run` python are judged by one rule: a test
    module (`-m pytest|unittest|tox|nox`) or a script-file operand is repo
    work; `-c`, any other module, stdin and a heredoc are not (those stay the
    script-write and commit detectors' business). Bare `python` used to count
    whatever it ran, and `python3 tests/test_x.py` used to miss."""
    for command in (f"{interp} {args}", f"cd x && {interp} {args}", f"ls | {interp} {args}"):
        action = {"tool": "Bash", "command": command}
        assert guard._is_repo_sensitive_action(action) is sensitive, command


@pytest.mark.parametrize(
    "command",
    [
        "python tests/test_x.py",
        "python3 -X dev -m pytest",
        ".venv/bin/python3 -m pytest",
        "python3 -mpytest",
    ],
)
def test_python_test_runs_are_repo_work(command: str) -> None:
    assert guard._is_repo_sensitive_action({"tool": "Bash", "command": command})


@pytest.mark.parametrize(
    "command",
    [
        "python -c 'print(1)'",
        "python -m json.tool",
        "python - <<'EOF'\nprint(1)\nEOF",
        "python3 <<'EOF'\nprint(1)\nEOF",
    ],
)
def test_python_reads_are_not_repo_work(command: str) -> None:
    """#434 review: bare `python` is narrowed to match `python3`."""
    assert not guard._is_repo_sensitive_action({"tool": "Bash", "command": command})


def test_python_heredoc_that_writes_is_still_repo_work() -> None:
    """The script-write detector still judges a heredoc body (#391)."""
    command = "python - <<'EOF'\nopen('x', 'w').write('y')\nEOF"
    assert guard._is_repo_sensitive_action({"tool": "Bash", "command": command})


#: Wrappers from the stage-wrapper table that exec their trailing command,
#: with switches (#434 review: git/gh/test runners behind them were missed).
_WRAPPER_PREFIXES = (
    "chronic",
    "caffeinate",
    "caffeinate -t 60",
    "stdbuf -oL",
    "stdbuf -o L",
    "ionice -c 3",
    "nice -n 5",
    "nice",
    "timeout 5",
    "env FOO=1",
    "time",
    "sudo -u bob",
    "xargs -n 1",
    "/usr/bin/env",
)


@pytest.mark.parametrize("wrapper", _WRAPPER_PREFIXES)
@pytest.mark.parametrize(
    "command",
    [
        "git commit -m x",
        "git push",
        "git -C . add -A",
        "gh pr create",
        "pytest -q",
        "python3 -m pytest",
        ".venv/bin/python3 -m pytest",
    ],
)
def test_git_gh_and_test_runners_behind_wrappers_are_repo_work(wrapper: str, command: str) -> None:
    """#434 review: the git-verb, `gh`, test-runner and commit patterns are
    matched against each stage's program, after wrappers are peeled."""
    for text in (f"{wrapper} {command}", f"cd x && {wrapper} {command}"):
        action = {"tool": "Bash", "command": text}
        assert guard._is_repo_sensitive_action(action), text
        assert guard._is_commit_action(action) is (" commit" in command), text


def test_wrapped_read_only_git_is_not_repo_work() -> None:
    for command in ("chronic git status", "stdbuf -oL git log -1", "nice -n 5 git diff"):
        action = {"tool": "Bash", "command": command}
        assert not guard._is_repo_sensitive_action(action), command
        assert not guard._is_commit_action(action), command


@pytest.mark.parametrize(
    "command", ['bash -c "git commit -m x"', "chronic git commit -m x", "nice -n 5 git commit -m x"]
)
def test_wrapped_commit_gets_the_freshness_verdict_end_to_end(tmp_path: Path, command: str) -> None:
    """#434 review: not only `_is_commit_action`: the full check demands a
    fresh base for a commit hidden in a `-c` body or behind a wrapper."""
    repo = tmp_path / "wrapped"
    repo.mkdir()
    _git_init(repo)
    subprocess.run(
        ["git", "-C", str(repo), "remote", "add", "origin", "https://x.invalid/y.git"],
        check=True,
    )
    session = "wrapped-commit-fresh"
    guard.clear_gate(session)
    guard.record_consult(session, kind="read", target=guard.GIT_RULES_NOTE, relevant=True)
    verdict = guard.decide(
        {"tool": "Bash", "command": command, "cwd": repo.as_posix(), "session": session}
    )
    assert not verdict.allow
    assert verdict.rule_id == "repo-work-fresh-base"
    guard.clear_gate(session)


def _raise(*_args: object, **_kwargs: object) -> None:
    raise RuntimeError("walker exploded")


def test_local_code_texts_fails_open_when_the_walk_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#434 review (AGENTS.md invariant 2): a walker crash degrades to the
    command alone, it never raises into the agent."""
    monkeypatch.setattr(guard, "_shell_walk", _raise)
    assert guard._local_code_texts("bash -c 'git commit -m x'") == ["bash -c 'git commit -m x'"]
    action = {"tool": "Bash", "command": "git commit -m x"}
    assert guard._is_repo_sensitive_action(action)
    assert guard._is_commit_action(action)


def test_writes_into_repo_fails_open_when_the_walk_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, outside = _write_probe_repo(tmp_path)
    monkeypatch.setattr(guard, "_shell_walk", _raise)
    action = _repo_write_action("echo x > src/x.py", repo, outside)
    assert guard._writes_into_repo(action) is False
    assert guard._is_repo_sensitive_action(action) is False


def test_stage_code_texts_fails_open_when_the_stage_split_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(guard, "_program_stages", _raise)
    assert guard._stage_code_texts("chronic git commit -m x") == ["chronic git commit -m x"]


@pytest.mark.parametrize(
    "command",
    [
        "stdbuf -oL sed -i 's/a/b/' f",
        "stdbuf -o L sed -i 's/a/b/' f",
        "caffeinate sed -i 's/a/b/' f",
        "caffeinate -t 60 sed -i 's/a/b/' f",
        "ionice -c 3 sed -i 's/a/b/' f",
        "chronic sed -i 's/a/b/' f",
    ],
)
def test_more_stage_wrappers_reach_the_program_behind_them(command: str) -> None:
    """#434: stdbuf/caffeinate/ionice/chronic exec their trailing command."""
    stages = guard._program_stages(policy.shell_code_text(command), command)
    assert [program for program, *_ in stages] == ["sed"]
    assert guard._is_repo_sensitive_action({"tool": "Bash", "command": command})


def _write_probe_repo(tmp_path: Path) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (repo / "src").mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    return repo, outside


#: Commands that write or remove a file inside the repo (#434).
_WRITES_INTO_REPO = (
    "echo x > src/x.py",
    "echo x >> src/x.py",
    "echo x >src/x.py",
    "echo x 1> src/x.py",
    "echo x &> src/x.py",
    "echo x >| src/x.py",
    "echo x > 'src/x y.py'",
    "cat > src/x.py <<'EOF'\nprint(1)\nEOF",
    "echo x > {repo}/src/x.py",
    "cd src && echo x > x.py",
    "cd {outside} && echo x > {repo}/x",
    "tee src/x.py < in",
    "ls | tee -a src/log.txt",
    "ls | tee {outside}/a.log src/b.log",
    "cp a src/b",
    "cp {outside}/a {repo}/src/b",
    "cp -t src {outside}/a",
    "cp --target-directory=src {outside}/a",
    "mv a src/b",
    "mv src/a {outside}/b",
    "rm src/x.py",
    "rm -rf -- src",
    "stdbuf -oL rm {repo}/src/x.py",
    "bash -c 'echo x > src/x.py'",
    "bash -c 'cd src && rm x.py'",
    # #434 review: `>&word` and `>& word` write the file (stdout and stderr).
    "echo x >& src/x.py",
    "echo x >&src/x.py",
    # A placeholder is not a target, but a real destination still is.
    "find {outside} -exec cp {{}} src/ \\;",
    # A quote inside the "(( … ))" region: not arithmetic, the write stands.
    "echo '((' > src/x; echo '))'",
)

#: Commands whose writes all land OUTSIDE the repo: they must not count (the
#: #412 false-positive class must not grow).
_WRITES_OUTSIDE_REPO = (
    "ls > /dev/null",
    "ls >/dev/null 2>&1",
    "echo hi 2>&1",
    "ls 1>/dev/null 2>/dev/null",
    "echo x >&2",
    "ls &> /dev/null",
    "echo x > {outside}/out.log",
    "pwd >> {outside}/a",
    "ls | tee {outside}/log.txt",
    "ls | tee -a {outside}/log.txt",
    "cp src/a {outside}/b",
    "rm -rf {outside}/scratch",
    "mv {outside}/a {outside}/b",
    "cd {outside} && echo x > y",
    "echo 'a > b'",
    "grep -rn '>' src",
    "diff <(ls) <(ls src)",
    "echo x > $OUT",
    "rm $TMP/x",
    "cat src/x.py",
    "ls src | grep x",
    "bash -c 'ls > /dev/null'",
    # #434 review: a `>` that compares is not a redirect.
    "[[ a > b ]] && echo y",
    "(( n > 3 )) && echo y",
    "if (( $# > 1 )); then echo y; fi",
    "[ a \\> b ]",
    "echo $(( 3 > 1 ))",
    "bash -c '[[ a > b ]]'",
    # #434 review: `find -exec` placeholders are never file operands.
    "find /var/log -name '*.old' -exec rm {{}} +",
    "find /var/log -name '*.old' -exec rm {{}} \\;",
)


def _repo_write_action(command: str, repo: Path, outside: Path) -> dict[str, object]:
    command = command.format(repo=repo.as_posix(), outside=outside.as_posix())
    return {"tool": "Bash", "command": command, "cwd": repo.as_posix()}


@pytest.mark.parametrize("command", _WRITES_INTO_REPO)
def test_redirects_and_file_ops_into_the_repo_are_repo_work(tmp_path: Path, command: str) -> None:
    """#434: a redirect, `tee`, `cp`, `mv` or `rm` aimed inside the target
    repo writes the repo as surely as `sed -i` does."""
    repo, outside = _write_probe_repo(tmp_path)
    assert guard._is_repo_sensitive_action(_repo_write_action(command, repo, outside))


@pytest.mark.parametrize("command", _WRITES_OUTSIDE_REPO)
def test_redirects_and_file_ops_outside_the_repo_are_not_repo_work(
    tmp_path: Path, command: str
) -> None:
    repo, outside = _write_probe_repo(tmp_path)
    assert not guard._is_repo_sensitive_action(_repo_write_action(command, repo, outside))


@pytest.mark.usefixtures("windows_tokens")
@pytest.mark.parametrize("command", _WRITES_INTO_REPO)
def test_windows_tokenizing_redirects_into_the_repo_are_repo_work(
    tmp_path: Path, command: str
) -> None:
    repo, outside = _write_probe_repo(tmp_path)
    assert guard._is_repo_sensitive_action(_repo_write_action(command, repo, outside))


@pytest.mark.usefixtures("windows_tokens")
@pytest.mark.parametrize("command", _WRITES_OUTSIDE_REPO)
def test_windows_tokenizing_redirects_outside_the_repo_are_not_repo_work(
    tmp_path: Path, command: str
) -> None:
    repo, outside = _write_probe_repo(tmp_path)
    assert not guard._is_repo_sensitive_action(_repo_write_action(command, repo, outside))


#: #450: more write tools whose target lands inside the repo.
_MORE_WRITE_TOOLS_INTO_REPO = (
    "install {outside}/a src/b",
    "install -m 644 {outside}/a {repo}/src/b",
    "install -m644 -o root {outside}/a src/b",
    "install -t src {outside}/a {outside}/b",
    "install --target-directory=src {outside}/a",
    "install -d {outside}/d src/new",
    "install -dm755 src/new",
    "dd if={outside}/a of=src/b bs=1M",
    "dd if=/dev/zero of={repo}/src/blob count=1",
    "truncate -s 0 src/x.py",
    "truncate --size 10 {outside}/a src/x.py",
    "truncate -r {outside}/ref src/x.py",
    "touch src/x.py",
    "touch -d yesterday src/x.py",
    "touch -t 202601010000 {outside}/a src/x.py",
    "touch -r {outside}/ref src/x.py",
    "ln -s {outside}/a src/link",
    "ln -sf {outside}/a {repo}/src/link",
    "ln -t src {outside}/a",
    "ln -s {outside}/a",
    "rsync -a {outside}/a src/",
    "rsync -av -e ssh --exclude .git {outside}/d/ {repo}/src/",
    "find {outside} -name '*.py' -exec touch {{}} src/stamp \\;",
    # #450 review: a trailing value switch has no value and adds nothing.
    "touch src/x.py -d",
    "install {outside}/a src/b -m",
    # `--remove-source-files` deletes the sources, wherever they go.
    "rsync -a --remove-source-files src/a {outside}/",
    "rsync --remove-source-files src/a host:backup/",
    "rsync -a --remove-sou src/a {outside}/",
    # Out-of-band writes: their values are local write targets.
    "rsync -a --backup-dir=src {outside}/a {outside}/b",
    "rsync -a --backup-dir src {outside}/a {outside}/b",
    "rsync -a --log-file=src/log.txt {outside}/a {outside}/b",
    "rsync -a --log-file=src/log.txt {outside}/a host:b",
    "rsync -a --temp-dir src {outside}/a {outside}/b",
    "rsync -a -T src {outside}/a {outside}/b",
    "rsync -a -Tsrc {outside}/a {outside}/b",
    "rsync -a --partial-dir=src {outside}/a {outside}/b",
    # GNU getopt takes any unambiguous prefix of a long option.
    "cp --target src {outside}/a",
    "install --target src {outside}/a",
    "install --dir src/new {outside}/x",
    "rsync -a --backup-d=src {outside}/a {outside}/b",
)

#: #450: the same tools aimed outside the repo, or at a remote/device path.
_MORE_WRITE_TOOLS_OUTSIDE_REPO = (
    "install src/a {outside}/b",
    "install -m 644 src/a {outside}/b",
    "install -t {outside} src/a src/b",
    "install -d {outside}/d",
    "dd if=src/a of={outside}/b",
    "dd if=src/a of=/dev/null",
    "dd if=src/a",
    "truncate -s 0 {outside}/a",
    "truncate -r src/x.py {outside}/a",
    "touch {outside}/a",
    "touch -r src/x.py {outside}/a",
    "touch -d src {outside}/a",
    "ln -s src/a {outside}/link",
    "ln -t {outside} src/a",
    "cd {outside} && ln -s {repo}/src/a",
    "rsync -a src/ {outside}/copy/",
    "rsync -a --exclude src {outside}/a {outside}/b",
    "rsync -a src/ host:backup/",
    "rsync -a src/ user@host:{repo}/src/",
    "rsync -a src/ rsync://host/module/",
    "rsync -a src/ host::module",
    "rsync src",
    "find {outside} -exec touch {{}} +",
    # #450 review: each case fails if its table entry is removed, because
    # the trailing value would otherwise be read as a target in the cwd.
    "rsync -a src/ {outside}/copy/ --exclude x",
    "rsync -a src/ {outside}/copy/ -e ssh",
    "install src/a {outside}/b -m 644",
    "rsync -a src/ {outside}/copy/ --copy-as user",
    "rsync -a src/ {outside}/copy/ --max-alloc 1G",
    "rsync -a src/ {outside}/copy/ --early-input x",
    # A value switch at the very end has no value: never a target.
    "install src/a {outside}/b -t",
    "touch {outside}/a -d",
    "cp src/a {outside}/b --target",
    # A long flag that is a prefix of a value option is not abbreviating it.
    "rsync -a --partial {outside}/a src {outside}/b",
    "rsync -a --backup {outside}/a src {outside}/b",
    # Receiver-side writes of a remote destination happen remotely.
    "rsync -a --backup-dir=src {outside}/a host:b",
    "rsync -a --remove-source-files host:src/a {outside}/",
)


@pytest.mark.parametrize("command", _MORE_WRITE_TOOLS_INTO_REPO)
def test_more_write_tools_into_the_repo_are_repo_work(tmp_path: Path, command: str) -> None:
    """#450: `install`, `dd of=`, `truncate`, `touch`, `ln` and `rsync` aimed
    inside the target repo write it as surely as `cp` does."""
    repo, outside = _write_probe_repo(tmp_path)
    assert guard._is_repo_sensitive_action(_repo_write_action(command, repo, outside))


@pytest.mark.parametrize("command", _MORE_WRITE_TOOLS_OUTSIDE_REPO)
def test_more_write_tools_outside_the_repo_are_not_repo_work(
    tmp_path: Path, command: str
) -> None:
    """#450: a target outside the repo, a device, or a remote rsync
    destination is not repo work."""
    repo, outside = _write_probe_repo(tmp_path)
    assert not guard._is_repo_sensitive_action(_repo_write_action(command, repo, outside))


@pytest.mark.usefixtures("windows_tokens")
@pytest.mark.parametrize("command", _MORE_WRITE_TOOLS_INTO_REPO)
def test_windows_tokenizing_more_write_tools_into_the_repo_are_repo_work(
    tmp_path: Path, command: str
) -> None:
    repo, outside = _write_probe_repo(tmp_path)
    assert guard._is_repo_sensitive_action(_repo_write_action(command, repo, outside))


@pytest.mark.usefixtures("windows_tokens")
@pytest.mark.parametrize("command", _MORE_WRITE_TOOLS_OUTSIDE_REPO)
def test_windows_tokenizing_more_write_tools_outside_the_repo_are_not_repo_work(
    tmp_path: Path, command: str
) -> None:
    repo, outside = _write_probe_repo(tmp_path)
    assert not guard._is_repo_sensitive_action(_repo_write_action(command, repo, outside))


@pytest.mark.parametrize(
    ("word", "remote"),
    [
        ("host:path", True),
        ("user@host:/abs", True),
        ("host::module", True),
        ("rsync://host/module", True),
        ("C:/repo/src", False),
        ("C:\\repo\\src", False),
        ("./a:b", False),
        ("src/", False),
    ],
)
def test_rsync_remote_destination(word: str, remote: bool) -> None:
    """#450: a `host:path` or `rsync://` destination is remote, a drive is not."""
    assert guard._rsync_remote(word) is remote


def test_single_operand_ln_with_an_unknown_cwd_is_not_repo_work(tmp_path: Path) -> None:
    """#450 review: `ln -s TARGET` links into the cwd; when the cwd is not
    knowable, the link's location is not judged."""
    repo, outside = _write_probe_repo(tmp_path)
    action = _repo_write_action('cd "$X" && ln -s {outside}/a', repo, outside)
    assert guard._writes_into_repo(action) is False
    assert not guard._is_repo_sensitive_action(action)


def test_unclosed_quote_in_a_file_op_degrades_and_the_next_stage_is_judged(
    tmp_path: Path,
) -> None:
    """#450 review (AGENTS.md invariant 2): an unbalanced quote makes a
    site's words unreadable; it yields no target instead of raising, and a
    following valid stage is still judged."""
    repo, outside = _write_probe_repo(tmp_path)
    assert guard._file_op_targets("touch", "touch 'unclosed") == []
    assert guard._writes_into_repo(_repo_write_action("touch 'unclosed", repo, outside)) is False
    into = _repo_write_action('bash -c "touch \'unclosed" && touch src/x.py', repo, outside)
    assert guard._writes_into_repo(into) is True
    away = _repo_write_action('bash -c "touch \'unclosed" && touch {outside}/x', repo, outside)
    assert guard._writes_into_repo(away) is False


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("--target-directory", "--target-directory"),
        ("--target", "--target-directory"),
        ("--t", "--target-directory"),
        ("--s", "--s"),  # ambiguous: --suffix, --sparse... left as a flag
        ("--", "--"),
        ("--nope", "--nope"),
    ],
)
def test_long_option_unique_prefix(given: str, expected: str) -> None:
    """#450 review: one helper resolves GNU unique-prefix long options."""
    known = frozenset({"--target-directory", "--suffix", "--sparse"})
    assert guard._long_option(given, known) == expected


def test_redirect_with_no_target_repo_is_not_repo_work(tmp_path: Path) -> None:
    """No target repo, nothing to protect: the redirect check fails open."""
    action = {"tool": "Bash", "command": "echo x > x.py", "cwd": tmp_path.as_posix()}
    assert not guard._writes_into_repo(action)


def _two_probe_repos(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Repo A, repo B and a directory in neither (#448)."""
    a, outside = _write_probe_repo(tmp_path)
    b = tmp_path / "b"
    (b / ".git").mkdir(parents=True)
    (b / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    return a, b, outside


def test_write_into_the_cwd_repo_counts_after_cd_into_another_repo(tmp_path: Path) -> None:
    """#448: the command-level repo follows the `cd` into B, but the redirect
    ran in A, and A is a repo: the write is repo work for A."""
    a, b, _outside = _two_probe_repos(tmp_path)
    action = {
        "tool": "Bash",
        "command": f"echo x > README.md && cd {b.as_posix()}",
        "cwd": a.as_posix(),
    }
    assert guard._repo_root_for_action(action) == b
    assert guard._writes_into_repo(action)
    assert guard._bash_write_repo(action) == a.resolve()
    assert guard._is_repo_sensitive_action(action)
    session = "448-shape-a"
    guard.clear_gate(session)
    guard.mark_consulted(session)
    verdict = guard.decide({**action, "session": session})
    assert not verdict.allow
    assert verdict.rule_id == "repo-work-read-git-rules"
    guard.clear_gate(session)


def _consulted_git_rules(session: str) -> None:
    guard.clear_gate(session)
    guard.record_consult(session, kind="read", target=guard.GIT_RULES_NOTE, relevant=True)


def test_commit_freshness_stays_on_the_cd_repo_when_writing_another(tmp_path: Path) -> None:
    """#448 review: `cd B && git commit && echo y > A/f` commits in B. With B
    fresh and A stale, the command is judged against B's freshness only."""
    a = _mk_repo(tmp_path, "a")
    b = _mk_repo(tmp_path, "b")
    session = "448-fresh-b"
    _consulted_git_rules(session)
    guard._record_git_freshness(session, b, f"git -C {b} fetch")
    command = f"cd {b.as_posix()} && git commit -m x && echo y > {a.as_posix()}/f"
    verdict = guard.decide(
        {"tool": "Bash", "command": command, "cwd": a.as_posix(), "session": session}
    )
    assert verdict.allow, verdict.reason
    guard.clear_gate(session)


def test_written_repo_makes_no_freshness_demand(tmp_path: Path) -> None:
    """#448 review item 2: from a cwd in no repo, a commit after a non-literal
    `cd "$D"` resolves no repo of its own. The repo it also writes into opens
    the consult gate, but the commit does not land there, so no freshness is
    demanded of it (as on main)."""
    a = _mk_repo(tmp_path, "a")
    outside = tmp_path / "outside"
    outside.mkdir()
    command = f'cd "$D" && git commit -m x && echo y > {a.as_posix()}/f'
    action = {"tool": "Bash", "command": command, "cwd": outside.as_posix()}
    assert guard._repo_root_for_action(action) is None
    assert guard._bash_write_repo(action) == a
    session = "448-no-fresh-a"
    guard.clear_gate(session)
    guard.mark_consulted(session)
    gated = guard.decide({**action, "session": session})
    assert gated.rule_id == "repo-work-read-git-rules"
    _consulted_git_rules(session)
    verdict = guard.decide({**action, "session": session})
    assert verdict.allow, verdict.reason
    guard.clear_gate(session)


def test_write_into_a_submodule_resolves_the_inner_repo(tmp_path: Path) -> None:
    """#448 review: a target under a nested worktree (a submodule's `.git`
    pointer file) resolves to the inner repo, even from the outer repo's cwd."""
    outer = _mk_repo(tmp_path, "outer")
    inner = outer / "sub"
    inner.mkdir()
    (inner / ".git").write_text("gitdir: ../.git/modules/sub\n", encoding="utf-8")
    action = {"tool": "Bash", "command": "echo x > sub/x.py", "cwd": outer.as_posix()}
    assert guard._repo_root_for_action(action) == outer
    assert guard._bash_write_repo(action) == inner


def test_mixed_targets_still_gate_on_the_repo_one(tmp_path: Path) -> None:
    """#448 review (Dixie): a target in no repo before one in a repo does not
    end the search."""
    repo, _b, outside = _two_probe_repos(tmp_path)
    command = f"echo x > {outside.as_posix()}/out && echo y > {repo.as_posix()}/README.md"
    action = {"tool": "Bash", "command": command, "cwd": outside.as_posix()}
    assert guard._bash_write_repo(action) == repo.resolve()
    session = "448-mixed"
    guard.clear_gate(session)
    guard.mark_consulted(session)
    verdict = guard.decide({**action, "session": session})
    assert not verdict.allow
    assert verdict.rule_id == "repo-work-read-git-rules"
    guard.clear_gate(session)


@pytest.mark.parametrize(
    ("command", "gated"),
    [
        ("sed -i 's/a/b/' {repo}/README.md", True),
        ("sed -i.bak -e 's/a/b/' -e 's/c/d/' {repo}/README.md", True),
        ("sed -i '' 's/a/b/' {repo}/README.md", True),
        ("sed --in-place --expression='s/a/b/' {repo}/README.md", True),
        ("sed -n -i 's/a/b/' {outside}/f {repo}/README.md", True),
        ("perl -pi -e 's/a/b/' {repo}/x", True),
        ("perl -i -pe 's/a/b/' {repo}/x", True),
        ("sed -i 's/a/b/' {outside}/f", False),
        ("perl -pi -e 's/a/b/' {outside}/f", False),
        # No in-place flag: a stdout filter writes nothing.
        ("sed 's/a/b/' {repo}/README.md", False),
    ],
)
def test_in_place_editors_into_a_repo_from_outside_are_gated(
    tmp_path: Path, command: str, gated: bool
) -> None:
    """#448 review item 1: `sed -i`/`perl -pi` on a file in a repo, run from a
    cwd in no repo, is gated like the Edit tool on that path; on a file in no
    repo it is not."""
    repo, _b, outside = _two_probe_repos(tmp_path)
    command = command.format(repo=repo.as_posix(), outside=outside.as_posix())
    action = {"tool": "Bash", "command": command, "cwd": outside.as_posix()}
    assert (guard._bash_write_repo(action) == repo.resolve()) is gated
    session = "448-in-place"
    guard.clear_gate(session)
    guard.mark_consulted(session)
    verdict = guard.decide({**action, "session": session})
    assert verdict.allow is not gated
    if gated:
        assert verdict.rule_id == "repo-work-read-git-rules"
    guard.clear_gate(session)


@pytest.mark.parametrize(
    ("program", "args", "expected"),
    [
        ("sed", ["-i", "s/a/b/", "f"], ["f"]),
        ("sed", ["-i", "", "s/a/b/", "f"], ["f"]),
        ("sed", ["-i.bak", "s/a/b/", "f", "g"], ["f", "g"]),
        ("sed", ["-e", "s/a/b/", "-i", "f"], ["f"]),
        ("sed", ["-f", "script.sed", "-i", "f"], ["f"]),
        ("sed", ["--expression=s/a/b/", "--in-place", "f"], ["f"]),
        ("sed", ["-i", "--", "s/a/b/", "-f"], ["-f"]),
        ("sed", ["s/a/b/", "f"], None),
        ("perl", ["-pi", "-e", "s/a/b/", "f"], ["f"]),
        ("perl", ["-i.orig", "-p", "script.pl", "f"], ["f"]),
        ("ruby", ["-pi", "-e", "x", "f"], ["f"]),
        ("grep", ["-i", "x", "f"], None),
    ],
)
def test_in_place_edit_operands(program: str, args: list[str], expected: list[str] | None) -> None:
    """#448 review item 1: the file operands come after the script, or are
    every positional word when `-e`/`-f` supplied the script."""
    assert guard._in_place_edit_operands(program, args) == expected


def test_heredoc_write_into_a_repo_from_outside_any_repo_is_gated(tmp_path: Path) -> None:
    """#448: from a cwd in no repo, `cat > <repo>/src/x.py <<EOF` writes the
    repo exactly as the Write tool on that path does, so it hits the same
    git-rules gate."""
    repo, _b, outside = _two_probe_repos(tmp_path)
    command = f"cat > {repo.as_posix()}/src/x.py <<'EOF'\nprint(1)\nEOF"
    action = {"tool": "Bash", "command": command, "cwd": outside.as_posix()}
    assert guard._repo_root_for_action(action) is None
    assert guard._writes_into_repo(action)
    assert guard._bash_write_repo(action) == repo.resolve()
    session = "448-heredoc-from-outside"
    guard.clear_gate(session)
    guard.mark_consulted(session)
    verdict = guard.decide({**action, "session": session})
    assert not verdict.allow
    assert verdict.rule_id == "repo-work-read-git-rules"
    guard.clear_gate(session)


@pytest.mark.parametrize(
    "command",
    [
        "echo x > {outside}/y",
        "echo x > y",
        "cp a {outside}/b",
        "rm -rf {outside}/scratch",
        "echo x > /dev/null",
        "echo x > $OUT",
    ],
)
def test_writes_outside_every_repo_do_not_count(tmp_path: Path, command: str) -> None:
    """#448: a target in no repo is not repo work, whatever the cwd."""
    _a, _b, outside = _two_probe_repos(tmp_path)
    action = {
        "tool": "Bash",
        "command": command.format(outside=outside.as_posix()),
        "cwd": outside.as_posix(),
    }
    assert not guard._writes_into_repo(action)
    assert guard._bash_write_repo(action) is None
    session = "448-outside"
    guard.clear_gate(session)
    guard.mark_consulted(session)
    assert guard.decide({**action, "session": session}).allow
    guard.clear_gate(session)


def test_bash_write_repo_fails_open(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """#448 (AGENTS.md invariant 2): a crash in the walk or the repo lookup
    degrades to "no repo written", never an exception."""
    repo, _b, outside = _two_probe_repos(tmp_path)
    action = {
        "tool": "Bash",
        "command": f"echo x > {repo.as_posix()}/src/x.py",
        "cwd": outside.as_posix(),
    }
    monkeypatch.setattr(guard, "_is_worktree_root", _raise)
    assert guard._bash_write_repo(action) is None
    assert guard._writes_into_repo(action) is False
    monkeypatch.undo()
    monkeypatch.setattr(guard, "_shell_walk", _raise)
    assert guard._bash_write_repo(action) is None
    assert guard._writes_into_repo(action) is False


def test_target_repo_walk_is_memoised_per_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#448: every target of one command shares one memo, so many targets in
    one tree check each directory once; the walk has no depth bound, like the
    Write tool's."""
    repo, _b, outside = _two_probe_repos(tmp_path)
    scratch = outside / "a" / "b" / "c"
    deep = repo.joinpath(*(f"d{i}" for i in range(80)))
    calls: list[Path] = []
    real = guard._is_worktree_root

    def counting(path: Path) -> bool:
        calls.append(path)
        return real(path)

    monkeypatch.setattr(guard, "_is_worktree_root", counting)
    targets = " ".join(f"{scratch.as_posix()}/f{i}" for i in range(50))
    command = f"touch {targets} {deep.as_posix()}/x"
    action = {"tool": "Bash", "command": command, "cwd": outside.as_posix()}
    assert guard._bash_write_repo(action) == repo.resolve()
    assert len(calls) == len(set(calls))  # every directory is checked once
