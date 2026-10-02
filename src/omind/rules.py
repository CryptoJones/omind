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
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from omind import filelock, paths, policy

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


def _has_github_remote(repo: Path) -> bool:
    """True if ``repo`` has any git remote pointing at github.com.

    Used to tell a genuine ``gh`` failure apart from a repo that is simply not on
    GitHub (a local or mesh-only repo, e.g. the OMI vault that pushes to pluto/seed
    over SSH). The latter must not be logged as a failure — it is expected.
    """
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), "remote", "-v"],
            capture_output=True,
            text=True,
            timeout=5,
        )
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
        proc = subprocess.run(
            ["git", "-C", str(repo), "ls-remote", "--heads", "origin"],
            capture_output=True,
            text=True,
            timeout=10,
        )
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
        proc = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return proc.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def _git_out(repo: Path, *args: str) -> str | None:
    """``git -C repo <args>`` stdout, or None on any failure (fails open)."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout if proc.returncode == 0 else None


def _default_push_branches(repo: Path, remote: str) -> list[str]:
    """Branches a refspec-less ``git push [<remote>]`` in ``repo`` lands on
    (#423): what ``@{push}`` resolves to, so ``feature/x`` tracking
    ``origin/main`` under ``push.default=upstream`` is judged as ``main``. A
    configured ``remote.<name>.push`` refspec sourced from ``HEAD`` adds its
    destination too, because ``@{push}`` does not resolve one (git 2.54 says
    "push refspecs for 'origin' do not include 'feature'").

    Fails open: anything git cannot answer (detached HEAD, no upstream, not a
    repo, git missing) leaves the checked-out branch, judged as before.
    """
    current = _repo_branch(repo)
    dests: list[str] = []
    try:
        push = _git_out(repo, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{push}")
        remotes = (_git_out(repo, "remote") or "").split()
        push = (push or "").strip()
        owner = max((r for r in remotes if push.startswith(r + "/")), key=len, default="")
        if owner and (not remote or remote == owner):
            dests.append(push.removeprefix(owner + "/"))
        if not remote and current and current != "HEAD":
            remote = (
                _git_out(repo, "config", f"branch.{current}.pushRemote")
                or _git_out(repo, "config", "remote.pushDefault")
                or _git_out(repo, "config", f"branch.{current}.remote")
                or "origin"
            ).strip()
        if remote in remotes:
            specs = _git_out(repo, "config", "--get-all", f"remote.{remote}.push") or ""
            for spec in specs.split():
                src, _, dst = spec.lstrip("+").partition(":")
                if src.upper() in _CURRENT_BRANCH_REFS:
                    dests.append(dst.removeprefix("refs/heads/") or current)
    except Exception as exc:  # noqa: BLE001 - enforcement fails open
        _breadcrumb(f"rules_push_dest({repo})", exc)
        dests = []
    return dests or [current]


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


def _rule_matches(text: str, pattern: str) -> bool:
    return fnmatch.fnmatch(text, pattern) or fnmatch.fnmatch(_canonical_git(text), pattern)


_BLANKED_QUOTE_RE = re.compile(r"""'( *)'|"( *)\"""")


def _code_text(text: str) -> str:
    """``text`` as the shell runs it, for matching a repo-scoped rule (#413
    review 3): quoted data blanked (:func:`policy.shell_code_text`), so
    ``grep 'git push' README.md`` and ``git commit -m "… git push …"`` do not
    read as a push. A quoted SINGLE word is unquoted instead: ``git "push"``
    and ``origin 'main'`` are arguments, not prose."""

    def unquote(m: re.Match[str]) -> str:
        inner = text[m.start() + 1 : m.end() - 1]
        return inner if inner and not any(ch.isspace() for ch in inner) else m.group()

    return _BLANKED_QUOTE_RE.sub(unquote, policy.shell_code_text(text))


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
    for token in tokens:
        if token == "--tags":
            refs.append("(tags)")
            continue
        if token.startswith("-"):
            continue
        positional.append(token)
    # First positional token is the remote; the rest are refspecs.
    for token in positional[1:]:
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
    if not refs:
        return [_DEFAULT_PUSH + (positional[0] if positional else "")]
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
                remote = branch.removeprefix(_DEFAULT_PUSH)
                branches += fact(
                    f"push:{remote}", functools.partial(_default_push_branches, remote=remote)
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
    at /public). A command matches on its code text (:func:`_code_text`):
    quoted arguments of a program that does not run them are data, so
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
    """
    tool = str(action.get("tool") or "")
    command = str(action.get("command") or "")
    target = command or str(action.get("path") or "")
    if not command:
        view = None
    code_texts: list[str] | None = None  # per site, computed on first need
    facts: dict[tuple[str, Path], Any] = {}
    for rule in rules if rules is not None else load_rules(omi_dir):
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
                judged = [
                    (s.repo, _pushed_branches(s.text))
                    for s, code in zip(view.sites, code_texts, strict=True)
                    if _rule_matches(code, rule.match)
                ]
                # A body run by an executor the guard does not unwrap
                # (`su -c '…'`) is code: judged as before #394, fail closed.
                judged += [
                    (r, _pushed_branches(s.text, in_code=False))
                    for s in view.sites
                    if s.opaque and _rule_matches(s.text, rule.match)
                    for r in (s.repo, repo)
                ]
                if not judged:
                    if not _rule_matches(_code_text(view.local_text), rule.match):
                        continue  # quoted data, or only inside an ssh payload
                    # A glob spanning simple commands: the command as a whole.
                    judged = [(repo, _pushed_branches(view.local_text))]
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
