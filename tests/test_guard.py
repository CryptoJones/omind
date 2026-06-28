# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Aaron K. Clark
"""Tests for the harness-agnostic OMI-compliance guard decision engine."""

from __future__ import annotations

import importlib.resources
import io
import json
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


def test_omi_consult_is_allowed_and_sets_the_per_turn_sentinel() -> None:
    guard.clear_gate("s1")
    assert guard.decide({"is_omi_consult": True, "session": "s1"}).allow
    assert guard.consulted_this_turn("s1")
    guard.clear_gate("s1")


def test_hard_block_fires_even_when_consulted() -> None:
    guard.mark_consulted("s2")  # gate is satisfied, yet a hard rule still wins
    verdict = guard.decide({"tool": "Bash", "command": "gh pr merge 9", "session": "s2"})
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
        "git push https://github.com/x/y.git main",
        "git push github main",
        "gh pr create --title x",
        "gh repo delete x/y",
        "gh api -X DELETE repos/x/y",
    ):
        assert not guard.decide({"command": cmd, "session": "s4"}).allow, cmd
    guard.clear_gate("s4")


def test_codeberg_push_is_allowed_after_consult() -> None:
    guard.mark_consulted("s5")
    cmd = "git push git@codeberg.org:CryptoJones/omind.git main"
    assert guard.decide({"command": cmd, "session": "s5"}).allow
    guard.clear_gate("s5")


def test_github_push_is_opt_in_not_hard() -> None:
    guard.mark_consulted("s7")
    bare = "git push https://x@github.com/CryptoJones/omind.git main"
    assert not guard.decide({"command": bare, "session": "s7"}).allow  # blocked by default
    optin = "OMI_PUSH_GITHUB=1 " + bare
    assert guard.decide({"command": optin, "session": "s7"}).allow  # deliberate push allowed
    # the opt-in does NOT bypass the absolute hard rules
    assert not guard.decide(
        {"command": "OMI_PUSH_GITHUB=1 gh pr create --title x", "session": "s7"}
    ).allow
    assert not guard.decide(
        {"command": "OMI_PUSH_GITHUB=1 gh repo delete x/y", "session": "s7"}
    ).allow
    guard.clear_gate("s7")


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
    assert "gh-pr-create-merge" in out and "seed" in out
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
    guard.mark_consulted("optf")
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


def test_opt_in_is_recognized_on_a_newline_led_line_in_a_multiline_command() -> None:
    """3.0.2 regression: a newline is a shell command boundary, so an opt-in assignment
    at the START of a line inside a multi-line script is a real leading assignment and
    must satisfy — the 2.46.0 separator class omitted `\\n` and wrongly re-blocked it."""
    guard.mark_consulted("mlopt")
    multiline = "git fetch codeberg main\n  OMI_PUSH_GITHUB=1 git push --force github main"
    assert guard.decide({"command": multiline, "session": "mlopt"}).allow
    # the forgery guard still holds: a mid-line (space-led) token is NOT a leading assignment
    assert not guard.decide(
        {"command": "git push github main\necho OMI_PUSH_GITHUB=1", "session": "mlopt"}
    ).allow
    guard.clear_gate("mlopt")


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
    Read of a real content note still does. The core binary is never invoked on a
    Read, so a nonexistent path is fine here."""
    hook = _render_hook(tmp_path, "/nonexistent/omind")
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
        # PR via the API to an OWNED (CryptoJones) repo is still blocked; the owner
        # must use Codeberg. (A third-party owner here would now ALLOW — covered by
        # test_third_party_pr_allowed_owned_pr_blocked.)
        "gh api repos/CryptoJones/omind/pulls -f title=x -f head=y",  # PR via the API
        "pkexec rm -rf /tmp/x",
        "doas reboot",
        "su -c 'rm -rf /tmp/x' root",
    ]
    for cmd in blocked:
        assert not guard.decide({"command": cmd, "session": "b1"}).allow, cmd
    # a GET listing of pulls (no write field / POST) is NOT a PR create -> allowed
    assert guard.decide({"command": "gh api repos/acme/widget/pulls", "session": "b1"}).allow
    # privesc still has the deliberate opt-in (a real leading assignment, #2)
    assert guard.decide(
        {"command": "OMI_SUDO_OK=1 pkexec systemctl restart x", "session": "b1"}
    ).allow
    guard.clear_gate("b1")


def test_third_party_pr_allowed_owned_pr_blocked() -> None:
    """The GitHub-PR hard-block is owner-aware: a PR to a repo the owner does NOT
    control (third-party OSS) is allowed, while a PR to a CryptoJones-owned repo
    still goes to Codeberg (blocked). A bare `gh pr create|merge` (no `--repo`)
    defaults to the upstream and stays blocked as the safe default."""
    guard.mark_consulted("b2")

    def allowed(cmd: str) -> bool:
        return guard.decide({"command": cmd, "session": "b2"}).allow

    # third-party OSS PRs — ALLOWED (must name --repo <non-CryptoJones>/<repo>)
    assert allowed("gh pr create --repo yurukusa/claude-code-hooks")
    assert allowed("gh pr merge --repo yurukusa/x")
    assert allowed("gh pr create --title x --repo yurukusa/y")
    # PR to an owned (CryptoJones) repo — BLOCKED (Codeberg-only), case-insensitive
    assert not allowed("gh pr create --repo CryptoJones/omind")
    assert not allowed("gh pr create --repo cryptojones/omind")
    # bare create/merge (no --repo) defaults to the upstream — BLOCKED
    assert not allowed("gh pr create")
    assert not allowed("gh pr merge")

    # gh api .../pulls writes: owner-aware the same way
    assert allowed(
        "gh api repos/yurukusa/claude-code-hooks/pulls -f title=x -f head=y -f base=main"
    )
    assert not allowed("gh api --method POST repos/CryptoJones/omind/pulls -f title=x")
    # a GET listing of a third-party repo's pulls (no write) is unchanged — allowed
    assert allowed("gh api repos/yurukusa/x/pulls")

    guard.clear_gate("b2")


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
