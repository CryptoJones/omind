# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Aaron K. Clark
"""Tests for omind.hooks: journal append, formatting, safety, concurrency."""

from __future__ import annotations

import io
import json
import re
import threading
from datetime import datetime
from pathlib import Path

import pytest

from omind import hooks
from omind.store import OmiStore, parse_note

_NOW = datetime(2026, 6, 9, 14, 32, 0)
_BULLET_RE = re.compile(r"^- \d\d:\d\d \[session [\w]+\] ")


def _read_journal(omi: Path, now: datetime = _NOW) -> str:
    return (hooks.journal_dir(omi) / hooks.journal_name(now)).read_text(encoding="utf-8")


def _action_bullets(text: str) -> list[str]:
    in_actions = False
    out: list[str] = []
    for line in text.splitlines():
        if line.strip() == "## Actions":
            in_actions = True
            continue
        if in_actions and line.startswith("- "):
            out.append(line)
    return out


# -- naming ------------------------------------------------------------------


def test_playbook_rules_are_compiled_into_each_session_capsule(tmp_path: Path) -> None:
    assert "Playbook.md" in hooks.PRIMING_FILES
    (tmp_path / "Playbook.md").write_text(
        "# OMI Playbook\n\n- sudo -> fleet-sudo; never hand CJ homework.\n",
        encoding="utf-8",
    )
    ctx = hooks.build_session_start_context(tmp_path)
    assert "OMI capsule: Playbook.md" in ctx
    assert "fleet-sudo" in ctx


def test_journal_name_is_deterministic_per_day() -> None:
    assert hooks.journal_name(_NOW) == "Session Journal 2026-06-09.md"


def test_journal_name_is_accepted_by_store_safe_name(tmp_path: Path) -> None:
    store = OmiStore(tmp_path)
    # must not raise (no separators, ends .md)
    resolved = store.safe_name(hooks.journal_name(_NOW))
    assert resolved.name == "Session Journal 2026-06-09.md"


def test_journal_dir_is_journal_subfolder(tmp_path: Path) -> None:
    assert hooks.journal_dir(tmp_path) == tmp_path / "Journal"


# -- append / header ---------------------------------------------------------


def test_append_creates_header_when_absent(tmp_path: Path) -> None:
    hooks.append_entry(tmp_path, "- 14:32 [session abcd1234] PostToolUse Edit -> x.py (ok)", _NOW)
    text = _read_journal(tmp_path)
    fields = parse_note(text)
    assert fields.title == "Session Journal 2026-06-09"
    assert "session-journal" in fields.tags
    assert "## Actions" in text
    assert len(_action_bullets(text)) == 1


def test_append_lands_in_journal_subfolder_and_stays_unindexed(tmp_path: Path) -> None:
    hooks.append_entry(tmp_path, "- 14:32 [session abcd1234] PostToolUse Edit -> x.py (ok)", _NOW)
    assert (tmp_path / "Journal" / hooks.journal_name(_NOW)).is_file()
    assert not (tmp_path / hooks.journal_name(_NOW)).exists()  # nothing at top level
    store = OmiStore(tmp_path)
    assert store.list_notes() == []  # top-level-only glob skips Journal/
    store.update_index()
    assert "Session Journal" not in (tmp_path / "index.md").read_text(encoding="utf-8")


def test_append_is_additive_single_header(tmp_path: Path) -> None:
    for i in range(3):
        bullet = f"- 14:32 [session abcd1234] PostToolUse Bash -> c{i} (ok)"
        hooks.append_entry(tmp_path, bullet, _NOW)
    text = _read_journal(tmp_path)
    assert text.count("# Session Journal") == 1  # header written once
    assert len(_action_bullets(text)) == 3


def test_journal_parses_under_store_parse_note(tmp_path: Path) -> None:
    for i in range(5):
        bullet = f"- 14:3{i} [session abcd1234] PostToolUse Write -> n{i}.md (ok)"
        hooks.append_entry(tmp_path, bullet, _NOW)
    fields = parse_note(_read_journal(tmp_path))  # must not raise
    assert fields.title == "Session Journal 2026-06-09"
    assert set(fields.tags) >= {"session-journal", "omi"}


# -- format_entry ------------------------------------------------------------


def test_format_entry_posttooluse_edit() -> None:
    event = {
        "hook_event_name": "PostToolUse",
        "session_id": "a1b2c3d4e5",
        "tool_name": "Edit",
        "tool_input": {"file_path": "/repo/src/x.py"},
        "tool_response": {"success": True},
    }
    line = hooks.format_entry(event, now=_NOW)
    assert line is not None
    assert "PostToolUse Edit -> /repo/src/x.py (ok)" in line
    assert line.startswith("- 14:32 [session a1b2c3d4]")


def test_format_entry_bash_command_truncated_and_wrapped() -> None:
    long_cmd = "echo " + "x" * 200
    event = {
        "hook_event_name": "PostToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": long_cmd},
        "tool_response": {},
    }
    line = hooks.format_entry(event, now=_NOW)
    assert line is not None
    assert "`echo" in line  # backtick-wrapped command
    assert len(line) < len(long_cmd)  # truncated


def test_format_entry_error_outcome() -> None:
    event = {
        "hook_event_name": "PostToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": "false"},
        "tool_response": {"error": "boom"},
    }
    line = hooks.format_entry(event, now=_NOW)
    assert line is not None and line.endswith("(error)")


def test_format_entry_stop_returns_turn_line() -> None:
    line = hooks.format_entry({"session_id": "zz"}, event_name="Stop", now=_NOW)
    assert line is not None and "Stop -> turn ended" in line


def test_format_entry_session_start_returns_none() -> None:
    assert hooks.format_entry({}, event_name="SessionStart", now=_NOW) is None


def test_format_entry_empty_event_is_recorded() -> None:
    line = hooks.format_entry({}, event_name="PostToolUse", now=_NOW)
    assert line is not None
    assert "[session unknown]" in line


# -- _extract_outcome --------------------------------------------------------


def test_outcome_stderr_only_is_ok() -> None:
    # healthy tools (git, curl, npm, dnf…) write progress/warnings to stderr
    assert hooks._extract_outcome({"stdout": "done", "stderr": "warning: detached HEAD"}) == "ok"


def test_outcome_explicit_is_error() -> None:
    assert hooks._extract_outcome({"is_error": True}) == "error"


def test_outcome_success_false_is_error() -> None:
    assert hooks._extract_outcome({"success": False}) == "error"


def test_outcome_nonzero_exit_code_is_error() -> None:
    assert hooks._extract_outcome({"exit_code": 1}) == "error"


def test_outcome_zero_exit_code_with_stderr_is_ok() -> None:
    assert hooks._extract_outcome({"exit_code": 0, "stderr": "Cloning into 'repo'…"}) == "ok"


def test_outcome_empty_response_is_ok() -> None:
    assert hooks._extract_outcome({}) == "ok"


def test_outcome_empty_error_string_is_ok() -> None:
    assert hooks._extract_outcome({"error": ""}) == "ok"


def test_outcome_nonempty_error_dict_is_error() -> None:
    assert hooks._extract_outcome({"error": {"code": -32000, "message": "boom"}}) == "error"


# -- read_event --------------------------------------------------------------


def test_read_event_empty_stdin_returns_empty() -> None:
    assert hooks.read_event(io.StringIO("")) == {}


def test_read_event_garbage_returns_empty() -> None:
    assert hooks.read_event(io.StringIO("not json{")) == {}


def test_read_event_non_object_returns_empty() -> None:
    assert hooks.read_event(io.StringIO("[1, 2, 3]")) == {}


def test_read_event_valid_object() -> None:
    assert hooks.read_event(io.StringIO('{"tool_name": "Edit"}')) == {"tool_name": "Edit"}


# -- run_hook ----------------------------------------------------------------


def test_run_hook_returns_zero_and_records(tmp_path: Path) -> None:
    stdin = io.StringIO('{"hook_event_name": "PostToolUse", "tool_name": "Read", '
                        '"tool_input": {"file_path": "a.txt"}}')
    rc = hooks.run_hook("PostToolUse", tmp_path, stdin=stdin)
    assert rc == 0
    journal = hooks.journal_dir(tmp_path) / hooks.journal_name()
    bullets = _action_bullets(journal.read_text(encoding="utf-8"))
    assert len(bullets) == 1


def test_run_hook_never_raises_even_if_append_fails(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    def boom(*_a: object, **_k: object) -> None:
        raise RuntimeError("disk gone")

    monkeypatch.setattr(hooks, "append_entry", boom)
    stdin = io.StringIO('{"hook_event_name": "PostToolUse", "tool_name": "Read"}')
    assert hooks.run_hook("PostToolUse", tmp_path, stdin=stdin) == 0


def test_run_hook_session_start_emits_context_no_journal(tmp_path: Path) -> None:
    out = io.StringIO()
    rc = hooks.run_hook("SessionStart", tmp_path, stdin=io.StringIO(""), stdout=out)
    assert rc == 0
    assert "additionalContext" in out.getvalue()
    assert not list(tmp_path.rglob("Session Journal*.md"))  # no journal written


def test_session_start_injects_priming_note_content(tmp_path: Path) -> None:
    (tmp_path / "index.md").write_text(
        "## Recent Memories\n- [[Some Memory]] — useful context\n",
        encoding="utf-8",
    )
    (tmp_path / "Memory Workflow.md").write_text("OMI is the source", encoding="utf-8")
    (tmp_path / "CLAUDE CODE PERSONALITY.md").write_text("You are Dix", encoding="utf-8")
    ctx = hooks.build_session_start_context(tmp_path)
    assert "[[Some Memory]]" in ctx
    assert "You are Dix" in ctx
    assert "===== OMI capsule: index.md =====" in ctx


def test_session_start_caps_runaway_note(tmp_path: Path) -> None:
    (tmp_path / "Playbook.md").write_text(
        "x" * (hooks._PRIMING_FILE_CHAR_CAP + 500), encoding="utf-8"
    )
    ctx = hooks.build_session_start_context(tmp_path)
    assert "…[truncated]" in ctx


def test_session_start_falls_back_when_no_notes(tmp_path: Path) -> None:
    ctx = hooks.build_session_start_context(tmp_path)  # empty vault
    assert "source of truth" in ctx
    assert "could not be read" in ctx


# -- Hermes pre_llm_call priming ----------------------------------------------


@pytest.fixture
def isolated_state(monkeypatch, tmp_path: Path):  # type: ignore[no-untyped-def]
    """Point the per-session prime markers at a throwaway dir."""
    state = tmp_path / "state"
    monkeypatch.setattr(hooks.paths, "state_dir", lambda: state)
    return state


def test_pre_llm_call_emits_context_once_per_session(
    tmp_path: Path, isolated_state: Path
) -> None:
    (tmp_path / "index.md").write_text("- [[A Memory]] — relevant\n", encoding="utf-8")
    event = '{"session_id": "sess-abc-123"}'

    first = io.StringIO()
    hooks.run_hook("pre_llm_call", tmp_path, stdin=io.StringIO(event), stdout=first)
    payload = json.loads(first.getvalue())
    assert "[[A Memory]]" in payload["context"]  # primed on first turn

    second = io.StringIO()
    hooks.run_hook("pre_llm_call", tmp_path, stdin=io.StringIO(event), stdout=second)
    second_payload = json.loads(second.getvalue())
    assert "OMI capsule" not in second_payload["context"]  # priming is once per session
    assert "consult gate remains armed" in second_payload["context"]  # preflight is every turn


def test_pre_llm_call_primes_each_call_without_session_id(
    tmp_path: Path, isolated_state: Path
) -> None:
    (tmp_path / "index.md").write_text("RECENT: [[A Memory]]", encoding="utf-8")
    # No session id to dedup on -> prime every call rather than risk never.
    for _ in range(2):
        out = io.StringIO()
        hooks.run_hook("pre_llm_call", tmp_path, stdin=io.StringIO("{}"), stdout=out)
        assert "context" in out.getvalue()


def test_pre_llm_call_never_raises_on_garbage(
    tmp_path: Path, isolated_state: Path
) -> None:
    out = io.StringIO()
    rc = hooks.run_hook("pre_llm_call", tmp_path, stdin=io.StringIO("not json{"), stdout=out)
    assert rc == 0  # tolerated; never blocks the agent


# -- session-start dynamic priming (session state + journal tail) -------------


def _write_priming_files(omi: Path, body: str | None = None) -> None:
    for name in hooks.PRIMING_FILES:
        text = body if body is not None else f"BODY OF {name}"
        if name == "index.md":
            text = "## Standing Directives\n" + text
        (omi / name).write_text(text, encoding="utf-8")


def test_session_start_injects_newest_session_state_by_name(tmp_path: Path) -> None:
    _write_priming_files(tmp_path)
    (tmp_path / "Session State omind 2026-06-01.md").write_text(
        "OLD STATE", encoding="utf-8"
    )
    (tmp_path / "Session State omind 2026-06-09.md").write_text(
        "NEW STATE", encoding="utf-8"
    )
    ctx = hooks.build_session_start_context(tmp_path, cwd="/work/repos/omind")
    assert "NEW STATE" in ctx  # newest filename wins
    assert "OLD STATE" not in ctx  # older handoffs stay out of context
    assert "OMI capsule: Session State omind 2026-06-09.md (project handoff)" in ctx


def test_session_start_selects_only_cwd_matched_project_handoff(tmp_path: Path) -> None:
    _write_priming_files(tmp_path)
    (tmp_path / "Session State omind 2026-06-09.md").write_text(
        "OMIND HANDOFF", encoding="utf-8"
    )
    (tmp_path / "Session State website 2026-06-10.md").write_text(
        "WEBSITE HANDOFF", encoding="utf-8"
    )
    ctx = hooks.build_session_start_context(tmp_path, cwd="/work/repos/omind")
    assert "OMIND HANDOFF" in ctx
    assert "WEBSITE HANDOFF" not in ctx


def test_session_start_matches_handoff_content_and_requires_cwd(tmp_path: Path) -> None:
    _write_priming_files(tmp_path)
    state = tmp_path / "Session State 2026-06-09.md"
    state.write_text("Current project: omind\nCONTENT MATCH", encoding="utf-8")
    assert "CONTENT MATCH" not in hooks.build_session_start_context(tmp_path)
    matched = hooks.build_session_start_context(tmp_path, cwd="/work/repos/omind")
    assert "CONTENT MATCH" in matched


def test_session_start_missing_session_state_degrades_to_static(tmp_path: Path) -> None:
    _write_priming_files(tmp_path)
    ctx = hooks.build_session_start_context(tmp_path)
    assert "BODY OF index.md" in ctx  # static priming unchanged
    assert "Session State" not in ctx
    assert "auto-journal" not in ctx


def test_session_start_journal_tail_is_last_bullets_only(tmp_path: Path) -> None:
    _write_priming_files(tmp_path)
    old = datetime(2026, 6, 1, 9, 0, 0)
    hooks.append_entry(tmp_path, "- 09:00 [session old00000] PostToolUse Bash -> OLD-J (ok)", old)
    for i in range(hooks._JOURNAL_TAIL_BULLETS + 5):
        bullet = f"- 14:32 [session abcd1234] PostToolUse Bash -> c{i:02d} (ok)"
        hooks.append_entry(tmp_path, bullet, _NOW)
    ctx = hooks.build_session_start_context(tmp_path)
    assert "auto-journal" not in ctx
    assert "OLD-J" not in ctx  # only the newest journal is primed
    assert "-> c24 (ok)" not in ctx  # action trails are recalled on demand
    assert "-> c04 (ok)" not in ctx  # bullets beyond the tail dropped
    tail = [ln for ln in ctx.splitlines() if ln.startswith("- 14:32 [session abcd1234]")]
    assert tail == []
    assert "- Created:" not in ctx  # metadata list lines are not action bullets


def test_session_start_total_cap_truncates_dynamic_first(tmp_path: Path) -> None:
    from omind import ai_usage

    ai_usage.set_profile(tmp_path, "full")
    per = 42_000 // len(hooks.PRIMING_FILES) - 80  # ~42k static total, regardless of count
    _write_priming_files(tmp_path, body="s" * per + " STATIC-END")
    state = tmp_path / "Session State omind 2026-06-09.md"
    state.write_text("d" * hooks._PRIMING_FILE_CHAR_CAP, encoding="utf-8")
    ctx = hooks.build_session_start_context(tmp_path, cwd="/work/repos/omind")
    assert len(ctx) <= hooks._TOTAL_CONTEXT_CHAR_CAP
    for name in hooks.PRIMING_FILES:
        assert f"===== OMI capsule: {name} =====" in ctx
    assert "dddd" in ctx  # matched handoff keeps a bounded capsule section
    assert "…[truncated]" in ctx


def test_session_start_reserves_dynamic_when_static_fills_cap(tmp_path: Path) -> None:
    from omind import ai_usage

    ai_usage.set_profile(tmp_path, "full")
    _write_priming_files(tmp_path, body="s" * hooks._PRIMING_FILE_CHAR_CAP)  # ~48k static
    (tmp_path / "Session State omind 2026-06-09.md").write_text(
        "DYNAMIC-STATE", encoding="utf-8"
    )
    ctx = hooks.build_session_start_context(tmp_path, cwd="/work/repos/omind")
    assert "DYNAMIC-STATE" in ctx
    assert len(ctx) <= hooks._TOTAL_CONTEXT_CHAR_CAP


def test_session_start_expense_profiles_apply_hard_caps(tmp_path: Path) -> None:
    from omind import ai_usage

    _write_priming_files(tmp_path, body="x" * hooks._PRIMING_FILE_CHAR_CAP)
    for name, cap in (("full", 24_000), ("balanced", 8_000), ("economy", 4_000)):
        ai_usage.set_profile(tmp_path, name)
        context = hooks.build_session_start_context(tmp_path)
        assert len(context) <= cap
        for priming in hooks.PRIMING_FILES:
            assert f"===== OMI capsule: {priming} =====" in context
    hooks.emit_session_start_context(tmp_path, out=io.StringIO())
    event = ai_usage.read_events(tmp_path)[-1]
    assert event["profile"] == "economy"
    assert event["avoided_tokens"] > 0


def test_session_start_records_priming_once_per_emission(
    tmp_path: Path, isolated_state: Path
) -> None:
    from omind import ai_usage

    (tmp_path / "index.md").write_text("remember", encoding="utf-8")
    event = '{"session_id":"token-session"}'
    hooks.run_hook("pre_llm_call", tmp_path, stdin=io.StringIO(event), stdout=io.StringIO())
    hooks.run_hook("pre_llm_call", tmp_path, stdin=io.StringIO(event), stdout=io.StringIO())
    events = ai_usage.read_events(tmp_path)
    assert len(events) == 1
    assert events[0]["operation"] == "priming"
    assert events[0]["measurement"] == "estimated"


def test_run_hook_stop_records_turn_line(tmp_path: Path) -> None:
    hooks.run_hook("Stop", tmp_path, stdin=io.StringIO('{"session_id": "qq"}'))
    text = (hooks.journal_dir(tmp_path) / hooks.journal_name()).read_text(encoding="utf-8")
    assert "Stop -> turn ended" in text


# -- concurrency -------------------------------------------------------------


def test_concurrent_appends_serialize(tmp_path: Path) -> None:
    n = 40

    def worker(i: int) -> None:
        bullet = f"- 14:32 [session t{i:04d}] PostToolUse Bash -> c{i} (ok)"
        hooks.append_entry(tmp_path, bullet, _NOW)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    text = _read_journal(tmp_path)
    bullets = _action_bullets(text)
    assert len(bullets) == n  # no lost writes
    assert all(_BULLET_RE.match(b) for b in bullets)  # no torn/interleaved lines
    assert text.count("# Session Journal") == 1  # exactly one header


# -- failure breadcrumbs -------------------------------------------------------


def test_append_entry_failure_leaves_breadcrumb(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    blocker = tmp_path / "not-a-vault"
    blocker.write_text("a file where the OMI folder should be", encoding="utf-8")
    hooks.append_entry(blocker, "- 14:32 [session abcd1234] PostToolUse Bash (ok)")
    log = hooks.failure_log_path()
    assert log.is_file()
    assert "append_entry" in log.read_text(encoding="utf-8")


def test_breadcrumb_write_failure_is_itself_swallowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The state dir is also unwritable (a file sits where it should be):
    # the breadcrumb attempt must not turn a swallowed error into a raise.
    state_blocker = tmp_path / "state-blocker"
    state_blocker.write_text("x", encoding="utf-8")
    monkeypatch.setenv("XDG_STATE_HOME", str(state_blocker))
    vault_blocker = tmp_path / "not-a-vault"
    vault_blocker.write_text("x", encoding="utf-8")
    hooks.append_entry(vault_blocker, "- bullet")  # must simply return


def test_failure_log_restarts_past_the_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    log = hooks.failure_log_path()
    log.parent.mkdir(parents=True)
    log.write_text("x" * (hooks._FAILURE_LOG_CAP_BYTES + 1), encoding="utf-8")
    blocker = tmp_path / "not-a-vault"
    blocker.write_text("x", encoding="utf-8")
    hooks.append_entry(blocker, "- bullet")
    text = log.read_text(encoding="utf-8")
    assert len(text) < hooks._FAILURE_LOG_CAP_BYTES
    assert not text.startswith("x")
    assert "append_entry" in text


def test_run_hook_breadcrumbs_unexpected_errors_and_still_returns_0(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))

    def boom(*args: object, **kwargs: object) -> str:
        raise RuntimeError("boom")

    monkeypatch.setattr(hooks, "format_entry", boom)
    rc = hooks.run_hook("PostToolUse", tmp_path / "OMI", stdin=io.StringIO("{}"))
    assert rc == 0
    assert "run_hook" in hooks.failure_log_path().read_text(encoding="utf-8")


# -- update nudge in SessionStart context ------------------------------------


def test_session_start_context_surfaces_update_nudge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omind import update

    monkeypatch.setattr(
        update,
        "update_nudge",
        lambda: "omind 9.9.9 is available (you're on 1.0.0) — run `omind self-update` to upgrade.",
    )
    ctx = hooks.build_session_start_context(tmp_path)
    assert ctx.startswith("⚠️")  # surfaced at the very top
    assert "omind 9.9.9 is available" in ctx
    assert "OMI is the durable-memory source of truth" in ctx


def test_session_start_context_no_nudge_when_up_to_date(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omind import update

    monkeypatch.setattr(update, "update_nudge", lambda: None)
    ctx = hooks.build_session_start_context(tmp_path)
    assert "⚠️" not in ctx
    assert "self-update" not in ctx


def test_session_start_context_survives_nudge_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omind import update

    def boom() -> str:
        raise RuntimeError("network down")

    monkeypatch.setattr(update, "update_nudge", boom)
    ctx = hooks.build_session_start_context(tmp_path)  # must never raise
    assert "OMI is the durable-memory source of truth" in ctx
    assert "⚠️" not in ctx
