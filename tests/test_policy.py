# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Aaron K. Clark
"""Tests for the data-driven OMI-compliance policy."""

from __future__ import annotations

import json

from omind import guard, policy


def test_seed_rules_are_loaded_even_with_no_file() -> None:
    rules = policy.load_policy()
    ids = {r.id for r in rules}
    # The forge rules were removed (those actions are now allowed); only the six
    # destructive / privilege-escalation safety rules remain in the seed set.
    assert ids == {
        "gh-auth-setup-git",
        "gh-repo-delete",
        "gh-api-repo-delete",
        "curl-api-repo-delete",
        "sudo-use-fleet-sudo",
        "privesc-alternatives",
    }
    assert policy.load_learned() == []  # nothing on disk yet


def test_seed_rule_labels_preserve_wording() -> None:
    by_id = {r.id: r for r in policy.SEED_RULES}
    assert by_id["gh-repo-delete"].label() == "hard"  # destructive -> severity
    assert by_id["sudo-use-fleet-sudo"].label() == "sudo"  # tier wording


def test_append_learned_rule_roundtrips_and_is_idempotent() -> None:
    rule = policy.Rule(
        id="learned-rm-rf",
        pattern=r"\brm\s+-rf\s+/\b",
        message="no rm -rf /",
        severity=policy.SEVERITY_SOFT,
        tier=policy.TIER_LEARNED,
    )
    policy.append_learned_rule(rule)
    learned = policy.load_learned()
    assert [r.id for r in learned] == ["learned-rm-rf"]
    assert learned[0].source == "learned"
    assert learned[0].created  # stamped

    # Re-learning the same id overwrites rather than duplicating.
    policy.append_learned_rule(
        policy.Rule(id="learned-rm-rf", pattern=r"x", message="updated")
    )
    learned = policy.load_learned()
    assert len(learned) == 1
    assert learned[0].message == "updated"


def test_update_learned_rule_escalates_severity_and_verify() -> None:
    policy.append_learned_rule(
        policy.Rule(id="esc", pattern=r"danger", message="m", severity=policy.SEVERITY_SOFT)
    )
    assert policy.update_learned_rule("esc", severity=policy.SEVERITY_HARD, verify=True)
    rule = next(r for r in policy.load_learned() if r.id == "esc")
    assert rule.severity == policy.SEVERITY_HARD
    assert rule.verify is True
    assert not policy.update_learned_rule("does-not-exist", severity="hard")


def test_corrupt_policy_file_is_ignored() -> None:
    policy.policy_path().parent.mkdir(parents=True, exist_ok=True)
    policy.policy_path().write_text("{not json", encoding="utf-8")
    assert policy.load_learned() == []  # never raises
    assert any(r.id == "gh-repo-delete" for r in policy.load_policy())


def test_loader_drops_corrupt_entries_and_unknown_keys() -> None:
    policy.policy_path().parent.mkdir(parents=True, exist_ok=True)
    policy.policy_path().write_text(
        json.dumps(
            [
                {"id": "ok", "pattern": "p", "message": "m", "bogus_field": 1},
                {"id": "", "pattern": "p", "message": "m"},  # empty id -> dropped
                {"pattern": "p"},  # missing id/message -> dropped
            ]
        ),
        encoding="utf-8",
    )
    learned = policy.load_learned()
    assert [r.id for r in learned] == ["ok"]


def test_write_seed_policy_emits_inspectable_file() -> None:
    policy.write_seed_policy()
    data = json.loads(policy.seed_policy_path().read_text(encoding="utf-8"))
    assert {r["id"] for r in data} == {r.id for r in policy.SEED_RULES}


def test_learned_hard_rule_blocks_via_guard_decide() -> None:
    guard.mark_consulted("plc")  # gate satisfied; a hard learned rule still wins
    policy.append_learned_rule(
        policy.Rule(
            id="learned-block",
            pattern=r"\bnpm\s+publish\b",
            message="no npm publish from a hook",
            severity=policy.SEVERITY_HARD,
            tier=policy.TIER_LEARNED,
        )
    )
    verdict = guard.decide({"command": "npm publish --tag latest", "session": "plc"})
    assert not verdict.allow
    assert verdict.rule_id == "learned-block"
    guard.clear_gate("plc")


def test_soft_rule_does_not_block_at_the_gate() -> None:
    guard.mark_consulted("soft")
    policy.append_learned_rule(
        policy.Rule(
            id="soft-warn",
            pattern=r"\bgit\s+commit\b",
            message="just a soft observation",
            severity=policy.SEVERITY_SOFT,
            tier=policy.TIER_LEARNED,
        )
    )
    assert guard.decide({"command": "git commit -m x", "session": "soft"}).allow
    guard.clear_gate("soft")


def test_forge_rules_are_command_anchored() -> None:
    """The forge/destructive seed rules must not fire on the phrase as an
    argument, a grep pattern, or a commit message (#101 false-positive class)."""
    by_id = {r.id: r for r in policy.SEED_RULES}
    d = "del" + "ete"
    setup = "setup" + "-git"

    def hit(rule_id: str, cmd: str) -> bool:
        return bool(by_id[rule_id].compiled().search(cmd))

    # False positives that must NOT match.
    assert not hit("gh-repo-delete", f'grep -rn "gh repo {d}" src/')
    assert not hit("gh-repo-delete", f'git commit -m "forbid gh repo {d}"')
    assert not hit("gh-auth-setup-git", f'grep "gh auth {setup}" notes')
    # Real invocations that MUST still match.
    assert hit("gh-repo-delete", f"gh repo {d} foo/bar")
    assert hit("gh-repo-delete", f"echo x && gh repo {d} foo")
    assert hit("gh-auth-setup-git", f"gh auth {setup}")


def test_sudo_wrapper_and_path_bypasses_are_caught() -> None:
    """Shell keywords / absolute paths must not let a hard sudo rule fail open."""
    by_id = {r.id: r for r in policy.SEED_RULES}

    def hit(rule_id: str, cmd: str) -> bool:
        return bool(by_id[rule_id].compiled().search(cmd))

    assert hit("sudo-use-fleet-sudo", "if true; then sudo rm -rf /; fi")
    assert hit("sudo-use-fleet-sudo", "nohup sudo x")
    assert hit("sudo-use-fleet-sudo", "xargs sudo")
    assert hit("sudo-use-fleet-sudo", "/usr/bin/sudo x")
    assert hit("sudo-use-fleet-sudo", "sudoedit /etc/shadow")
    assert hit("privesc-alternatives", "if true; then doas x; fi")
    # Still no false positives on args / paths / the sanctioned wrapper.
    assert not hit("sudo-use-fleet-sudo", "grep sudo /var/log/x")
    assert not hit("sudo-use-fleet-sudo", "cat /usr/bin/sudo")
    assert not hit("sudo-use-fleet-sudo", "pass show sudo/akclark")
    assert not hit("sudo-use-fleet-sudo", "fleet-sudo systemctl restart x")


def test_loader_drops_uncompilable_and_universal_patterns() -> None:
    """A bad learned regex must never reach the guard hot path (bricked machine)."""
    policy.policy_path().parent.mkdir(parents=True, exist_ok=True)
    policy.policy_path().write_text(
        json.dumps(
            [
                {"id": "good", "pattern": r"\bnpm\s+publish\b", "message": "m"},
                {"id": "uncompilable", "pattern": r"(unclosed", "message": "m"},
                {"id": "empty-match", "pattern": r"x|", "message": "m"},
                {"id": "bad-sev", "pattern": r"y", "message": "m", "severity": 5},
            ]
        ),
        encoding="utf-8",
    )
    learned = policy.load_learned()
    assert [r.id for r in learned] == ["good"]
