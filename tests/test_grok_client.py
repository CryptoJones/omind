# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Aaron K. Clark
"""Grok Build client: event translation and `omind setup --agent grok`."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
import tomlkit

from omind import adapters, agents, harness, paths
from omind.agents import diagnose_grok, run_setup_for
from omind.provision import ProvisionError, SetupConfig


def test_grok_payload_translates_shell_and_consult() -> None:
    shell = harness.translate_event(
        "claude",
        {
            "hookEventName": "pre_tool_use",
            "sessionId": "sess-1",
            "toolName": "run_terminal_command",
            "toolInput": {"command": "git status"},
        },
    )
    assert shell["tool_name"] == "Bash"
    assert shell["tool_input"]["command"] == "git status"
    assert shell["session_id"] == "sess-1"
    assert shell["hook_event_name"] == "PreToolUse"

    consult = harness.translate_event(
        "grok",
        {
            "hook_event_name": "PreToolUse",
            "sessionId": "sess-1",
            "toolName": "omi__search-vault",
            "toolInput": {"query": "grok"},
        },
    )
    assert consult["tool_name"] == "mcp__omi__search-vault"
    action = adapters.normalize_action(consult)
    assert action["is_omi_consult"] is True

    write = harness.translate_event(
        "grok",
        {"toolName": "search_replace", "toolInput": {"path": "a.py"}},
    )
    assert write["tool_name"] == "Edit"
    assert write["tool_input"]["path"] == "a.py"


def test_claude_event_is_not_treated_as_grok() -> None:
    event = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": "ls"},
        "session_id": "s",
    }
    assert harness.payload_is_grok(event) is False
    assert harness.translate_event("claude", event)["tool_name"] == "Bash"


def test_claude_hook_denies_grok_payload_as_json() -> None:
    event = {
        "hookEventName": "pre_tool_use",
        "hook_event_name": "PreToolUse",
        "sessionId": "st",
        "toolName": "run_terminal_command",
        "toolInput": {"command": "gh repo delete acme/widget"},
    }
    stdout, stderr = io.StringIO(), io.StringIO()
    # run_adapter writes the process streams; capture by patching is heavier
    # than rendering the translated verdict the same way the selftest does.
    translated = harness.translate_event("claude", event)
    action = adapters.normalize_action(translated)
    from omind import guard

    verdict = guard.decide(action)
    code = harness.render_decision(
        verdict, harness.FMT_GROK, stdout, stderr, event="PreToolUse"
    )
    assert verdict.allow is False
    assert code == 0 and stderr.getvalue() == ""
    body = json.loads(stdout.getvalue())
    assert body["decision"] == "deny"
    assert body["reason"].startswith("OMI guard: ")
    assert "never delete a repo" in body["reason"]


def test_grok_dispatcher_lifts_inner_mcp_tool() -> None:
    event = harness.translate_event(
        "grok",
        {
            "toolName": "use_tool",
            "toolInput": {
                "tool_name": "omi__recall-note",
                "tool_input": {"name": "Operational Rules - Git Repos and Secrets"},
            },
        },
    )
    assert event["tool_name"] == "mcp__omi__recall-note"
    assert event["tool_input"]["name"].startswith("Operational Rules")


@pytest.fixture
def grok_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    root = tmp_path / "grok-home"
    root.mkdir()
    monkeypatch.setattr(agents, "grok_config_dir", lambda: root)
    return root


def _config(tmp_path: Path, **kw: object) -> SetupConfig:
    return SetupConfig(vault=tmp_path / "vault", agent="grok", **kw)  # type: ignore[arg-type]


def _quiet(_: str) -> None:
    pass


def test_grok_config_dir_honors_grok_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("GROK_HOME", str(tmp_path / "custom"))
    assert agents.grok_config_dir() == tmp_path / "custom"


def test_grok_setup_registers_mcp_hooks_skill_and_rules(
    tmp_path: Path, grok_home: Path
) -> None:
    config = _config(tmp_path)
    run_setup_for(config, log=_quiet)

    doc = tomlkit.parse(agents.grok_config_path().read_text(encoding="utf-8"))
    omi = doc["mcp_servers"]["omi"]
    assert omi["command"]
    assert list(omi["args"])[-5:] == [
        "node",
        "--vault",
        str(config.vault),
        "--folder",
        config.folder,
    ]

    hooks = json.loads(agents.grok_hooks_path().read_text(encoding="utf-8"))["hooks"]
    for event in agents.GROK_HOOK_EVENTS:
        assert event in hooks
    pre = hooks["PreToolUse"][0]["hooks"][0]
    assert "guard adapter --harness grok" in pre["command"]
    assert pre["timeout"] == 30
    assert "guard preflight --harness grok" in hooks["UserPromptSubmit"][0]["hooks"][0]["command"]
    post = hooks["PostToolUse"][0]["hooks"][0]["command"]
    assert "hook PostToolUse" in post and "--harness grok" in post

    skill = agents.grok_skill_dir() / paths.AGENT_SKILL_FILENAME
    assert skill.is_file()
    assert str(config.vault) in skill.read_text(encoding="utf-8")

    rules = agents.grok_rules_path().read_text(encoding="utf-8")
    assert "omind:grok-bootstrap:start" in rules
    assert str(config.omi_dir) in rules


def test_grok_setup_is_idempotent(tmp_path: Path, grok_home: Path) -> None:
    config = _config(tmp_path)
    run_setup_for(config, log=_quiet)
    before = {
        "toml": agents.grok_config_path().read_text(encoding="utf-8"),
        "hooks": agents.grok_hooks_path().read_text(encoding="utf-8"),
        "rules": agents.grok_rules_path().read_text(encoding="utf-8"),
    }
    actions = run_setup_for(config, log=_quiet)
    assert agents.grok_config_path().read_text(encoding="utf-8") == before["toml"]
    assert agents.grok_hooks_path().read_text(encoding="utf-8") == before["hooks"]
    assert agents.grok_rules_path().read_text(encoding="utf-8") == before["rules"]
    assert before["rules"].count("omind:grok-bootstrap:start") == 1
    assert not any("register MCP" in a for a in actions)


def test_grok_setup_preserves_foreign_content(tmp_path: Path, grok_home: Path) -> None:
    agents.grok_config_path().write_text(
        '# keep me\nmodel = "grok"\n\n[mcp_servers.other]\ncommand = "/bin/other"\nargs = ["x"]\n',
        encoding="utf-8",
    )
    (grok_home / "hooks").mkdir()
    (grok_home / "hooks" / "mine.json").write_text('{"hooks": {}}\n', encoding="utf-8")
    agents.grok_rules_path().parent.mkdir(parents=True)
    agents.grok_rules_path().write_text("# my note\nBe brief.\n", encoding="utf-8")

    run_setup_for(_config(tmp_path), log=_quiet)

    text = agents.grok_config_path().read_text(encoding="utf-8")
    assert "# keep me" in text
    assert 'model = "grok"' in text
    doc = tomlkit.parse(text)
    assert "other" in doc["mcp_servers"]
    assert "omi" in doc["mcp_servers"]
    assert (grok_home / "hooks" / "mine.json").read_text(encoding="utf-8") == '{"hooks": {}}\n'
    rules = agents.grok_rules_path().read_text(encoding="utf-8")
    assert "Be brief." in rules
    assert rules.count("omind:grok-bootstrap:start") == 1


def test_grok_setup_refuses_corrupt_toml(tmp_path: Path, grok_home: Path) -> None:
    agents.grok_config_path().write_text("this = [\n", encoding="utf-8")
    with pytest.raises(ProvisionError):
        run_setup_for(_config(tmp_path), log=_quiet)
    assert agents.grok_config_path().read_text(encoding="utf-8") == "this = [\n"


def test_grok_setup_refuses_unreadable_hooks(tmp_path: Path, grok_home: Path) -> None:
    path = agents.grok_hooks_path()
    path.parent.mkdir(parents=True)
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ProvisionError):
        run_setup_for(_config(tmp_path), log=_quiet)
    assert path.read_text(encoding="utf-8") == "{not json"


def test_grok_setup_refuses_undecodable_rules(tmp_path: Path, grok_home: Path) -> None:
    path = agents.grok_rules_path()
    path.parent.mkdir(parents=True)
    original = b"\xff\xfe not utf-8"
    path.write_bytes(original)
    with pytest.raises(ProvisionError):
        run_setup_for(_config(tmp_path), log=_quiet)
    assert path.read_bytes() == original


def test_grok_setup_fails_without_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    missing = tmp_path / "no-grok"
    monkeypatch.setattr(agents, "grok_config_dir", lambda: missing)
    with pytest.raises(ProvisionError):
        run_setup_for(_config(tmp_path), log=_quiet)


def test_grok_dry_run_writes_nothing(tmp_path: Path, grok_home: Path) -> None:
    actions = run_setup_for(_config(tmp_path, dry_run=True), log=_quiet)
    assert any("register MCP" in a for a in actions)
    assert not agents.grok_config_path().exists()
    assert not agents.grok_hooks_path().exists()
    assert not agents.grok_rules_path().exists()


def test_diagnose_grok_reports_state(tmp_path: Path, grok_home: Path) -> None:
    config = _config(tmp_path)
    before = {c.key: c.level for c in diagnose_grok(config)}
    assert before["grok_root"] == "ok"
    assert before["grok_mcp_registration"] == "fail"
    assert before["grok_hooks"] == "fail"
    assert before["grok_skill"] == "fail"
    assert before["grok_bootstrap"] == "fail"

    run_setup_for(config, log=_quiet)
    after = {c.key: c.level for c in diagnose_grok(config)}
    assert after["grok_mcp_registration"] == "ok"
    assert after["grok_hooks"] == "ok"
    assert after["grok_skill"] == "ok"
    assert after["grok_bootstrap"] == "ok"
