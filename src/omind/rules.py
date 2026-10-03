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

Conditions are ``repo_visibility`` (via ``gh repo view``, cached one day,
**fail-open to UNKNOWN**: a rule conditioned on visibility does not fire when
visibility cannot be determined), ``branch`` (the repo's checked-out branch),
and ``repo_has_commits`` (whether the *remote* holds any commit yet).
``except_repos`` matches the origin remote's repository name.

``repo_has_commits`` exists so a deny can be narrowed to repos that actually
have history — an initial commit into an empty repo has nothing to open a pull
request against, so the branch+PR ceremony cannot apply to it::

    when:
      repo_visibility: public
      branch: [main, master]
      repo_has_commits: true    # only deny once there IS history to protect

It reads the REMOTE, not the local checkout: you must commit locally before you
can push, so a local check would never see an empty repo. Unlike visibility this
condition **fails safe** — when the remote cannot be reached the condition is
treated as satisfied, so an unreachable network can never silently hand out the
empty-repo exemption. Narrowing a deny is exactly where fail-open would be
wrong.
"""

from __future__ import annotations

import fnmatch
import functools
import json
import os
import re
import shlex
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from omind import deadline, filelock, paths, policy

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
    when_has_commits: bool | None = None
    except_repos: tuple[str, ...] = ()
    note: str = "(seed)"
    invalid: str = ""  # non-empty on a skipped block: the reason, for `rules list`

    def conditioned_on_visibility(self) -> bool:
        return bool(self.when_visibility)

    def conditioned_on_has_commits(self) -> bool:
        return self.when_has_commits is not None


SEED_NOTE_RULES = (
    NoteRule(
        id="no-direct-push-public-main",
        tool="Bash",
        # Also matches `git -C <dir> push` / `git -c k=v push`: rules are
        # tested with git's global options dropped too (#414).
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
    when_raw = data.get("when")
    when = when_raw if isinstance(when_raw, dict) else {}
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
    has_commits: bool | None = None
    if "repo_has_commits" in when:
        raw = when.get("repo_has_commits")
        if isinstance(raw, bool):
            has_commits = raw
        else:
            # A bare `repo_has_commits: yes` parses as bool in YAML, but a typo
            # like `repo_has_commits: "true"` would silently become truthy, so
            # anything non-boolean is rejected loudly rather than guessed at.
            problems.append("repo_has_commits must be true or false")
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
        when_has_commits=has_commits,
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


def _lookup(
    args: list[str], cap: float, cwd: Path | None = None
) -> subprocess.CompletedProcess[str]:
    """``subprocess.run`` for one fact lookup, under its own timeout (#460):
    ``cap`` outside the guard, cut by :func:`deadline.lookup_timeout` while it
    judges, so a slow ``gh``/``git`` leaves that fact unknown instead of
    spending the judging budget. Raises :class:`subprocess.TimeoutExpired`
    without starting the process when no lookup time is left; every caller
    already treats that as an unknown fact."""
    timeout = deadline.lookup_timeout(cap)
    if timeout <= 0:
        raise subprocess.TimeoutExpired(args, 0)
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=timeout)


def _has_github_remote(repo: Path) -> bool:
    """True if ``repo`` has any git remote pointing at github.com.

    Used to tell a genuine ``gh`` failure apart from a repo that is simply not on
    GitHub (a local or mesh-only repo, e.g. the OMI vault that pushes to pluto/seed
    over SSH). The latter must not be logged as a failure — it is expected.
    """
    try:
        proc = _lookup(["git", "-C", str(repo), "remote", "-v"], 5)
    except (OSError, subprocess.SubprocessError):
        return False
    if proc.returncode != 0:
        return False
    return any(_is_github_host(url) for url in _remote_urls(proc.stdout))


def _remote_urls(remote_v: str) -> list[str]:
    """The URLs out of ``git remote -v`` output (``name<TAB>url (fetch|push)``)."""
    urls = []
    for line in remote_v.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            urls.append(parts[1])
    return urls


def _is_github_host(url: str) -> bool:
    """True only if ``url``'s HOST is github.com (or a subdomain of it).

    A substring test is not enough: ``https://github.com.evil.example/x`` and
    ``https://not-github.com/x`` both contain the string but are not GitHub, and
    misclassifying one flips the public-repo branch+PR deny into silence.
    """
    host = ""
    if "://" in url:
        host = url.split("://", 1)[1].split("/", 1)[0]
    elif ":" in url:  # scp-like: [user@]host:path
        host = url.split(":", 1)[0]
    host = host.rsplit("@", 1)[-1].split("?", 1)[0]  # strip creds, query
    host = host.rsplit(":", 1)[0] if host.count(":") == 1 else host  # strip :port
    host = host.strip().lower().rstrip(".")
    return host == "github.com" or host.endswith(".github.com")


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
        proc = _lookup(
            ["gh", "repo", "view", "--json", "visibility", "-q", ".visibility"], 10, cwd=repo
        )
        visibility = proc.stdout.strip().lower() if proc.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        visibility = ""
    if visibility not in ("public", "private", "internal"):
        if not _has_github_remote(repo):
            # No GitHub remote at all (e.g. the OMI mesh vault): ``gh`` cannot
            # classify it and that is expected, not a failure. Such a repo is by
            # definition not public, so treat it as private — visibility-conditioned
            # public-repo rules then correctly do not fire — and cache it WITHOUT a
            # breadcrumb. Only a repo that HAS a GitHub remote yet fails lookup is a
            # real error worth recording.
            visibility = "private"
        else:
            _breadcrumb(f"rules_visibility({repo})", "gh visibility lookup failed")
            return _VISIBILITY_UNKNOWN
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = path.with_name(path.name + ".lock")
        with filelock.exclusive(lock_path):
            latest_cache: dict[str, Any] = {}
            try:
                latest_cache = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError):
                latest_cache = {}
            latest_cache[str(repo)] = {
                "visibility": visibility,
                "ts": now.isoformat(timespec="seconds"),
            }
            paths.atomic_write_text(path, json.dumps(latest_cache) + "\n", mode=0o600)
    except OSError:
        pass
    return visibility


def _repo_name(repo: Path) -> str:
    try:
        proc = _lookup(["git", "-C", str(repo), "remote", "get-url", "origin"], 5)
        url = proc.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""
    if not url:
        return ""
    name = url.rstrip("/").rsplit("/", 1)[-1]
    return name[:-4] if name.endswith(".git") else name


def _remote_has_commits(repo: Path) -> bool | None:
    """Whether ``origin`` holds any commit yet. ``None`` when undeterminable.

    Deliberately reads the remote rather than the local checkout: a push is
    always preceded by a local commit, so a local probe would report "has
    commits" every time and the empty-repo case could never be detected.

    Not cached. A repo goes from empty to non-empty exactly once, and that
    single transition is the whole point of the condition — a stale cache would
    keep granting the exemption after the first commit landed.
    """
    try:
        proc = _lookup(["git", "-C", str(repo), "ls-remote", "--heads", "origin"], 10)
    except (OSError, subprocess.SubprocessError):
        _breadcrumb(f"rules_has_commits({repo})", "ls-remote failed")
        return None
    if proc.returncode != 0:
        # No origin, no network, or no auth. Unknown, not "empty".
        _breadcrumb(f"rules_has_commits({repo})", "ls-remote returned non-zero")
        return None
    return bool(proc.stdout.strip())


def _repo_branch(repo: Path) -> str:
    try:
        proc = _lookup(["git", "-C", str(repo), "rev-parse", "--abbrev-ref", "HEAD"], 5)
        return proc.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def _git_out(repo: Path, *args: str, config: tuple[str, ...] = ()) -> str | None:
    """``git [-c k=v]... -C repo <args>`` stdout, or None on any failure (fails
    open). ``config`` is the push site's own ``-c`` settings (#433 review),
    filtered by :func:`_cmdline_config`, so a read sees what the push will."""
    flags = [f for kv in config for f in ("-c", kv)]
    try:
        proc = _lookup(["git", *flags, "-C", str(repo), *args], 5)
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout if proc.returncode == 0 else None


def _push_remote(repo: Path, current: str, remotes: list[str], config: tuple[str, ...]) -> str:
    """The remote a ``git push`` that names none goes to. On a branch: its
    ``pushRemote``, ``remote.pushDefault``, its ``remote``, else ``origin``.
    On a detached HEAD (#433 review) there is no branch to ask, so
    ``remote.pushDefault``, else ``origin``, else the single remote."""
    if current and current != "HEAD":
        found = (
            _git_out(repo, "config", f"branch.{current}.pushRemote", config=config)
            or _git_out(repo, "config", "remote.pushDefault", config=config)
            or _git_out(repo, "config", f"branch.{current}.remote", config=config)
            or "origin"
        )
        return found.strip()
    found = _git_out(repo, "config", "remote.pushDefault", config=config) or ""
    if found.strip():
        return found.strip()
    if "origin" in remotes or len(remotes) != 1:
        return "origin"
    return remotes[0]


def _default_push_branches(repo: Path, remote: str, config: tuple[str, ...] = ()) -> list[str]:
    """Branches a refspec-less ``git push [<remote>]`` in ``repo`` lands on
    (#423): what ``@{push}`` resolves to, so ``feature/x`` tracking
    ``origin/main`` under ``push.default=upstream`` is judged as ``main``. A
    configured ``remote.<name>.push`` refspec sourced from ``HEAD`` adds its
    destination too, because ``@{push}`` does not resolve one (git 2.54 says
    "push refspecs for 'origin' do not include 'feature'").

    ``config`` is the push's own ``git -c k=v`` settings, which win over the
    stored ones (#433 review).

    Fails open: anything git cannot answer (detached HEAD, no upstream, not a
    repo, git missing) leaves the checked-out branch, judged as before.
    """
    current = _repo_branch(repo)
    dests: list[str] = []
    try:
        push = _git_out(
            repo, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{push}", config=config
        )
        remotes = (_git_out(repo, "remote") or "").split()
        push = (push or "").strip()
        owner = max((r for r in remotes if push.startswith(r + "/")), key=len, default="")
        if owner and (not remote or remote == owner):
            dests.append(push.removeprefix(owner + "/"))
        if not remote:
            remote = _push_remote(repo, current, remotes, config)
        specs: list[str] = []
        if remote in remotes:
            # `remote.<name>.mirror` makes a bare push a `--mirror` (#433
            # review); git refuses refspecs with it, so nothing else applies.
            mirror = _git_out(repo, "config", "--bool", f"remote.{remote}.mirror", config=config)
            if (mirror or "").strip() == "true":
                return _mirror_branches(repo, remote) or [current]
            specs = (
                _git_out(repo, "config", "--get-all", f"remote.{remote}.push", config=config) or ""
            ).split()
            for spec in specs:
                src, _, dst = spec.lstrip("+").partition(":")
                if src.upper() in _CURRENT_BRANCH_REFS:
                    dests.append(dst.removeprefix("refs/heads/") or current)
        # `push.default=matching`, or a configured `:` refspec (which wins
        # over push.default), pushes every local branch the remote also has,
        # not just the checked-out one (#433). A URL has no stored refspecs
        # but still follows push.default.
        default = (_git_out(repo, "config", "push.default", config=config) or "").strip()
        if any(s.lstrip("+") == ":" for s in specs) or (not specs and default == "matching"):
            dests += _matching_branches(repo, remote)
    except Exception as exc:  # noqa: BLE001 - enforcement fails open
        _breadcrumb(f"rules_push_dest({repo})", exc)
        dests = []
    return dests or [current]


def _local_branches(repo: Path) -> list[str]:
    """Every local branch of ``repo``: what ``git push --all`` sends (#433).
    Empty when git cannot answer (fails open). Read from the full ref, because
    ``%(refname:short)`` shortens ``main`` to ``heads/main`` when a tag is also
    named ``main`` (#433 review)."""
    out = _git_out(repo, "for-each-ref", "--format=%(refname)", "refs/heads/")
    return [ref.removeprefix("refs/heads/") for ref in (out or "").split()]


def _tracked_branches(repo: Path, remote: str) -> set[str]:
    """Branches ``remote`` has, read from its remote-tracking refs, not over
    the network, so the guard never blocks on a slow remote. Empty when the
    remote was never fetched, is a URL, or git cannot answer."""
    if not remote:
        return set()
    prefix = f"refs/remotes/{remote}/"
    out = _git_out(repo, "for-each-ref", "--format=%(refname)", prefix)
    return {ref.removeprefix(prefix) for ref in (out or "").split()} - {"HEAD"}


def _matching_branches(repo: Path, remote: str) -> list[str]:
    """Local branches of ``repo`` that ``remote`` also has: what a matching
    push (``push.default=matching``, or the ``:`` refspec) sends (#433).

    A clone tracks the remote's ``main``, which is the case the rule exists
    for. When the remote's branches are unknown (never fetched, a URL, or none
    tracked), every local branch is judged instead: an unknown must never
    widen an exemption (#433 review).
    """
    local = _local_branches(repo)
    theirs = _tracked_branches(repo, remote)
    if not theirs:
        return local
    return [b for b in local if b in theirs]


def _mirror_branches(repo: Path, remote: str) -> list[str]:
    """What ``git push --mirror`` changes (#433 review): every local branch it
    sends, and every branch of ``remote`` it deletes because ``repo`` lacks
    it. A deletion of ``main`` is a push to ``main`` (#424)."""
    local = _local_branches(repo)
    return local + sorted(_tracked_branches(repo, remote) - set(local))


def _mirror_target(repo: Path, remote: str) -> list[str]:
    """:func:`_mirror_branches` for ``git push --mirror [<remote>]``, resolving
    the default remote when none is named. Fails open to no branches."""
    try:
        if not remote:
            remotes = (_git_out(repo, "remote") or "").split()
            remote = _push_remote(repo, _repo_branch(repo), remotes, ())
        return _mirror_branches(repo, remote)
    except Exception as exc:  # noqa: BLE001 - enforcement fails open
        _breadcrumb(f"rules_mirror({repo})", exc)
        return []


#: Config keys of a push site's own ``git -c k=v`` that the guard passes to its
#: git reads (#433 review). Only the ones that decide where a push lands: an
#: arbitrary key (``core.fsmonitor``) would let the command run code in the
#: guard's own git calls.
_PUSH_CONFIG_KEY_RE = re.compile(
    r"push\.default|remote\.pushdefault|remote\..+\.(?:push|mirror)"
    r"|branch\..+\.(?:pushremote|remote|merge)",
    re.IGNORECASE,
)


def _cmdline_config(options: str) -> tuple[str, ...]:
    """The push-related ``-c k=v`` settings in ``options``, git's global
    options before ``push`` (#433 review), unquoted, in command order."""
    found: list[str] = []
    for m in re.finditer(rf"(?:^|[ \t])-c[ \t]+({_GIT_OPT_VALUE})", options):
        try:
            words = shlex.split(m.group(1))
        except ValueError:
            continue
        value = "".join(words)
        key = value.partition("=")[0]
        if _PUSH_CONFIG_KEY_RE.fullmatch(key):
            found.append(value)
    return tuple(found)


# Accept bare tokens, quoted paths (which may contain spaces), and blanked quoted
# literals (#317 / #333 / #345) after -C or -c. One shell word: unquoted runs
# and quoted runs in any order, so `user.name='A B'` is one value (#414). A
# backslash escapes the next character (`O\'Brien`, `a=\"b`): shell_code_text
# leaves an escaped quote outside quotes as-is, and read as an unclosed quoted
# run it hid the push from the rule (#431 review). Each alternative starts with a
# different character, so the match stays linear.
_GIT_OPT_VALUE = r"""(?:\\.|[^\s"'\\]|"[^"]*"|'[^']*')+"""
# git's global options between `git` and the subcommand (#414): `-C <dir>`,
# `-c k=v`, `--git-dir[=]<p>` and its siblings, and flag-only options such as
# `--no-pager` / `-P`. A subcommand never starts with `-`, so the run of options
# stops there and `git log --grep push` still reads as `log`.
#
# The alternatives must not overlap: `--git-dir=X` once matched both the named
# branch and the generic `--long[=v]` one, so a run of them with no push after
# backtracked exponentially (8.6 s at 24 options, #413 review 3). The generic
# branch excludes the named options by lookahead; possessive quantifiers and
# atomic groups would also do it, but need Python 3.11 and we support 3.10.
_GIT_NAMED_OPTS = r"(?:git-dir|work-tree|namespace|super-prefix|config-env)"
_GIT_GLOBAL_OPTS = (
    rf"(?:-[Cc][ \t]+{_GIT_OPT_VALUE}[ \t]+"
    rf"|--{_GIT_NAMED_OPTS}(?:=|[ \t]+){_GIT_OPT_VALUE}[ \t]+"
    rf"|--(?!{_GIT_NAMED_OPTS}(?![a-z-]))[a-z][a-z-]*(?:={_GIT_OPT_VALUE})?[ \t]+"
    rf"|-[pP][ \t]+)*"
)
_PUSH_ARGS_RE = re.compile(rf"\bgit[ \t]+{_GIT_GLOBAL_OPTS}push\b(?P<rest>[^;|&`)\n]*)")
_GIT_OPTS_RE = re.compile(rf"\bgit[ \t]+{_GIT_GLOBAL_OPTS}")


def _canonical_git(text: str) -> str:
    """``text`` with git's global options dropped (`git -C d -c k=v push` ->
    `git push`), so a rule written as ``*git push*`` matches a push however its
    options are spelled (#414)."""
    return _GIT_OPTS_RE.sub("git ", text)


#: Characters that make a glob more than a literal.
_GLOB_SPECIAL_RE = re.compile(r"[*?\[]")


def _glob_matches(text: str, pattern: str) -> bool:
    """``fnmatch.fnmatch(text, pattern)``. A ``*literal*`` pattern, the usual
    rule shape, is a substring test: fnmatch's ``.*`` backtracks over the
    whole text, which was slow on a long command (#445)."""
    core = pattern[1:-1]
    if len(pattern) >= 2 and pattern[0] == pattern[-1] == "*" and not _GLOB_SPECIAL_RE.search(core):
        return os.path.normcase(core) in os.path.normcase(text)
    return fnmatch.fnmatch(text, pattern)


def _rule_matches(text: str, pattern: str) -> bool:
    deadline.check()  # one call per site: a long chain stays inside the hook budget (#460)
    return _glob_matches(text, pattern) or _glob_matches(_canonical_git(text), pattern)


_BLANKED_QUOTE_RE = re.compile(r"""'( *)'|"( *)\"""")
#: Any blank ``str.isspace`` would find: one regex step per quoted word, not
#: a Python generator per character (#445).
_BLANK_RE = re.compile(r"\s")
#: A quoted single word in the raw text: every quote :func:`_code_text`
#: unquotes is one of these (a blanked quote's body is the raw body, so a
#: blank-free one matches here at the same offset).
_QUOTED_WORD_RE = re.compile(r"""'[^'\s]+'|"(?:[^"\s\\]|\\\S)+\"""")


def _code_text(text: str) -> str:
    """``text`` as the shell runs it, for matching a repo-scoped rule (#413
    review 3): quoted data blanked (:func:`policy.shell_code_text`), so
    ``grep 'git push' README.md`` and ``git commit -m "… git push …"`` do not
    read as a push. A quoted SINGLE word is unquoted instead: ``git "push"``
    and ``origin 'main'`` are arguments, not prose."""

    def unquote(m: re.Match[str]) -> str:
        inner = text[m.start() + 1 : m.end() - 1]
        return inner if inner and not _BLANK_RE.search(inner) else m.group()

    code = policy.shell_code_text(text)
    if not _QUOTED_WORD_RE.search(text):
        # No quoted single word anywhere: every blanked quote stays blank, so
        # there is nothing to unquote one match at a time (#445).
        return code
    return _BLANKED_QUOTE_RE.sub(unquote, code)


#: Refspecs that name the checked-out branch, resolved per target repo (#423).
#: Compared upper-cased: on a case-insensitive filesystem (macOS APFS, Windows
#: NTFS) git resolves `head` to `.git/HEAD`, so `git push origin head` pushes
#: the checked-out branch; matching it everywhere only makes the rule stricter.
_CURRENT_BRANCH_REFS = frozenset({"HEAD", "@"})
#: Marker :func:`_pushed_branches` returns for a `HEAD` / `@` refspec with no
#: explicit destination: the checked-out branch of the target repo.
_CURRENT_REF = "(current)"
#: Marker prefix for a push with no refspec (`git push`, `git push origin`),
#: judged by where it really lands, ``@{push}`` (#423). The remote follows.
_DEFAULT_PUSH = "(default-push):"
#: Separates a ``(default-push):`` marker's remote from the push's own ``-c
#: k=v`` settings (#433 review), which :func:`_judge` hands to git.
_CONFIG_SEP = "\0"
#: Marker for ``git push --all`` / ``--branches``: every local branch of the
#: target repo (#433).
_ALL_BRANCHES = "(all-branches)"
#: Marker prefix for ``git push --mirror``: every local branch, and every
#: remote branch it deletes (#433 review). The remote follows.
_MIRROR = "(mirror):"
#: Marker prefix for the ``:`` refspec (``git push origin :``): every local
#: branch the remote also has (#433). The remote follows.
_MATCHING = "(matching):"
#: Push options that send every local branch whatever the refspecs (#433).
_ALL_BRANCH_OPTS = frozenset({"--all", "--branches"})


def _pushed_branches(command: str, *, in_code: bool = True) -> list[str] | None:
    """Branch names a ``git push`` targets, or ``None`` when the text holds no
    push. A push with no refspec gives one ``(default-push):<remote>`` marker
    (empty remote for a bare ``git push``), which :func:`_judge` resolves.

    A tag push (``git push origin v8.6.1`` / ``--tags``) from a checked-out
    main matched the branch condition via HEAD and got denied (#240 v1 false
    positive): when the command names refspecs, judge those instead of HEAD.
    Refspecs like ``HEAD:main`` count as their destination, and a literal
    destination stays literal (``feature:HEAD`` pushes a remote ref named
    ``HEAD``). A ``HEAD`` / ``@`` refspec with no destination gives the
    ``(current)`` marker, which :func:`_judge` resolves to the target repo's
    checked-out branch (#423).

    The push is located in :func:`policy.shell_code_text` (#394), so a push
    inside an ssh payload or quoted string is never the one judged; its
    arguments are then read from the same span of the raw command. With
    ``in_code=False`` the first push anywhere in the text counts, quoted or not:
    for a body an opaque executor runs (``su -c '…'``) or a command the guard
    could not parse, which is how every push was read before #394. There the
    refspec also sheds a host language's ``,;)]}`` (``run('git push origin
    main', …)``).
    """
    match = _PUSH_ARGS_RE.search(policy.shell_code_text(command) if in_code else command)
    if not match:
        return None
    refs: list[str] = []
    # shell_code_text is length-preserving (it blanks quoted bodies to spaces,
    # it never drops them), so the offsets of the match in the masked text are
    # the same offsets in the raw command. That is what lets this slice read
    # the real arguments, quotes included (`git push origin "main"`).
    rest = command[match.start("rest") : match.end("rest")]
    # Outside code (an executor's body, `subprocess.run('git push origin
    # main', shell=True)`) a refspec may carry the host language's quote,
    # comma or bracket: shed them too, so the body's real target is judged.
    junk = "'\"" if in_code else "'\",;)]}"
    tokens = [t.strip(junk) for t in rest.split() if t.strip(junk)]
    positional: list[str] = []
    mirror = False
    for token in tokens:
        if token == "--tags":
            refs.append("(tags)")
            continue
        if token in _ALL_BRANCH_OPTS:
            refs.append(_ALL_BRANCHES)
            continue
        if token == "--mirror":
            mirror = True
            continue
        if token.startswith("-"):
            continue
        positional.append(token)
    # First positional token is the remote; the rest are refspecs.
    for token in positional[1:]:
        if token.lstrip("+") == ":":
            refs.append(_MATCHING + positional[0])
            continue
        if ":" not in token and token.lstrip("+").upper() in _CURRENT_BRANCH_REFS:
            refs.append(_CURRENT_REF)
            continue
        dest = token.rsplit(":", 1)[-1]
        # Force-push refspecs prefix the destination with '+' (`git push
        # origin +main`); without stripping it, "+main" never matched the
        # branch condition and the public-main deny silently skipped the
        # riskiest variant (2026-08-27 review).
        dest = dest.lstrip("+")
        dest = dest.removeprefix("refs/heads/")
        if dest.startswith("refs/tags/") or re.fullmatch(r"v?\d+[\w.\-]*", dest):
            refs.append("(tags)")
        else:
            refs.append(dest)
    remote = positional[0] if positional else ""
    if mirror:
        refs.append(_MIRROR + remote)
    if not refs:
        # The push's own `git -c k=v` settings decide where it lands too.
        config = _cmdline_config(command[match.start() : match.start("rest")])
        return [_CONFIG_SEP.join((_DEFAULT_PUSH + remote, *config))]
    return refs


@dataclass(frozen=True)
class RuleHit:
    rule: NoteRule
    outcome: str  # "deny" | "warn"
    detail: str = ""


@dataclass(frozen=True)
class CommandSite:
    """One simple command the local shell runs, and the repo it runs in."""

    text: str
    repo: Path | None
    #: The program runs a quoted argument as code the guard did not unwrap
    #: (``su -c``, a python ``-c`` calling ``os.system``): its text fails closed.
    opaque: bool = False
    #: Finds the repos the files this command writes or removes land in
    #: (#458). Called only when a repo-scoped rule matches this command, so a
    #: long command no rule matches never pays for its write targets.
    writes: Callable[[], tuple[Path, ...]] | None = field(default=None, compare=False, repr=False)

    @functools.cached_property
    def write_repos(self) -> tuple[Path, ...]:
        """The repos this command writes into, each the repo the Write tool on
        that path would be judged against (#458). ``()`` when unknown or on
        any failure: the command is still judged where it runs (fail open)."""
        if self.writes is None:
            return ()
        try:
            return tuple(self.writes())
        except deadline.DeadlineExceededError:
            raise  # never partial information: the guard denies (#460)
        except Exception:
            return ()


@dataclass(frozen=True)
class CommandView:
    """A Bash command as the guard parsed it (#394): ``local_text`` is the raw
    command with ssh remote payloads blanked; ``sites`` lists each simple
    command the local shell runs (``sh -c`` / ``eval`` bodies unwrapped)."""

    local_text: str
    sites: tuple[CommandSite, ...]


def _judge(
    rule: NoteRule,
    repo: Path | None,
    pushed: list[str] | None,
    facts: dict[tuple[str, Path], Any] | None = None,
) -> str | None:
    """Whether ``rule``'s repo conditions hold for ``repo`` and the pushed
    refspecs: ``"hit"``, ``"unknown"`` (visibility undeterminable) or None.
    ``facts`` memoises per-repo lookups across one evaluation, so a long chain
    of pushes in one repo asks git for its branch once."""
    if repo is None:
        return None
    deadline.check()
    memo: dict[tuple[str, Path], Any] = {} if facts is None else facts

    def fact(name: str, fn: Any) -> Any:
        if (name, repo) not in memo:
            memo[(name, repo)] = fn(repo)
        return memo[(name, repo)]

    if rule.except_repos and fact("name", _repo_name) in rule.except_repos:
        return None
    if rule.when_branch:
        # `HEAD` / `@` name the checked-out branch (#423): `git push -u
        # origin HEAD` from main pushes main, so it is judged as main. A push
        # with no refspec is judged by where it lands (`@{push}`).
        branches: list[str] = []
        for branch in pushed if pushed is not None else [_CURRENT_REF]:
            if branch == _CURRENT_REF:
                branches.append(fact("branch", _repo_branch))
            elif branch.startswith(_DEFAULT_PUSH):
                remote, *config = branch.removeprefix(_DEFAULT_PUSH).split(_CONFIG_SEP)
                branches += fact(
                    f"push:{branch}",
                    functools.partial(_default_push_branches, remote=remote, config=tuple(config)),
                )
            elif branch == _ALL_BRANCHES:
                branches += fact("local_branches", _local_branches)
            elif branch.startswith(_MIRROR):
                remote = branch.removeprefix(_MIRROR)
                branches += fact(
                    f"mirror:{remote}", functools.partial(_mirror_target, remote=remote)
                )
            elif branch.startswith(_MATCHING):
                remote = branch.removeprefix(_MATCHING)
                branches += fact(
                    f"matching:{remote}", functools.partial(_matching_branches, remote=remote)
                )
            else:
                branches.append(branch)
        if not any(branch in rule.when_branch for branch in branches):
            return None
    if rule.conditioned_on_has_commits():
        has_commits = fact("has_commits", _remote_has_commits)
        # Fail SAFE: an undeterminable remote must never widen an
        # exemption, so unknown is treated as satisfying the condition.
        if has_commits is not None and has_commits != rule.when_has_commits:
            return None
    if rule.conditioned_on_visibility():
        visibility = fact("visibility", _repo_visibility)
        if visibility == _VISIBILITY_UNKNOWN:
            return "unknown"
        if visibility != rule.when_visibility:
            return None
    return "hit"


def evaluate(
    action: dict[str, Any],
    omi_dir: Path | str,
    repo: Path | None,
    *,
    rules: list[NoteRule] | None = None,
    view: CommandView | None = None,
) -> RuleHit | None:
    """First matching rule for ``action``, or ``None``. Deterministic, no model.

    ``repo`` is the enclosing git repo when the guard resolved one; rules with
    repo-scoped conditions (visibility/branch/except_repos) require it and do
    not fire without one.

    ``view`` (#394) is the guard's parse of a Bash command. With it, a
    repo-scoped rule judges EACH simple command the local shell runs that it
    matches, against that command's own repo and refspec, and denies when any
    of them hits (`git status && cd /public && git push origin main` is judged
    at /public). A matching command is also judged against each repo it
    writes into (``CommandSite.write_repos``, #458), so ``cat > X/f`` run
    from /tmp is judged at X, as the Write tool on X/f is. A command matches
    on its code text (:func:`_code_text`): quoted arguments of a program that
    does not run them are data, so
    ``grep 'git push' README.md``, ``echo 'git push origin main'`` and
    ``git commit -m "… git push …"`` judge nothing (#413 review 3). Quoted
    text IS judged, against both the command's repo and ``repo``, when its
    program runs it as code the guard did not unwrap (``su -c '…'``, a
    python ``-c`` that calls ``os.system``): those fail closed. A glob only
    the whole command's code text matches (one spanning two commands) is
    judged against ``repo``. A match only inside an ssh remote payload is
    skipped: that git runs on another host, so the local repo says nothing
    about it. Rules WITHOUT repo conditions keep matching the raw command,
    ssh payloads and quoted text included: a remote side effect is still a
    side effect. Without ``view``, the raw command is judged against
    ``repo``, as before #394.

    A git global option between ``git`` and its subcommand never hides a
    match (#414): ``*git push*`` matches ``git -C d push`` and
    ``git -c k=v push``, but not ``git log --grep push``.

    Raises :class:`deadline.DeadlineExceededError` when the guard's judging
    budget runs out mid-evaluation; the guard then denies (#460).
    """
    tool = str(action.get("tool") or "")
    command = str(action.get("command") or "")
    target = command or str(action.get("path") or "")
    if not command:
        view = None
    code_texts: list[str] | None = None  # per site, computed on first need
    facts: dict[tuple[str, Path], Any] = {}
    for rule in rules if rules is not None else load_rules(omi_dir):
        deadline.check()
        if rule.invalid:
            continue
        if rule.tool not in ("*", tool):
            continue
        if not _rule_matches(target, rule.match) and not (
            view and any(_rule_matches(s.text, rule.match) for s in view.sites)
        ):
            continue
        repo_scoped = (
            rule.conditioned_on_visibility()
            or rule.when_branch
            or rule.except_repos
            or rule.conditioned_on_has_commits()
        )
        if repo_scoped:
            judged: list[tuple[Path | None, list[str] | None]]
            if view is None:
                judged = [(repo, _pushed_branches(command, in_code=False))]
            else:
                if code_texts is None:
                    code_texts = [_code_text(s.text) for s in view.sites]
                # A matching command is judged where it runs AND in each repo
                # it writes into (#458): `cat > X/f` from /tmp writes X, as
                # the Write tool on X/f would.
                judged = [
                    (r, _pushed_branches(s.text))
                    for s, code in zip(view.sites, code_texts, strict=True)
                    if _rule_matches(code, rule.match)
                    for r in (s.repo, *s.write_repos)
                ]
                # A body run by an executor the guard does not unwrap
                # (`su -c '…'`) is code: judged as before #394, fail closed.
                judged += [
                    (r, _pushed_branches(s.text, in_code=False))
                    for s in view.sites
                    if s.opaque and _rule_matches(s.text, rule.match)
                    for r in (s.repo, repo, *s.write_repos)
                ]
                if not judged:
                    if not _rule_matches(_code_text(view.local_text), rule.match):
                        continue  # quoted data, or only inside an ssh payload
                    # A glob spanning simple commands: the command as a whole,
                    # where it runs and in every repo it writes into.
                    pushed = _pushed_branches(view.local_text)
                    written = dict.fromkeys(r for s in view.sites for r in s.write_repos)
                    judged = [(r, pushed) for r in (repo, *written)]
            outcomes = {_judge(rule, r, pushed, facts) for r, pushed in judged}
            if "hit" not in outcomes:
                if "unknown" in outcomes:
                    # Fail-open: never deny on a condition we could not check.
                    return RuleHit(rule, "unknown-visibility", "visibility unknown")
                continue
        return RuleHit(rule, rule.action)
    return None


def export_rules(omi_dir: Path | str) -> str:
    """The always-binds rule set as Markdown, for pasting into an agent's
    always-loaded surface (``CLAUDE.md`` / ``AGENTS.md``).

    #321, fix 7: a behavioral invariant sitting behind probabilistic retrieval
    is not a rule, it is a coin flip — the vault held three separate filings of
    one correction, the signature of a rule that never fires. Invariants belong
    in the file the harness loads unconditionally; recall keeps the rest.
    """
    lines = [
        "<!-- generated by `omind rules export`; edit the omind-rule blocks in",
        "     the vault, not this block. -->",
        "## Hard rules (omind)",
        "",
        "These bind on every turn. They are enforcement, not recall — the guard",
        "denies the matching action whether or not any memory was retrieved.",
        "",
    ]
    for rule in load_rules(omi_dir):
        conditions = []
        if rule.when_visibility:
            conditions.append(f"{rule.when_visibility} repos")
        if rule.when_branch:
            conditions.append("on " + "/".join(rule.when_branch))
        if rule.except_repos:
            conditions.append("except " + ", ".join(rule.except_repos))
        scope = f" ({'; '.join(conditions)})" if conditions else ""
        verb = "NEVER" if rule.action == ACTION_DENY else "Take care with"
        lines.append(f"- **{verb} `{rule.match}`**{scope} — {rule.message}")
    if len(lines) == 7:
        lines.append("- _(no rules compiled)_")
    lines.append("")
    lines.append("*Proudly Made in Nebraska. Go Big Red! 🌽 <https://xkcd.com/2347/>*")
    return "\n".join(lines)


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
        if rule.conditioned_on_has_commits():
            conditions.append(f"has_commits={str(rule.when_has_commits).lower()}")
        if rule.except_repos:
            conditions.append(f"except {list(rule.except_repos)}")
        cond = f" when {', '.join(conditions)}" if conditions else ""
        lines.append(
            f"[{rule.action}] {rule.id}: {rule.tool} {rule.match!r}{cond} (from {rule.note})"
        )
    return "\n".join(lines) if lines else "(no rules)"
