# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Aaron K. Clark
"""Machine-readable note rules compiled into deterministic PreToolUse checks.

Every rule a hook can decide must never depend on model attention (#240): the
cryptojones.github.io exception was violated three times even though the
governing note was force-recalled each time. Operators declare rules in fenced
``omind-rule`` blocks inside ordinary vault notes::

    ```omind-rule
    id: no-direct-push-public-main
    tool: Bash
    match: "git push*"
    when:
      repo_visibility: public
      branch: [main, master]
    except_repos: [cryptojones.github.io]
    action: deny
    message: "Public repo: branch + PR required."
    ```

``load_rules`` scans top-level ``*.md`` for these blocks (cached per file
``(mtime_ns, size)``); invalid blocks are skipped with a breadcrumb, never
raised — a broken rule must never brick the guard. A note rule with the same
``id`` as a seed rule replaces it, so exceptions stay operator-editable.

v1 conditions are ``repo_visibility`` (via ``gh repo view``, cached one day,
**fail-open to UNKNOWN**: a rule conditioned on visibility does not fire when
visibility cannot be determined) and ``branch`` (the repo's checked-out
branch). ``except_repos`` matches the origin remote's repository name.
"""

from __future__ import annotations

import fnmatch
import json
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from omind import paths

ACTION_DENY = "deny"
ACTION_WARN = "warn"
_ACTIONS = (ACTION_DENY, ACTION_WARN)

#: Visibility cache TTL. Repo visibility changes rarely; `gh` calls are slow.
_VISIBILITY_TTL_HOURS = 24
_VISIBILITY_UNKNOWN = "unknown"

_BLOCK_RE = re.compile(r"```omind-rule\s*\n(.*?)```", re.DOTALL)

#: Cold-start seed: the incident class that motivated this module. The
#: repo-deletion incident is already covered by ``policy.SEED_RULES``.
#: Operators add per-repo exceptions by declaring a note rule with this same
#: ``id`` (note rules replace seeds by id).
SEED_NOTE_RULES: tuple[NoteRule, ...] = ()  # populated below the dataclass


@dataclass(frozen=True)
class NoteRule:
    id: str
    tool: str
    match: str
    action: str
    message: str
    when_visibility: str = ""
    when_branch: tuple[str, ...] = ()
    except_repos: tuple[str, ...] = ()
    note: str = "(seed)"
    invalid: str = ""  # non-empty on a skipped block: the reason, for `rules list`

    def conditioned_on_visibility(self) -> bool:
        return bool(self.when_visibility)


SEED_NOTE_RULES = (
    NoteRule(
        id="no-direct-push-public-main",
        tool="Bash",
        match="*git push*",
        action=ACTION_DENY,
        message=(
            "Public repo on main/master: feature branch + PR required, never a "
            "direct push. Declare an `omind-rule` block with this id in a vault "
            "note to add per-repo exceptions."
        ),
        when_visibility="public",
        when_branch=("main", "master"),
    ),
)


def _breadcrumb(context: str, exc: BaseException | str) -> None:
    from omind import hooks

    hooks._record_failure(context, exc if isinstance(exc, BaseException) else RuntimeError(exc))


def _parse_block(text: str, note: str) -> NoteRule:
    """One fenced block -> NoteRule; an invalid block returns a stub with
    ``invalid`` set (skipped by the matcher, shown by ``rules list``)."""
    import yaml

    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        return NoteRule("", "", "", "", "", note=note, invalid=f"YAML error: {exc}")
    if not isinstance(data, dict):
        return NoteRule("", "", "", "", "", note=note, invalid="not a mapping")
    rule_id = str(data.get("id") or "").strip()
    tool = str(data.get("tool") or "").strip()
    match = str(data.get("match") or "").strip()
    action = str(data.get("action") or "").strip().lower()
    message = str(data.get("message") or "").strip()
    when = data.get("when") if isinstance(data.get("when"), dict) else {}
    visibility = str(when.get("repo_visibility") or "").strip().lower()
    branches = when.get("branch")
    if isinstance(branches, str):
        branches = [branches]
    branches = tuple(str(b).strip() for b in branches or [] if str(b).strip())
    excepts = data.get("except_repos")
    if isinstance(excepts, str):
        excepts = [excepts]
    excepts = tuple(str(r).strip() for r in excepts or [] if str(r).strip())
    problems = []
    if not rule_id:
        problems.append("missing id")
    if not tool:
        problems.append("missing tool")
    if not match:
        problems.append("missing match")
    if action not in _ACTIONS:
        problems.append(f"action must be one of {_ACTIONS}")
    if action == ACTION_DENY and not message:
        problems.append("deny requires message")
    if problems:
        return NoteRule(
            rule_id, tool, match, action, message, note=note, invalid="; ".join(problems)
        )
    return NoteRule(
        id=rule_id,
        tool=tool,
        match=match,
        action=action,
        message=message,
        when_visibility=visibility,
        when_branch=branches,
        except_repos=excepts,
        note=note,
    )


#: Per-file parse cache: {path: ((mtime_ns, size), [NoteRule, ...])}.
_file_cache: dict[str, tuple[tuple[int, int], list[NoteRule]]] = {}


def _rules_in_file(path: Path) -> list[NoteRule]:
    try:
        stat = path.stat()
        key = (stat.st_mtime_ns, stat.st_size)
    except OSError:
        return []
    cached = _file_cache.get(str(path))
    if cached is not None and cached[0] == key:
        return cached[1]
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    rules: list[NoteRule] = []
    if "```omind-rule" in text:
        for block in _BLOCK_RE.findall(text):
            rule = _parse_block(block, path.name)
            if rule.invalid:
                _breadcrumb(f"rules({path.name})", f"skipped invalid rule: {rule.invalid}")
            rules.append(rule)
    _file_cache[str(path)] = (key, rules)
    return rules


def load_rules(omi_dir: Path | str, *, include_invalid: bool = False) -> list[NoteRule]:
    """Seed rules plus every valid note rule in top-level ``*.md``; a note rule
    replaces a seed rule with the same id. Never raises."""
    collected: dict[str, NoteRule] = {r.id: r for r in SEED_NOTE_RULES}
    invalid: list[NoteRule] = []
    try:
        notes = sorted(Path(omi_dir).glob("*.md"))
    except OSError:
        notes = []
    for path in notes:
        for rule in _rules_in_file(path):
            if rule.invalid:
                invalid.append(rule)
            else:
                collected[rule.id] = rule
    result = list(collected.values())
    return result + invalid if include_invalid else result


def _visibility_cache_path() -> Path:
    return paths.state_dir() / "repo-visibility.json"


def _repo_visibility(repo: Path, *, now: datetime | None = None) -> str:
    """``public`` / ``private`` / ``unknown`` for ``repo``, via ``gh``, cached
    on disk for a day. UNKNOWN on any failure — visibility-conditioned rules
    then do not fire (fail-open), but the miss is breadcrumbed."""
    now = now or datetime.now()
    path = _visibility_cache_path()
    cache: dict[str, Any] = {}
    try:
        cache = json.loads(path.read_text(encoding="utf-8"))
        entry = cache.get(str(repo))
        if isinstance(entry, dict):
            stamp = datetime.fromisoformat(str(entry.get("ts")))
            if now - stamp < timedelta(hours=_VISIBILITY_TTL_HOURS):
                return str(entry.get("visibility") or _VISIBILITY_UNKNOWN)
    except (OSError, ValueError, TypeError):
        cache = cache if isinstance(cache, dict) else {}
    try:
        proc = subprocess.run(
            ["gh", "repo", "view", "--json", "visibility", "-q", ".visibility"],
            cwd=repo,
            capture_output=True,
            text=True,
            timeout=10,
        )
        visibility = proc.stdout.strip().lower() if proc.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        visibility = ""
    if visibility not in ("public", "private", "internal"):
        _breadcrumb(f"rules_visibility({repo})", "gh visibility lookup failed")
        return _VISIBILITY_UNKNOWN
    cache[str(repo)] = {"visibility": visibility, "ts": now.isoformat(timespec="seconds")}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        paths.atomic_write_text(path, json.dumps(cache) + "\n", mode=0o600)
    except OSError:
        pass
    return visibility


def _repo_name(repo: Path) -> str:
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        url = proc.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""
    if not url:
        return ""
    name = url.rstrip("/").rsplit("/", 1)[-1]
    return name[:-4] if name.endswith(".git") else name


def _repo_branch(repo: Path) -> str:
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return proc.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


_PUSH_ARGS_RE = re.compile(r"\bgit\s+(?:-C\s+\S+\s+|-c\s+\S+\s+)*push\b(?P<rest>[^;|&`\n]*)")


def _pushed_branches(command: str) -> list[str] | None:
    """Branch names a ``git push`` explicitly targets, or ``None`` for a bare
    push (no refspec — the checked-out branch is what gets pushed).

    A tag push (``git push origin v8.6.1`` / ``--tags``) from a checked-out
    main matched the branch condition via HEAD and got denied (#240 v1 false
    positive): when the command names refspecs, judge those instead of HEAD.
    Refspecs like ``HEAD:main`` count as their destination.
    """
    match = _PUSH_ARGS_RE.search(command)
    if not match:
        return None
    refs: list[str] = []
    tokens = [t for t in match.group("rest").split() if t]
    positional: list[str] = []
    for token in tokens:
        if token == "--tags":
            refs.append("(tags)")
            continue
        if token.startswith("-"):
            continue
        positional.append(token)
    # First positional token is the remote; the rest are refspecs.
    for token in positional[1:]:
        dest = token.rsplit(":", 1)[-1]
        dest = dest.removeprefix("refs/heads/")
        if dest.startswith("refs/tags/") or re.fullmatch(r"v?\d+[\w.\-]*", dest):
            refs.append("(tags)")
        else:
            refs.append(dest)
    return refs or None


@dataclass(frozen=True)
class RuleHit:
    rule: NoteRule
    outcome: str  # "deny" | "warn"
    detail: str = ""


def evaluate(
    action: dict[str, Any],
    omi_dir: Path | str,
    repo: Path | None,
    *,
    rules: list[NoteRule] | None = None,
) -> RuleHit | None:
    """First matching rule for ``action``, or ``None``. Deterministic, no model.

    ``repo`` is the enclosing git repo when the guard resolved one; rules with
    repo-scoped conditions (visibility/branch/except_repos) require it and do
    not fire without one.
    """
    tool = str(action.get("tool") or "")
    command = str(action.get("command") or "")
    target = command or str(action.get("path") or "")
    for rule in rules if rules is not None else load_rules(omi_dir):
        if rule.invalid:
            continue
        if rule.tool not in ("*", tool):
            continue
        if not fnmatch.fnmatch(target, rule.match):
            continue
        repo_scoped = (
            rule.conditioned_on_visibility() or rule.when_branch or rule.except_repos
        )
        if repo_scoped:
            if repo is None:
                continue
            if rule.except_repos and _repo_name(repo) in rule.except_repos:
                continue
            if rule.when_branch:
                pushed = _pushed_branches(command)
                branches = pushed if pushed is not None else [_repo_branch(repo)]
                if not any(branch in rule.when_branch for branch in branches):
                    continue
            if rule.conditioned_on_visibility():
                visibility = _repo_visibility(repo)
                if visibility == _VISIBILITY_UNKNOWN:
                    # Fail-open: never deny on a condition we could not check.
                    return RuleHit(rule, "unknown-visibility", "visibility unknown")
                if visibility != rule.when_visibility:
                    continue
        return RuleHit(rule, rule.action)
    return None


def format_rules(omi_dir: Path | str) -> str:
    """Human-readable compiled-rule listing for ``omind rules list``."""
    lines: list[str] = []
    for rule in load_rules(omi_dir, include_invalid=True):
        if rule.invalid:
            lines.append(f"[skipped] {rule.note}: {rule.invalid}")
            continue
        conditions = []
        if rule.when_visibility:
            conditions.append(f"visibility={rule.when_visibility}")
        if rule.when_branch:
            conditions.append(f"branch in {list(rule.when_branch)}")
        if rule.except_repos:
            conditions.append(f"except {list(rule.except_repos)}")
        cond = f" when {', '.join(conditions)}" if conditions else ""
        lines.append(
            f"[{rule.action}] {rule.id}: {rule.tool} {rule.match!r}{cond} "
            f"(from {rule.note})"
        )
    return "\n".join(lines) if lines else "(no rules)"
