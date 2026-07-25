# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Aaron K. Clark
"""Tests for ``omind checkpoint`` — the scheduled recent-work recorder."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from omind import checkpoint, compliance, hooks
from omind.cli import main


def _omi(tmp_path: Path) -> Path:
    omi = tmp_path / "OMI"
    omi.mkdir(parents=True, exist_ok=True)
    return omi


def _write_journal(omi: Path, day: datetime, bullets: list[str]) -> Path:
    jdir = hooks.journal_dir(omi)
    jdir.mkdir(parents=True, exist_ok=True)
    path = jdir / hooks.journal_name(day)
    path.write_text(
        f"# Session Journal {day.strftime('%Y-%m-%d')}\n\n## Actions\n" + "\n".join(bullets) + "\n",
        encoding="utf-8",
    )
    return path


def test_parse_since() -> None:
    assert checkpoint.parse_since("15m").total_seconds() == 900
    assert checkpoint.parse_since("2h").total_seconds() == 7200
    assert checkpoint.parse_since("90").total_seconds() == 5400  # bare number = minutes
    assert checkpoint.parse_since("1d").total_seconds() == 86400
    assert checkpoint.parse_since("garbage").total_seconds() == 900  # fallback = 15m


def test_gather_activity_windows_both_trails(tmp_path: Path) -> None:
    omi = _omi(tmp_path)
    now = datetime(2026, 6, 20, 12, 30, 0)
    _write_journal(
        omi,
        now,
        [
            "- 12:25 [session s1] PostToolUse Bash -> `ls` (ok)",  # in window
            "- 12:28 [session s1] PostToolUse Read -> /x/y.md (ok)",  # in window
            "- 12:00 [session s1] PostToolUse Bash -> `old` (ok)",  # 30m ago — out
        ],
    )
    compliance.log_event(
        compliance.KIND_VIOLATION, tool="Bash", command="gh repo delete a/b",
        outcome="deny", rule_id="gh-repo-delete", now=datetime(2026, 6, 20, 12, 29, 0),
    )
    compliance.log_event(
        compliance.KIND_DECISION, tool="Bash", command="old", outcome="allow",
        now=datetime(2026, 6, 20, 11, 0, 0),  # out of window
    )
    cutoff = now - checkpoint.parse_since("15m")
    act = checkpoint.gather_activity(omi, cutoff, now)
    assert len(act.actions) == 2
    assert {a["tool"] for a in act.actions} == {"Bash", "Read"}
    assert len(act.guard_events) == 1  # only the 12:29 deny is in the window


def test_render_section_deterministic() -> None:
    act = checkpoint.Activity(
        actions=[
            {"time": "12:25", "event": "PostToolUse", "tool": "Bash", "detail": "x"},
            {"time": "12:28", "event": "PostToolUse", "tool": "Read", "detail": "y"},
        ],
        guard_events=[
            {"ts": "t", "outcome": "deny", "kind": "violation", "command": "gh repo delete a/b"}
        ],
    )
    out = checkpoint.render_section(act, "15m", datetime(2026, 6, 20, 12, 30))
    assert "### 12:30 — last 15m" in out
    assert "2 action(s)" in out and "Bash×1" in out and "Read×1" in out
    assert "1 deny" in out and "gh repo delete a/b" in out


def test_render_section_empty() -> None:
    out = checkpoint.render_section(checkpoint.Activity(), "15m", datetime(2026, 6, 20, 12, 30))
    assert "no recorded activity" in out


def test_write_checkpoint_upserts_then_appends(tmp_path: Path) -> None:
    omi = _omi(tmp_path)
    now = datetime(2026, 6, 20, 12, 30, 0)
    _write_journal(omi, now, ["- 12:25 [session s1] PostToolUse Bash -> `ls` (ok)"])
    action, filename = checkpoint.write_checkpoint(omi, since="15m", now=now)
    assert filename == "Worklog 2026-06-20.md"
    text = (omi / filename).read_text(encoding="utf-8")
    assert "### 12:30" in text and "1 action(s)" in text

    now2 = datetime(2026, 6, 20, 12, 45, 0)
    _write_journal(
        omi,
        now2,
        [
            "- 12:25 [session s1] PostToolUse Bash -> `ls` (ok)",  # out of the 12:30 cutoff
            "- 12:40 [session s1] PostToolUse Edit -> f.py (ok)",  # in window
        ],
    )
    checkpoint.write_checkpoint(omi, since="15m", now=now2)
    text2 = (omi / filename).read_text(encoding="utf-8")
    assert text2.count("### ") == 2  # appended a second checkpoint section
    assert "### 12:45" in text2 and "Edit×1" in text2


def test_append_section_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(checkpoint, "_MAX_SECTIONS", 2)
    body = ""
    for i in range(4):
        body = checkpoint._append_section(body, f"### {i}\n- x\n")
    assert "### 2" in body and "### 3" in body  # newest kept
    assert "### 0" not in body and "### 1" not in body  # oldest trimmed


def test_checkpoint_cli_run_creates_worklog(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _omi(tmp_path)
    rc = main(["checkpoint", "--vault", str(tmp_path), "--folder", "OMI"])
    assert rc == 0
    assert "Worklog" in capsys.readouterr().out  # creates the note even with no activity


def test_checkpoint_install_and_uninstall_timer(tmp_path: Path) -> None:
    checkpoint.install_timer("15m", tmp_path / "vault", "OMI", log=lambda _m: None, reload=False)
    unit_dir = checkpoint.systemd_user_dir()
    timer = (unit_dir / checkpoint.TIMER_UNIT_NAME).read_text(encoding="utf-8")
    assert "OnUnitActiveSec=900s" in timer
    service = (unit_dir / checkpoint.SERVICE_UNIT_NAME).read_text(encoding="utf-8")
    assert "checkpoint run --since 15m" in service
    checkpoint.uninstall_timer(log=lambda _m: None, reload=False)
    assert not (unit_dir / checkpoint.TIMER_UNIT_NAME).exists()
    assert not (unit_dir / checkpoint.SERVICE_UNIT_NAME).exists()


def test_checkpoint_cli_install_timer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(checkpoint, "_systemctl", lambda _args: None)  # don't touch real systemd
    rc = main(
        ["checkpoint", "install-timer", "--every", "30m",
         "--vault", str(tmp_path), "--folder", "OMI"]
    )
    assert rc == 0
    timer = (checkpoint.systemd_user_dir() / checkpoint.TIMER_UNIT_NAME).read_text(encoding="utf-8")
    assert "OnUnitActiveSec=1800s" in timer


def test_high_expense_checkpoint_skips_llm_and_records_avoided_tokens(tmp_path: Path) -> None:
    from omind import ai_usage

    omi = _omi(tmp_path)
    ai_usage.set_profile(omi, "high")
    activity = checkpoint.Activity(
        actions=[{"time": "12:00", "event": "PostToolUse", "tool": "Bash", "detail": "work"}]
    )
    rendered = checkpoint.render_section(
        activity, "15m", datetime(2026, 6, 20, 12, 30), llm=True, omi_dir=omi
    )
    assert "1 action(s)" in rendered
    event = ai_usage.read_events(omi)[-1]
    assert event["operation"] == "checkpoint"
    assert event["status"] == "skipped"
    assert event["avoided_tokens"] > 0
