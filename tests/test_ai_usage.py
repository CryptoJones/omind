# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Aaron K. Clark

from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from omind import ai_usage
from omind.cli import main


def test_profile_default_saved_and_environment_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    omi = tmp_path / "OMI"
    assert ai_usage.profile_info(omi) == {
        "saved": "economy",
        "effective": "economy",
        "source": "default",
    }
    assert ai_usage.set_profile(omi, "medium")["effective"] == "balanced"
    monkeypatch.setenv(ai_usage.PROFILE_ENV, "high")
    assert ai_usage.profile_info(omi) == {
        "saved": "balanced",
        "effective": "economy",
        "source": "environment",
    }
    with pytest.raises(ValueError):
        ai_usage.set_profile(omi, "pricey")


def test_ledger_is_per_vault_private_and_skips_torn_lines(tmp_path: Path) -> None:
    first = tmp_path / "one" / "OMI"
    second = tmp_path / "two" / "OMI"
    ai_usage.record_priming(first, 9)
    assert ai_usage.read_events(second) == []
    path = ai_usage.usage_path(first)
    # Windows models file privacy with ACLs rather than POSIX mode bits.
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600
    with path.open("ab") as stream:
        stream.write(b"{torn\xff\n")
    events = ai_usage.read_events(first)
    assert len(events) == 1
    assert events[0]["input_tokens"] == 3
    serialized = json.dumps(events)
    assert "prompt" not in serialized and "response" not in serialized


def test_usage_summary_separates_exact_estimated_and_avoided(tmp_path: Path) -> None:
    omi = tmp_path / "OMI"
    now = datetime(2026, 7, 16, 20, 0)
    ai_usage.log_event(
        omi,
        "verifier",
        input_tokens=10,
        output_tokens=2,
        now=now - timedelta(hours=1),
    )
    ai_usage.log_event(
        omi,
        "priming",
        measurement="estimated",
        input_tokens=25,
        now=now - timedelta(hours=2),
    )
    ai_usage.log_event(
        omi,
        "checkpoint",
        status="skipped",
        measurement="estimated",
        avoided_tokens=50,
        now=now - timedelta(days=2),
    )
    day = ai_usage.usage_summary(omi, since="24h", now=now)
    assert day["totals"]["input_tokens"] == 35
    assert day["exact"]["input_tokens"] == 10
    assert day["estimated"]["input_tokens"] == 25
    assert day["totals"]["avoided_tokens"] == 0
    all_time = ai_usage.usage_summary(omi, since="all", now=now)
    assert all_time["totals"]["avoided_tokens"] == 50
    with pytest.raises(ValueError):
        ai_usage.usage_summary(omi, since="weekly")
    with pytest.raises(ValueError):
        ai_usage.parse_window("9" * 100_000 + "x")


def test_run_claude_records_provider_usage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    omi = tmp_path / "OMI"
    monkeypatch.setattr(ai_usage.shutil, "which", lambda _name: "/usr/bin/claude")

    def fake_run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        payload = {
            "result": "RELEVANT",
            "model": "claude-test",
            "usage": {
                "input_tokens": 12,
                "output_tokens": 3,
                "cache_read_input_tokens": 4,
                "cache_creation_input_tokens": 5,
            },
        }
        return subprocess.CompletedProcess([], 0, json.dumps(payload), "")

    monkeypatch.setattr(ai_usage.subprocess, "run", fake_run)
    assert ai_usage.run_claude(omi, "verifier", "secret prompt", timeout=2) == "RELEVANT"
    event = ai_usage.read_events(omi)[0]
    assert event["measurement"] == "exact"
    assert event["input_tokens"] == 12
    assert event["cache_read_tokens"] == 4
    assert "secret prompt" not in json.dumps(event)


def test_run_claude_estimates_malformed_json_and_records_profile_skip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    omi = tmp_path / "OMI"
    monkeypatch.setattr(ai_usage.shutil, "which", lambda _name: "/usr/bin/claude")
    monkeypatch.setattr(
        ai_usage.subprocess,
        "run",
        lambda *_a, **_k: subprocess.CompletedProcess([], 0, "RELEVANT", ""),
    )
    assert ai_usage.run_claude(omi, "verifier", "abcd", timeout=2) == "RELEVANT"
    assert ai_usage.read_events(omi)[0]["measurement"] == "estimated"
    assert ai_usage.run_claude(omi, "checkpoint", "abcdefgh", timeout=2, allowed=False) is None
    skipped = ai_usage.read_events(omi)[-1]
    assert skipped["status"] == "skipped"
    assert skipped["avoided_tokens"] == 2


def test_cli_profile_and_json_usage(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    vault = tmp_path / "vault"
    args = ["--vault", str(vault), "--folder", "OMI"]
    assert main(["ai", "profile", "medium", *args]) == 0
    assert "effective=balanced" in capsys.readouterr().out
    assert main(["ai", "usage", "--since", "all", "--json", *args]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["profile"]["effective"] == "balanced"
    assert payload["totals"]["input_tokens"] == 0


def test_full_verifier_caps_prompt_and_lower_expense_profiles_skip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omind import verify

    omi = tmp_path / "OMI"
    captured: list[tuple[str, bool]] = []

    def fake_run(
        _omi: Path | str,
        _operation: str,
        prompt: str,
        *,
        timeout: int,
        allowed: bool = True,
    ) -> str:
        del timeout
        captured.append((prompt, allowed))
        return "RELEVANT"

    monkeypatch.setattr(ai_usage, "run_claude", fake_run)
    ai_usage.set_profile(omi, "full")
    assert verify._ask_model("t" * 2_000, "m" * 4_000, omi) is True
    assert captured[-1][1] is True
    assert "t" * 1_001 not in captured[-1][0]
    assert "m" * 2_001 not in captured[-1][0]
    ai_usage.set_profile(omi, "balanced")
    assert verify._ask_model("task", "material", omi) is True
    assert captured[-1][1] is False
    ai_usage.set_profile(omi, "economy")
    assert verify._ask_model("task", "material", omi) is True
    assert captured[-1][1] is False


def test_mcp_and_cache_inclusive_session_share_are_accounted_privately(
    tmp_path: Path,
) -> None:
    omi = tmp_path / "OMI"
    transcript = tmp_path / "session.jsonl"
    assistant = {
        "type": "assistant",
        "message": {
            "id": "msg-1",
            "usage": {
                "input_tokens": 100,
                "output_tokens": 20,
                "cache_read_input_tokens": 300,
                "cache_creation_input_tokens": 80,
            },
        },
    }
    transcript.write_text(
        json.dumps(assistant) + "\n" + json.dumps(assistant) + "\n",
        encoding="utf-8",
    )
    ai_usage.record_session_transcript(omi, transcript, session_id="session-1")
    ai_usage.record_priming(omi, 400)
    ai_usage.record_mcp_response(
        omi,
        {
            "tool_name": "mcp__omi__recall-note",
            "session_id": "session-1",
            "tool_response": {"content": "private memory text"},
        },
    )

    summary = ai_usage.usage_summary(omi, since="all")
    assert summary["session"]["count"] == 1
    assert summary["session"]["totals"] == {
        "input_tokens": 100,
        "output_tokens": 20,
        "cache_read_tokens": 300,
        "cache_write_tokens": 80,
        "avoided_tokens": 0,
    }
    assert summary["operations"]["mcp"]["input_tokens"] > 0
    assert summary["traffic"]["provider_tokens"] == 500
    assert summary["traffic"]["omi_share_percent"] is not None
    ledger = ai_usage.usage_path(omi).read_text(encoding="utf-8")
    assert "private memory text" not in ledger and "msg-1" not in ledger


def test_share_is_unavailable_until_parent_session_usage_is_observed(tmp_path: Path) -> None:
    omi = tmp_path / "OMI"
    ai_usage.record_priming(omi, 4_000)
    summary = ai_usage.usage_summary(omi, since="all")
    assert summary["traffic"]["provider_tokens"] == 0
    assert summary["traffic"]["omi_share_percent"] is None
