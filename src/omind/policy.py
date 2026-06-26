# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Aaron K. Clark
"""Data-driven OMI-compliance policy — the deny set as appendable data.

Phase 2 of the enforcement roadmap promotes the guard's in-code deny set to a
DATA table the learning loop appends to. The SEED rules still live in code, so a
blank machine enforces with no files on disk (cold-start safe); *learned* rules
are read from / written to ``state_dir()/policy.json`` under the same advisory
file lock every omind writer uses.

A rule's :attr:`Rule.pattern` is matched against a normalized action command.
:attr:`Rule.severity` decides the verdict:

* ``hard`` — deny outright (the destructive/forge set + github-push tier).
* ``soft`` — recorded by the detector (Layer E) but does **not** block; the
  recidivism loop (:mod:`omind.learn`) can escalate a soft rule to ``hard``.

The ``github_push`` tier denies unless the command carries the rule's
:attr:`Rule.opt_in` token (``OMI_PUSH_GITHUB=1``) — the deliberate-mirror path.
The verdict label the guard prints is derived here so the wording lives in one
place: ``github-push`` for that tier, otherwise the severity.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from omind import filelock, paths

SEVERITY_HARD = "hard"
SEVERITY_SOFT = "soft"

TIER_DESTRUCTIVE = "destructive"
TIER_GITHUB_PUSH = "github_push"
TIER_SUDO = "sudo"
TIER_LEARNED = "learned"


@dataclass
class Rule:
    """One policy rule. ``seed`` rules ship in code; ``learned`` rules persist
    to ``policy.json`` and can be escalated by the recidivism loop."""

    id: str
    pattern: str
    message: str
    severity: str = SEVERITY_HARD
    tier: str = TIER_DESTRUCTIVE
    opt_in: str | None = None
    source: str = "seed"
    created: str = ""
    hits: int = 0
    #: Set by escalation once a rule recurs past the verifier threshold: the
    #: action-type is flagged for Layer C scrutiny even when it would otherwise
    #: pass the gate. Carried in data so the learning loop owns the decision.
    verify: bool = False

    def compiled(self) -> re.Pattern[str]:
        return re.compile(self.pattern)

    def label(self) -> str:
        """The parenthetical the guard prints: ``github-push`` for that tier,
        else the severity (preserved wording for existing reasons)."""
        if self.tier == TIER_GITHUB_PUSH:
            return "github-push"
        if self.tier == TIER_SUDO:
            return "sudo"
        return self.severity


#: The destructive / forge deny set + the github-push opt-in tier, ported
#: verbatim from the original in-code ``guard`` rules. This is the seed of the
#: data-driven policy; the learning loop appends to ``policy.json`` over the top.
SEED_RULES: tuple[Rule, ...] = (
    Rule(
        id="gh-auth-setup-git",
        pattern=r"\bgh\s+auth\s+setup-git\b",
        message=(
            "never 'gh auth setup-git'. GitHub auth = the gh-YOLO PAT from pass via "
            "a one-shot (per-command) credential helper. Read OMI: github-auth-ssh."
        ),
    ),
    Rule(
        id="gh-pr-create-merge",
        # Owner-aware: a PR to a CryptoJones repo must go to Codeberg, so BLOCK it —
        # but a PR to a THIRD-PARTY OSS repo the owner doesn't control is legitimate.
        # The trailing negative lookahead fails (→ rule doesn't match → ALLOW) only
        # when an explicit `--repo <non-CryptoJones>/…` is named; a bare
        # `gh pr create|merge` (which defaults to the upstream, possibly a CryptoJones
        # repo) stays BLOCKED as the safe default.
        pattern=(
            r"\bgh\s+pr\s+(create|merge)\b"
            r"(?![^|;&]*--repo\s+(?!(?i:CryptoJones)/)[\w.-]+/)"
        ),
        message=(
            "GitHub never gets a PR to a repo you own. PR + merge happen on Codeberg; "
            "GitHub mirrors Codeberg's exact commit. Read OMI: codeberg-authoritative. "
            "A third-party OSS PR is allowed when you name --repo <owner>/<repo> "
            "(owner other than CryptoJones)."
        ),
    ),
    Rule(
        id="gh-repo-delete",
        pattern=r"\bgh\s+repo\s+delete\b",
        message=(
            "never delete a repo from a hook-reachable command. Typed-name "
            "confirmation only. Read OMI: Operational Rules - Git Repos and Secrets."
        ),
    ),
    Rule(
        id="gh-api-repo-delete",
        # Order-independent (red-team #B1): two lookaheads after `gh api`, so
        # `gh api repos/o/r -X DELETE` (path before method) is caught as well as
        # `gh api -X DELETE repos/o/r`. Both lookaheads stay within one simple
        # command (no pipe/;/&), so an unrelated later command can't trip it.
        pattern=r"gh\s+api(?=[^|;&]*(?:-X\s*|--method\s*)DELETE)(?=[^|;&]*repos/)",
        message=(
            "never DELETE a repo via the API. Typed-name confirmation only. "
            "Read OMI: Operational Rules - Git Repos and Secrets."
        ),
    ),
    Rule(
        id="curl-api-repo-delete",
        # red-team #B1: the gh rules only cover `gh`; a raw `curl -X DELETE
        # https://api.github.com/repos/...` deleted a repo (or sub-resource)
        # straight past them. Order-independent like the gh-api rule.
        pattern=(
            r"curl(?=[^|;&]*(?:-X\s*|--request\s*)DELETE)"
            r"(?=[^|;&]*api\.github\.com/repos/)"
        ),
        message=(
            "never DELETE a GitHub repo/resource via the raw API. Use the reviewed "
            "path; typed-name confirmation only. Read OMI: Operational Rules - Git "
            "Repos and Secrets."
        ),
    ),
    Rule(
        id="gh-api-pr-create",
        # red-team #B1: `gh pr create` is blocked, but the same PR could be opened
        # via `gh api repos/o/r/pulls -f ...`. Match a WRITE to .../pulls (a -f field
        # or an explicit POST); a plain GET listing has neither, so reads pass.
        # Owner-aware: only a write to a CryptoJones-owned repo's /pulls is blocked
        # (those go to Codeberg); a write to a third-party owner's /pulls is a
        # legitimate OSS contribution and passes.
        pattern=(
            r"gh\s+api(?=[^|;&]*repos/(?i:CryptoJones)/[^|;&]*/pulls)"
            r"(?=[^|;&]*(?:-f\b|--field\b|--method\s*POST|-X\s*POST))"
        ),
        message=(
            "GitHub never gets a PR to a repo you own — not via `gh pr create` nor the "
            "API. PR + merge happen on Codeberg; GitHub mirrors Codeberg's exact "
            "commit. Read OMI: codeberg-authoritative. A third-party OSS PR via "
            "`gh api repos/<owner>/<repo>/pulls` is allowed for owners other than "
            "CryptoJones."
        ),
    ),
    Rule(
        id="github-https-push",
        pattern=r"(push|remote\s+(set-url|add))[^|;&]*https://[^\s]*github\.com",
        message=(
            "no HTTPS-GitHub push/remote-set. For a deliberate mirror of Codeberg's "
            "exact commit, prefix OMI_PUSH_GITHUB=1 and use the gh-YOLO pass "
            "credential helper. Read OMI: github-auth-ssh, codeberg-authoritative."
        ),
        tier=TIER_GITHUB_PUSH,
        opt_in="OMI_PUSH_GITHUB=1",
    ),
    Rule(
        id="github-push-discretionary",
        pattern=r"\bgit\s+push\b[^|;&]*github",
        message=(
            "no discretionary GitHub push. Codeberg is the source of truth (push it "
            "first). A deliberate mirror push opts in with OMI_PUSH_GITHUB=1. "
            "Read OMI: codeberg-authoritative."
        ),
        tier=TIER_GITHUB_PUSH,
        opt_in="OMI_PUSH_GITHUB=1",
    ),
    Rule(
        id="sudo-use-fleet-sudo",
        pattern=r"(?<![\w-])sudo\b",
        message=(
            "raw sudo is blocked — run `fleet-sudo <cmd>` instead (it reads the "
            "fleet sudo password from pass; never guess the per-host entry, never "
            "hand CJ a command to run). Deliberate raw sudo opts in with "
            "OMI_SUDO_OK=1. See the OMI Playbook."
        ),
        tier=TIER_SUDO,
        opt_in="OMI_SUDO_OK=1",
    ),
    Rule(
        id="privesc-alternatives",
        # red-team #B1: only the literal `sudo` was blocked, so pkexec / doas /
        # run0 / su walked straight past. Same tier + opt-in as raw sudo. The
        # lookbehind keeps `sudo` (handled by its own rule) and words ending in
        # these tokens from matching; `su` won't match inside `sudo`/`issue`/etc.
        pattern=r"(?<![\w-])(pkexec|doas|run0|su)\b",
        message=(
            "raw privilege escalation is blocked — run `fleet-sudo <cmd>` instead "
            "(pkexec/doas/run0/su included). Deliberate raw escalation opts in with "
            "OMI_SUDO_OK=1. See the OMI Playbook."
        ),
        tier=TIER_SUDO,
        opt_in="OMI_SUDO_OK=1",
    ),
)

#: Persisted-rule field names (the dataclass attributes). Used to filter unknown
#: keys out of on-disk data so a forward-compat field can't crash the loader.
_RULE_FIELDS = frozenset(Rule.__dataclass_fields__)


def policy_path() -> Path:
    """The machine-local learned-rules table the learning loop appends to."""
    return paths.state_dir() / "policy.json"


def seed_policy_path() -> Path:
    """Where ``omind setup`` writes the SEED ruleset for transparency/editing.

    The guard never reads this — the seed lives in code so a blank machine
    enforces with no files — but exposing it makes the active policy inspectable.
    """
    return paths.state_dir() / "seed-policy.json"


def _rule_from_dict(data: dict[str, object]) -> Rule | None:
    """Build a Rule from on-disk data, dropping unknown keys. ``None`` if it
    lacks the required ``id``/``pattern``/``message`` (a corrupt entry is skipped,
    never fatal)."""
    kwargs = {k: v for k, v in data.items() if k in _RULE_FIELDS}
    required = ("id", "pattern", "message")
    if not all(isinstance(kwargs.get(k), str) and kwargs.get(k) for k in required):
        return None
    try:
        return Rule(**kwargs)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _rule_to_dict(rule: Rule) -> dict[str, object]:
    return asdict(rule)


def load_learned() -> list[Rule]:
    """The learned rules from ``policy.json``; ``[]`` on any miss (never raises)."""
    try:
        raw = json.loads(policy_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(raw, list):
        return []
    rules: list[Rule] = []
    for item in raw:
        if isinstance(item, dict):
            rule = _rule_from_dict(item)
            if rule is not None:
                rule.source = "learned"
                rules.append(rule)
    return rules


def load_policy() -> list[Rule]:
    """The active policy: SEED rules first, then learned rules. SEED is always
    present (it lives in code), so this is safe on a blank machine."""
    return [*SEED_RULES, *load_learned()]


def _mutate_learned(fn: Callable[[list[Rule]], list[Rule]]) -> None:
    """Load, transform, and atomically rewrite ``policy.json`` under the lock.

    Best-effort: a filesystem error leaves the table unchanged rather than
    raising into a hook. The lock serializes concurrent learners (Claude + the
    web UI + cron) exactly like the OMI store's ``.omi.lock``.
    """
    path = policy_path()
    with contextlib.suppress(OSError):
        path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = path.parent / "policy.lock"
        fd = os.open(lock_path, os.O_WRONLY | os.O_CREAT, 0o644)
        try:
            filelock.lock_fd(fd)
            new_rules = fn(load_learned())
            tmp = path.parent / "policy.json.tmp"
            tmp.write_text(
                json.dumps([_rule_to_dict(r) for r in new_rules], indent=2) + "\n",
                encoding="utf-8",
            )
            os.replace(tmp, path)
        finally:
            filelock.unlock_fd(fd)
            os.close(fd)


def append_learned_rule(rule: Rule, *, now: datetime | None = None) -> None:
    """Add (or replace by id) a learned rule. Idempotent: re-learning the same
    id overwrites rather than duplicating. Stamps ``created`` if unset."""
    rule.source = "learned"
    if not rule.created:
        rule.created = (now or datetime.now()).isoformat(timespec="seconds")

    def apply(rules: list[Rule]) -> list[Rule]:
        return [*(r for r in rules if r.id != rule.id), rule]

    _mutate_learned(apply)


def update_learned_rule(
    rule_id: str,
    *,
    severity: str | None = None,
    hits: int | None = None,
    verify: bool | None = None,
) -> bool:
    """Patch a learned rule in place. Returns ``True`` if it existed and changed.

    Only learned rules are mutable — SEED rules are immutable code. Escalation
    (soft→hard, then ``verify=True``) goes through here.
    """
    found = False

    def apply(rules: list[Rule]) -> list[Rule]:
        nonlocal found
        for rule in rules:
            if rule.id == rule_id:
                found = True
                if severity is not None:
                    rule.severity = severity
                if hits is not None:
                    rule.hits = hits
                if verify is not None:
                    rule.verify = verify
        return rules

    _mutate_learned(apply)
    return found


def write_seed_policy() -> None:
    """Write the SEED ruleset to :func:`seed_policy_path` (scaffold-on-install).
    Best-effort; the guard does not depend on the file existing."""
    path = seed_policy_path()
    with contextlib.suppress(OSError):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps([_rule_to_dict(r) for r in SEED_RULES], indent=2) + "\n",
            encoding="utf-8",
        )
