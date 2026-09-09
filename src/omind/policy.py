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


def opt_in_satisfied(opt_in: str, command: str) -> bool:
    """True only when the ``VAR=VALUE`` opt-in token appears as a REAL leading
    environment assignment — at the command start, right after a shell separator
    (``;`` / ``&&`` / ``|`` / a NEWLINE), or via ``env`` — so it actually takes effect.

    A bare substring match (the old behaviour) let the token be forged in a comment
    or a string arg (``rm -rf / # OMI_SUDO_OK=1``, ``echo "OMI_SUDO_OK=1"``) and
    silently bypass a hard rule without ever setting the variable. That is not a
    deliberate opt-in, so it must not skip the deny.

    A newline IS a shell command boundary, so a line-leading assignment inside a
    multi-line script (``…\\n  OMI_PUSH_GITHUB=1 git push …``) is legitimate and must
    be recognised — omitting ``\\n`` from the separator class wrongly rejected it
    (3.0.2). A plain space is NOT a separator, so a mid-line ``echo OMI_SUDO_OK=1``
    still doesn't count.

    The optional ``env `` prefix must ITSELF be at command position — otherwise
    ``echo "use env OMI_SUDO_OK=1" && sudo …`` forged the opt-in from inside a
    string (the ``\\benv``-anywhere bug) and skipped a hard rule.

    Lives here (not in guard.py) so both the guard's deny path and the compliance
    detector's Layer-E recording share one strict matcher (2026-08-27 review)."""
    pattern = r"(?:^|[;&|\n])[ \t]*(?:env[ \t]+)?" + re.escape(opt_in) + r"(?=\s|$)"
    return re.search(pattern, command) is not None

#: Shell wrapper/keyword tokens that transparently precede the real command, so
#: the anchored token is still in command position after them:
#: ``if/while … ; then sudo …``, ``exec sudo …``, ``nohup sudo …``,
#: ``xargs sudo …``, ``time sudo …``. Without these, ``if true; then sudo rm``
#: sailed past the sudo hard rule (a fail-open of a hard control).
_CMD_WRAPPERS = r"then|do|else|elif|exec|nohup|command|time|builtin|xargs"

#: Prefix that anchors a ``match="command"`` pattern to COMMAND POSITION: the
#: command start, or immediately after a shell separator (``;`` ``&`` ``|``
#: NEWLINE ``(`` backtick — single chars suffice since ``&&`` / ``||`` / ``$(``
#: all END in a char in the class), skipping any leading ``VAR=val`` environment
#: assignments and shell wrapper keywords (:data:`_CMD_WRAPPERS`), and an
#: optional absolute/relative path to the binary (``/usr/bin/sudo``,
#: ``./x/sudo``). This is how a token like ``sudo`` is matched only when it is
#: the command being run — not when it appears as a grep arg, a path segment, a
#: filename, a commit message, or a ``pass show sudo/...`` value (the #98/#108
#: false-positive class). It mirrors the leading-assignment idea already proven
#: in ``guard._opt_in_satisfied``. Use ``[ \t]`` (not ``\s``) so the
#: assignment-skip never crosses a newline into another command.
_CMD_POSITION = (
    r"(?:^|[\n;&|`(])[ \t]*"
    r"(?:(?:\w+=\S*|" + _CMD_WRAPPERS + r")[ \t]+)*"
    r"(?:[./][^\s;&|`()]*/)?"
)

#: Interpreters whose heredoc body IS shell code for THIS shell to run, so its
#: separators are real and its body must stay visible to
#: :func:`shell_code_text`. Anything else (``cat``, ``python``, ``gh issue
#: create --body``) receives the body as DATA.
_SHELL_HEREDOC_BINARIES = frozenset({"sh", "bash", "zsh", "dash", "ksh", "mksh", "ash"})

#: Tokens that transparently precede the real binary when deciding whether a
#: heredoc's owner is a shell (``env FOO=1 bash <<EOF``).
_HEREDOC_OWNER_SKIP = frozenset({"env", "command", "exec", "nohup", "time", "builtin"})

#: A heredoc redirection: ``<<EOF``, ``<<-EOF``, ``<<'EOF'``, ``<<"EOF"``.
_HEREDOC_RE = re.compile(
    r"<<(-?)[ \t]*(?:(['\"])([A-Za-z_][A-Za-z0-9_]*)\2|([A-Za-z_][A-Za-z0-9_]*))"
)


def _heredoc_owner_is_shell(command: str, start: int) -> bool:
    """True when the simple command owning the heredoc at ``start`` is a shell.

    Scans back to the nearest separator and takes the first token that is not a
    ``VAR=val`` assignment or a transparent wrapper, comparing its basename.
    """
    segment = command[:start]
    cut = max(segment.rfind(c) for c in "\n;&|(`")
    for token in segment[cut + 1 :].split():
        base = token.rsplit("/", 1)[-1]
        if "=" in token and not token.startswith("-"):
            continue
        if base in _HEREDOC_OWNER_SKIP:
            continue
        return base in _SHELL_HEREDOC_BINARIES
    return False


def shell_code_text(command: str) -> str:
    """``command`` with every DATA region blanked out, length-preserving.

    A quoted string's body, and a heredoc body fed to something that is not a
    shell, are payload — text handed to another program or another host. They
    are *not* commands this shell runs, so the separators inside them are not
    separators (#317). Blanking them (to spaces, so offsets and any surrounding
    structure survive) is what lets a command-position test mean what it says.

    This is the anchoring primitive behind ``match="command"`` and the guard's
    local-repo classifiers. Without it, the ``&&`` inside
    ``ssh host 'cd /p && git commit …'`` made a REMOTE commit look like local
    repo work, and a newline inside a ``gh issue create --body "$(cat <<'MD' …``
    prose block put every line of that prose in command position — both
    reproduced live while writing #317.

    Deliberately NOT applied to the side-effect classifiers: a
    ``ssh host 'systemctl restart …'`` is a real side effect, merely a remote
    one, and masking there would be a fail-open rather than a fix. Shell
    heredocs (``bash <<EOF``) keep their bodies for the same reason.

    Command substitutions inside a double-quoted string (``"$(cat …)"``,
    ``"`cmd`"``) are code again — the shell runs them — so the scanner steps
    back into code context for their extent.
    """
    out = list(command)
    stack: list[str] = []
    heredocs: list[tuple[str, bool, bool]] = []
    i, n = 0, len(command)
    while i < n:
        ch = command[i]
        if stack and stack[-1] in "'\"":
            # --- DATA context: blank everything up to the closing quote. ---
            if stack[-1] == '"':
                # Only a double-quoted body re-enters code for a substitution,
                # and only there does a backslash escape the next character.
                if ch == "\\" and i + 1 < n:
                    out[i] = out[i + 1] = " "
                    i += 2
                    continue
                if command.startswith("$(", i):
                    stack.append("(")
                    i += 2
                    continue
                if ch == "`":
                    stack.append("`")
                    i += 1
                    continue
            if ch == stack[-1]:
                stack.pop()
            else:
                # The newline is blanked too: it is the separator that put
                # prose lines in command position.
                out[i] = " "
            i += 1
            continue
        # --- CODE context. ---
        if ch == "\\" and i + 1 < n:
            i += 2
            continue
        if ch in "'\"":
            stack.append(ch)
            i += 1
            continue
        if command.startswith("$(", i):
            stack.append("(")
            i += 2
            continue
        if ch == "`":
            if stack and stack[-1] == "`":
                stack.pop()
            else:
                stack.append("`")
            i += 1
            continue
        if ch == ")" and stack and stack[-1] == "(":
            stack.pop()
            i += 1
            continue
        match = _HEREDOC_RE.match(command, i)
        if match:
            delimiter = match.group(3) or match.group(4)
            heredocs.append(
                (delimiter, bool(match.group(1)), not _heredoc_owner_is_shell(command, i))
            )
            i = match.end()
            continue
        if ch == "\n" and heredocs:
            i = _blank_heredoc_bodies(command, out, i + 1, heredocs)
            continue
        i += 1
    return "".join(out)


def _blank_heredoc_bodies(
    command: str, out: list[str], pos: int, heredocs: list[tuple[str, bool, bool]]
) -> int:
    """Consume the pending heredoc bodies starting at ``pos``, blanking those
    marked as data. Returns the offset just past the last one. An UNTERMINATED
    heredoc consumes the rest of the string — it never runs as this shell's
    code, so leaving its tail in command position would only re-open #317."""
    n = len(command)
    for delimiter, strip_tabs, blank in heredocs:
        while pos < n:
            eol = command.find("\n", pos)
            end = n if eol == -1 else eol
            line = command[pos:end]
            if (line.lstrip("\t") if strip_tabs else line).strip() == delimiter:
                pos = end if eol == -1 else eol + 1
                break
            if blank:
                for k in range(pos, end if eol == -1 else eol + 1):
                    out[k] = " "
            pos = end if eol == -1 else eol + 1
    heredocs.clear()
    return pos


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
    #: ``"search"`` (default) matches ``pattern`` anywhere in the command.
    #: ``"command"`` wraps ``pattern`` in :data:`_CMD_POSITION` so it only fires
    #: when the token is in command position (start / after a shell separator,
    #: past leading env-assignments) — for escalation-keyword rules that must not
    #: false-positive on the keyword appearing as an argument or in a string.
    match: str = "search"
    source: str = "seed"
    created: str = ""
    hits: int = 0
    #: Set by escalation once a rule recurs past the verifier threshold: the
    #: action-type is flagged for Layer C scrutiny even when it would otherwise
    #: pass the gate. Carried in data so the learning loop owns the decision.
    verify: bool = False

    def compiled(self) -> re.Pattern[str]:
        if self.match == "command":
            return re.compile(_CMD_POSITION + r"(?:" + self.pattern + r")")
        return re.compile(self.pattern)

    def matches(self, command: str) -> bool:
        """True when this rule fires on ``command``.

        A ``match="command"`` rule is tested against :func:`shell_code_text`, so
        the keyword must be in command position in code the LOCAL shell runs —
        not inside a quoted payload or a prose heredoc body (#317). A
        ``match="search"`` rule keeps the raw text: those patterns are
        deliberately substring searches.
        """
        subject = shell_code_text(command) if self.match == "command" else command
        return bool(self.compiled().search(subject))

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
        # match="command" (#101): anchor to command position so `grep -rn "gh
        # auth setup-git"`, a commit message, or a heredoc writing this rule no
        # longer false-block — routine when working on omind itself.
        pattern=r"gh\s+auth\s+setup-git\b",
        match="command",
        message=(
            "never 'gh auth setup-git'. GitHub auth = the gh-YOLO PAT from pass via "
            "a one-shot (per-command) credential helper. Read OMI: github-auth-ssh."
        ),
    ),
    Rule(
        id="gh-repo-delete",
        pattern=r"gh\s+repo\s+delete\b",
        match="command",
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
        # Command-anchored (#101) so the phrase in a grep/commit message is safe.
        pattern=r"gh\s+api(?=[^|;&]*(?:-X\s*|--method\s*)DELETE)(?=[^|;&]*repos/)",
        match="command",
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
        match="command",
        message=(
            "never DELETE a GitHub repo/resource via the raw API. Use the reviewed "
            "path; typed-name confirmation only. Read OMI: Operational Rules - Git "
            "Repos and Secrets."
        ),
    ),
    Rule(
        id="sudo-use-fleet-sudo",
        # #98/#108: match `sudo` only in COMMAND POSITION (see _CMD_POSITION), not
        # as any token in the string — so `grep sudo`, `cat /var/log/sudo.log`,
        # `git commit -m "fix sudo"`, `pass show sudo/akclark`, and the sanctioned
        # `fleet-sudo --entry akclark/sudo` no longer false-positive, while
        # `sudo …`, `; sudo …`, `a && sudo …`, `a | sudo …`, `$(sudo …)`, and
        # `FOO=1 sudo …` still block. `fleet-sudo` never matches (it is not a
        # command-position `sudo` token), so no lookbehind is needed.
        pattern=r"sudo(?:edit)?\b",
        match="command",
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
        # run0 / su walked straight past. Same tier + opt-in as raw sudo, and the
        # same command-position anchoring (#98/#108) so `man su`, `git log --grep
        # su`, `cat doas.conf`, `tmux new -s run0` don't false-positive while
        # `pkexec …` / `doas …` / `su -c … root` (at command position) still block.
        pattern=r"(?:pkexec|doas|run0|su)\b",
        match="command",
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
    never fatal).

    A learned/hand-edited rule whose ``pattern`` does not compile — or matches
    the empty string (a trailing ``|``) — would crash or hard-block the guard on
    EVERY tool call (a bricked machine). It is dropped here so a bad table can
    never reach the guard hot path. A wrong-typed ``severity`` (a JSON number)
    that would silently demote a hard rule to non-blocking is likewise rejected.
    """
    kwargs = {k: v for k, v in data.items() if k in _RULE_FIELDS}
    required = ("id", "pattern", "message")
    if not all(isinstance(kwargs.get(k), str) and kwargs.get(k) for k in required):
        return None
    # Optional string fields must be strings if present (a JSON number for
    # ``severity`` would demote a hard rule; a bad ``match`` mode would confuse
    # the anchor logic).
    for key in ("severity", "tier", "match", "source", "created"):
        if key in kwargs and not isinstance(kwargs[key], str):
            return None
    try:
        rule = Rule(**kwargs)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    try:
        compiled = rule.compiled()
    except re.error:
        return None
    if compiled.search(""):  # matches everything → would block every action
        return None
    return rule


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
